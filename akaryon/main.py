import hashlib
import logging
import ipaddress
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from akaryon.agents.manager import ManagerAgent
from akaryon.agents.codex_cli import CodexCliBackend
from akaryon.agents.sessions import AgentSessionManager
from akaryon.api import (agents, approval_ui, approvals as approvals_api, chat, images,
                         memory, projects, tasks, web_search)
from akaryon.core.config import Settings, get_settings
from akaryon.core.events import EventBus
from akaryon.core.logging import configure_logging, request_id_context
from akaryon.models.router import ModelRouter
from akaryon.permissions.manager import PermissionManager
from akaryon.permissions.approvals import ApprovalManager
from akaryon.memory.manager import MemoryManager
from akaryon.memory.providers.ollama import OllamaEmbeddingProvider
from akaryon.memory.providers.openai import OpenAIEmbeddingProvider
from akaryon.tasks.manager import TaskManager
from akaryon.tasks.models import TaskStatus
from akaryon.database.models import Base
from akaryon.database.session import create_session_factory
from akaryon.tools.executor import ToolExecutor
from akaryon.tools.filesystem import FilesystemTool
from akaryon.tools.desktop import WindowsDesktopTool
from akaryon.tools.git import GitTool
from akaryon.tools.memory import MemoryTool
from akaryon.tools.registry import ToolRegistry
from akaryon.tools.browser import OpenBrowserTool
from akaryon.tools.terminal import TerminalTool
from akaryon.usage.ledger import UsageLedger

logger = logging.getLogger(__name__)
WEB_DIR = Path(__file__).with_name("web")
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _build_identifier() -> str:
    if not getattr(sys, "frozen", False):
        return "source"
    executable = Path(sys.executable)
    digest = hashlib.sha256()
    with executable.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"{executable.stem}#{digest.hexdigest()[:12]}"


BUILD_ID = _build_identifier()


