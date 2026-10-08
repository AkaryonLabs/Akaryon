from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
from threading import Lock
from uuid import uuid4

from akaryon.database.models import ApprovalRecord
from akaryon.database.session import session_scope


@dataclass
class ApprovalRequest:
    id: str
    task_id: str
    agent_id: str
    tool: str
    capability: str
    tool_call_id: str
    arguments: dict
    messages: list
    model: str
    provider: str
    conversation_id: str | None
    status: str = "pending"
    created_at: datetime | None = None
    decided_at: datetime | None = None
    scope: str | None = None
    proposal_digest: str | None = None
    expires_at: datetime | None = None


def approval_proposal_digest(task_id: str, agent_id: str, tool: str, capability: str,
                             arguments: dict, scope: str, expires_at: datetime) -> str:
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    payload = {"task_id": task_id, "agent_id": agent_id, "tool": tool,
               "capability": capability, "arguments": arguments, "scope": scope,
               "expires_at": expires_at.astimezone(timezone.utc).isoformat()}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class ApprovalManager:
    """Stores one-time decisions for specific tool calls."""

    def __init__(self, session_factory=None, on_expire=None) -> None:
        self.session_factory = session_factory
        self.on_expire = on_expire
        self._items: dict[str, ApprovalRequest] = {}
        self._lock = Lock()

    def create(self, task_id: str, agent_id: str, tool: str, capability: str, arguments: dict,
               messages: list, model: str, provider: str, conversation_id: str | None,
               tool_call_id: str, *, scope: str | None = None,
               proposal_digest: str | None = None,
               expires_at: datetime | None = None) -> ApprovalRequest:
        if proposal_digest is not None:
            if scope is None or expires_at is None:
                raise ValueError("Bound proposals require a scope and expiry")
            expected = approval_proposal_digest(task_id, agent_id, tool, capability,
                                                arguments, scope, expires_at)
            if not hmac.compare_digest(proposal_digest, expected):
                raise ValueError("Proposal digest does not match approval details")
        request = ApprovalRequest(
            id=str(uuid4()), task_id=task_id, agent_id=agent_id, tool=tool, capability=capability,
            tool_call_id=tool_call_id, arguments=dict(arguments), messages=list(messages), model=model,
            provider=provider, conversation_id=conversation_id, created_at=datetime.now(timezone.utc),
            scope=scope, proposal_digest=proposal_digest, expires_at=expires_at,
        )
        if self.session_factory:
            with session_scope(self.session_factory) as session:
                session.add(ApprovalRecord(id=request.id, task_id=request.task_id, agent_id=request.agent_id,
                                           tool=request.tool, capability=request.capability,
                                           tool_call_id=request.tool_call_id,
                                           arguments=request.arguments, messages=request.messages,
                                           model=request.model, provider=request.provider,
                                           conversation_id=request.conversation_id, status=request.status,
                                           created_at=request.created_at, scope=request.scope,
                                           proposal_digest=request.proposal_digest,
                                           expires_at=request.expires_at))
        else:
            with self._lock:
                self._items[request.id] = request
        return request

    def list_pending(self) -> list[ApprovalRequest]:
        now = datetime.now(timezone.utc)
        if self.session_factory:
            from sqlalchemy import select, update
            expired_task_ids = []
            with session_scope(self.session_factory) as session:
                expired_task_ids = list(session.scalars(select(ApprovalRecord.task_id).where(
                    ApprovalRecord.status == "pending", ApprovalRecord.expires_at.is_not(None),
                    ApprovalRecord.expires_at <= now,
                )).all())
                session.execute(update(ApprovalRecord).where(
                    ApprovalRecord.status == "pending", ApprovalRecord.expires_at.is_not(None),
                    ApprovalRecord.expires_at <= now,
                ).values(status="expired", decided_at=now))
                records = session.scalars(select(ApprovalRecord).where(ApprovalRecord.status == "pending")
                                          .order_by(ApprovalRecord.created_at)).all()
                pending = [self._from_record(record) for record in records]
            self._notify_expired(expired_task_ids)
            return pending
        expired_task_ids = []
        with self._lock:
            for item in self._items.values():
                if item.status == "pending" and item.expires_at and _utc(item.expires_at) <= now:
                    item.status, item.decided_at = "expired", now
                    expired_task_ids.append(item.task_id)
            pending = [item for item in self._items.values() if item.status == "pending"]
        self._notify_expired(expired_task_ids)
        return pending

    def _notify_expired(self, task_ids: list[str]) -> None:
        if self.on_expire:
            for task_id in set(task_ids):
                self.on_expire(task_id)

    def list_recent(self, limit: int = 25) -> list[ApprovalRequest]:
        """Return the latest decided requests for an audit view."""
        if self.session_factory:
            from sqlalchemy import select
            with session_scope(self.session_factory) as session:
                records = session.scalars(select(ApprovalRecord)
                                          .where(ApprovalRecord.status != "pending")
                                          .order_by(ApprovalRecord.decided_at.desc(),
                                                    ApprovalRecord.created_at.desc())
                                          .limit(limit)).all()
                return [self._from_record(record) for record in records]
        with self._lock:
            decided = [item for item in self._items.values() if item.status != "pending"]
            return sorted(decided, key=lambda item: item.decided_at or item.created_at,
                          reverse=True)[:limit]

    def decide(self, approval_id: str, approved: bool) -> ApprovalRequest | None:
        new_status = "approved" if approved else "denied"
        decided_at = datetime.now(timezone.utc)
        if self.session_factory:
            from sqlalchemy import or_, select, update
            expired_task_id = None
            result = None
            invalidated = False
            with session_scope(self.session_factory) as session:
                record = session.scalars(select(ApprovalRecord).where(
                    ApprovalRecord.id == approval_id, ApprovalRecord.status == "pending"
                ).with_for_update()).first()
                if record is None:
                    return None
                if record.expires_at and _utc(record.expires_at) <= decided_at:
                    session.execute(update(ApprovalRecord).where(
                        ApprovalRecord.id == approval_id, ApprovalRecord.status == "pending"
                    ).values(status="expired", decided_at=decided_at).execution_options(
                        synchronize_session=False))
                    record.status, record.decided_at = "expired", decided_at
                    expired_task_id = record.task_id
                elif record.proposal_digest:
                    try:
                        expected = approval_proposal_digest(
                            record.task_id, record.agent_id, record.tool, record.capability,
                            record.arguments, record.scope, record.expires_at,
                        ) if record.scope and record.expires_at else ""
                    except (TypeError, ValueError):
                        expected = ""
                    if not record.scope or not record.expires_at or not hmac.compare_digest(
                            record.proposal_digest, expected):
                        session.execute(update(ApprovalRecord).where(
                            ApprovalRecord.id == approval_id, ApprovalRecord.status == "pending"
                        ).values(status="invalidated", decided_at=decided_at).execution_options(
                            synchronize_session=False))
                        record.status, record.decided_at = "invalidated", decided_at
                        invalidated = True
                if not expired_task_id and not invalidated:
                    changed = session.execute(update(ApprovalRecord).where(
                        ApprovalRecord.id == approval_id, ApprovalRecord.status == "pending",
                        or_(ApprovalRecord.expires_at.is_(None), ApprovalRecord.expires_at > decided_at),
                    ).values(status=new_status, decided_at=decided_at).execution_options(
                        synchronize_session=False))
                    if changed.rowcount == 1:
                        record.status, record.decided_at = new_status, decided_at
                        result = self._from_record(record)
            if expired_task_id:
                self._notify_expired([expired_task_id])
                return None
            return result
        with self._lock:
            item = self._items.get(approval_id)
            if item is None or item.status != "pending":
                return None
            if item.expires_at and _utc(item.expires_at) <= decided_at:
                item.status, item.decided_at = "expired", decided_at
                expired_task_id = item.task_id
            else:
                expired_task_id = None
            if item.proposal_digest:
                try:
                    expected = approval_proposal_digest(
                        item.task_id, item.agent_id, item.tool, item.capability,
                        item.arguments, item.scope, item.expires_at,
                    ) if item.scope and item.expires_at else ""
                except (TypeError, ValueError):
                    expected = ""
                if expired_task_id:
                    pass
                elif not item.scope or not item.expires_at or not hmac.compare_digest(
                        item.proposal_digest, expected):
                    item.status, item.decided_at = "invalidated", decided_at
                    return None
            if expired_task_id:
                pass
            else:
                item.status, item.decided_at = new_status, decided_at
                return item
        self._notify_expired([expired_task_id])
        return None

    def cancel_for_task(self, task_id: str) -> int:
        decided_at = datetime.now(timezone.utc)
        if self.session_factory:
            from sqlalchemy import update
            with session_scope(self.session_factory) as session:
                result = session.execute(update(ApprovalRecord).where(
                    ApprovalRecord.task_id == task_id, ApprovalRecord.status == "pending"
                ).values(status="cancelled", decided_at=decided_at))
                return result.rowcount or 0
        with self._lock:
            pending = [item for item in self._items.values()
                       if item.task_id == task_id and item.status == "pending"]
            for item in pending:
                item.status, item.decided_at = "cancelled", decided_at
            return len(pending)

    @staticmethod
    def _from_record(record: ApprovalRecord) -> ApprovalRequest:
        return ApprovalRequest(id=record.id, task_id=record.task_id, agent_id=record.agent_id,
                               tool=record.tool, capability=record.capability,
                               tool_call_id=record.tool_call_id or "", arguments=record.arguments,
                               messages=record.messages or [], model=record.model or "",
                               provider=record.provider or "", conversation_id=record.conversation_id,
                               status=record.status, created_at=record.created_at,
                               decided_at=record.decided_at, scope=getattr(record, "scope", None),
                               proposal_digest=getattr(record, "proposal_digest", None),
                               expires_at=getattr(record, "expires_at", None))
