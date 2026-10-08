from dataclasses import dataclass, field
from collections.abc import Iterator
from pathlib import Path, PureWindowsPath
import re
from typing import Any, Literal, Protocol


@dataclass
class Agent:
    id: str
    name: str
    description: str
    instructions: str
    model: str | None = None
    tools: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    memory: Any = None
    state: str = "idle"


@dataclass(frozen=True)
class AgentActionProposal:
    """A requested action; adapters must never execute proposals themselves."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("An action proposal requires a name")
        if not isinstance(self.arguments, dict):
            raise ValueError("Action proposal arguments must be an object")


@dataclass(frozen=True)
class AgentBackendRequest:
    """Scoped work input passed to an external agent adapter."""

    task_id: str
    session_id: str
    prompt: str
    workspace_root: str
    allowed_capabilities: tuple[str, ...] = ()
    model: str | None = None
    timeout_seconds: int = 120
    max_output_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if not self.task_id or not self.session_id:
            raise ValueError("Backend requests require task and session identifiers")
        if not self.workspace_root.strip() or not (
                Path(self.workspace_root).is_absolute() or PureWindowsPath(self.workspace_root).is_absolute()):
            raise ValueError("Backend requests require an absolute approved workspace root")
        if self.timeout_seconds < 1 or self.max_output_bytes < 1:
            raise ValueError("Backend resource limits must be positive")
        if any(not capability.strip() for capability in self.allowed_capabilities):
            raise ValueError("Backend capabilities must be non-empty names")


@dataclass(frozen=True)
class AgentBackendEvent:
    """Normalized adapter output consumed by Akaryon's task/approval lifecycle."""

    type: Literal["text", "action_requested", "completed", "failed", "cancelled"]
    text: str = ""
    action: AgentActionProposal | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.type not in {"text", "action_requested", "completed", "failed", "cancelled"}:
            raise ValueError("Unknown backend event type")
        if self.type == "action_requested" and self.action is None:
            raise ValueError("Action-requested events require a proposal")
        if self.type != "action_requested" and self.action is not None:
            raise ValueError("Only action-requested events may contain a proposal")
        if self.type == "failed" and not self.error_code:
            raise ValueError("Failed events require a sanitized error code")
        if self.error_code is not None and not re.fullmatch(r"[a-z0-9_.-]{1,80}", self.error_code):
            raise ValueError("Backend error codes must be sanitized identifiers")
        if self.type != "failed" and self.error_code is not None:
            raise ValueError("Error codes are only valid on failed events")


class AgentBackendRun(Protocol):
    """One cancellable backend run; event output is normalized before reaching the host."""

    run_id: str

    def events(self) -> Iterator[AgentBackendEvent]: ...

    def cancel(self) -> None: ...


class AgentBackend(Protocol):
    """Adapter boundary for a selected external agent product.

    Adapters may return proposed actions but must not execute filesystem, shell,
    browser, or desktop actions. Akaryon routes each proposal through its own
    tool registry, permission manager, and approval workflow.
    """

    backend_id: str

    def start(self, request: AgentBackendRequest) -> AgentBackendRun: ...

    def health(self) -> bool: ...
