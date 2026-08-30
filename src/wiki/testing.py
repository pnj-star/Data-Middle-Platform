"""Wiki test-DB bootstrap, shared by pytest fixtures and the setup script.

Creates the dedicated test database (``POSTGRES_TEST_DB``, default
``wiki_test``) on the dev Postgres and runs Alembic migrations against it.
Tests hit **real Postgres — never SQLite** — so dialect differences (JSON,
timestamptz, upsert) surface in tests instead of being hidden (ROADMAP P0-T6).
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text

from src.config import config as app_config

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def test_database_name() -> str:
    """Name of the dedicated wiki test database."""
    return app_config.postgres.test_db


def test_dsn() -> str:
    """DSN for the wiki test database (same server, different database)."""
    c = app_config.postgres
    return f"postgresql+psycopg://{c.user}:{c.password}@{c.host}:{c.port}/{test_database_name()}"


def _admin_dsn() -> str:
    """DSN for the maintenance 'postgres' database.

    Used to create/drop the test database. Independent of ``POSTGRES_DB`` so it
    works even when the test suite has pointed config at the (not-yet-existing)
    test database.
    """
    c = app_config.postgres
    return f"postgresql+psycopg://{c.user}:{c.password}@{c.host}:{c.port}/postgres"


def ensure_test_database(drop_first: bool = False) -> None:
    """Create the test database if missing (idempotent).

    ``drop_first=True`` drops and recreates it — use to reset a dirty test DB.
    CREATE DATABASE cannot run inside a transaction, hence AUTOCOMMIT.
    """
    admin = create_engine(_admin_dsn(), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            name = test_database_name()
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": name},
            ).scalar()
            if exists and drop_first:
                # WITH (FORCE) terminates other connections (e.g. a TestClient
                # startup that already bound an engine to this DB) so the drop
                # never fails with ObjectInUse.
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
                exists = False
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
                print(f"created test database '{name}'")
    finally:
        admin.dispose()


def run_migrations(url: str | None = None) -> None:
    """Run ``alembic upgrade head`` against ``url`` (default: test DSN).

    The Alembic Config's ``sqlalchemy.url`` is set here, which env.py prefers
    over the project config (see alembic/env.py ``_db_url``).
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url or test_dsn())
    command.upgrade(cfg, "head")


def reset_test_database() -> None:
    """Drop + recreate the test database and migrate it to head."""
    ensure_test_database(drop_first=True)
    run_migrations()
