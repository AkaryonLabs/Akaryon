import hmac
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from akaryon.tasks.models import TaskStatus

router = APIRouter(prefix="/approvals", tags=["approvals"])
_approval_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="ApprovalToken",
    description="Use the value configured by the operator as AKARYON_APPROVAL_TOKEN.",
)


class ApprovalDecision(BaseModel):
    approved: bool


def _authorize(request: Request, authorization: str | None) -> None:
    expected = request.app.state.settings.approval_token
    if not expected:
        raise HTTPException(status_code=503, detail="Approval endpoints are disabled; configure AKARYON_APPROVAL_TOKEN")
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid approval token")


def _public(approval) -> dict:
    return {"id": approval.id, "task_id": approval.task_id, "agent_id": approval.agent_id,
            "tool": approval.tool, "capability": approval.capability,
            "provider": approval.provider,
            "arguments": approval.arguments, "status": approval.status,
            "scope": approval.scope, "expires_at": approval.expires_at,
            "proposal_digest": approval.proposal_digest,
            "created_at": approval.created_at, "decided_at": approval.decided_at}


def list_pending_approvals(request: Request, authorization: str | None) -> list[dict]:
    _authorize(request, authorization)
    return [_public(item) for item in request.app.state.approvals.list_pending()]


def list_recent_approvals(request: Request, authorization: str | None, limit: int = 25) -> list[dict]:
    _authorize(request, authorization)
    return [_public(item) for item in request.app.state.approvals.list_recent(limit)]


@router.get("")
def list_approvals(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_approval_bearer),
    authorization: str | None = Header(default=None, include_in_schema=False),
) -> list[dict]:
    supplied = f"Bearer {credentials.credentials}" if credentials else authorization
    return list_pending_approvals(request, supplied)


def apply_approval_decision(approval_id: str, body: ApprovalDecision, request: Request) -> dict:
    approvals = request.app.state.approvals
    pending = next((item for item in approvals.list_pending() if item.id == approval_id), None)
    if pending is None:
        expired = next((item for item in approvals.list_recent(100)
                        if item.id == approval_id and item.status == "expired"), None)
        if expired:
            raise HTTPException(status_code=410, detail="Approval expired; submit a fresh request")
        raise HTTPException(status_code=404, detail="Pending approval not found")
    task = request.app.state.tasks.get(pending.task_id)
    if task is None or task.status is not TaskStatus.WAITING_FOR_APPROVAL:
        raise HTTPException(status_code=409, detail="Associated task is not waiting for approval")
    decision = approvals.decide(approval_id, body.approved)
    if decision is None:
        raise HTTPException(status_code=409, detail="Approval was already decided")

    if decision.provider == "codex_cli":
        session = request.app.state.agent_sessions.get_for_task(task.id)
        if body.approved:
            result = request.app.state.tool_executor.execute_approved(decision)
            if result.get("status") == "completed":
                task.status = TaskStatus.COMPLETED
                task.error = None
                final_response = f"Approved Codex filesystem action completed. {result.get('output', '')}"
                request.app.state.events.publish("TaskCompleted", task_id=task.id,
                                                 session_id=session.id if session else None,
                                                 backend="codex_cli")
            else:
                task.status = TaskStatus.FAILED
                task.error = "codex_approved_action_failed"
                final_response = "The approved Codex filesystem action could not be completed: " + str(
                    result.get("error") or result.get("status") or "unknown error")
                request.app.state.events.publish("TaskFailed", task_id=task.id,
                                                 session_id=session.id if session else None,
                                                 backend="codex_cli")
            task.result = final_response
            if session:
                session.status, session.result, session.error = task.status.value, final_response, task.error
                request.app.state.agent_sessions.save(session)
            if decision.conversation_id:
                request.app.state.memory.add_message(decision.conversation_id, "assistant", final_response)
            request.app.state.events.publish("ApprovalGranted", approval_id=approval_id,
                                             task_id=task.id, backend="codex_cli")
        else:
            task.status = TaskStatus.CANCELLED
            task.error = "codex_proposal_denied"
            task.result = "The proposed Codex filesystem action was denied and was not run."
            if session:
                session.status, session.result, session.error = "cancelled", task.result, task.error
                request.app.state.agent_sessions.save(session)
            if decision.conversation_id:
                request.app.state.memory.add_message(decision.conversation_id, "assistant", task.result)
            request.app.state.events.publish("ApprovalDenied", approval_id=approval_id,
                                             task_id=task.id, backend="codex_cli")
    elif body.approved:
        result = request.app.state.tool_executor.execute_approved(decision)
        try:
            final_response = request.app.state.manager.resume_approved(task, decision, result)
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Approved tool ran, but the model could not resume") from exc
        task.result = final_response
        if task.status is not TaskStatus.WAITING_FOR_APPROVAL and decision.conversation_id:
            request.app.state.memory.add_message(decision.conversation_id, "assistant", final_response)
        request.app.state.events.publish("ApprovalGranted", approval_id=approval_id, task_id=task.id)
    else:
        task.status = TaskStatus.CANCELLED
        task.result = json.dumps({"status": "approval_denied", "approval_id": approval_id})
        session = request.app.state.agent_sessions.get_for_task(task.id)
        if session:
            session.status = "cancelled"
            session.result = task.result
            request.app.state.agent_sessions.save(session)
        if decision.conversation_id:
            request.app.state.memory.add_message(decision.conversation_id, "assistant", "I did not run the requested tool action because it was denied.")
        request.app.state.events.publish("ApprovalDenied", approval_id=approval_id, task_id=task.id)

    if task.status is not TaskStatus.WAITING_FOR_APPROVAL:
        task.completed_at = datetime.now(timezone.utc)
    request.app.state.tasks._save(task)
    return {"approval": _public(decision), "task_id": task.id, "task_status": task.status.value,
            "approval_required": task.status is TaskStatus.WAITING_FOR_APPROVAL,
            "response": task.result}


@router.post("/{approval_id}/decision")
def decide_approval(
    approval_id: str,
    body: ApprovalDecision,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_approval_bearer),
    authorization: str | None = Header(default=None, include_in_schema=False),
) -> dict:
    supplied = f"Bearer {credentials.credentials}" if credentials else authorization
    _authorize(request, supplied)
    return apply_approval_decision(approval_id, body, request)
