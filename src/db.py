"""MySQL database layer for document-pipeline storage.

The pipeline tables live in the same MySQL database as ``rag_parent_block``.
Public function names intentionally retain the older SQLite-era vocabulary:
``insert_file`` writes ``metadata`` and ``insert_conversion`` writes
``markdown``.
"""
from __future__ import annotations

import json
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Iterator

import pymysql
import pymysql.cursors

from .config import config
from .logging_config import get_logger

_log = get_logger(__name__)
_db_initialized = False
_db_init_lock = threading.Lock()


def _get_conn() -> pymysql.connections.Connection:
    """Open a short-lived connection so API and worker threads do not share one."""
    conn = pymysql.connect(
        host=config.mysql.host,
        port=config.mysql.port,
        user=config.mysql.user,
        password=config.mysql.password,
        database=config.mysql.database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    return conn


@contextmanager
def _db() -> Iterator[pymysql.connections.Connection]:
    """Yield a MySQL connection, creating baseline tables on first use."""
    global _db_initialized
    if not _db_initialized:
        with _db_init_lock:
            if not _db_initialized:
                init_db()
                _db_initialized = True
    conn = _get_conn()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Create pipeline tables when deploying against an empty database."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    id VARCHAR(32) PRIMARY KEY,
                    name VARCHAR(512) NOT NULL,
                    stored_path VARCHAR(1024) NULL,
                    size BIGINT UNSIGNED NOT NULL DEFAULT 0,
                    type VARCHAR(16) NOT NULL DEFAULT 'text',
                    extension VARCHAR(16) NOT NULL DEFAULT '',
                    mushroom_type VARCHAR(128) NOT NULL DEFAULT '',
                    product_id VARCHAR(128) NOT NULL DEFAULT '',
                    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
                    kb_id VARCHAR(64) NOT NULL DEFAULT 'default',
                    sha256 CHAR(64) NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'uploaded',
                    error TEXT NULL,
                    output_path VARCHAR(1024) NULL,
                    chunk_count INT UNSIGNED NULL,
                    previous_tenant_id VARCHAR(64) NULL,
                    previous_kb_id VARCHAR(64) NULL,
                    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
                    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                        ON UPDATE CURRENT_TIMESTAMP(3),
                    KEY idx_metadata_tenant_kb (tenant_id, kb_id),
                    KEY idx_metadata_status (status),
                    KEY idx_metadata_name (name(191)),
                    KEY idx_metadata_sha256 (sha256)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
                """
            )
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM information_schema.columns "
                "WHERE table_schema = DATABASE() "
                "AND table_name = 'metadata' AND column_name = 'sha256'"
            )
            if int(cur.fetchone()["cnt"]) == 0:
                cur.execute(
                    "ALTER TABLE metadata ADD COLUMN sha256 CHAR(64) NULL AFTER kb_id"
                )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS markdown (
                    file_id VARCHAR(32) PRIMARY KEY,
                    markdown MEDIUMTEXT NULL,
                    raw_markdown MEDIUMTEXT NULL,
                    metadata_json JSON NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'pending',
                    clean_state VARCHAR(16) NOT NULL DEFAULT 'raw',
                    clean_report_json JSON NULL,
                    cleaner_version VARCHAR(32) NULL,
                    cleaned_at DATETIME(3) NULL,
                    markdown_version INT UNSIGNED NOT NULL DEFAULT 1,
                    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
                    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                        ON UPDATE CURRENT_TIMESTAMP(3),
                    CONSTRAINT fk_markdown_file FOREIGN KEY (file_id)
                        REFERENCES metadata(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
                """
            )
            for _clean_column, _clean_definition in (
                ("raw_markdown", "MEDIUMTEXT NULL AFTER markdown"),
                ("clean_state", "VARCHAR(16) NOT NULL DEFAULT 'raw' AFTER status"),
                ("clean_report_json", "JSON NULL AFTER clean_state"),
                ("cleaner_version", "VARCHAR(32) NULL AFTER clean_report_json"),
                ("cleaned_at", "DATETIME(3) NULL AFTER cleaner_version"),
            ):
                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() "
                    "AND table_name = 'markdown' AND column_name = %s",
                    (_clean_column,),
                )
                if int(cur.fetchone()["cnt"]) == 0:
                    cur.execute(
                        "ALTER TABLE markdown ADD COLUMN "
                        f"{_clean_column} {_clean_definition}"
                    )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    id VARCHAR(32) PRIMARY KEY,
                    seq BIGINT UNSIGNED NOT NULL AUTO_INCREMENT UNIQUE,
                    file_id VARCHAR(32) NOT NULL,
                    parent_id VARCHAR(128) NULL,
                    parent_title VARCHAR(512) NULL,
                    child_content TEXT NOT NULL,
                    chunk_index INT UNSIGNED NOT NULL DEFAULT 0,
                    child_size INT UNSIGNED NOT NULL DEFAULT 0,
                    parent_size INT UNSIGNED NOT NULL DEFAULT 0,
                    parent_hash CHAR(64) NULL,
                    child_hash CHAR(64) NULL,
                    deduplicated TINYINT(1) NOT NULL DEFAULT 0,
                    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
                    KEY idx_chunks_file (file_id),
                    KEY idx_chunks_parent (parent_id),
                    CONSTRAINT fk_chunks_file FOREIGN KEY (file_id)
                        REFERENCES metadata(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
                """
            )
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM information_schema.columns "
                "WHERE table_schema = DATABASE() "
                "AND table_name = 'chunks' AND column_name = 'seq'"
            )
            if int(cur.fetchone()["cnt"]) == 0:
                cur.execute(
                    "ALTER TABLE chunks ADD COLUMN seq BIGINT UNSIGNED NOT NULL "
                    "AUTO_INCREMENT UNIQUE AFTER id"
                )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS publish_staging (
                    file_id VARCHAR(32) PRIMARY KEY,
                    parent_rows_json MEDIUMTEXT NOT NULL,
                    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
                    CONSTRAINT fk_staging_file FOREIGN KEY (file_id)
                        REFERENCES metadata(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingestion_jobs (
                    job_id VARCHAR(32) PRIMARY KEY,
                    file_id VARCHAR(32) NOT NULL,
                    action VARCHAR(32) NOT NULL DEFAULT 'publish',
                    state VARCHAR(32) NOT NULL DEFAULT 'pending',
                    target_version INT UNSIGNED NULL,
                    expected_chunks INT UNSIGNED NULL,
                    indexed_chunks INT UNSIGNED NULL,
                    retry_count INT UNSIGNED NOT NULL DEFAULT 0,
                    last_error TEXT NULL,
                    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
                    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                        ON UPDATE CURRENT_TIMESTAMP(3),
                    KEY idx_jobs_file (file_id),
                    KEY idx_jobs_state (state),
                    CONSTRAINT fk_jobs_file FOREIGN KEY (file_id)
                        REFERENCES metadata(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
                """
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _datetime_to_epoch(value: Any) -> float:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc).timestamp()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _decode_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Convert MySQL values to API/internal dict-friendly values."""
    if row is None:
        return None
    decoded = dict(row)
    for key, value in list(decoded.items()):
        if isinstance(value, datetime):
            decoded[key] = value.isoformat(timespec="milliseconds")
        elif isinstance(value, date):
            decoded[key] = value.isoformat()
    if "metadata_json" in decoded:
        raw = decoded.pop("metadata_json")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        decoded["metadata"] = raw or {}
    if "clean_report_json" in decoded:
        raw = decoded.pop("clean_report_json")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        decoded["clean_report"] = raw or {}
    if "file_id" in decoded and "id" not in decoded:
        decoded["id"] = decoded["file_id"]
    return decoded


# --- metadata CRUD -----------------------------------------------------------


def insert_file(name: str, stored_path: str, size: int, file_type: str,
                extension: str, mushroom_type: str = "",
                product_id: str = "", tenant_id: str = "default",
                kb_id: str = "default",
                sha256: str = "") -> dict[str, Any]:
    fid = uuid.uuid4().hex
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO metadata
                    (id, name, stored_path, size, type, extension,
                     mushroom_type, product_id, tenant_id, kb_id, sha256, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'uploaded')
                """,
                (fid, name, stored_path, size, file_type, extension,
                 mushroom_type, product_id, tenant_id, kb_id, sha256 or None),
            )
        conn.commit()
    result = get_file(fid)
    assert result is not None
    return result