def _origin_tuple(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or
                parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            return None
        scheme = parsed.scheme.lower()
        port = parsed.port or (443 if scheme == "https" else 80)
        return scheme, parsed.hostname.rstrip(".").casefold(), port
    except ValueError:
        return None


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    events = EventBus()
    settings = settings or get_settings()
    session_factory = None
    if settings.database_url:
        engine, session_factory = create_session_factory(settings.database_url)
        if settings.database_create_tables:
            Base.metadata.create_all(engine)
    model_router = ModelRouter(settings)
    embedding_provider = None
    if settings.embedding_provider == "openai":
        embedding_provider = OpenAIEmbeddingProvider(settings.openai_api_key, settings.embedding_model)
    elif settings.embedding_provider == "ollama":
        embedding_provider = OllamaEmbeddingProvider(
            settings.ollama_base_url, settings.ollama_embedding_model, settings.provider_timeout_seconds)
    elif settings.embedding_provider != "none":
        raise ValueError(f"Unsupported embedding provider: {settings.embedding_provider}")
    memory_manager = MemoryManager(session_factory, embedding_provider, events)
    task_manager = TaskManager(events, session_factory)
    agent_sessions = AgentSessionManager(session_factory)
    if settings.max_estimated_cost_per_month_usd is not None and session_factory is None:
        raise ValueError("A monthly estimated-cost cap requires AKARYON_DATABASE_URL for persistent accounting")
    usage_ledger = (UsageLedger(session_factory, settings.model_prices)
                    if settings.max_estimated_cost_per_month_usd is not None else None)
    permissions = PermissionManager()
    for agent_id, capability in settings.configured_capabilities:
        permissions.grant(agent_id, capability)
    tool_registry = ToolRegistry()
    terminal_tool = TerminalTool(settings)
    tool_registry.register(FilesystemTool(settings))
    tool_registry.register(MemoryTool(memory_manager))
    tool_registry.register(terminal_tool)
    tool_registry.register(GitTool(terminal_tool))
    tool_registry.register(OpenBrowserTool())
    desktop_tool = WindowsDesktopTool(settings.desktop_apps_json)
    tool_registry.register(desktop_tool)
    def expire_approval_task(task_id: str) -> None:
        task = task_manager.get(task_id)
        if task is None or task.status is not TaskStatus.WAITING_FOR_APPROVAL:
            return
        task.status, task.error = TaskStatus.FAILED, "approval_expired"
        task.result = "The proposed action expired without being run. Submit it again if it is still needed."
        task.completed_at = datetime.now(timezone.utc)
        task_manager._save(task)
        session = agent_sessions.get_for_task(task.id)
        if session:
            session.status, session.error, session.result = "failed", task.error, task.result
            agent_sessions.save(session)
        events.publish("TaskFailed", task_id=task.id, session_id=session.id if session else None,
                       reason="approval_expired")

    approval_manager = ApprovalManager(session_factory, on_expire=expire_approval_task)
    tool_executor = ToolExecutor(tool_registry, permissions, events, approval_manager)
    manager = ManagerAgent(model_router, task_manager, events, tool_executor, agent_sessions, usage_ledger)
    codex_backend = CodexCliBackend(settings.codex_cli_command)

    def provider_statuses() -> list[dict]:
        statuses = []
        for provider_id, provider in model_router.providers.items():
            status: dict = {
                "status": "local" if provider_id == "mock" else "configured",
                "model_ready": True if provider_id == "mock" else None,
            }
            if provider_id == "ollama":
                status.update(provider.health_snapshot())
            statuses.append({
                "id": provider_id,
                "model": getattr(provider, "default_model", model_router.default_model),
                "capabilities": sorted(getattr(provider, "capabilities", ())),
                **status,
            })
        return statuses

    app = FastAPI(title="Akaryon Core", version="0.1.0")
    app.mount("/ui-assets", StaticFiles(directory=WEB_DIR), name="ui-assets")
    hosted = None
    if settings.access_mode == "invited":
        from akaryon.hosted_auth import HostedAccess, hosted_headers
        hosted = HostedAccess(settings, session_factory)
    if hosted:
        hosted.install(app)
    else:
        @app.get("/auth/session", include_in_schema=False)
        def local_session():
            return {"hosted": False}

    @app.get("/", include_in_schema=False)
    def home() -> FileResponse:
        response = FileResponse(WEB_DIR / "index.html", headers={
            "Cache-Control": "no-cache",
            "Content-Security-Policy": "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
            "Referrer-Policy": "same-origin",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        })
        return response

    @app.middleware("http")
    async def request_observability(request: Request, call_next):
        supplied_id = request.headers.get("X-Request-ID", "")
        try:
            request_id = str(uuid.UUID(supplied_id)) if supplied_id else str(uuid.uuid4())
        except (ValueError, AttributeError):
            request_id = str(uuid.uuid4())
        token = request_id_context.set(request_id)
        started = time.monotonic()
        try:
            if hosted:
                denied = hosted.guard(request)
                if denied is not None:
                    denied.headers["X-Request-ID"] = request_id
                    return hosted_headers(denied)
            remote = request.client.host if request.client else ""
            try:
                loopback = remote.lower() in {"localhost", "testclient"} or ipaddress.ip_address(remote).is_loopback
            except ValueError:
                loopback = False
            request_host = (request.url.hostname or "").rstrip(".").casefold()
            try:
                local_host = ipaddress.ip_address(request_host).is_loopback
            except ValueError:
                local_host = request_host == "localhost"
            # Starlette's in-process test client uses these synthetic names;
            # permit them only when the peer is the synthetic test client.
            test_host = remote.lower() == "testclient" and request_host == "testserver"
            if not hosted and (not loopback or not (local_host or test_host)):
                response = JSONResponse({"detail": "Akaryon is available from this computer only"},
                                        status_code=403)
                response.headers["X-Request-ID"] = request_id
                logger.info("http request completed", extra={
                    "request_id": request_id, "method": request.method,
                    "path": request.url.path, "status_code": 403,
                    "duration_seconds": time.monotonic() - started,
                })
                return response
            if request.method.upper() in _STATE_CHANGING_METHODS:
                fetch_site = request.headers.get("sec-fetch-site")
                origin = request.headers.get("origin")
                request_origin = _origin_tuple(hosted.origin if hosted else str(request.base_url))
                supplied_origin = _origin_tuple(origin) if origin is not None else None
                cross_site = (
                    (fetch_site is not None and fetch_site != "same-origin") or
                    (origin is not None and
                     (supplied_origin is None or supplied_origin != request_origin))
                )
                if cross_site:
                    response = JSONResponse(
                        {"detail": "Cross-origin state-changing requests are not allowed"},
                        status_code=403)
                    response.headers["X-Request-ID"] = request_id
                    logger.info("http request completed", extra={
                        "request_id": request_id, "method": request.method,
                        "path": request.url.path, "status_code": 403,
                        "duration_seconds": time.monotonic() - started,
                    })
                    return response
            response = await call_next(request)
            if hosted:
                hosted_headers(response)
            response.headers["X-Request-ID"] = request_id
            logger.info("http request completed", extra={
                "request_id": request_id, "method": request.method,
                "path": request.url.path, "status_code": response.status_code,
                "duration_seconds": time.monotonic() - started,
            })
            return response
        except Exception:
            logger.exception("http request failed", extra={
                "request_id": request_id, "method": request.method,
                "path": request.url.path, "status_code": 500,
                "duration_seconds": time.monotonic() - started,
            })
            raise
        finally:
            request_id_context.reset(token)

    app.state.events = events
    app.state.settings = settings
    app.state.approvals = approval_manager
    app.state.model_router = model_router
    app.state.memory = memory_manager
    app.state.embedding_provider = embedding_provider
    app.state.tasks = task_manager
    app.state.agent_sessions = agent_sessions
    app.state.usage_ledger = usage_ledger
    app.state.manager = manager
    app.state.codex_backend = codex_backend
    app.state.permissions = permissions
    app.state.tool_registry = tool_registry
    app.state.desktop_tool = desktop_tool
    app.state.tool_executor = tool_executor

    @app.get("/health")
    def health() -> dict:
        codex_status = codex_backend.health_snapshot()
        return {
            "status": "ok",
            "build_id": BUILD_ID,
            "environment": settings.env,
            "provider": settings.default_provider,
            "model": model_router.default_model,
            "image_generation_available": ("openai" in model_router.providers and
                                            "image_generation" in getattr(model_router.providers["openai"], "capabilities", ())),
            "image_generation_model": model_router.openai_image_model,
            "image_generation_cost_reservation_usd": settings.image_generation_cost_reservation_usd,
            "image_input_cost_reservation_usd": settings.image_input_cost_reservation_usd,
            "web_search_available": ("openai" in model_router.providers and
                                     callable(getattr(model_router.providers["openai"], "search_web", None))),
            "openai_search_model": model_router.openai_search_model,
            "web_search_cost_reservation_usd": settings.web_search_cost_reservation_usd,
            "database_enabled": bool(settings.database_url),
            "approval_enabled": bool(settings.approval_token),
            "embedding_provider": settings.embedding_provider,
            "embedding_model": (
                settings.ollama_embedding_model if settings.embedding_provider == "ollama" else
                settings.embedding_model if settings.embedding_provider == "openai" else None
            ),
            "provider_timeout_seconds": settings.provider_timeout_seconds,
            "provider_max_output_tokens": settings.provider_max_output_tokens,
            "provider_max_total_output_tokens": settings.provider_max_total_output_tokens,
            "max_estimated_cost_per_task_usd": settings.max_estimated_cost_per_task_usd,
            "max_estimated_cost_per_month_usd": settings.max_estimated_cost_per_month_usd,
            "estimated_cost_month_to_date_usd": usage_ledger.month_to_date() if usage_ledger else None,
            "priced_model_count": len(model_router.model_prices),
            "task_provider_routes": [
                {"task_type": task_type, "provider": route["provider"],
                 "model": route.get("model")}
                for task_type, route in sorted(model_router.task_provider_routes.items())
            ],
            "max_context_messages": settings.max_context_messages,
            "max_context_characters": settings.max_context_characters,
            "max_memory_context_characters": settings.max_memory_context_characters,
            "max_attachment_bytes": settings.max_attachment_bytes,
            "max_attachment_characters": settings.max_attachment_characters,
            "codex_cli_available": codex_status["available"],
            "codex_cli_checking": codex_status["checking"],
            "codex_cli_status": codex_status["status"],
            "desktop_app_aliases": desktop_tool.application_aliases,
            "available_providers": provider_statuses(),
        }

    app.include_router(chat.router)
    app.include_router(images.router)
    app.include_router(web_search.router)
    app.include_router(approvals_api.router)
    app.include_router(approval_ui.router)
    app.include_router(agents.router)
    app.include_router(tasks.router)
    app.include_router(memory.router)
    app.include_router(projects.router)
    return app


app = create_app()
