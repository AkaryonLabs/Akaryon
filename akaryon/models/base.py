from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Protocol


@dataclass
class ModelRequest:
    messages: list[dict[str, Any]]
    model: str | None = None
    max_output_tokens: int | None = None
    temperature: float = 0.2
    tools: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Temporary image payloads are passed only to providers that explicitly
    # support image input. Callers must not put these in saved messages.
    image_inputs: list[dict[str, str]] = field(default_factory=list)


@dataclass
class ModelResponse:
    text: str
    provider: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    raw: Any = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ModelStreamEvent:
    type: Literal["text", "tool_calls", "usage"]
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


class ModelProvider(Protocol):
    provider_id: str
    capabilities: frozenset[str]

    def generate(self, request: ModelRequest) -> ModelResponse: ...

    def stream(self, request: ModelRequest) -> Iterator[str]: ...

    def stream_events(self, request: ModelRequest) -> Iterator[ModelStreamEvent]: ...
