from typing import Any, Protocol


class MemoryStore(Protocol):
    def store(self, conversation_id: str, role: str, content: str) -> None: ...

    def retrieve(self, conversation_id: str) -> list[dict[str, str]]: ...


class MemoryRetriever(Protocol):
    def search(self, query: str, *, project_id: str | None = None,
               limit: int = 10) -> list[dict[str, Any]]: ...
