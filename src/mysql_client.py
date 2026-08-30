"""MySQL storage for parent blocks (rag_parent_block table).

Child chunks live in Milvus with only a ``parent_id`` reference; the full
parent content lives here as the authoritative source for LLM context.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any

import pymysql
import pymysql.cursors

from .config import config as app_config
from .logging_config import get_logger

_log = get_logger(__name__)

_conn: pymysql.Connection | None = None
_lock = threading.Lock()


def _get_conn() -> pymysql.Connection:
    global _conn
    with _lock:
        if _conn is None or not _conn.open:
            _conn = pymysql.connect(
                host=app_config.mysql.host,
                port=app_config.mysql.port,
                user=app_config.mysql.user,
                password=app_config.mysql.password,
                database=app_config.mysql.database,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False,
            )
        _conn.ping(reconnect=True)
    return _conn


def close() -> None:
    global _conn
    if _conn and _conn.open:
        _conn.close()
        _conn = None


def insert_parent_block(
    parent_id: str,
    title: str,
    content: str,
    *,
    tenant_id: str = "default",
    kb_id: str = "default",
    summary: str | None = None,
    source_type: str = "document",
    source_id: str | None = None,
    source_uri: str | None = None,
    category: str | None = None,
    tags: list | dict | None = None,
) -> str:
    """Insert a parent block row; returns the business parent_id."""
    conn = _get_conn()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    tags_json = json.dumps(tags, ensure_ascii=False) if tags else None

    sql = """
        INSERT INTO rag_parent_block
            (tenant_id, kb_id, parent_id, title, content, summary,
             source_type, source_id, source_uri, category, tags,
             status, visibility, doc_version, content_sha256, content_chars,
             created_by, updated_by, published_at)
        VALUES (%s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                'active', 'tenant', 1, %s, %s,
                'pipeline', 'pipeline',
                %s)
        ON DUPLICATE KEY UPDATE
            title = VALUES(title),
            content = VALUES(content),
            summary = VALUES(summary),
            source_uri = VALUES(source_uri),
            category = VALUES(category),
            tags = VALUES(tags),
            status = 'active',
            content_sha256 = VALUES(content_sha256),
            content_chars = VALUES(content_chars),
            # A retry must not bump the version when authoritative text is unchanged.
            doc_version = IF(
                content_sha256 <=> VALUES(content_sha256),
                rag_parent_block.doc_version,
                rag_parent_block.doc_version + 1
            ),
            updated_by = 'pipeline',
            published_at = VALUES(published_at)
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (
                tenant_id, kb_id, parent_id,
                title[:512] if title else None,
                content,
                summary,
                source_type, source_id, source_uri,
                category[:128] if category else None,
                tags_json,
                sha256, len(content), now,
            ))
        conn.commit()
        return parent_id
    except Exception:
        conn.rollback()
        raise


