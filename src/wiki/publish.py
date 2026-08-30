"""Wiki publish flow (ROADMAP P1-T5).

Publish = slice page markdown → embed → insert new-revision vectors → delete
old-revision vectors (insert-then-delete, D9) → mark `published` and advance
`pages.current_revision_id` (D10).

State machine: `draft → publishing → published / publish_failed`.

Cardinal invariant (D10): a revision is only marked `published` AFTER its
vectors are in Milvus, so "a published page's current_revision_id" always
equals "the revision that has vectors". Search pre-filter (P1-T6) relies on
this.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src import embedder
from src.chunker import chunk as run_chunker
from src.dedup import content_hash
from src.logging_config import get_logger
from src import milvus_expr as mx
from src.wiki import milvus as wm
from src.wiki.database import session_scope
from src.wiki.models import (
    PAGE_STATUS_DRAFT,
    PAGE_STATUS_PUBLISHED,
    PAGE_STATUS_PUBLISH_FAILED,
    PAGE_STATUS_PUBLISHING,
    Page,
    Revision,
)

_log = get_logger(__name__)


class WikiPublishError(RuntimeError):
    """Raised when a publish cannot proceed (missing page/revision, etc.)."""


def _load_page_revision(page_id: str, revision_id: int):
    """Read (page, revision) and detach the data the worker needs."""
    with session_scope() as s:
        page = s.get(Page, page_id)
        if page is None:
            raise WikiPublishError(f"page {page_id} not found")
        rev = s.execute(
            select(Revision).where(
                Revision.page_id == page_id,
                Revision.revision_id == revision_id,
            )
        ).scalar_one_or_none()
        if rev is None:
            raise WikiPublishError(f"revision {revision_id} of page {page_id} not found")
        return page.id, page.space_id, page.source_file_id or "", rev.id, rev.content_md


def _build_entities(page_id: str, revision_id: int, space_id: str,
                    source_file_id: str, tree) -> list[dict]:
    """Chunk tree → wiki collection entities (fields per ROADMAP §7).

    Row id = {page_id}:{revision_id}:{chunk_index} (deterministic, P0-T4).

    Intra-page dedup (ROADMAP P3-T7, D7): a duplicate child chunk within the
    page is embedded once — chunk_index is re-numbered after dedup so row ids
    stay unique. Cross-page knowledge-unit reuse is intentionally NOT done here
    (see ROADMAP P3-T7 note: it conflicts with the single-ownership invariant
    that the search pre-filter depends on).
    """
    entities: list[dict] = []
    seen: set[str] = set()
    idx = 0
    for parent in tree.parents:
        for child in parent.children:
            h = content_hash(child.content)
            if h in seen:
                continue  # intra-page duplicate knowledge unit → skip
            seen.add(h)
            entities.append({
                "id": wm.make_row_id(page_id, revision_id, idx),
                "page_id": page_id,
                "revision_id": revision_id,
                "space_id": space_id,
                "content": child.content,
                "chunk_index": idx,
                "parent_title": parent.title,
                "parent_content": parent.content,
                "source_file_id": source_file_id,
            })
            idx += 1
    return entities


def _set_status(page_id: str, status: str, current_revision_row_id: str | None = None) -> None:
    with session_scope() as s:
        page = s.get(Page, page_id)
        if page is None:
            return
        page.status = status
        if current_revision_row_id is not None:
            page.current_revision_id = current_revision_row_id


def publish_revision(
    page_id: str,
    revision_id: int,
    chunk_size: int | None = None,
    overlap: int | None = None,
    max_parent_size: int | None = None,
    parent_lookback_tokens: int | None = None,
    child_lookback_tokens: int | None = None,
) -> int:
    """Publish one revision: slice → embed → insert → delete-old → published.

    Returns the number of vectors inserted. On failure the page is flipped to
    `publish_failed` and the exception re-raised (Celery records FAILURE).

    Delete-old is best-effort: a leftover is invisible to search (pre-filter
    only asks for the published revision) and is converged by the cleanup task.
    """
    from src.config import config as app_config

    pid, space_id, source_file_id, rev_row_id, markdown = _load_page_revision(page_id, revision_id)
    tree = run_chunker(
        markdown,
        chunk_size if chunk_size is not None else app_config.app.chunk_size_default,
        overlap if overlap is not None else app_config.app.chunk_overlap_default,
        max_parent_size if max_parent_size is not None else app_config.app.max_parent_size_default,
        parent_lookback_tokens if parent_lookback_tokens is not None else app_config.app.parent_lookback_default,
        child_lookback_tokens,
    )
    entities = _build_entities(pid, revision_id, space_id, source_file_id, tree)

    try:
        if entities:
            # Idempotent re-publish (P4-T6): clear this revision's own vectors
            # BEFORE inserting so a re-run never collides on the deterministic
            # row ids ({page}:{rev}:{idx}) — safe with multiple workers.
            wm.delete_by_expr(
                mx.and_expr(mx.eq("page_id", pid), mx.in_int_expr("revision_id", [revision_id]))
            )
            embeddings = embedder.get_embeddings([e["content"] for e in entities])
            for e, emb in zip(entities, embeddings):
                e["embedding"] = emb
            wm.insert_vectors(entities)
        # Insert-then-delete (D9): remove every older revision's vectors.
        try:
            wm.delete_by_expr(
                mx.and_expr(mx.eq("page_id", pid), mx.ne_int_expr("revision_id", revision_id))
            )
        except Exception:
            # Converge immediately rather than relying on an external schedule:
            # retry the cleanup in-process. If that also fails the leftovers are
            # invisible to search (pre-filter reads Postgres pointers) and the
            # cleanup task remains runnable for manual/periodic convergence.
            _log.warning("delete-old failed for page %s rev %s; retrying cleanup",
                         pid, revision_id, exc_info=True)
            try:
                cleanup_page_vectors(pid, keep_revision_id=revision_id)
            except Exception:
                _log.warning("cleanup retry also failed for page %s rev %s",
                             pid, revision_id, exc_info=True)
    except Exception:
        _set_status(pid, PAGE_STATUS_PUBLISH_FAILED)
        raise

    # Vector write succeeded → mark published + advance pointer (D10).
    _set_status(pid, PAGE_STATUS_PUBLISHED, rev_row_id)
    _log.info("Published page %s revision %d (%d vectors)", pid, revision_id, len(entities))
    return len(entities)


def _has_vectors(page_id: str, revision_id: int) -> bool:
    expr = mx.and_expr(mx.eq("page_id", page_id), mx.in_int_expr("revision_id", [revision_id]))
    return wm.count(expr) > 0


def recover_stale_publishes(stale_seconds: int = 3600) -> list[str]:
    """Resolve pages stuck in `publishing` (dead worker) — see ROADMAP D10.

    For each stale page, inspect the highest-numbered revision against Milvus:
    - vectors exist  → the publish actually completed → advance to `published`.
    - no vectors     → the publish never wrote → back to `draft` and purge any
      partially-inserted vectors for that page.

    Returns the ids of pages that were recovered. Safe to run repeatedly.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
    recovered: list[str] = []
    with session_scope() as s:
        pages = s.execute(
            select(Page).where(Page.status == PAGE_STATUS_PUBLISHING, Page.updated_at < cutoff)
        ).scalars().all()
        for page in pages:
            latest = s.execute(
                select(Revision)
                .where(Revision.page_id == page.id)
                .order_by(Revision.revision_id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if latest is None:
                page.status = PAGE_STATUS_DRAFT
                recovered.append(page.id)
                continue
            if _has_vectors(page.id, latest.revision_id):
                page.status = PAGE_STATUS_PUBLISHED
                page.current_revision_id = latest.id
                _log.info("Recovered page %s → published (vectors present)", page.id)
            else:
                page.status = PAGE_STATUS_DRAFT
                try:
                    wm.delete_by_expr(mx.eq("page_id", page.id))
                except Exception:
                    _log.warning("cleanup of partial vectors failed for %s", page.id, exc_info=True)
                _log.info("Recovered page %s → draft (no vectors)", page.id)
            recovered.append(page.id)
    return recovered


def cleanup_page_vectors(page_id: str, keep_revision_id: int | None = None) -> int:
    """Delete a page's vectors except (optionally) one revision. Idempotent —
    safe to re-run; this is the convergence task for delete-old failures (D9)."""
    if keep_revision_id is None:
        expr = mx.eq("page_id", page_id)
    else:
        expr = mx.and_expr(mx.eq("page_id", page_id), mx.ne_int_expr("revision_id", keep_revision_id))
    n = wm.delete_by_expr(expr)
    _log.info("Cleanup page %s (keep rev %s): deleted %d vectors", page_id, keep_revision_id, n)
    return n