def get_file(file_id: str) -> dict[str, Any] | None:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM metadata WHERE id = %s", (file_id,))
            row = cur.fetchone()
    return _decode_row(row)


def find_duplicate_file(
    sha256: str, *, tenant_id: str, kb_id: str, product_id: str,
) -> dict[str, Any] | None:
    """Return an existing file matching the content fingerprint within scope."""
    if not sha256:
        return None
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM metadata
                WHERE sha256 = %s AND tenant_id = %s
                  AND kb_id = %s AND product_id = %s
                ORDER BY created_at ASC LIMIT 1
                """,
                (sha256, tenant_id, kb_id, product_id),
            )
            row = cur.fetchone()
    return _decode_row(row)


def count_files_by_stored_path(stored_path: str) -> int:
    """Count metadata rows referencing one physical upload object."""
    if not stored_path:
        return 0
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM metadata WHERE stored_path = %s",
                (stored_path,),
            )
            row = cur.fetchone()
    return int(row["cnt"]) if row else 0


def count_files_by_name(name: str, *, tenant_id: str | None = None,
                        kb_id: str | None = None) -> int:
    """Count same-named files before deciding whether legacy purge is safe."""
    if not name:
        return 0
    sql = "SELECT COUNT(*) AS cnt FROM metadata WHERE name = %s"
    params: list[Any] = [name]
    if tenant_id is not None:
        sql += " AND tenant_id = %s"
        params.append(tenant_id)
    if kb_id is not None:
        sql += " AND kb_id = %s"
        params.append(kb_id)
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    return int(row["cnt"]) if row else 0


def list_files(status: str = "", file_type: str = "",
               page: int = 1, limit: int = 50, *,
               tenant_id: str | None = None,
               kb_id: str | None = None,
               q: str = "") -> tuple[list[dict[str, Any]], int]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = %s")
        params.append(status)
    if file_type:
        clauses.append("type = %s")
        params.append(file_type)
    if tenant_id:
        clauses.append("tenant_id = %s")
        params.append(tenant_id)
    if kb_id:
        clauses.append("kb_id = %s")
        params.append(kb_id)
    if q:
        # Disable LIKE wildcards so user input is treated as literal text.
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("name LIKE %s ESCAPE '\\\\'")
        params.append(f"%{escaped}%")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS cnt FROM metadata{where}", params)
            count_row = cur.fetchone()
            offset = (page - 1) * limit
            cur.execute(
                f"SELECT * FROM metadata{where} "
                "ORDER BY created_at DESC LIMIT %s OFFSET %s",
                [*params, limit, offset],
            )
            rows = cur.fetchall()
    total = int(count_row["cnt"]) if count_row else 0
    return [_decode_row(r) for r in rows], total


def get_file_ids_by_name(q: str, *, tenant_id: str | None = None,
                          kb_id: str | None = None,
                          limit: int = 500) -> tuple[list[str], int]:
    """Resolve filename text to source IDs for Milvus admin filtering."""
    q = (q or "").strip()
    if not q:
        return [], 0
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    clauses = ["name LIKE %s ESCAPE '\\\\'"]
    params: list[Any] = [f"%{escaped}%"]
    if tenant_id:
        clauses.append("tenant_id = %s")
        params.append(tenant_id)
    if kb_id:
        clauses.append("kb_id = %s")
        params.append(kb_id)
    where = " WHERE " + " AND ".join(clauses)
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM metadata" + where, params)
            total = int(cur.fetchone()["cnt"])
            cur.execute(
                "SELECT id FROM metadata" + where +
                " ORDER BY created_at DESC LIMIT %s",
                [*params, limit],
            )
            rows = cur.fetchall()
    return [row["id"] for row in rows], total


def update_file_status(file_id: str, status: str, error: str | None = None) -> None:
    """Update pipeline status. A successful transition clears the error."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE metadata SET status = %s, error = %s WHERE id = %s",
                (status, error, file_id),
            )
        conn.commit()


