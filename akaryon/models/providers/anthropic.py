"""Anthropic Messages API adapter for Akaryon's provider-neutral chat format."""

import json
from collections.abc import Iterator
from typing import Any

from akaryon.core.exceptions import ProviderError
from akaryon.models.base import ModelRequest, ModelResponse, ModelStreamEvent


class AnthropicProvider:
    provider_id = "anthropic"
    capabilities = frozenset({"text", "text_attachments", "tool_calling"})

    def __init__(self, api_key: str | None, default_model: str, *, client: Any = None,
                 max_tokens: int = 4096, timeout: float = 120.0) -> None:
        if client is None:
            if not api_key:
                raise ProviderError("AKARYON_ANTHROPIC_API_KEY is required for the Anthropic provider")
            try:
                from anthropic import Anthropic
            except ImportError as exc:
                raise ProviderError("Install the optional dependency with: pip install -e .[anthropic]") from exc
            client = Anthropic(api_key=api_key, timeout=timeout)
        self.client = client
        self.default_model = default_model
        self.max_tokens = max_tokens

    def generate(self, request: ModelRequest) -> ModelResponse:
        options = self._options(request)
        try:
            result = self.client.messages.create(**options)
        except Exception as exc:
            raise ProviderError(f"Anthropic request failed: {type(exc).__name__}") from exc
        text_parts, calls = [], []
        for index, block in enumerate(result.content):
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append({"id": block.id or f"anthropic-call-{index}", "name": block.name,
                              "arguments": json.dumps(block.input)})
        usage = result.usage.model_dump() if hasattr(result.usage, "model_dump") else {}
        return ModelResponse("".join(text_parts), self.provider_id, result.model, usage, result, calls)

    def stream(self, request: ModelRequest) -> Iterator[str]:
        for event in self.stream_events(request):
            if event.type == "text" and event.text:
                yield event.text

    def stream_events(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        options = self._options(request)
        try:
            with self.client.messages.stream(**options) as stream:
                for text in stream.text_stream:
                    yield ModelStreamEvent("text", text=text)
                final = stream.get_final_message()
                usage = getattr(final, "usage", None)
                if usage:
                    usage_data = usage.model_dump() if hasattr(usage, "model_dump") else {}
                    if usage_data:
                        yield ModelStreamEvent("usage", usage=usage_data)
                calls = [{"id": block.id, "name": block.name, "arguments": json.dumps(block.input)}
                         for block in final.content if block.type == "tool_use"]
                if calls:
                    yield ModelStreamEvent("tool_calls", tool_calls=calls)
        except Exception as exc:
            raise ProviderError(f"Anthropic stream failed: {type(exc).__name__}") from exc

    def _options(self, request: ModelRequest) -> dict[str, Any]:
        system = []
        messages = []
        for message in request.messages:
            role = message["role"]
            if role == "system":
                system.append(message.get("content", ""))
                continue
            if role == "tool":
                block = {"type": "tool_result", "tool_use_id": message.get("tool_call_id", ""),
                         "content": message.get("content", "")}
                if messages and messages[-1]["role"] == "user":
                    messages[-1]["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block]})
                continue
            content: Any = message.get("content") or ""
            if role == "assistant" and message.get("tool_calls"):
                blocks = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for call in message["tool_calls"]:
                    function = call["function"]
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except json.JSONDecodeError as exc:
                        raise ProviderError("Invalid tool arguments in message history") from exc
                    blocks.append({"type": "tool_use", "id": call.get("id", ""),
                                   "name": function["name"], "input": arguments})
                content = blocks
            if messages and messages[-1]["role"] == role and isinstance(messages[-1]["content"], list):
                if isinstance(content, list):
                    messages[-1]["content"].extend(content)
                else:
                    messages[-1]["content"].append({"type": "text", "text": content})
            else:
                messages.append({"role": role, "content": content})
        options: dict[str, Any] = {"model": request.model or self.default_model,
                                   "max_tokens": request.max_output_tokens or self.max_tokens,
                                   "messages": messages,
                                   "temperature": request.temperature}
        if system:
            options["system"] = "\n\n".join(system)
        if request.tools:
            options["tools"] = [
                {"name": tool["function"]["name"],
                 "description": tool["function"].get("description", ""),
                 "input_schema": tool["function"].get("parameters", {"type": "object", "properties": {}})}
                for tool in request.tools
            ]
        return options
