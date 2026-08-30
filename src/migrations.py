"""Minimal ordered schema-migration mechanism (stdlib only).

Baseline schema in :mod:`src.db` is frozen. Every future schema change is a
new entry in :data:`MIGRATIONS`, never an edit to the baseline DDL. Migrations
are applied in ascending ``version`` order, one transaction each; a failure
rolls back both the migration and its version row, so it is retried on the next
startup.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Callable, NamedTuple

from .dedup import child_content_hash, parent_content_hash
from .logging_config import get_logger

_log = get_logger(__name__)


class Migration(NamedTuple):
    version: int
    name: str
    up: Callable[[sqlite3.Connection], None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _add_chunks_parent_id(conn: sqlite3.Connection) -> None:
    """Add chunks.parent_id and backfill contiguous parent groups.

    Ordering by rowid (insertion order) is essential: children are inserted
    parent-by-parent. A parent boundary is detected by ``chunk_index`` resetting
    (children of one parent are strictly increasing 0,1,2,…), which correctly
    separates parents that share the same title.
    """
    conn.execute("ALTER TABLE chunks ADD COLUMN parent_id TEXT")
    rows = conn.execute(
        "SELECT id, file_id, parent_title, chunk_index FROM chunks ORDER BY rowid"
    ).fetchall()
    current_key: tuple | None = None
    prev_index = -1
    parent_id: str | None = None
    for rid, file_id, title, chunk_index in rows:
        key = (file_id, title)
        if key != current_key or chunk_index <= prev_index:
            current_key = key
            parent_id = uuid.uuid4().hex
        prev_index = chunk_index
        conn.execute(
            "UPDATE chunks SET parent_id = ? WHERE id = ?", (parent_id, rid)
        )


def _add_files_output_path(conn: sqlite3.Connection) -> None:
    """Add files.output_path (normalized image location for cleanup).

    Historical values are only knowable from Milvus (not reachable at
    migration time), so there is nothing to backfill.
    """
    conn.execute("ALTER TABLE files ADD COLUMN output_path TEXT DEFAULT NULL")


def _drop_job_tables(conn: sqlite3.Connection) -> None:
    """Remove the dead chunk_jobs / ingestion_jobs tables (legacy DBs only)."""
    conn.execute("DROP TABLE IF EXISTS chunk_jobs")
    conn.execute("DROP TABLE IF EXISTS ingestion_jobs")


def _add_dedup_columns(conn: sqlite3.Connection) -> None:
    """Add content-fingerprint columns to chunks and backfill hashes.

    ``parent_hash`` / ``child_hash`` are normalized SHA-256 fingerprints used by
    src.dedup to skip content already in Milvus. ``deduplicated`` records whether
    a row was actually inserted into Milvus (0) or skipped as a duplicate (1).

    Historical rows are backfilled by hashing their stored content so existing
    ingested files immediately participate as dedup sources.
    """
    conn.execute("ALTER TABLE chunks ADD COLUMN parent_hash TEXT")
    conn.execute("ALTER TABLE chunks ADD COLUMN child_hash TEXT")
    conn.execute("ALTER TABLE chunks ADD COLUMN deduplicated INTEGER DEFAULT 0")
    rows = conn.execute(
        "SELECT id, parent_content, child_content FROM chunks WHERE parent_hash IS NULL"
    ).fetchall()
    for cid, pc, cc in rows:
        conn.execute(
            "UPDATE chunks SET parent_hash = ?, child_hash = ? WHERE id = ?",
            (parent_content_hash(pc), child_content_hash(cc), cid),
        )


def _add_files_updated_at(conn: sqlite3.Connection) -> None:
    """Add files.updated_at (last status transition timestamp).

    Powers stale-task recovery: a file stuck in converting/chunking/ingesting
    for longer than the worker's time limit is presumed orphaned (worker died)
    and is reset to a recoverable predecessor state on the next startup.
    """
    conn.execute("ALTER TABLE files ADD COLUMN updated_at TEXT")
    conn.execute("UPDATE files SET updated_at = created_at WHERE updated_at IS NULL")


def _add_files_tenant_kb(conn: sqlite3.Connection) -> None:
    """Add multi-tenant columns to files for Milvus recall filtering."""
    conn.execute("ALTER TABLE files ADD COLUMN tenant_id TEXT DEFAULT 'default'")
    conn.execute("ALTER TABLE files ADD COLUMN kb_id TEXT DEFAULT 'default'")


MIGRATIONS: list[Migration] = [
    Migration(1, "add_chunks_parent_id", _add_chunks_parent_id),
    Migration(2, "add_files_output_path", _add_files_output_path),
    Migration(3, "drop_job_tables", _drop_job_tables),
    Migration(4, "add_dedup_columns", _add_dedup_columns),
    Migration(5, "add_files_updated_at", _add_files_updated_at),
    Migration(6, "add_files_tenant_kb", _add_files_tenant_kb),
]


def _add_publish_staging(conn: sqlite3.Connection) -> None:
    """Create publish_staging for deferred MySQL writes (staged publishing).

    Chunk phase saves parent block metadata here; ingest phase reads it,
    writes vectors first, verifies, then upserts MySQL — eliminating the
    window where MySQL content advances before its vectors are ready.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS publish_staging (
            file_id          TEXT PRIMARY KEY,
            parent_rows_json TEXT NOT NULL,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


MIGRATIONS.append(Migration(7, "add_publish_staging", _add_publish_staging))


def _add_ingestion_jobs(conn: sqlite3.Connection) -> None:
    """Create ingestion_jobs state machine (D12).

    Tracks every staged publish through explicit states so recovery logic
    knows exactly where a failed publish stopped:

    pending → chunking → embedding → vector_verified
            → switching → switched → cleanup_pending → done

    Any failure BEFORE `switched` leaves the old MySQL version intact
    (safe to retry). A failure AFTER `switched` must forward-fix, never
    rollback — the new version is already authoritative.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingestion_jobs (
            job_id           TEXT PRIMARY KEY,
            file_id          TEXT NOT NULL,
            action           TEXT NOT NULL DEFAULT 'publish',
            state            TEXT NOT NULL DEFAULT 'pending',
            target_version   INTEGER,
            expected_chunks  INTEGER,
            indexed_chunks   INTEGER,
            retry_count      INTEGER DEFAULT 0,
            last_error       TEXT,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (file_id) REFERENCES files(id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_file ON ingestion_jobs(file_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_state ON ingestion_jobs(state)"
    )


MIGRATIONS.append(Migration(8, "add_ingestion_jobs", _add_ingestion_jobs))


def _create_tracker(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def run_migrations(conn: sqlite3.Connection) -> int:
    """Apply all pending migrations on ``conn``. Returns how many ran."""
    _create_tracker(conn)
    applied = {
        r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    count = 0
    for mig in MIGRATIONS:
        if mig.version in applied:
            continue
        with conn:  # one transaction per migration (rollback-safe)
            mig.up(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (mig.version, mig.name, _now()),
            )
        _log.info("Applied migration v%d: %s", mig.version, mig.name)
        count += 1
    return count