def update_file_output_path(file_id: str, output_path: str) -> None:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE metadata SET output_path = %s WHERE id = %s",
                (output_path, file_id),
            )
        conn.commit()


def update_file_meta(
    file_id: str, *,
    mushroom_type: str | None = None,
    product_id: str | None = None,
    tenant_id: str | None = None,
    kb_id: str | None = None,
) -> bool:
    """Update editable metadata atomically and capture the published scope.

    The old tenant/knowledge-base pair is captured before it is overwritten,
    including when stale published data exists in a non-done pipeline state.
    If the user edits several times before re-ingesting, the first
    captured pair remains authoritative: that is where the searchable old
    vectors still live.
    """
    sets: list[str] = []
    params: list[Any] = []
    with _db() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM metadata WHERE id = %s FOR UPDATE",
                    (file_id,),
                )
                current = cur.fetchone()
                if not current:
                    return False

                for col, value in (
                    ("mushroom_type", mushroom_type),
                    ("product_id", product_id),
                ):
                    if value is not None:
                        sets.append(f"{col} = %s")
                        params.append(value)

                scope_changed = bool(
                    (tenant_id is not None and tenant_id != current["tenant_id"])
                    or (kb_id is not None and kb_id != current["kb_id"])
                )
                if scope_changed and not current.get("previous_tenant_id"):
                    sets.extend([
                        "previous_tenant_id = %s",
                        "previous_kb_id = %s",
                    ])
                    params.extend([current["tenant_id"], current["kb_id"]])

                if tenant_id is not None:
                    sets.append("tenant_id = %s")
                    params.append(tenant_id)
                if kb_id is not None:
                    sets.append("kb_id = %s")
                    params.append(kb_id)

                if not sets:
                    return False
                params.append(file_id)
                cur.execute(
                    f"UPDATE metadata SET {', '.join(sets)} WHERE id = %s",
                    params,
                )
                changed = cur.rowcount > 0
            conn.commit()
            return changed
        except Exception:
            conn.rollback()
            raise


