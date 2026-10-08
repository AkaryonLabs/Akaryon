from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


@dataclass
class Conversation:
    id: str = field(default_factory=lambda: str(uuid4()))
    project_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    messages: list[dict[str, str]] = field(default_factory=list)
