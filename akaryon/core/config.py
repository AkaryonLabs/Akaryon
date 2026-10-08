from functools import lru_cache
import json
import math
import re
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AKARYON_", env_file=".env", extra="ignore")

    env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    access_mode: Literal["local", "invited"] = "local"
    public_url: str = ""
    github_client_id: str = ""
    github_client_secret: str = ""
    invited_email: str = ""
    invited_github_id: str = ""
    default_provider: Literal["mock", "openai", "anthropic", "ollama"] = "mock"
    default_model: str = "mock-default"
    mock_scenario: Literal["echo", "filesystem_list", "terminal_workspace", "browser_navigation",
                            "fail_once", "tool_optout"] = "echo"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    openai_search_model: str = "gpt-5-search-api"
    web_search_cost_reservation_usd: float = Field(default=0.30, gt=0, le=100)
    openai_image_model: str = "gpt-image-2.5-sunburst"
    image_generation_cost_reservation_usd: float = Field(default=0.30, gt=0, le=100)
    image_input_cost_reservation_usd: float = Field(default=0.10, gt=0, le=100)
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-3-5-haiku-latest"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.2:3b"
    codex_cli_command: str = "codex"
    provider_timeout_seconds: int = Field(default=120, ge=1, le=1800)
    provider_max_output_tokens: int = Field(default=4096, ge=1, le=32768)
    provider_max_total_output_tokens: int = Field(default=8192, ge=1, le=131072)
    model_prices_json: str = "{}"
    task_provider_routes_json: str = "{}"
    max_estimated_cost_per_task_usd: float | None = Field(default=None, ge=0)
    max_estimated_cost_per_month_usd: float | None = Field(default=None, ge=0)
    max_context_messages: int = Field(default=40, ge=1, le=200)
    max_context_characters: int = Field(default=60000, ge=1000, le=500000)
    max_memory_context_characters: int = Field(default=16000, ge=1000, le=100000)
    max_attachment_bytes: int = Field(default=5_242_880, ge=1, le=8_388_608)
    max_attachment_characters: int = Field(default=40000, ge=1000, le=200000)
    embedding_provider: str = "none"
    embedding_model: str = "text-embedding-3-small"
    ollama_embedding_model: str = "nomic-embed-text:latest"
    database_url: str | None = None
    database_create_tables: bool = False
    approval_token: str | None = None
    allowed_directories: str = "workspace"
    max_filesystem_file_bytes: int = Field(default=1_048_576, ge=1)
    max_directory_entries: int = Field(default=1_000, ge=1)
    max_terminal_output_bytes: int = Field(default=1_048_576, ge=1)
    terminal_allowlist: str = "git,python,py"
    agent_capabilities: str = ""
    desktop_apps_json: str = "{}"
    log_level: str = "INFO"

    @property
    def allowed_paths(self) -> list[str]:
        return [part.strip() for part in self.allowed_directories.split(",") if part.strip()]

    @property
    def allowed_commands(self) -> set[str]:
        return {part.strip().lower() for part in self.terminal_allowlist.split(",") if part.strip()}

    @property
    def configured_capabilities(self) -> list[tuple[str, str]]:
        entries = []
        for item in self.agent_capabilities.split(","):
            agent, separator, capability = item.strip().partition(":")
            if separator and agent and capability:
                entries.append((agent, capability))
        return entries

    @property
    def model_prices(self) -> dict[str, dict[str, float]]:
        try:
            raw = json.loads(self.model_prices_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("AKARYON_MODEL_PRICES_JSON must be valid JSON") from exc
        if not isinstance(raw, dict):
            raise ValueError("AKARYON_MODEL_PRICES_JSON must be a JSON object")
        prices = {}
        for model_key, rates in raw.items():
            if not isinstance(model_key, str) or not model_key.strip() or not isinstance(rates, dict):
                raise ValueError("Model price entries must map provider:model names to rate objects")
            normalized = {}
            for rate_name in ("input_usd_per_million", "output_usd_per_million"):
                value = rates.get(rate_name)
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
                    raise ValueError(f"{model_key} requires a non-negative finite {rate_name}")
                normalized[rate_name] = float(value)
            prices[model_key] = normalized
        return prices

    @property
    def task_provider_routes(self) -> dict[str, dict[str, str]]:
        """Return explicitly configured task routes without provider fallback."""
        try:
            raw = json.loads(self.task_provider_routes_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("AKARYON_TASK_PROVIDER_ROUTES_JSON must be valid JSON") from exc
        if not isinstance(raw, dict):
            raise ValueError("AKARYON_TASK_PROVIDER_ROUTES_JSON must be a JSON object")

        routes: dict[str, dict[str, str]] = {}
        allowed_providers = {"mock", "openai", "anthropic", "ollama"}
        for task_type, route in raw.items():
            if (not isinstance(task_type, str) or
                    not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", task_type)):
                raise ValueError("Task route names must be lowercase identifiers up to 64 characters")
            if isinstance(route, str):
                route = {"provider": route}
            if (not isinstance(route, dict) or set(route) - {"provider", "model"} or
                    not isinstance(route.get("provider"), str) or
                    route["provider"] not in allowed_providers):
                raise ValueError(f"Task route {task_type} requires a supported provider")
            model = route.get("model")
            if model is not None and (not isinstance(model, str) or not model.strip() or len(model) > 200):
                raise ValueError(f"Task route {task_type} has an invalid model")
            normalized = {"provider": route["provider"]}
            if model is not None:
                normalized["model"] = model.strip()
            routes[task_type] = normalized
        return routes


@lru_cache
def get_settings() -> Settings:
    return Settings()
