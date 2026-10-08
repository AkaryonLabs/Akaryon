from dataclasses import dataclass
from typing import Any


@dataclass
class AgentState:
    status: str = "idle"
    last_result: Any = None
    error: str | None = None
