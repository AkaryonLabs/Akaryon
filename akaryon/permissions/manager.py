from dataclasses import dataclass
from enum import StrEnum


class PermissionAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True)
class PermissionDecision:
    action: PermissionAction
    reason: str


class PermissionManager:
    """Safe-by-default policy. Tool invocation requires an explicit grant."""

    def __init__(self) -> None:
        self._grants: set[tuple[str, str]] = set()
        self._denials: set[tuple[str, str]] = set()

    def grant(self, agent_id: str, capability: str) -> None:
        self._denials.discard((agent_id, capability))
        self._grants.add((agent_id, capability))

    def deny(self, agent_id: str, capability: str) -> None:
        self._grants.discard((agent_id, capability))
        self._denials.add((agent_id, capability))

    def check(self, agent_id: str, capability: str, *, approval: bool = False) -> PermissionDecision:
        if (agent_id, capability) in self._denials:
            return PermissionDecision(PermissionAction.DENY, "Capability explicitly denied")
        if (agent_id, capability) in self._grants:
            return PermissionDecision(PermissionAction.ALLOW, "Explicit capability grant")
        action = PermissionAction.APPROVAL_REQUIRED if approval else PermissionAction.DENY
        return PermissionDecision(action, f"No grant for {agent_id}:{capability}")
