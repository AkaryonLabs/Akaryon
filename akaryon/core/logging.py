import contextvars
import json
import logging
from datetime import datetime, timezone

from akaryon.core.config import get_settings

request_id_context: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_context.get()
        return True


class StructuredFormatter(logging.Formatter):
    _fields = ("request_id", "method", "path", "status_code", "duration_seconds",
               "event", "task_id", "session_id", "agent_id", "provider", "model",
               "tool", "tool_status", "memory_id", "scope", "project_id")

    def format(self, record: logging.LogRecord) -> str:
        result = {"timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                  "level": record.levelname, "logger": record.name, "message": record.getMessage()}
        for field in self._fields:
            value = getattr(record, field, None)
            if value is not None:
                result[field] = value
        return json.dumps(result, ensure_ascii=False, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RequestContextFilter())
    handler.setFormatter(StructuredFormatter())
    logging.basicConfig(level=getattr(logging, get_settings().log_level.upper(), logging.INFO),
                        handlers=[handler])