def clear_previous_scope(file_id: str) -> None:
    """Forget the pre-publish scope after its cleanup succeeds."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE metadata
                SET previous_tenant_id = NULL, previous_kb_id = NULL
                WHERE id = %s
                """,
                (file_id,),
            )
        conn.commit()


def delete_file(file_id: str) -> None:
    """Delete metadata; markdown/chunks/staging/jobs cascade."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM metadata WHERE id = %s", (file_id,))
        conn.commit()


# --- markdown CRUD -----------------------------------------------------------


def insert_conversion(file_id: str, markdown: str, metadata: dict,
                      status: str = "done") -> str:
    """Upsert one Markdown row and bump its version only when text changes."""
    metadata_json = json.dumps(metadata, ensure_ascii=False)
    with _db() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT markdown FROM markdown WHERE file_id = %s FOR UPDATE",
                    (file_id,),
                )
                existing = cur.fetchone()
                if existing is None:
                    cur.execute(
                        """
                        INSERT INTO markdown
                            (file_id, markdown, raw_markdown, metadata_json,
                             status, markdown_version, clean_state)
                        VALUES (%s, %s, %s, %s, %s, 1, 'raw')
                        """,
                        (file_id, markdown, markdown, metadata_json, status),
                    )
                elif (existing.get("markdown") or "") == markdown:
                    cur.execute(
                        """
                        UPDATE markdown
                        SET markdown = %s, raw_markdown = %s,
                            metadata_json = %s, status = %s,
                            clean_state = 'raw', clean_report_json = NULL,
                            cleaner_version = NULL, cleaned_at = NULL
                        WHERE file_id = %s
                        """,
                        (markdown, markdown, metadata_json, status, file_id),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE markdown
                        SET markdown = %s, raw_markdown = %s,
                            metadata_json = %s, status = %s,
                            clean_state = 'raw', clean_report_json = NULL,
                            cleaner_version = NULL, cleaned_at = NULL,
                            markdown_version = markdown_version + 1
                        WHERE file_id = %s
                        """,
                        (markdown, markdown, metadata_json, status, file_id),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return file_id


