from typing import Any

from akaryon.core.exceptions import ProviderError


class OpenAIEmbeddingProvider:
    provider_id = "openai"
    dimensions = 1536

    def __init__(self, api_key: str | None, model: str) -> None:
        if not api_key:
            raise ProviderError("AKARYON_OPENAI_API_KEY is required for OpenAI embeddings")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ProviderError("Install OpenAI support with: uv sync --extra openai") from exc
        self.client: Any = OpenAI(api_key=api_key)
        self.model = model

    def embed(self, text: str) -> list[float]:
        try:
            result = self.client.embeddings.create(model=self.model, input=text, dimensions=self.dimensions)
            return result.data[0].embedding
        except Exception as exc:
            raise ProviderError(f"OpenAI embedding request failed: {exc}") from exc
