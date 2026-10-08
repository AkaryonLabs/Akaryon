"""Local Ollama embeddings."""

import json
import math
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from akaryon.core.exceptions import ProviderError


class OllamaEmbeddingProvider:
    provider_id = "ollama"

    def __init__(self, base_url: str, model: str, timeout_seconds: int = 120) -> None:
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ProviderError("AKARYON_OLLAMA_BASE_URL must be an HTTP(S) URL")
        if not model.strip() or timeout_seconds < 1:
            raise ProviderError("Ollama embedding model and timeout must be configured")
        self.url = base_url.rstrip("/") + "/api/embed"
        self.model = model
        self.timeout_seconds = timeout_seconds

    def embed(self, text: str) -> list[float]:
        payload = json.dumps({"model": self.model, "input": text, "truncate": False}).encode("utf-8")
        request = Request(self.url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise ProviderError(f"Ollama embedding request failed ({type(exc).__name__})") from exc
        vectors = result.get("embeddings") if isinstance(result, dict) else None
        if (not isinstance(vectors, list) or len(vectors) != 1 or
                not isinstance(vectors[0], list) or not vectors[0]):
            raise ProviderError("Ollama returned an invalid embedding response")
        vector = vectors[0]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
               for value in vector):
            raise ProviderError("Ollama returned an invalid embedding vector")
        return [float(value) for value in vector]
