"""Wiki-domain database wiring (ROADMAP P1-T1).

Module-level engine is created lazily so the test suite can drop/recreate the
dedicated test DB *before* the first connection, then ``get_engine()`` rebinds
the fresh schema (see tests/conftest.py wiki_engine and docs/testing.md).

For the legacy pipeline, SQLite (src/db.py) is untouched; this module is
exclusively the Postgres wiki domain (ROADMAP D15).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.config import config as app_config

_engine: Engine | None = None
_session_factory: sessionmaker | None = None


def get_engine() -> Engine:
    """Return the singleton wiki Postgres engine (created on first use)."""
    global _engine
    if _engine is None:
        _engine = create_engine(
            app_config.postgres.dsn,
            pool_size=app_config.postgres.pool_size,
            max_overflow=app_config.postgres.max_overflow,
            pool_timeout=5,
            pool_pre_ping=True,
            # Startup/health must fail fast when Postgres is unavailable.
            connect_args={"connect_timeout": 5},
        )
    return _engine


def get_session_factory() -> sessionmaker:
    """Return the singleton sessionmaker bound to the wiki engine."""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False
        )
    return _session_factory


def dispose_engine() -> None:
    """Drop the cached engine + factory. Used by test teardown / DB reset so a
    recreated test database is rebound cleanly."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


@contextmanager
def session_scope():
    """Context manager: yields a Session, commits on success, rolls back on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a Session and always closes it.

    Commit/rollback is the caller's responsibility (same convention as the
    legacy SQLite code paths).
    """
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