_CONVERSION_COLUMNS = (
    "file_id, markdown, metadata_json, status, markdown_version, "
    "raw_markdown, clean_state, clean_report_json, cleaner_version, "
    "cleaned_at, created_at, updated_at"
)


def get_conversion(file_id: str, include_raw: bool = True) -> dict[str, Any] | None:
    cols = _CONVERSION_COLUMNS
    if not include_raw:
        cols = cols.replace("raw_markdown, ", "")
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {cols} FROM markdown WHERE file_id = %s", (file_id,))
            row = cur.fetchone()
    return _decode_row(row)


def get_clean_states_batch(file_ids: list[str]) -> dict[str, str]:
    """Return each file's markdown clean_state for list rendering."""
    if not file_ids:
        return {}
    placeholders = ",".join("%s" for _ in file_ids)
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT file_id, clean_state FROM markdown "
                f"WHERE file_id IN ({placeholders})",
                file_ids,
            )
            rows = cur.fetchall()
    return {r["file_id"]: r.get("clean_state") or "raw" for r in rows}


def get_clean_flags_batch(file_ids: list[str]) -> dict[str, bool]:
    """Return whether each file has a cleaned markdown for list rendering."""
    if not file_ids:
        return {}
    placeholders = ",".join("%s" for _ in file_ids)
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT file_id, markdown, raw_markdown, clean_state "
                f"FROM markdown WHERE file_id IN ({placeholders})",
                file_ids,
            )
            rows = cur.fetchall()
    flags: dict[str, bool] = {}
    for r in rows:
        clean_state = r.get("clean_state") or "raw"
        raw = r.get("raw_markdown") or r.get("markdown") or ""
        md = r.get("markdown") or ""
        if clean_state == "cleaned":
            flags[r["file_id"]] = True
        elif clean_state == "edited":
            flags[r["file_id"]] = md != raw
        else:
            flags[r["file_id"]] = False
    return flags


def mark_conversion_cleaned(
    file_id: str, markdown: str, report: dict, cleaner_version: str,
    raw_markdown: str | None = None,
) -> None:
    """Persist the cleaned text while keeping the original raw conversion."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE markdown
                SET markdown = %s,
                    raw_markdown = COALESCE(raw_markdown, %s),
                    clean_state = 'cleaned',
                    clean_report_json = %s,
                    cleaner_version = %s,
                    cleaned_at = CURRENT_TIMESTAMP(3),
                    markdown_version = CASE
                        WHEN markdown = %s THEN markdown_version
                        ELSE markdown_version + 1
                    END
                WHERE file_id = %s
                """,
                (
                    markdown,
                    raw_markdown,
                    json.dumps(report, ensure_ascii=False),
                    cleaner_version,
                    markdown,
                    file_id,
                ),
            )
        conn.commit()


def update_conversion_markdown(file_id: str, markdown: str,
                               target: str = "cleaned") -> None:
    """Persist edited markdown.

    ``target="raw"`` overwrites the original conversion and invalidates any
    cleaned copy; ``target="cleaned"`` edits the current cleaned text while
    keeping ``raw_markdown`` untouched.
    """
    if target == "raw":
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE markdown
                    SET markdown = %s,
                        raw_markdown = %s,
                        status = CASE WHEN %s = '' THEN 'pending' ELSE 'done' END,
                        clean_state = 'edited',
                        clean_report_json = NULL,
                        cleaner_version = NULL,
                        cleaned_at = NULL,
                        markdown_version = markdown_version + 1
                    WHERE file_id = %s
                    """,
                    (markdown, markdown, markdown, file_id),
                )
            conn.commit()
        return
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE markdown
                SET markdown = %s,
                    status = CASE WHEN %s = '' THEN 'pending' ELSE 'done' END,
                    clean_state = 'cleaned',
                    clean_report_json = NULL,
                    cleaner_version = NULL,
                    cleaned_at = NULL,
                    markdown_version = markdown_version + 1
                WHERE file_id = %s
                """,
                (markdown, markdown, file_id),
            )
        conn.commit()


