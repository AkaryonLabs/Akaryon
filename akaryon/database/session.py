from collections.abc import Iterator
from contextlib import contextmanager


def create_session_factory(database_url: str):
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
    except ImportError as exc:
        raise RuntimeError("Install PostgreSQL support with: uv sync --extra postgres") from exc
    engine = create_engine(database_url, pool_pre_ping=True)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@contextmanager
def session_scope(session_factory) -> Iterator:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
