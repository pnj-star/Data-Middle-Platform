"""Content-fingerprint deduplication for Milvus ingest.

Strategy (agreed with owner):
- **Parent-level dedup** (cross-file + intra-file): hash the normalized full
  ``##`` parent section. If a parent with the same hash is already in Milvus
  (inserted by a ``status='done'`` file) or was already inserted earlier in this
  same file, skip the whole parent and all of its children. Because we only skip
  when the ENTIRE section is equivalent after normalization, no parent is ever
  partially removed — the retained copy is complete, so retrieval never sees a
  half-drained parent block.
- **Child-level dedup** (intra-parent only): within one kept parent, skip a
  child whose normalized content already appeared earlier under the same parent
  (a repeated paragraph inside a section). The parent context is identical, so
  nothing is lost.
- Children are intentionally NOT deduped across different parents: that would
  drop one parent's context for shared text, which we decided is a context loss
  worth avoiding.

Normalization folds case, maps full-width forms to half-width (NFKC), and drops
ALL whitespace — not just collapses it — so OCR/layout artifacts that lose the
space between words (``Anti-counterfeitingDescription`` vs
``Anti-counterfeiting Description``) still collide. For long parent sections the
risk of two genuinely different texts differing only in spacing is negligible;
this is the trade-off the owner accepted.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

from .logging_config import get_logger

_log = get_logger(__name__)


def normalize_text(text: str) -> str:
    """Fold case + NFKC (full-width → half-width) + drop all whitespace."""
    s = unicodedata.normalize("NFKC", text or "")
    s = s.casefold()
    return re.sub(r"\s+", "", s)


def content_hash(text: str) -> str:
    """SHA-256 hex digest of the normalized text."""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def parent_content_hash(parent_content: str) -> str:
    return content_hash(parent_content)


def child_content_hash(child_content: str) -> str:
    return content_hash(child_content)


def filter_chunks(
    chunk_rows: list[dict[str, Any]], file_id: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Decide which of a file's chunk rows actually get inserted into Milvus.

    Returns ``(kept_rows, skipped_ids)``. ``chunk_rows`` are rows from
    :func:`src.db.get_chunks` in document order (chunks.seq); rows are grouped by
    parent id internally, so children of one parent never need to be contiguous.

    A parent is skipped (all of its rows) when its normalized hash is already in
    Milvus from another ``done`` file, or when an identical parent was already
    inserted earlier in this same file (duplicate sections within one document).
    Within a kept parent, a child is skipped only if the identical child was
    already inserted under the same parent in this file.
    """
    if not chunk_rows:
        return [], []

    from . import db  # lazy: avoid db -> migrations -> dedup import cycle

    current_file = db.get_file(file_id)
    tenant_id = current_file.get("tenant_id", "default") if current_file else "default"
    kb_id = current_file.get("kb_id", "default") if current_file else "default"
    product_id = current_file.get("product_id", "") if current_file else ""
    seen_milvus = db.get_ingested_parent_hashes(
        exclude_file_id=file_id, tenant_id=tenant_id, kb_id=kb_id,
        product_id=product_id,
    )

    # Group rows by parent id (each ``##`` section gets a unique parent_id from
    # the chunker, so two identical sections are separate groups and the second
    # is detected as an intra-file duplicate). Legacy rows without a parent_id
    # are treated as one-row groups so cross-file dedup still applies to them.
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for row in chunk_rows:
        key = row.get("parent_id") or row["id"]
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    def _ph(row: dict[str, Any]) -> str:
        return row.get("parent_hash") or parent_content_hash(row.get("parent_content") or "")

    seen_in_file: set[str] = set()
    kept: list[dict[str, Any]] = []
    skipped: list[str] = []
    skipped_groups = 0
    skipped_children = 0

    for key in order:
        rows = groups[key]
        ph = _ph(rows[0])
        if ph in seen_milvus or ph in seen_in_file:
            skipped.extend(r["id"] for r in rows)
            skipped_groups += 1
            continue
        seen_in_file.add(ph)
        seen_children: set[str] = set()
        for row in rows:
            ch = row.get("child_hash") or child_content_hash(row.get("child_content") or "")
            if ch in seen_children:
                skipped.append(row["id"])
                skipped_children += 1
                continue
            kept.append(row)
            seen_children.add(ch)

    if skipped:
        _log.info(
            "Dedup for file %s: %d parent section(s) skipped (%d rows), %d child rows skipped, %d kept",
            file_id, skipped_groups, skipped_groups, skipped_children, len(kept),
        )
    return kept, skipped


def invalidate_dependents(file_id: str) -> list[str]:
    """Mark other files that depended on this file's Milvus content as ``chunked``.

    Call BEFORE deleting/purging this file's rows. When this file is removed, any
    parent section that other files skipped as a duplicate is gone from Milvus;
    those files must be re-ingested to restore it. Returns the affected file ids.

    Without this, deleting the "owner" of a shared section would silently remove
    that section for every later file that was deduped against it while their
    SQLite chunks still claim it — the exact silent-data-loss window we decided
    to guard against.
    """
    from . import db

    source_file = db.get_file(file_id)
    if not source_file:
        return []
    tenant_id = source_file.get("tenant_id", "default")
    kb_id = source_file.get("kb_id", "default")
    product_id = source_file.get("product_id", "")

    parents = db.get_inserted_parent_hashes(file_id)
    if not parents:
        return []
    dependents = db.get_dependent_files(
        parents, exclude_file_id=file_id,
        tenant_id=tenant_id, kb_id=kb_id, product_id=product_id,
    )
    if dependents:
        db.update_file_statuses(dependents, "chunked")
        _log.warning(
            "Dedup source %s removed: marking %d dependent file(s) 'chunked' for re-ingest: %s",
            file_id, len(dependents), dependents,
        )
    return dependents