# --- chunks CRUD -------------------------------------------------------------


def insert_chunks_batch(entries: list[dict[str, Any]]) -> int:
    if not entries:
        return 0
    rows = [
        (uuid.uuid4().hex, e["file_id"], e.get("parent_id"), e["child_content"],
         e["chunk_index"], e["child_size"], e.get("parent_hash"),
         e.get("child_hash"))
        for e in entries
    ]
    with _db() as conn:
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO chunks
                        (id, file_id, parent_id, child_content, chunk_index,
                         child_size, parent_hash, child_hash)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    _log.info("Batch inserted %d chunks", len(entries))
    return len(entries)


def get_chunks(file_id: str) -> list[dict[str, Any]]:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM chunks WHERE file_id = %s ORDER BY seq",
                (file_id,),
            )
            rows = cur.fetchall()
    return [_decode_row(r) for r in rows]


def delete_chunks(file_id: str, chunk_ids: list[str] | None = None) -> int:
    with _db() as conn:
        try:
            with conn.cursor() as cur:
                if chunk_ids:
                    placeholders = ",".join("%s" for _ in chunk_ids)
                    cur.execute(
                        f"DELETE FROM chunks WHERE file_id = %s "
                        f"AND id IN ({placeholders})",
                        [file_id, *chunk_ids],
                    )
                else:
                    cur.execute(
                        "DELETE FROM chunks WHERE file_id = %s", (file_id,)
                    )
                affected = cur.rowcount
            conn.commit()
            return affected
        except Exception:
            conn.rollback()
            raise


