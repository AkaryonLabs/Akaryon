from akaryon.core.config import Settings, get_settings
from akaryon.core.exceptions import ProviderError
from akaryon.models.base import ModelProvider
from akaryon.models.providers.mock import MockProvider
from akaryon.models.providers.openai import OpenAIProvider
from akaryon.models.providers.ollama import OllamaProvider
from akaryon.models.providers.anthropic import AnthropicProvider


class ModelRouter:
    def __init__(self, settings: Settings | None = None) -> None:
        cfg = settings or get_settings()
        mock = MockProvider(cfg)
        mock.default_model = cfg.default_model
        providers: dict[str, ModelProvider] = {"mock": mock}
        if cfg.openai_api_key:
            providers["openai"] = OpenAIProvider(cfg.openai_api_key, cfg.openai_model,
                                                 timeout=cfg.provider_timeout_seconds,
                                                 max_output_tokens=cfg.provider_max_output_tokens,
                                                 search_model=cfg.openai_search_model)
        if cfg.anthropic_api_key:
            providers["anthropic"] = AnthropicProvider(cfg.anthropic_api_key, cfg.anthropic_model,
                                                       timeout=cfg.provider_timeout_seconds,
                                                       max_tokens=cfg.provider_max_output_tokens)
        providers["ollama"] = OllamaProvider(cfg.ollama_base_url, cfg.ollama_model,
                                             timeout=cfg.provider_timeout_seconds,
                                             max_output_tokens=cfg.provider_max_output_tokens)
        self.providers = providers
        self.openai_image_model = cfg.openai_image_model
        self.image_input_cost_reservation_usd = cfg.image_input_cost_reservation_usd
        self.openai_search_model = cfg.openai_search_model
        self.max_output_tokens_per_task = cfg.provider_max_total_output_tokens
        self.model_prices = cfg.model_prices
        self.task_provider_routes = cfg.task_provider_routes
        self.max_estimated_cost_per_task_usd = cfg.max_estimated_cost_per_task_usd
        self.max_estimated_cost_per_month_usd = cfg.max_estimated_cost_per_month_usd
        self.default_provider = cfg.default_provider
        if cfg.default_provider == "openai":
            self.default_model = cfg.openai_model
        elif cfg.default_provider == "anthropic":
            self.default_model = cfg.anthropic_model
        elif cfg.default_provider == "ollama":
            self.default_model = cfg.ollama_model
        else:
            self.default_model = cfg.default_model
        if self.default_provider not in self.providers:
            if self.default_provider == "openai":
                detail = "AKARYON_OPENAI_API_KEY is required when AKARYON_DEFAULT_PROVIDER=openai"
            elif self.default_provider == "anthropic":
                detail = "AKARYON_ANTHROPIC_API_KEY is required when AKARYON_DEFAULT_PROVIDER=anthropic"
            else:
                detail = f"Configured default provider is unavailable: {self.default_provider}"
            raise ProviderError(detail)

    def select(self, task_type: str | None = None, provider_id: str | None = None,
               model: str | None = None) -> tuple[ModelProvider, str]:
        route = self.task_provider_routes.get(task_type or "") if provider_id is None else None
        selected_provider_id = provider_id or (route or {}).get("provider") or self.default_provider
        provider = self.providers.get(selected_provider_id)
        if provider is None:
            raise ProviderError(f"Configured provider is unavailable: {selected_provider_id}")
        routed_model = (route or {}).get("model") if provider_id is None else None
        selected_model = model or routed_model or getattr(provider, "default_model", self.default_model)
        return provider, selected_model
