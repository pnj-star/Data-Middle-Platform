"""Alembic environment for the wiki domain (Postgres). Per ROADMAP P0-T3.

DSN comes from the project's typed config (src/config.py, POSTGRES_* env vars),
not from alembic.ini, so migration and runtime share one source of truth.
"""
from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context

from src.config import config as app_config
from src.wiki.models import Base

config = context.config

# Configure the app loggers (alembic.ini [loggers] section).
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _db_url() -> str:
    """DSN for migrations.

    Defaults to the project's typed config. Tests / tooling may override by
    setting ``sqlalchemy.url`` in the Alembic Config (see src/wiki/testing.py);
    the placeholder value in alembic.ini is ignored.
    """
    url = config.get_main_option("sqlalchemy.url")
    if url and not url.startswith("driver://"):
        return url
    return app_config.postgres.dsn


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DB connection)."""
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against the configured Postgres."""
    connectable = create_engine(_db_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
