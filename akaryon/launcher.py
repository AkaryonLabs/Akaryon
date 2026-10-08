"""Console entry point for running the local Akaryon API server."""

import os
import sys
from pathlib import Path

import uvicorn

from akaryon.core.config import get_settings


def _prepare_packaged_storage(settings) -> None:
    """Give the packaged local app a migrated SQLite store by default."""
    if not settings.database_url:
        database_path = (Path(sys.executable).resolve().parent / "akaryon.db").as_posix()
        settings.database_url = f"sqlite:///{database_path}"
    if not settings.database_url.startswith("sqlite") or ":memory:" in settings.database_url:
        return

    from alembic import command
    from alembic.config import Config

    resource_root = Path(sys._MEIPASS)
    config = Config(str(resource_root / "alembic.ini"))
    config.set_main_option("script_location", str(resource_root / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    command.upgrade(config, "head")


def main() -> None:
    if getattr(sys, "frozen", False):
        # Resolve .env and relative SQLite paths beside the executable, not the caller's cwd.
        os.chdir(Path(sys.executable).resolve().parent)
    settings = get_settings()
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        _prepare_packaged_storage(settings)
    uvicorn.run("akaryon.main:app", host=settings.host, port=settings.port,
                reload=False, log_config=None)


if __name__ == "__main__":
    main()