def insert_parent_blocks(
    blocks: list[dict[str, Any]],
    *,
    tenant_id: str = "default",
    kb_id: str = "default",
) -> int:
    """Batch-insert parent block dicts. Each dict must have at least
    ``parent_id``, ``title``, ``content``. Optional keys match the column names.
    Returns number of rows written.
    """
    if not blocks:
        return 0
    conn = _get_conn()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    rows = []
    for b in blocks:
        content = b["content"]
        sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        tags = b.get("tags")
        category = b.get("category")
        source_uri = b.get("source_uri")
        rows.append((
            b.get("tenant_id", tenant_id),
            b.get("kb_id", kb_id),
            b["parent_id"],
            (b.get("title") or "")[:512] or None,
            content,
            b.get("summary"),
            b.get("source_type", "document"),
            b.get("source_id"),
            b.get("source_uri"),
            (category or "")[:128] or None,
            json.dumps(tags, ensure_ascii=False) if tags else None,
            sha256,
            len(content),
            now,
        ))

    sql = """
        INSERT INTO rag_parent_block
            (tenant_id, kb_id, parent_id, title, content, summary,
             source_type, source_id, source_uri, category, tags,
             status, visibility, doc_version, content_sha256, content_chars,
             created_by, updated_by, published_at)
        VALUES (%s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                'active', 'tenant', 1, %s, %s,
                'pipeline', 'pipeline',
                %s)
        ON DUPLICATE KEY UPDATE
            title = VALUES(title),
            content = VALUES(content),
            summary = VALUES(summary),
            source_uri = VALUES(source_uri),
            category = VALUES(category),
            tags = VALUES(tags),
            status = 'active',
            content_sha256 = VALUES(content_sha256),
            content_chars = VALUES(content_chars),
            # Preserve the version on task retries / identical-content upserts.
            doc_version = IF(
                content_sha256 <=> VALUES(content_sha256),
                rag_parent_block.doc_version,
                rag_parent_block.doc_version + 1
            ),
            updated_by = 'pipeline',
            published_at = VALUES(published_at)
    """
    try:
        with conn.cursor() as cur:
            affected = cur.executemany(sql, rows)

        # Copy the authoritative versions back onto the caller's blocks so the
        # subsequent Milvus insert can carry exactly matching doc_version values.
        parent_ids = [b["parent_id"] for b in blocks]
        placeholders = ",".join("%s" for _ in parent_ids)
        with conn.cursor() as version_cur:
            version_cur.execute(
                f"""
                    SELECT parent_id, doc_version
                    FROM rag_parent_block
                    WHERE tenant_id = %s AND kb_id = %s
                      AND parent_id IN ({placeholders})
                """,
                (tenant_id, kb_id, *parent_ids),
            )
            versions = {
                r["parent_id"]: r["doc_version"] for r in version_cur.fetchall()
            }
        for b in blocks:
            b["doc_version"] = versions.get(b["parent_id"], 1)
        conn.commit()
        _log.info("Upserted %d parent blocks into MySQL", len(rows))
        return len(rows)
    except Exception:
        conn.rollback()
        raise


def get_parent_versions(parent_ids: list[str], *, tenant_id: str,
                        kb_id: str) -> dict[str, int]:
    """Return {parent_id: doc_version} scoped to one tenant knowledge base."""
    if not parent_ids:
        return {}
    conn = _get_conn()
    result: dict[str, int] = {}
    with conn.cursor() as cur:
        # Keep the IN list bounded even if a very large document was chunked.
        for start in range(0, len(parent_ids), 500):
            batch = parent_ids[start:start + 500]
            placeholders = ",".join("%s" for _ in batch)
            cur.execute(
                f"""
                    SELECT parent_id, doc_version
                    FROM rag_parent_block
                    WHERE tenant_id = %s AND kb_id = %s
                      AND parent_id IN ({placeholders})
                """,
                (tenant_id, kb_id, *batch),
            )
            result.update({r["parent_id"]: r["doc_version"] for r in cur.fetchall()})
    return result


def get_parent_versions_and_hashes(
    parent_ids: list[str], *, tenant_id: str, kb_id: str,
) -> dict[str, dict[str, Any]]:
    """Return {parent_id: {"doc_version": int, "content_sha256": str}}.

    Used by the staged publish flow to determine target doc_version values
    BEFORE any MySQL write, so vector inserts can carry the correct version
    while old content remains authoritative until verification succeeds.
    """
    if not parent_ids:
        return {}
    conn = _get_conn()
    result: dict[str, dict[str, Any]] = {}
    with conn.cursor() as cur:
        for start in range(0, len(parent_ids), 500):
            batch = parent_ids[start:start + 500]
            placeholders = ",".join("%s" for _ in batch)
            cur.execute(
                f"""
                    SELECT parent_id, doc_version, content_sha256
                    FROM rag_parent_block
                    WHERE tenant_id = %s AND kb_id = %s
                      AND parent_id IN ({placeholders})
                """,
                (tenant_id, kb_id, *batch),
            )
            for r in cur.fetchall():
                result[r["parent_id"]] = {
                    "doc_version": r["doc_version"],
                    "content_sha256": r["content_sha256"],
                }
    return result


