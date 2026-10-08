from typing import Protocol


class EmbeddingProvider(Protocol):
    provider_id: str

    def embed(self, text: str) -> list[float]: ...
