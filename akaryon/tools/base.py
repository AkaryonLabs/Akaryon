from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolResult:
    ok: bool
    output: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class Tool(Protocol):
    name: str
    description: str
    capability: str
    schema: dict[str, Any]

    def execute(self, **kwargs: Any) -> ToolResult: ...