def update_chunk_content(file_id: str, chunk_id: str, child_content: str,
                         child_size: int | None = None) -> None:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE chunks SET child_content = %s, child_size = %s
                WHERE id = %s AND file_id = %s
                """,
                (child_content,
                 child_size if child_size is not None else len(child_content),
                 chunk_id, file_id),
            )
        conn.commit()


def get_chunk_count(file_id: str) -> int:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM chunks WHERE file_id = %s",
                (file_id,),
            )
            row = cur.fetchone()
    return int(row["cnt"]) if row else 0


def get_chunk_counts_batch(file_ids: list[str]) -> dict[str, int]:
    if not file_ids:
        return {}
    placeholders = ",".join("%s" for _ in file_ids)
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT file_id, COUNT(*) AS cnt FROM chunks "
                f"WHERE file_id IN ({placeholders}) GROUP BY file_id",
                file_ids,
            )
            rows = cur.fetchall()
    return {r["file_id"]: int(r["cnt"]) for r in rows}


def clear_chunks(file_id: str) -> int:
    return delete_chunks(file_id)


# --- content fingerprint dedup ----------------------------------------------


def get_ingested_parent_hashes(
    exclude_file_id: str | None = None, *,
    tenant_id: str | None = None, kb_id: str | None = None,
    product_id: str | None = None,
) -> set[str]:
    sql = (
        "SELECT DISTINCT c.parent_hash FROM chunks c "
        "JOIN metadata m ON m.id = c.file_id "
        "WHERE c.parent_hash IS NOT NULL AND c.deduplicated = 0 "
        "AND m.status = 'done'"
    )
    params: list[Any] = []
    if exclude_file_id:
        sql += " AND c.file_id != %s"
        params.append(exclude_file_id)
    if tenant_id is not None:
        sql += " AND m.tenant_id = %s"
        params.append(tenant_id)
    if kb_id is not None:
        sql += " AND m.kb_id = %s"
        params.append(kb_id)
    if product_id is not None:
        sql += " AND m.product_id = %s"
        params.append(product_id)
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return {r["parent_hash"] for r in rows}


def get_inserted_parent_hashes(file_id: str) -> set[str]:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT parent_hash FROM chunks "
                "WHERE file_id = %s AND parent_hash IS NOT NULL "
                "AND deduplicated = 0",
                (file_id,),
            )
            rows = cur.fetchall()
    return {r["parent_hash"] for r in rows}


def get_dependent_files(
    parent_hashes: set[str], exclude_file_id: str, *,
    tenant_id: str | None = None, kb_id: str | None = None,
    product_id: str | None = None,
) -> list[str]:
    if not parent_hashes:
        return []
    hashes = list(parent_hashes)
    placeholders = ",".join("%s" for _ in hashes)
    sql = (
        f"SELECT DISTINCT c.file_id FROM chunks c "
        f"JOIN metadata m ON m.id = c.file_id "
        f"WHERE c.parent_hash IN ({placeholders}) "
        f"AND c.deduplicated = 1 AND c.file_id != %s"
    )
    params: list[Any] = [*hashes, exclude_file_id]
    if tenant_id is not None:
        sql += " AND m.tenant_id = %s"
        params.append(tenant_id)
    if kb_id is not None:
        sql += " AND m.kb_id = %s"
        params.append(kb_id)
    if product_id is not None:
        sql += " AND m.product_id = %s"
        params.append(product_id)
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return [r["file_id"] for r in rows]


def mark_chunks_dedup(chunk_ids: list[str], deduplicated: bool) -> int:
    if not chunk_ids:
        return 0
    placeholders = ",".join("%s" for _ in chunk_ids)
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE chunks SET deduplicated = %s "
                f"WHERE id IN ({placeholders})",
                [1 if deduplicated else 0, *chunk_ids],
            )
            affected = cur.rowcount
        conn.commit()
    return affected


def update_file_statuses(file_ids: list[str], status: str) -> int:
    if not file_ids:
        return 0
    placeholders = ",".join("%s" for _ in file_ids)
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE metadata SET status = %s, error = NULL "
                f"WHERE id IN ({placeholders})",
                [status, *file_ids],
            )
            affected = cur.rowcount
        conn.commit()
    return affected


# --- publish staging ---------------------------------------------------------


def save_staging(file_id: str, parent_rows_json: str) -> None:
    with _db() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO publish_staging (file_id, parent_rows_json)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE
                        parent_rows_json = VALUES(parent_rows_json),
                        created_at = CURRENT_TIMESTAMP(3)
                    """,
                    (file_id, parent_rows_json),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def get_staging(file_id: str) -> str | None:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT parent_rows_json FROM publish_staging WHERE file_id = %s",
                (file_id,),
            )
            row = cur.fetchone()
    return row["parent_rows_json"] if row else None


def clear_staging(file_id: str) -> None:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM publish_staging WHERE file_id = %s", (file_id,)
            )
        conn.commit()


# --- ingestion job state machine --------------------------------------------

JOB_STATE_PENDING = "pending"
JOB_STATE_CHUNKING = "chunking"
JOB_STATE_EMBEDDING = "embedding"
JOB_STATE_VECTOR_VERIFIED = "vector_verified"
JOB_STATE_SWITCHING = "switching"
JOB_STATE_SWITCHED = "switched"
JOB_STATE_CLEANUP_PENDING = "cleanup_pending"
JOB_STATE_DONE = "done"
JOB_STATE_FAILED = "failed"
JOB_STATE_COMMITTED_FAILED = "committed_failed"

_IN_FLIGHT_STATES = {
    JOB_STATE_PENDING, JOB_STATE_CHUNKING, JOB_STATE_EMBEDDING,
    JOB_STATE_VECTOR_VERIFIED, JOB_STATE_SWITCHING, JOB_STATE_CLEANUP_PENDING,
}
_UNFINISHED_SWITCH_STATES = {
    JOB_STATE_SWITCHED, JOB_STATE_CLEANUP_PENDING, JOB_STATE_COMMITTED_FAILED,
}


