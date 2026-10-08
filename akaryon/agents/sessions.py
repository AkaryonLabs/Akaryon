from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from akaryon.database.models import AgentSessionRecord
from akaryon.database.session import session_scope


@dataclass
class AgentSession:
    agent_id: str
    id: str = field(default_factory=lambda: str(uuid4()))
    task_id: str | None = None
    conversation_id: str | None = None
    status: str = "pending"
    model: str | None = None
    provider: str | None = None
    result: str | None = None
    error: str | None = None
    state: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AgentSessionManager:
    def __init__(self, session_factory=None) -> None:
        self.session_factory = session_factory
        self.sessions: dict[str, AgentSession] = {}

    def create(self, agent_id: str, task_id: str | None = None,
               conversation_id: str | None = None) -> AgentSession:
        session = AgentSession(agent_id=agent_id, task_id=task_id, conversation_id=conversation_id)
        self.save(session)
        return session

    def save(self, session: AgentSession) -> None:
        session.updated_at = datetime.now(timezone.utc)
        if not self.session_factory:
            self.sessions[session.id] = session
            return
        with session_scope(self.session_factory) as db:
            record = db.get(AgentSessionRecord, session.id)
            values = asdict(session)
            if record is None:
                db.add(AgentSessionRecord(**values))
            else:
                for key, value in values.items():
                    setattr(record, key, value)

    def get(self, session_id: str) -> AgentSession | None:
        if not self.session_factory:
            return self.sessions.get(session_id)
        with session_scope(self.session_factory) as db:
            record = db.get(AgentSessionRecord, session_id)
            return self._from_record(record) if record else None

    def get_for_task(self, task_id: str) -> AgentSession | None:
        if not self.session_factory:
            return next((s for s in self.sessions.values() if s.task_id == task_id), None)
        from sqlalchemy import select
        with session_scope(self.session_factory) as db:
            record = db.scalar(select(AgentSessionRecord).where(AgentSessionRecord.task_id == task_id)
                               .order_by(AgentSessionRecord.created_at.desc()).limit(1))
            return self._from_record(record) if record else None

    def list_for_agent(self, agent_id: str, offset: int = 0, limit: int = 50) -> tuple[list[AgentSession], int]:
        if not self.session_factory:
            all_sessions = sorted((s for s in self.sessions.values() if s.agent_id == agent_id),
                                  key=lambda s: s.created_at, reverse=True)
            return all_sessions[offset:offset + limit], len(all_sessions)
        from sqlalchemy import func, select
        with session_scope(self.session_factory) as db:
            stmt = select(AgentSessionRecord).where(AgentSessionRecord.agent_id == agent_id)
            total = db.scalar(select(func.count()).select_from(AgentSessionRecord)
                              .where(AgentSessionRecord.agent_id == agent_id)) or 0
            records = db.scalars(stmt.order_by(AgentSessionRecord.created_at.desc())
                                 .offset(offset).limit(limit)).all()
            return [self._from_record(record) for record in records], total

    def latest_ids_for_tasks(self, task_ids: list[str]) -> dict[str, str]:
        """Return the newest linked session ID for each task in one bounded query per batch."""
        unique_ids = list(dict.fromkeys(task_id for task_id in task_ids if task_id))
        if not unique_ids:
            return {}
        if not self.session_factory:
            wanted = set(unique_ids)
            records = sorted((session for session in self.sessions.values()
                             if session.task_id in wanted),
                            key=lambda session: session.created_at, reverse=True)
            result: dict[str, str] = {}
            for session in records:
                result.setdefault(session.task_id, session.id)
            return result

        from sqlalchemy import select
        result: dict[str, str] = {}
        with session_scope(self.session_factory) as db:
            # Stay below SQLite's usual bind-parameter limit for long task histories.
            for start in range(0, len(unique_ids), 500):
                batch = unique_ids[start:start + 500]
                records = db.scalars(
                    select(AgentSessionRecord)
                    .where(AgentSessionRecord.task_id.in_(batch))
                    .order_by(AgentSessionRecord.created_at.desc())
                ).all()
                for record in records:
                    if record.task_id:
                        result.setdefault(record.task_id, record.id)
        return result

    @staticmethod
    def _from_record(record: AgentSessionRecord) -> AgentSession:
        return AgentSession(id=record.id, agent_id=record.agent_id, task_id=record.task_id,
                            conversation_id=record.conversation_id, status=record.status,
                            model=record.model, provider=record.provider, result=record.result,
                            error=record.error, state=record.state or {}, created_at=record.created_at,
                            updated_at=record.updated_at or record.created_at)
