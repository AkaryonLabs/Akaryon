import logging
import math

from akaryon.memory.conversation import Conversation
from akaryon.database.models import ConversationRecord, MemoryEntryRecord, MessageRecord, ProjectRecord
from akaryon.database.session import session_scope
from akaryon.memory.entry import MemoryEntry
from akaryon.memory.security import MemorySecurityError, contains_credential_material
from akaryon.core.events import EventBus

_STOP_WORDS = {"a", "an", "and", "are", "at", "do", "does", "for", "how", "i", "in", "is",
               "it", "me", "my", "of", "on", "or", "please", "the", "tell", "to", "was",
               "were", "what", "when", "where", "why"}
_UNSET = object()
logger = logging.getLogger(__name__)


class ConversationProjectConflict(ValueError):
    """Raised when a conversation is reused outside its original project context."""


class MemoryManager:
    """Process-local starter store; replace with SQLAlchemy-backed persistence for deployment."""

    semantic_max_cosine_distance = 0.5

    def __init__(self, session_factory=None, embedding_provider=None,
                 events: EventBus | None = None) -> None:
        self.session_factory = session_factory
        self.embedding_provider = embedding_provider
        self.events = events
        self.conversations: dict[str, Conversation] = {}
        self.projects: dict[str, dict] = {}
        self.memory_entries: dict[str, MemoryEntry] = {}

    def get_or_create(self, conversation_id: str | None,
                      project_id: str | None = None) -> Conversation:
        if self.session_factory:
            from uuid import uuid4
            conversation_id = conversation_id or str(uuid4())
            with session_scope(self.session_factory) as session:
                record = session.get(ConversationRecord, conversation_id)
                if record is None:
                    if project_id and session.get(ProjectRecord, project_id) is None:
                        raise ValueError("Project not found")
                    record = ConversationRecord(id=conversation_id, project_id=project_id)
                    session.add(record)
                    session.flush()
                elif record.project_id != project_id:
                    raise ConversationProjectConflict(
                        "Conversation project context cannot be changed. Start a new conversation to switch projects.")
                messages = [{"role": m.role, "content": m.content} for m in record.messages]
                return Conversation(id=record.id, project_id=record.project_id,
                                    created_at=record.created_at, messages=messages)
        if conversation_id and conversation_id in self.conversations:
            conversation = self.conversations[conversation_id]
            if conversation.project_id != project_id:
                raise ConversationProjectConflict(
                    "Conversation project context cannot be changed. Start a new conversation to switch projects.")
            return conversation
        if project_id and project_id not in self.projects:
            raise ValueError("Project not found")
        conversation = (Conversation(id=conversation_id, project_id=project_id)
                        if conversation_id else Conversation(project_id=project_id))
        self.conversations[conversation.id] = conversation
        return conversation

    def add_message(self, conversation_id: str, role: str, content: str) -> None:
        if self.session_factory:
            with session_scope(self.session_factory) as session:
                if session.get(ConversationRecord, conversation_id) is None:
                    session.add(ConversationRecord(id=conversation_id))
                    session.flush()
                session.add(MessageRecord(conversation_id=conversation_id, role=role, content=content))
            return
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            conversation = self.get_or_create(conversation_id)
        conversation.messages.append({"role": role, "content": content})

    def retrieve(self, conversation_id: str | None = None,
                 limit: int | None = None) -> list[dict[str, str]]:
        if conversation_id is None:
            return []
        if self.session_factory:
            from sqlalchemy import select
            with session_scope(self.session_factory) as session:
                record = session.get(ConversationRecord, conversation_id)
                if record is None:
                    return []
                stmt = select(MessageRecord).where(
                    MessageRecord.conversation_id == conversation_id
                ).order_by(MessageRecord.id.desc())
                if limit is not None:
                    stmt = stmt.limit(limit)
                messages = list(session.scalars(stmt).all())
                return [{"role": message.role, "content": message.content}
                        for message in reversed(messages)]
        conversation = self.conversations.get(conversation_id)
        messages = list(conversation.messages) if conversation else []
        return messages[-limit:] if limit is not None else messages

    def list_conversations(self, offset: int = 0, limit: int = 50) -> tuple[list[dict], int]:
        if self.session_factory:
            from sqlalchemy import func, select
            with session_scope(self.session_factory) as session:
                stmt = select(ConversationRecord.id, ConversationRecord.project_id,
                              ConversationRecord.created_at,
                              func.count(MessageRecord.id)).outerjoin(MessageRecord).group_by(
                                  ConversationRecord.id, ConversationRecord.project_id,
                                  ConversationRecord.created_at
                              )
                total = session.scalar(select(func.count()).select_from(ConversationRecord)) or 0
                rows = session.execute(stmt.order_by(ConversationRecord.created_at.desc())
                                       .offset(offset).limit(limit))
                return ([{"id": row[0], "project_id": row[1], "created_at": row[2],
                          "message_count": row[3]} for row in rows], total)
        conversations = sorted(self.conversations.values(), key=lambda c: c.created_at, reverse=True)
        page = conversations[offset:offset + limit]
        return ([{"id": c.id, "project_id": c.project_id, "created_at": c.created_at,
                  "message_count": len(c.messages)} for c in page], len(conversations))

    def get_conversation(self, conversation_id: str, offset: int = 0,
                         limit: int = 50) -> dict | None:
        if self.session_factory:
            from sqlalchemy import func, select
            with session_scope(self.session_factory) as session:
                conversation = session.get(ConversationRecord, conversation_id)
                if conversation is None:
                    return None
                total = session.scalar(select(func.count(MessageRecord.id)).where(
                    MessageRecord.conversation_id == conversation_id
                )) or 0
                messages = session.scalars(select(MessageRecord)
                                           .where(MessageRecord.conversation_id == conversation_id)
                                           .order_by(MessageRecord.id).offset(offset).limit(limit)).all()
                return {"id": conversation.id, "project_id": conversation.project_id,
                        "created_at": conversation.created_at,
                        "message_count": total,
                        "messages": [{"role": message.role, "content": message.content} for message in messages]}
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            return None
        return {"id": conversation.id, "project_id": conversation.project_id,
                "created_at": conversation.created_at,
                "message_count": len(conversation.messages),
                "messages": conversation.messages[offset:offset + limit]}

    def store_entry(self, title: str, content: str, scope: str = "long_term",
                    project_id: str | None = None, metadata: dict | None = None) -> MemoryEntry:
        if contains_credential_material(title, content):
            raise MemorySecurityError("Memory cannot store credentials or private keys")
        entry = MemoryEntry(title=title, content=content, scope=scope, project_id=project_id,
                            metadata=metadata or {})
        entry.embedding = self._embed(f"{title}\n{content}")
        if self.session_factory:
            with session_scope(self.session_factory) as session:
                if project_id and session.get(ProjectRecord, project_id) is None:
                    raise ValueError("Project not found")
                session.add(MemoryEntryRecord(id=entry.id, title=entry.title, content=entry.content,
                                              scope=entry.scope, project_id=entry.project_id,
                                              metadata_json=entry.metadata, embedding=entry.embedding,
                                              created_at=entry.created_at))
        else:
            if project_id and project_id not in self.projects:
                raise ValueError("Project not found")
            self.memory_entries[entry.id] = entry
        if self.events:
            self.events.publish("MemoryStored", memory_id=entry.id, scope=entry.scope,
                                project_id=entry.project_id)
        return entry

    def update_entry(self, memory_id: str, title: str, content: str) -> dict | None:
        """Edit a saved note and refresh its embedding when configured."""
        if contains_credential_material(title, content):
            raise MemorySecurityError("Memory cannot store credentials or private keys")
        embedding = self._embed(f"{title}\n{content}")
        if self.session_factory:
            with session_scope(self.session_factory) as session:
                record = session.get(MemoryEntryRecord, memory_id)
                if record is None:
                    return None
                record.title, record.content, record.embedding = title, content, embedding
                session.flush()
                return self._entry_dict(record)
        entry = self.memory_entries.get(memory_id)
        if entry is None:
            return None
        entry.title, entry.content, entry.embedding = title, content, embedding
        return self._entry_dict(entry)

    def delete_entry(self, memory_id: str) -> bool:
        if self.session_factory:
            with session_scope(self.session_factory) as session:
                record = session.get(MemoryEntryRecord, memory_id)
                if record is None:
                    return False
                session.delete(record)
                return True
        return self.memory_entries.pop(memory_id, None) is not None

    def search_entries(self, query: str = "", scope: str | None = None,
                       project_id: str | None = None, offset: int = 0,
                       limit: int = 20, *, global_only: bool = False,
                       query_embedding=_UNSET) -> tuple[list[dict], int]:
        terms = self._query_terms(query)
        if query_embedding is _UNSET:
            query_embedding = self._embed(query) if query.strip() else None
        if self.session_factory:
            from sqlalchemy import case, func, or_, select, type_coerce
            with session_scope(self.session_factory) as session:
                stmt = select(MemoryEntryRecord)
                if scope:
                    stmt = stmt.where(MemoryEntryRecord.scope == scope)
                if project_id:
                    stmt = stmt.where(MemoryEntryRecord.project_id == project_id)
                if global_only:
                    stmt = stmt.where(MemoryEntryRecord.project_id.is_(None))
                if query_embedding and self._is_postgres():
                    from pgvector.sqlalchemy import Vector
                    semantic_stmt = stmt.where(MemoryEntryRecord.embedding.is_not(None))
                    vector = type_coerce(query_embedding, Vector(len(query_embedding)))
                    matching_dimension = func.vector_dims(MemoryEntryRecord.embedding) == len(query_embedding)
                    distance = case((matching_dimension,
                                     MemoryEntryRecord.embedding.op("<=>")(vector)), else_=None)
                    semantic_stmt = semantic_stmt.where(distance <= self.semantic_max_cosine_distance)
                    total = session.scalar(select(func.count()).select_from(semantic_stmt.subquery())) or 0
                    if total:
                        records = session.scalars(semantic_stmt.order_by(distance).offset(offset).limit(limit)).all()
                        return [self._entry_dict(record) for record in records], total
                    query_embedding = None
                if not query_embedding and terms:
                    stmt = stmt.where(or_(*[
                        func.lower(MemoryEntryRecord.title).contains(term, autoescape=True) |
                        func.lower(MemoryEntryRecord.content).contains(term, autoescape=True)
                        for term in terms
                    ]))
                records = session.scalars(stmt.order_by(MemoryEntryRecord.created_at.desc())).all()
                entries = [self._entry_dict(record, include_embedding=bool(query_embedding)) for record in records]
        else:
            entries = [self._entry_dict(entry, include_embedding=bool(query_embedding)) for entry in self.memory_entries.values()
                       if (scope is None or entry.scope == scope)
                       and (project_id is None or entry.project_id == project_id)
                       and (not global_only or entry.project_id is None)
                       and (query_embedding or not terms or any(
                           term in (entry.title + " " + entry.content).casefold() for term in terms
                       ))]
        if query_embedding:
            entries = [entry for entry in entries
                       if isinstance(entry.get("_embedding"), list)
                       and len(entry["_embedding"]) == len(query_embedding)]
            entries = [entry for entry in entries
                       if self._cosine_distance(query_embedding, entry["_embedding"])
                       <= self.semantic_max_cosine_distance]
            if not entries:
                return self.search_entries(query, scope, project_id, offset, limit,
                                           global_only=global_only, query_embedding=None)
            entries.sort(key=lambda entry: self._cosine_distance(query_embedding, entry["_embedding"]))
        else:
            entries.sort(key=lambda entry: self._score(entry, terms), reverse=True)
        total = len(entries)
        page = entries[offset:offset + limit]
        for entry in page:
            entry.pop("_embedding", None)
        return page, total

    @staticmethod
    def _score(entry: dict, terms: list[str]) -> tuple[int, str]:
        text = (entry["title"] + " " + entry["content"]).casefold()
        return sum(text.count(term) for term in terms), entry["created_at"].isoformat()

    @staticmethod
    def _entry_dict(entry, include_embedding: bool = False) -> dict:
        if isinstance(entry, MemoryEntryRecord):
            result = {"id": entry.id, "title": entry.title, "content": entry.content,
                    "scope": entry.scope, "project_id": entry.project_id,
                    "metadata": entry.metadata_json or {}, "created_at": entry.created_at}
            if include_embedding:
                result["_embedding"] = entry.embedding
            return result
        result = {"id": entry.id, "title": entry.title, "content": entry.content,
                "scope": entry.scope, "project_id": entry.project_id,
                "metadata": entry.metadata, "created_at": entry.created_at}
        if include_embedding:
            result["_embedding"] = entry.embedding
        return result

    def create_project(self, name: str, path: str | None) -> dict:
        from uuid import uuid4
        project = {"id": str(uuid4()), "name": name, "path": path}
        if self.session_factory:
            with session_scope(self.session_factory) as session:
                session.add(ProjectRecord(**project))
            return project
        self.projects[project["id"]] = project
        return project

    def list_projects(self) -> list[dict]:
        if self.session_factory:
            from sqlalchemy import select
            with session_scope(self.session_factory) as session:
                records = session.scalars(select(ProjectRecord).order_by(ProjectRecord.created_at)).all()
                return [{"id": r.id, "name": r.name, "path": r.path} for r in records]
        return list(self.projects.values())

    def has_project(self, project_id: str) -> bool:
        if self.session_factory:
            with session_scope(self.session_factory) as session:
                return session.get(ProjectRecord, project_id) is not None
        return project_id in self.projects

    def retrieve_relevant(self, query: str, project_id: str | None = None,
                          limit: int = 8) -> list[dict]:
        if not self._query_terms(query):
            return []
        query_embedding = self._embed(query)
        results: list[dict] = []
        for scope in ("long_term", "document"):
            entries, _ = self.search_entries(query, scope=scope, limit=limit, global_only=True,
                                             query_embedding=query_embedding)
            results.extend(entries)
        if project_id:
            for scope in ("project", "document"):
                entries, _ = self.search_entries(query, scope=scope, project_id=project_id,
                                                 limit=limit, query_embedding=query_embedding)
                results.extend(entries)
        deduplicated = {entry["id"]: entry for entry in results}
        terms = self._query_terms(query)
        ranked = (list(deduplicated.values()) if query_embedding else
                  sorted(deduplicated.values(), key=lambda entry: self._score(entry, terms), reverse=True))
        return ranked[:limit]

    def _embed(self, text: str) -> list[float] | None:
        if self.embedding_provider is None or not text.strip():
            return None
        try:
            vector = self.embedding_provider.embed(text)
            if (not isinstance(vector, list) or not vector or
                    any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                        not math.isfinite(value) for value in vector)):
                raise ValueError("Embedding provider returned an invalid vector")
            return [float(value) for value in vector]
        except Exception:
            logger.exception("Memory embedding failed; using keyword retrieval")
            return None

    def _is_postgres(self) -> bool:
        if not self.session_factory:
            return False
        bind = self.session_factory.kw.get("bind")
        return bool(bind and bind.dialect.name == "postgresql")

    @staticmethod
    def _cosine_distance(left: list[float], right: list[float]) -> float:
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if not left_norm or not right_norm:
            return 1.0
        return 1.0 - dot / (left_norm * right_norm)

    @staticmethod
    def _query_terms(query: str) -> list[str]:
        return [part.casefold().strip(".,!?;:()[]{}\"'") for part in query.split()
                if len(part.strip(".,!?;:()[]{}\"'")) >= 3
                and part.casefold().strip(".,!?;:()[]{}\"'") not in _STOP_WORDS]
