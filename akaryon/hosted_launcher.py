"""Render entry point: migrate the remote store, then bind the assigned port."""
import os
import gzip
from pathlib import Path


def main():
    os.environ["AKARYON_ACCESS_MODE"] = "invited"
    os.environ["AKARYON_ENV"] = "production"
    os.environ["AKARYON_DEFAULT_PROVIDER"] = "openai"
    # Cloud chat has no authority to run terminal, desktop, browser, or file tools.
    os.environ["AKARYON_AGENT_CAPABILITIES"] = ""
    os.environ["AKARYON_TERMINAL_ALLOWLIST"] = ""
    os.environ["AKARYON_DESKTOP_APPS_JSON"] = "{}"
    os.environ["AKARYON_CODEX_CLI_COMMAND"] = "akaryon-cloud-codex-disabled"
    os.environ["AKARYON_ALLOWED_DIRECTORIES"] = "workspace"
    os.environ["AKARYON_DATABASE_CREATE_TABLES"] = "false"
    # Keep large binary/JS assets compressed in Git; reconstruct the normal
    # paths when a Render instance starts.
    web = Path("akaryon/web")
    for stored_name, live_name in (("void-speaker.glb.gz", "void-speaker.glb"),
                                   ("void-speaker.bundle.js.gz", "void-speaker.bundle.js")):
        compressed = web / stored_name
        if compressed.exists():
            with gzip.open(compressed, "rb") as source:
                payload = source.read(16 * 1024 * 1024 + 1)
            if len(payload) > 16 * 1024 * 1024:
                raise SystemExit("A bundled Akaryon model asset exceeded the size limit.")
            (web / live_name).write_bytes(payload)
    database_url = os.environ.get("AKARYON_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
    for prefix in ("postgres://", "postgresql://"):
        if database_url.startswith(prefix):
            database_url = "postgresql+psycopg://" + database_url[len(prefix):]
            break
    if not database_url:
        raise SystemExit("A persistent DATABASE_URL must be configured before launch.")
    os.environ["AKARYON_DATABASE_URL"] = database_url
    from akaryon.core.config import get_settings
    settings = get_settings()
    if not settings.openai_api_key:
        raise SystemExit("Set AKARYON_OPENAI_API_KEY in Render's environment settings.")
    # Validate authentication configuration before making database changes.
    from akaryon.hosted_auth import HostedAccess
    HostedAccess(settings, object())
    from alembic import command
    from alembic.config import Config
    command.upgrade(Config("alembic.ini"), "head")
    Path("workspace").mkdir(exist_ok=True)
    import uvicorn
    # One worker: active tasks and event streams currently live in this process.
    # Disable Uvicorn access logs so OAuth callback codes never enter logs.
    uvicorn.run("akaryon.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "10000")),
                workers=1, access_log=False, proxy_headers=False, log_config=None)


if __name__ == "__main__":
    main()
