"""Shared test setup using a dedicated MySQL test database."""
from __future__ import annotations

import os

from dotenv import load_dotenv
from pathlib import Path
import pymysql

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

_TEST_DB = os.environ.get("MYSQL_TEST_DATABASE", "rag_pipeline_test")
_connection = pymysql.connect(
    host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("MYSQL_PORT", "3307")),
    user=os.environ.get("MYSQL_USER", "root"),
    password=os.environ.get("MYSQL_PASSWORD", ""),
    charset="utf8mb4",
)
with _connection.cursor() as _cursor:
    _cursor.execute(f"DROP DATABASE IF EXISTS `{_TEST_DB}`")
    _cursor.execute(
        f"CREATE DATABASE `{_TEST_DB}` "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
    )
_connection.commit()
_connection.close()

# load_dotenv does not override an explicitly set variable, so these win over
# values in the developer .env file.
os.environ["MYSQL_DATABASE"] = _TEST_DB
os.environ["AUTO_START_WORKER"] = "false"
os.environ["MINERU_AUTO_START"] = "false"
os.environ["POSTGRES_DB"] = os.environ.get("POSTGRES_TEST_DB", "wiki_test")

import pytest  # noqa: E402


def pytest_configure(config):
    """Force auth defaults suitable for API unit tests."""
    from src.config import config as app_config

    app_config.security.api_key = ""
    app_config.security.jwt_secret = "test-secret"
    app_config.security.ip_rate_limit_per_minute = 0
    app_config.security.allow_registration = True


@pytest.fixture(autouse=True)
def mysql_pipeline_database():
    """Give every test a clean set of pipeline tables."""
    from src import db as pipeline_db

    pipeline_db.init_db()
    connection = pipeline_db._get_conn()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS rag_parent_block (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    tenant_id VARCHAR(64) NOT NULL,
                    kb_id VARCHAR(64) NOT NULL,
                    parent_id VARCHAR(128) NOT NULL,
                    title VARCHAR(512) NULL,
                    content MEDIUMTEXT NOT NULL,
                    summary TEXT NULL,
                    source_type VARCHAR(32) NOT NULL DEFAULT 'document',
                    source_id VARCHAR(255) NULL,
                    source_uri VARCHAR(1024) NULL,
                    category VARCHAR(128) NULL,
                    tags JSON NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'active',
                    visibility VARCHAR(32) NOT NULL DEFAULT 'private',
                    doc_version BIGINT UNSIGNED NOT NULL DEFAULT 1,
                    content_sha256 CHAR(64) NOT NULL,
                    content_chars INT UNSIGNED NOT NULL DEFAULT 0,
                    published_at DATETIME(3) NULL,
                    created_by VARCHAR(64) NULL,
                    updated_by VARCHAR(64) NULL,
                    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
                    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                        ON UPDATE CURRENT_TIMESTAMP(3),
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_tenant_kb_parent (tenant_id, kb_id, parent_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
            """)
            cursor.execute("DELETE FROM rag_parent_block")
        connection.commit()

        yield

        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM ingestion_jobs")
            cursor.execute("DELETE FROM publish_staging")
            cursor.execute("DELETE FROM chunks")
            cursor.execute("DELETE FROM markdown")
            cursor.execute("DELETE FROM metadata")
            cursor.execute("DELETE FROM rag_parent_block")
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def mock_milvus(monkeypatch):
    """Replace Milvus client operations with recording fakes."""
    from src import milvus_client as mc

    calls = {"delete_by_expr": [], "delete_by_ids": [], "query_by_source": []}

    def fake_delete_by_expr(expr, collection_name):
        calls["delete_by_expr"].append((expr, collection_name))
        return 0

    def fake_delete_by_ids(ids, collection_name, **kwargs):
        calls["delete_by_ids"].append((ids, collection_name, kwargs))
        return len(ids)

    def fake_query_by_source(source, collection_name, limit=100, page=1):
        calls["query_by_source"].append((source, collection_name))
        return [], 0

    monkeypatch.setattr(mc, "delete_by_expr", fake_delete_by_expr)
    monkeypatch.setattr(mc, "delete_by_ids", fake_delete_by_ids)
    monkeypatch.setattr(mc, "query_by_source", fake_query_by_source)
    return calls


@pytest.fixture(scope="session")
def wiki_engine():
    """Engine bound to the dedicated Postgres test database."""
    from src.wiki import database as wdb
    from src.wiki.testing import reset_test_database

    wdb.dispose_engine()
    reset_test_database()
    yield wdb.get_engine()
    wdb.dispose_engine()


@pytest.fixture()
def wiki_session(wiki_engine):
    """Transactional wiki session rolled back after every test."""
    from sqlalchemy.orm import Session

    connection = wiki_engine.connect()
    trans = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()
