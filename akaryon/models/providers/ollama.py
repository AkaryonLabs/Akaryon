"""Ollama chat provider using its local HTTP API without extra dependencies."""

import json
import threading
import time
from collections.abc import Iterator
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from akaryon.core.exceptions import ProviderError
from akaryon.models.base import ModelRequest, ModelResponse, ModelStreamEvent


class OllamaProvider:
    provider_id = "ollama"
    capabilities = frozenset({"text", "text_attachments", "tool_calling"})

    def __init__(self, base_url: str, default_model: str, timeout: float = 120.0,
                 max_output_tokens: int = 4096) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self._health_lock = threading.Lock()
        self._health_state_lock = threading.Lock()
        self._health_checked_at: float | None = None
        self._health_result: dict[str, bool | str] = {"status": "unknown", "model_ready": False}
        self._health_checking = False

    def health_snapshot(self) -> dict[str, bool | str]:
        """Return local readiness immediately, refreshing stale state in a worker."""
        with self._health_state_lock:
            now = time.monotonic()
            stale = (self._health_checked_at is None or
                     now - self._health_checked_at >= 15)
            if stale and not self._health_checking:
                self._health_checking = True
                threading.Thread(target=self._refresh_health_snapshot, daemon=True,
                                 name="akaryon-ollama-health").start()
            if self._health_checking:
                return {"status": "checking", "model_ready": False, "checking": True}
            return {**self._health_result, "checking": False}

    def _refresh_health_snapshot(self) -> None:
        try:
            self.health_status()
        except Exception:
            with self._health_state_lock:
                self._health_result = {"status": "offline", "model_ready": False}
                self._health_checked_at = time.monotonic()
        finally:
            with self._health_state_lock:
                self._health_checking = False

    def health_status(self) -> dict[str, bool | str]:
        """Report local Ollama reachability and whether the configured model is installed."""
        with self._health_lock:
            with self._health_state_lock:
                now = time.monotonic()
                if (self._health_checked_at is not None and
                        now - self._health_checked_at < 15):
                    return dict(self._health_result)
            try:
                request = Request(f"{self.base_url}/api/tags", headers={"Accept": "application/json"})
                with urlopen(request, timeout=min(self.timeout, 3.0)) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
                    raise ValueError("Ollama model list is malformed")
                names = {
                    str(item.get("name") or item.get("model"))
                    for item in payload.get("models", [])
                    if isinstance(item, dict) and (item.get("name") or item.get("model"))
                }
                model_ready = self.default_model in names
                if ":" not in self.default_model:
                    model_ready = model_ready or f"{self.default_model}:latest" in names
                status = "online" if model_ready else "model_missing"
                result = {"status": status, "model_ready": model_ready}
            except (OSError, TimeoutError, ValueError, TypeError, AttributeError, HTTPError, URLError):
                result = {"status": "offline", "model_ready": False}
            with self._health_state_lock:
                self._health_result = result
                self._health_checked_at = time.monotonic()
                return dict(self._health_result)

    def generate(self, request: ModelRequest) -> ModelResponse:
        payload = self._payload(request, stream=False)
        try:
            with self._open(payload) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise ProviderError(f"Ollama request failed: {self._error_detail(exc)}") from exc
        message = result.get("message") or {}
        calls = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            calls.append({"id": call.get("id") or f"ollama-call-{index}",
                          "name": function.get("name", ""),
                          "arguments": json.dumps(function.get("arguments") or {})})
        return ModelResponse(text=message.get("content") or "", provider=self.provider_id,
                             model=result.get("model", request.model or self.default_model),
                             usage={key: result[key] for key in ("prompt_eval_count", "eval_count")
                                    if key in result}, raw=result, tool_calls=calls)

    def stream(self, request: ModelRequest) -> Iterator[str]:
        for event in self.stream_events(request):
            if event.type == "text" and event.text:
                yield event.text

    def stream_events(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        payload = self._payload(request, stream=True)
        try:
            with self._open(payload) as response:
                for line in response:
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    content = (item.get("message") or {}).get("content")
                    if content:
                        yield ModelStreamEvent("text", text=content)
                    calls = (item.get("message") or {}).get("tool_calls") or []
                    if calls:
                        yield ModelStreamEvent("tool_calls", tool_calls=[
                            {"id": call.get("id") or f"ollama-call-{index}",
                             "name": (call.get("function") or {}).get("name", ""),
                             "arguments": json.dumps((call.get("function") or {}).get("arguments") or {})}
                            for index, call in enumerate(calls)
                        ])
                    if item.get("error"):
                        raise ProviderError("Ollama stream returned an error")
                    usage = {key: item[key] for key in ("prompt_eval_count", "eval_count")
                             if isinstance(item.get(key), (int, float))}
                    if usage:
                        yield ModelStreamEvent("usage", usage=usage)
        except ProviderError:
            raise
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise ProviderError(f"Ollama stream failed: {self._error_detail(exc)}") from exc

    def _payload(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": request.model or self.default_model,
                                   "messages": request.messages, "stream": stream,
                                   "options": {"temperature": request.temperature,
                                               "num_predict": request.max_output_tokens or self.max_output_tokens}}
        if request.tools:
            payload["tools"] = request.tools
        return payload

    def _open(self, payload: dict[str, Any]):
        request = Request(f"{self.base_url}/api/chat", data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        return urlopen(request, timeout=self.timeout)

    @staticmethod
    def _error_detail(exc: Exception) -> str:
        if isinstance(exc, HTTPError):
            if exc.code == 404:
                return "HTTP 404; check the Ollama server URL and configured model name"
            return f"HTTP {exc.code}"
        if isinstance(exc, URLError):
            return "Ollama server is unreachable"
        if isinstance(exc, ValueError):
            return "Ollama returned invalid JSON"
        return type(exc).__name__
