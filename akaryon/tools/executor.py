import json
import hmac
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from akaryon.core.events import EventBus
from akaryon.permissions.manager import PermissionAction, PermissionManager
from akaryon.permissions.approvals import ApprovalManager
from akaryon.permissions.approvals import approval_proposal_digest
from akaryon.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, permissions: PermissionManager, events: EventBus,
                 approvals: ApprovalManager | None = None) -> None:
        self.registry, self.permissions, self.events = registry, permissions, events
        self.approvals = approvals

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema for tool in self.registry.list()]

    def execute(self, agent_id: str, name: str, arguments: str | dict[str, Any],
                task_id: str | None = None, *, messages: list | None = None,
                model: str = "", provider: str = "", conversation_id: str | None = None,
                tool_call_id: str = "") -> dict[str, Any]:
        tool = self.registry.get(name)
        if tool is None:
            return {"status": "denied", "error": "Unknown tool"}
        try:
            values = json.loads(arguments) if isinstance(arguments, str) else arguments
            if not isinstance(values, dict):
                raise ValueError("Tool arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            return {"status": "failed", "error": f"Invalid tool arguments: {exc}"}

        validator = getattr(tool, "validate", None)
        if validator:
            try:
                issue = validator(**values)
            except TypeError:
                return {"status": "failed", "error": "Invalid tool arguments"}
            if issue:
                return {"status": "failed", "error": issue}

        decision = self.permissions.check(agent_id, tool.capability, approval=True)
        # Some actions must show their exact arguments to the user every time,
        # even when the agent has a standing capability grant.
        must_approve = getattr(tool, "approval_required_each_time", False)
        if callable(must_approve):
            must_approve = must_approve(values)
        if must_approve and decision.action is PermissionAction.ALLOW:
            from akaryon.permissions.manager import PermissionDecision
            decision = PermissionDecision(PermissionAction.APPROVAL_REQUIRED,
                                          "This action always requires per-call approval")
        self.events.publish("ToolCalled", agent_id=agent_id, tool=name, capability=tool.capability)
        if decision.action is PermissionAction.APPROVAL_REQUIRED:
            approval = self.approvals.create(task_id, agent_id, name, tool.capability, values,
                                             messages or [], model, provider, conversation_id,
                                             tool_call_id) if self.approvals and task_id else None
            self.events.publish("ApprovalRequired", agent_id=agent_id, tool=name,
                                capability=tool.capability, task_id=task_id,
                                approval_id=approval.id if approval else None)
            return {"status": "approval_required", "tool": name, "capability": tool.capability,
                    "approval_id": approval.id if approval else None, "reason": decision.reason}
        if decision.action is PermissionAction.DENY:
            return {"status": "denied", "tool": name, "reason": decision.reason}

        return self._invoke(agent_id, name, tool, values)

    def execute_approved(self, request) -> dict[str, Any]:
        tool = self.registry.get(request.tool)
        if tool is None or tool.capability != request.capability:
            return {"status": "denied", "error": "Approved tool is no longer available"}
        if request.provider == "codex_cli":
            if not request.scope or not request.proposal_digest or not request.expires_at:
                return {"status": "denied", "error": "Codex proposal is missing its approval binding"}
            expires_at = request.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
                return {"status": "denied", "error": "Codex proposal approval expired"}
            expected = approval_proposal_digest(
                request.task_id, request.agent_id, request.tool, request.capability,
                request.arguments, request.scope, request.expires_at,
            )
            if not hmac.compare_digest(request.proposal_digest, expected):
                return {"status": "denied", "error": "Codex proposal changed after approval"}
            proposed_path = request.arguments.get("path")
            if not isinstance(proposed_path, str):
                return {"status": "denied", "error": "Codex filesystem proposal has no path"}
            scope = Path(request.scope).resolve()
            path = Path(proposed_path).resolve()
            if path != scope and scope not in path.parents:
                return {"status": "denied", "error": "Codex proposal is outside its approved workspace"}
        validator = getattr(tool, "validate", None)
        if validator:
            try:
                issue = validator(**request.arguments)
            except TypeError:
                return {"status": "failed", "error": "Invalid approved tool arguments"}
            if issue:
                return {"status": "failed", "error": issue}
        return self._invoke(request.agent_id, request.tool, tool, request.arguments)

    def _invoke(self, agent_id, name, tool, values) -> dict[str, Any]:
        started = time.monotonic()
        try:
            result = tool.execute(**values)
        except Exception as exc:
            duration = time.monotonic() - started
            logger.error("tool execution failed", extra={"agent_id": agent_id, "tool": name,
                        "tool_status": "failed", "duration_seconds": duration})
            return {"status": "failed", "error": str(exc), "metadata": {"duration_seconds": duration}}
        duration = time.monotonic() - started
        self.events.publish("ToolCompleted", agent_id=agent_id, tool=name, ok=result.ok)
        logger.info("tool execution completed", extra={"agent_id": agent_id, "tool": name,
                    "tool_status": "completed" if result.ok else "failed", "duration_seconds": duration})
        return {"status": "completed" if result.ok else "failed", "output": result.output,
                "error": result.error, "metadata": {**result.metadata, "duration_seconds": duration}}