def get_parent_content(parent_id: str, *, tenant_id: str,
                       kb_id: str) -> dict | None:
    """Fetch a tenant-scoped parent block for RAG context."""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
                SELECT * FROM rag_parent_block
                WHERE tenant_id = %s AND kb_id = %s
                  AND parent_id = %s AND status = 'active'
                LIMIT 1
            """,
            (tenant_id, kb_id, parent_id),
        )
        return cur.fetchone()


def delete_parents_by_source(source_id: str, *, tenant_id: str,
                             kb_id: str,
                             exclude_parent_ids: set[str] | None = None) -> int:
    """Soft-delete all parent blocks from a given source document."""
    conn = _get_conn()
    excluded = {p for p in (exclude_parent_ids or set()) if p}
    sql = (
        "UPDATE rag_parent_block SET status = 'deleted', updated_by = 'pipeline' "
        "WHERE tenant_id = %s AND kb_id = %s AND source_id = %s "
        "AND status != 'deleted'"
    )
    params: list[Any] = [tenant_id, kb_id, source_id]
    if excluded:
        sql += f" AND parent_id NOT IN ({','.join('%s' for _ in excluded)})"
        params.extend(excluded)
    try:
        with conn.cursor() as cur:
            affected = cur.execute(
                sql, params,
            )
        conn.commit()
        return affected
    except Exception:
        conn.rollback()
        raise


def delete_parents_by_source_scopes(
    source_id: str,
    scopes: list[dict[str, str]],
    *,
    current_tenant_id: str,
    current_kb_id: str,
    exclude_parent_ids: set[str] | None = None,
) -> int:
    """Soft-delete rows only in caller-provided tenant/knowledge-base scopes.

    Only the current scope may keep ``exclude_parent_ids``. Historical scopes
    are soft-deleted unconditionally: when content is unchanged, parent IDs
    repeat across scopes and must not leak the exclusion between tenants.
    """
    conn = _get_conn()
    excluded = {p for p in (exclude_parent_ids or set()) if p}
    current_tenant = str(current_tenant_id or "default")
    current_kb = str(current_kb_id or "default")
    affected = 0
    try:
        with conn.cursor() as cur:
            for scope in scopes:
                tenant_id = str(scope.get("tenant_id") or "")
                kb_id = str(scope.get("kb_id") or "")
                if not tenant_id or not kb_id:
                    continue
                scope_is_current = (
                    tenant_id == current_tenant and kb_id == current_kb
                )
                sql = (
                    "UPDATE rag_parent_block "
                    "SET status = 'deleted', updated_by = 'pipeline' "
                    "WHERE tenant_id = %s AND kb_id = %s AND source_id = %s "
                    "AND status != 'deleted'"
                )
                params: list[Any] = [tenant_id, kb_id, source_id]
                if excluded and scope_is_current:
                    placeholders = ",".join("%s" for _ in excluded)
                    sql += f" AND parent_id NOT IN ({placeholders})"
                    params.extend(excluded)
                affected += cur.execute(sql, params)
        conn.commit()
        return affected
    except Exception:
        conn.rollback()
        raise


def delete_parents_outside_scope(
    source_id: str, *, tenant_id: str, kb_id: str
) -> int:
    """Soft-delete active rows for a source outside its authoritative scope."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            affected = cur.execute(
                """
                UPDATE rag_parent_block
                SET status = 'deleted', updated_by = 'reconciler'
                WHERE source_id = %s
                  AND (tenant_id != %s OR kb_id != %s)
                  AND status != 'deleted'
                """,
                (source_id, tenant_id, kb_id),
            )
        conn.commit()
        return affected
    except Exception:
        conn.rollback()
        raise

def get_parent_scopes_by_source(source_id: str) -> list[dict[str, str]]:
    """Return every tenant/kb scope ever recorded for a document ID."""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT tenant_id, kb_id
            FROM rag_parent_block
            WHERE source_id = %s
            """,
            (source_id,),
        )
        return cur.fetchall()

def get_all_active_parents() -> list[dict[str, Any]]:
    """Return all non-deleted parent blocks (for reconciliation scanning).

    Only fetches fields needed for consistency checks, not full content.
    """
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT parent_id, tenant_id, kb_id, source_id,
                   doc_version, status
            FROM rag_parent_block
            WHERE status = 'active'
            """
        )
        return cur.fetchall()
