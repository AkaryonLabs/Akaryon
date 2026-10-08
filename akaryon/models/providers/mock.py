from collections.abc import Iterator
import json
from pathlib import Path
from threading import Lock

from akaryon.core.config import Settings, get_settings
from akaryon.core.exceptions import ProviderError
from akaryon.models.base import ModelRequest, ModelResponse, ModelStreamEvent


class MockProvider:
    provider_id = "mock"
    capabilities = frozenset({"text", "text_attachments", "tool_calling"})

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._fail_once_pending = self.settings.mock_scenario == "fail_once"
        self._scenario_lock = Lock()

    @staticmethod
    def _usage(request: ModelRequest, text: str = "") -> dict[str, int]:
        # Deterministic approximation for offline budget/ledger workflows.
        prompt = " ".join(str(message.get("content", "")) for message in request.messages)
        prompt_tokens = max(1, (len(prompt) + 3) // 4)
        completion_tokens = (len(text) + 3) // 4
        return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens}

    def generate(self, request: ModelRequest) -> ModelResponse:
        with self._scenario_lock:
            fail_this_call = self._fail_once_pending
            self._fail_once_pending = False
        if fail_this_call:
            raise ProviderError("Mock fail_once scenario injected a single transient failure")
        prompt = next((m["content"] for m in reversed(request.messages) if m["role"] == "user"), "")
        model = request.model or "mock-default"
        tool_result = next((m["content"] for m in reversed(request.messages) if m["role"] == "tool"), None)
        if tool_result is not None:
            text = f"Mock tool check complete. The tool returned: {tool_result}"
            return ModelResponse(text=text, provider=self.provider_id, model=model,
                                 usage=self._usage(request, text))
        if self.settings.mock_scenario == "tool_optout":
            if request.tools:
                return ModelResponse(
                    text="", provider=self.provider_id, model=model,
                    usage=self._usage(request),
                    tool_calls=[{"id": "mock-tool-optout", "name": "memory_store",
                                 "arguments": json.dumps({"title": "Unexpected mock write",
                                                           "content": "Must not be stored."})}],
                )
            text = f"Mock direct answer: {prompt}"
            return ModelResponse(text=text, provider=self.provider_id, model=model,
                                 usage=self._usage(request, text))
        if self.settings.mock_scenario == "filesystem_list":
            if not any(tool.get("function", {}).get("name") == "filesystem"
                       for tool in request.tools or []):
                raise ProviderError("Mock filesystem_list scenario requires the filesystem tool")
            path = str(Path(self.settings.allowed_paths[0]).resolve())
            return ModelResponse(
                text="",
                provider=self.provider_id,
                model=model,
                usage=self._usage(request),
                tool_calls=[{"id": "mock-filesystem-list", "name": "filesystem",
                             "arguments": json.dumps({"operation": "list", "path": path})}],
            )
        if self.settings.mock_scenario == "terminal_workspace":
            if not any(tool.get("function", {}).get("name") == "terminal"
                       for tool in request.tools or []):
                raise ProviderError("Mock terminal_workspace scenario requires the terminal tool")
            return ModelResponse(
                text="",
                provider=self.provider_id,
                model=model,
                usage=self._usage(request),
                tool_calls=[{"id": "mock-terminal-workspace", "name": "terminal",
                             "arguments": json.dumps({"command": ["python", "-c",
                                                                      "import os; print(os.getcwd())"]})}],
            )
        if self.settings.mock_scenario == "browser_navigation":
            if not any(tool.get("function", {}).get("name") == "open_browser"
                       for tool in request.tools or []):
                raise ProviderError("Mock browser_navigation scenario requires the browser tool")
            return ModelResponse(
                text="",
                provider=self.provider_id,
                model=model,
                usage=self._usage(request),
                tool_calls=[{"id": "mock-browser-navigation", "name": "open_browser",
                             "arguments": json.dumps({"url": "https://example.com/akaryon-offline-acceptance"})}],
            )
        text = f"Mock response: {prompt}"
        return ModelResponse(text=text, provider=self.provider_id, model=model,
                             usage=self._usage(request, text))

    def stream(self, request: ModelRequest) -> Iterator[str]:
        yield self.generate(request).text

    def stream_events(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        response = self.generate(request)
        if response.tool_calls:
            yield ModelStreamEvent("tool_calls", tool_calls=response.tool_calls)
        elif response.text:
            yield ModelStreamEvent("text", text=response.text)
        yield ModelStreamEvent("usage", usage=response.usage)
