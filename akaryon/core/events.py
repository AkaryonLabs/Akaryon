from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
import logging

logger = logging.getLogger("akaryon.events")


@dataclass(frozen=True)
class Event:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class EventBus:
    """Small in-process event bus; persistence can be added behind this interface."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[[Event], None]]] = {}
        self.history: list[Event] = []

    def subscribe(self, name: str, handler: Callable[[Event], None]) -> None:
        self._handlers.setdefault(name, []).append(handler)

    def publish(self, name: str, **payload: Any) -> Event:
        event = Event(name=name, payload=payload)
        self.history.append(event)
        logger.info("runtime event", extra={"event": name, **{
            key: payload[key] for key in ("task_id", "session_id", "agent_id", "tool", "provider", "model",
                                          "memory_id", "scope", "project_id")
            if key in payload
        }})
        for handler in self._handlers.get(name, []) + self._handlers.get("*", []):
            handler(event)
        return event
