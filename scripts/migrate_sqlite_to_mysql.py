"""One-time migration: copy all SQLite pipeline data into MySQL tables.

Prerequisites:
  1. MySQL tables (metadata, markdown, chunks, publish_staging,
     ingestion_jobs) already created via scripts/create_mysql_tables.sql.
  2. src/config.py MySQL connection parameters are correct.

Usage:
    python scripts/migrate_sqlite_to_mysql.py

Idempotent: uses INSERT IGNORE so re-running won't duplicate rows.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE))

from src.config import config as app_config  # noqa: E402
from src.logging_config import get_logger  # noqa: E402

import pymysql  # noqa: E402

_log = get_logger(__name__)


def _sqlite_conn() -> sqlite3.Connection:
    db_path = app_config.db_path_abs
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _mysql_conn() -> pymysql.Connection:
    return pymysql.connect(
        host=app_config.mysql.host,
        port=app_config.mysql.port,
        user=app_config.mysql.user,
        password=app_config.mysql.password,
        database=app_config.mysql.database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _migrate_metadata(sq: sqlite3.Connection, my: pymysql.Connection) -> int:
    rows = [dict(r) for r in sq.execute("SELECT * FROM files").fetchall()]
    if not rows:
        return 0
    sql = """
        INSERT IGNORE INTO metadata
            (id, name, stored_path, size, type, extension,
             mushroom_type, product_id, tenant_id, kb_id,
             status, error, output_path, chunk_count,
             created_at, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """
    params = [
        (
            r["id"], r["name"], r.get("stored_path"), r["size"],
            r["type"], r.get("extension", ""),
            r.get("mushroom_type", ""), r.get("product_id", ""),
            r.get("tenant_id", "default"), r.get("kb_id", "default"),
            r.get("status", "uploaded"), r.get("error"),
            r.get("output_path"), r.get("chunk_count"),
            r.get("created_at"), r.get("updated_at"),
        )
        for r in rows
    ]
    with my.cursor() as cur:
        cur.executemany(sql, params)
    my.commit()
    return len(params)


def _migrate_markdown(sq: sqlite3.Connection, my: pymysql.Connection) -> int:
    rows = [dict(r) for r in sq.execute("SELECT * FROM conversions").fetchall()]
    if not rows:
        return 0
    sql = """
        INSERT IGNORE INTO markdown
            (file_id, markdown, metadata_json, status, markdown_version,
             created_at, updated_at)
        VALUES (%s,%s,%s,%s,1,%s,%s)
    """
    params = []
    for r in rows:
        meta_raw = r.get("metadata")
        meta_json = None
        if meta_raw:
            try:
                meta_json = json.dumps(json.loads(meta_raw), ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                meta_json = json.dumps({"raw": str(meta_raw)}, ensure_ascii=False)
        params.append((
            r["file_id"], r.get("markdown"), meta_json,
            r.get("status", "done"),
            r.get("created_at"), r.get("updated_at"),
        ))
    with my.cursor() as cur:
        cur.executemany(sql, params)
    my.commit()
    return len(params)


def _migrate_chunks(sq: sqlite3.Connection, my: pymysql.Connection) -> int:
    rows = [dict(r) for r in sq.execute("SELECT * FROM chunks").fetchall()]
    if not rows:
        return 0
    sql = """
        INSERT IGNORE INTO chunks
            (id, file_id, parent_id, parent_title, child_content,
             chunk_index, child_size, parent_size, parent_hash,
             child_hash, deduplicated, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """
    params = [
        (
            r["id"], r["file_id"], r.get("parent_id"), r.get("parent_title"),
            r["child_content"], r["chunk_index"], r.get("child_size", 0),
            r.get("parent_size", 0), r.get("parent_hash"),
            r.get("child_hash"), 1 if r.get("deduplicated") else 0,
            r.get("created_at"),
        )
        for r in rows
    ]
    with my.cursor() as cur:
        cur.executemany(sql, params)
    my.commit()
    return len(params)


def _migrate_staging(sq: sqlite3.Connection, my: pymysql.Connection) -> int:
    rows = [dict(r) for r in sq.execute("SELECT * FROM publish_staging").fetchall()]
    if not rows:
        return 0
    sql = """
        INSERT IGNORE INTO publish_staging (file_id, parent_rows_json, created_at)
        VALUES (%s,%s,%s)
    """
    params = [(r["file_id"], r["parent_rows_json"], r.get("created_at")) for r in rows]
    with my.cursor() as cur:
        cur.executemany(sql, params)
    my.commit()
    return len(params)


def _migrate_jobs(sq: sqlite3.Connection, my: pymysql.Connection) -> int:
    rows = [dict(r) for r in sq.execute("SELECT * FROM ingestion_jobs").fetchall()]
    if not rows:
        return 0
    sql = """
        INSERT IGNORE INTO ingestion_jobs
            (job_id, file_id, action, state, target_version,
             expected_chunks, indexed_chunks, retry_count, last_error,
             created_at, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """
    params = [
        (
            r["job_id"], r["file_id"], r.get("action", "publish"),
            r.get("state", "pending"), r.get("target_version"),
            r.get("expected_chunks"), r.get("indexed_chunks"),
            r.get("retry_count", 0), r.get("last_error"),
            r.get("created_at"), r.get("updated_at"),
        )
        for r in rows
    ]
    with my.cursor() as cur:
        cur.executemany(sql, params)
    my.commit()
    return len(params)


def main() -> None:
    sq = _sqlite_conn()
    my = _mysql_conn()
    try:
        counts = {
            "metadata": _migrate_metadata(sq, my),
            "markdown": _migrate_markdown(sq, my),
            "chunks": _migrate_chunks(sq, my),
            "publish_staging": _migrate_staging(sq, my),
            "ingestion_jobs": _migrate_jobs(sq, my),
        }
        _log.info("Migration complete: %s", counts)
        for table, n in counts.items():
            print(f"  {table:20s} → {n} rows")
    finally:
        sq.close()
        my.close()


if __name__ == "__main__":
    main()