def create_job(file_id: str, action: str = "publish",
               expected_chunks: int | None = None) -> str:
    job_id = uuid.uuid4().hex
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingestion_jobs
                    (job_id, file_id, action, expected_chunks)
                VALUES (%s, %s, %s, %s)
                """,
                (job_id, file_id, action, expected_chunks),
            )
        conn.commit()
    return job_id


def update_job(job_id: str, state: str, *,
               indexed_chunks: int | None = None,
               target_version: int | None = None,
               error: str | None = None,
               increment_retry: bool = False) -> None:
    sets = ["state = %s"]
    params: list[Any] = [state]
    if indexed_chunks is not None:
        sets.append("indexed_chunks = %s")
        params.append(indexed_chunks)
    if target_version is not None:
        sets.append("target_version = %s")
        params.append(target_version)
    if error is not None:
        sets.append("last_error = %s")
        params.append(error[:2000])
    if increment_retry:
        sets.append("retry_count = retry_count + 1")
    params.append(job_id)
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE ingestion_jobs SET {', '.join(sets)} WHERE job_id = %s",
                params,
            )
        conn.commit()


def get_latest_job(file_id: str) -> dict | None:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM ingestion_jobs WHERE file_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (file_id,),
            )
            row = cur.fetchone()
    return _decode_row(row)


def get_stale_inflight_jobs(stale_seconds: int = 1800) -> list[dict]:
    cutoff = _now_offset(-stale_seconds)
    states = list(_IN_FLIGHT_STATES)
    placeholders = ",".join("%s" for _ in states)
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM ingestion_jobs "
                f"WHERE state IN ({placeholders}) AND updated_at < %s "
                f"ORDER BY created_at ASC",
                [*states, cutoff],
            )
            rows = cur.fetchall()
    return [_decode_row(r) for r in rows]


def _now_offset(seconds: int) -> datetime:
    from datetime import timedelta
    return _now() + timedelta(seconds=seconds)


def get_unfinished_switched_jobs() -> list[dict]:
    """Return jobs whose vector switch succeeded but cleanup did not finish."""
    states = sorted(_UNFINISHED_SWITCH_STATES)
    placeholders = ",".join("%s" for _ in states)
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM ingestion_jobs "
                f"WHERE state IN ({placeholders}) ORDER BY created_at ASC",
                states,
            )
            rows = cur.fetchall()
    return [_decode_row(r) for r in rows]


_STALE_STATE_FALLBACK = {
    "converting": "uploaded",
    "cleaning": "converted",
    "chunking": "converted",
    "ingesting": "chunked",
}


def recover_stale_files(stale_seconds: int = 3600) -> list[str]:
    cutoff_epoch = _now().timestamp() - stale_seconds
    states = list(_STALE_STATE_FALLBACK)
    placeholders = ",".join("%s" for _ in states)
    reset: list[str] = []
    with _db() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT id, status, updated_at FROM metadata "
                    f"WHERE status IN ({placeholders})",
                    states,
                )
                rows = cur.fetchall()
                fallback_updates: list[tuple[Any, ...]] = []
                for row in rows:
                    updated = _datetime_to_epoch(row.get("updated_at"))
                    if updated and updated < cutoff_epoch:
                        fallback = _STALE_STATE_FALLBACK.get(
                            row["status"], "uploaded"
                        )
                        fallback_updates.append((
                            fallback,
                            "任务中断，已自动重置，请重新触发",
                            _now(),
                            row["id"],
                        ))
                        reset.append(row["id"])
                if fallback_updates:
                    cur.executemany(
                        "UPDATE metadata SET status = %s, error = %s, "
                        "updated_at = %s WHERE id = %s",
                        fallback_updates,
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if reset:
        _log.warning("Recovered %d stale file(s)", len(reset))
    return reset
