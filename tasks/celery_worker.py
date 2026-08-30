"""Celery asynchronous task definitions for the ingest pipeline."""
from __future__ import annotations

import json
import hashlib
import os
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE))

from celery import Celery
from celery.result import AsyncResult
from celery.signals import task_prerun, worker_ready
import redis as redis_lib

from src.config import config as app_config
from src import db
from src import dedup
from src import milvus_client as mc
from src import milvus_expr as mx
from src.converter import convert, save_markdown
from src.markdown_cleaner import (CLEANER_VERSION, CleanConfig, clean_markdown)
from src.heading_hierarchy import HeadingHierarchyConfig, repair_heading_hierarchy
from src.chunker import chunk as run_chunker
from src.embedder import insert_chunks, delete_by_source
from src import mysql_client
from src.image_handler import insert_image as run_image_insert, delete_image_by_source
from src.logging_config import get_logger

_log = get_logger(__name__)


class TaskError(RuntimeError):
    """Raised by a task when its work could not be done (precondition or failure).

    Tasks must RAISE on failure, never ``return {"error": ...}``: Celery records
    the final state as SUCCESS whenever a task returns normally, overwriting any
    earlier ``update_state(FAILURE)``. A normal return with an error payload makes
    the frontend show "完成" for work that never happened.
    """

app = Celery("pipeline", broker=app_config.redis_url, backend=app_config.redis_url)
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    result_expires=3600,
    # Hard ceiling so a runaway task can never hang the worker forever.
    task_time_limit=1800,
    task_soft_time_limit=1500,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=5,
    broker_transport_options={"visibility_timeout": 3600},
    beat_schedule={
        "reconcile-mysql-milvus": {
            "task": "reconcile_consistency",
            "schedule": 3600.0,  # every hour
        },
    },
)

# Monkey-patch: make _store_result resilient to corrupted Redis task meta.
# When a nested task (called from ingest_full_pipeline) writes intermediate
# state that gets corrupted, Celery's _store_result -> _get_task_meta_for ->
# exception_to_python raises ValueError and crashes the worker with
# CRITICAL: Unrecoverable error. This patch catches that decode failure,
# deletes the corrupt meta via raw Redis, and proceeds with a clean write.
#
# NOTE: This is a workaround. The root cause is Redis key corruption from
# concurrent writes. A long-term fix would be to use Redis streams or a
# dedicated result backend. See: celery/celery#7878
_orig_store_result = app.backend._store_result


def _safe_store_result(task_id, result, state, traceback=None,
                       request=None, **kwargs):
    try:
        return _orig_store_result(task_id, result, state,
                                  traceback=traceback,
                                  request=request, **kwargs)
    except ValueError:
        # Corrupted meta in Redis - nuke it and retry with a fresh write.
        try:
            r = redis_lib.from_url(app_config.redis_url, decode_responses=False)
            r.delete(f"celery-task-meta-{task_id}")
            r.close()
        except Exception:
            pass
        # Clear the backend's in-memory result cache so the retry reads
        # from Redis (which is now empty) instead of a cached bad value.
        app.backend._cache.pop(task_id, None)
        return _orig_store_result(task_id, result, state,
                                  traceback=traceback,
                                  request=request, **kwargs)


app.backend._store_result = _safe_store_result


@task_prerun.connect
def _clear_stale_result(task_id=None, **kwargs):
    """Delete any stale result for this task_id directly from Redis.

    Uses raw Redis DELETE instead of AsyncResult.forget() to avoid
    triggering the result decoding path, which can crash the worker
    if a previous task stored a corrupted/non-JSON-serializable result.
    """
    try:
        r = redis_lib.from_url(app_config.redis_url, decode_responses=False)
        key = f"celery-task-meta-{task_id}"
        r.delete(key)
        r.close()
    except Exception:
        pass


@worker_ready.connect
def _recover_stale_files_on_startup(**kwargs):
    """Reset files left in converting/chunking/ingesting by a dead worker.

    Runs once per worker process startup. Only files stuck longer than the
    task time limit are touched, so a genuinely running task is never reset.
    """
    try:
        db.recover_stale_files(stale_seconds=app_config.app.recover_stale_seconds)
    except Exception:
        _log.exception("Stale-file recovery on worker startup failed")
    try:
        _recover_stale_jobs()
    except Exception:
        _log.exception("Stale ingestion-job recovery on startup failed")
 
 
def _published_scopes(file_info: dict) -> list[dict[str, str]]:
    """Return explicit old/new scopes for a metadata-scoped publish."""
    current = {
        "tenant_id": str(file_info.get("tenant_id") or "default"),
        "kb_id": str(file_info.get("kb_id") or "default"),
    }
    scopes = [current]
    old_tenant = file_info.get("previous_tenant_id")
    old_kb = file_info.get("previous_kb_id")
    known = {(current["tenant_id"], current["kb_id"])}
    if old_tenant and old_kb and (old_tenant, old_kb) not in known:
        scopes.append({
            "tenant_id": str(old_tenant),
            "kb_id": str(old_kb),
        })
        known.add((str(old_tenant), str(old_kb)))
    recorded_scopes = mysql_client.get_parent_scopes_by_source(
        file_info.get("id", "")
    )
    for row in recorded_scopes:
        tenant_id = str(row.get("tenant_id") or "")
        kb_id = str(row.get("kb_id") or "")
        if tenant_id and kb_id and (tenant_id, kb_id) not in known:
            scopes.append({
                "tenant_id": tenant_id,
                "kb_id": kb_id,
            })
            known.add((tenant_id, kb_id))
    return scopes


def _cleanup_superseded_vectors(
    file_id: str, keep_chunk_ids: set[str], *, tenant_id: str, kb_id: str,
) -> None:
    """Delete this document's old Milvus rows while keeping verified inserts."""
    collection_name = app_config.milvus.text_collection
    expr = mx.and_expr(
        mx.eq("source", file_id),
        mx.eq("tenant_id", tenant_id),
        mx.eq("kb_id", kb_id),
    )
    rows = mc.query_by_expr(expr, collection_name)
    keep_ids = {str(k) for k in (keep_chunk_ids or set())}
    stale_ids = [
        str(row["id"]) for row in rows
        if row.get("id") and str(row["id"]) not in keep_ids
    ]
    for start in range(0, len(stale_ids), 500):
        mc.delete_by_ids(
            stale_ids[start:start + 500], collection_name,
        )


def _forward_fix_cleanup(
    file_info: dict, *,
    exclude_parent_ids: set[str],
    keep_chunk_ids: set[str] | None = None,
) -> None:
    """Converge MySQL and Milvus after the new version was switched live."""
    file_id = file_info["id"]
    keep_chunk_ids = keep_chunk_ids or {
        str(row["id"]) for row in db.get_chunks(file_id)
    }
    current_tenant_id = str(file_info.get("tenant_id") or "default")
    current_kb_id = str(file_info.get("kb_id") or "default")
    scopes = _published_scopes(file_info)
    for scope in scopes:
        scope_is_current = (
            scope["tenant_id"] == str(file_info.get("tenant_id") or "default")
            and scope["kb_id"] == str(file_info.get("kb_id") or "default")
        )
        _cleanup_superseded_vectors(
            file_id, keep_chunk_ids if scope_is_current else set(),
            tenant_id=scope["tenant_id"], kb_id=scope["kb_id"],
        )
        if db.count_files_by_name(file_info.get("name", "")) <= 1:
            delete_by_source(
                file_info.get("name", ""),
                tenant_id=scope["tenant_id"], kb_id=scope["kb_id"],
            )
    mysql_client.delete_parents_by_source_scopes(
        file_id,
        scopes,
        current_tenant_id=current_tenant_id,
        current_kb_id=current_kb_id,
        exclude_parent_ids=exclude_parent_ids,
    )
    db.clear_staging(file_id)
    db.clear_previous_scope(file_id)


def _recover_stale_jobs(stale_seconds: int = 1800) -> dict:
    """Recover ingestion jobs left in-flight or post-switch by a dead worker.

    Two recovery paths (D12):

    - Pre-switch stale jobs: the old MySQL version is still authoritative.
      Mark as ``failed`` so the next publish attempt starts fresh.
    - Post-switch unfinished jobs: new content is already live. Complete
      cleanup (delete old-version vectors + staging) and mark ``done``.

    Returns a summary of what was recovered for logging / monitoring.
    """
    recovered = {"stale_pre_switch": 0, "stale_post_switch": 0}

    # Path 1: pre-switch failures → safe to abandon, retry later.
    stale = db.get_stale_inflight_jobs(stale_seconds)
    for job in stale:
        db.update_job(
            job["job_id"], db.JOB_STATE_FAILED,
            error=f"recovered from stale state '{job['state']}' after {stale_seconds}s",
        )
        recovered["stale_pre_switch"] += 1

    # Path 2: switched but cleanup never ran → forward-fix.
    unfinished = db.get_unfinished_switched_jobs()
    for job in unfinished:
        file_info = db.get_file(job["file_id"])
        if not file_info:
            db.update_job(job["job_id"], db.JOB_STATE_DONE,
                          error="forward-fix skipped: file row deleted")
            continue
        try:
            chunks = db.get_chunks(job["file_id"])
            keep_pids = {
                str(row["parent_id"]) for row in chunks if row.get("parent_id")
            }
            _forward_fix_cleanup(file_info, exclude_parent_ids=keep_pids)
            db.update_job(job["job_id"], db.JOB_STATE_DONE,
                          error=None)
            recovered["stale_post_switch"] += 1
        except Exception:
            _log.warning("Post-switch forward-fix failed for job %s",
                         job["job_id"], exc_info=True)

    if any(recovered.values()):
        _log.info("Recovered stale ingestion jobs: %+s", recovered)
    return recovered


def _load_json_file(path):
    if not path:
        return None
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = _BASE / file_path
    if not file_path.exists():
        return None
    return json.loads(file_path.read_text(encoding="utf-8"))


def _clean_config(*, layout_mode: bool = False) -> CleanConfig:
    c = app_config.clean
    return CleanConfig(
        layout_mode=layout_mode,
        force_single_h1=c.force_single_h1,
        max_heading_chars=c.max_heading_chars,
        merge_repeated_headings=c.merge_repeated_headings,
        max_repeated_gap_lines=c.max_repeated_gap_lines,
        max_gap_text_chars=c.max_gap_text_chars,
        max_repeated_body_text_chars=c.max_repeated_body_text_chars,
        require_image_gap=c.require_image_gap,
        require_image_body=c.require_image_body,
        allow_inline_bold_heading=c.allow_inline_bold_heading,
        remove_page_residue=c.remove_page_residue,
        split_paged_long_lines=c.split_paged_long_lines,
        min_paged_split_chars=c.min_paged_split_chars,
        convert_numbered_bodies=c.convert_numbered_bodies,
        max_numbered_list_chars=c.max_numbered_list_chars,
        dedupe_repeated_text=c.dedupe_repeated_text,
        min_duplicate_text_chars=c.min_duplicate_text_chars,
        normalize_ocr_spacing=c.normalize_ocr_spacing,
        clean_inline_tags=c.clean_inline_tags,
        normalize_list_bullets=c.normalize_list_bullets,
        clean_invisible_chars=c.clean_invisible_chars,
        collapse_whitespace=c.collapse_whitespace,
        clean_external_links=c.clean_external_links,
        merge_soft_wraps=c.merge_soft_wraps,
        merge_broken_sentences=c.merge_broken_sentences,
        max_merged_line_chars=c.max_merged_line_chars,
        min_broken_merge_chars=c.min_broken_merge_chars,
        remove_ocr_garbage_fragments=c.remove_ocr_garbage_fragments,
        min_ocr_garbage_chars=c.min_ocr_garbage_chars,
        max_ocr_garbage_chars=c.max_ocr_garbage_chars,
        noise_terms=list(c.noise_terms or []),
    )


def _ensure_clean(file_id: str, conversion: dict, file_info: dict,
                   *, force: bool = False) -> dict:
    """Run the conservative heading pass before chunking.

    Manual edits (``clean_state='edited'``) are never overwritten. Already
    cleaned rows are reused unless ``force`` is set (explicit re-clean). The
    raw conversion stays untouched in ``raw_markdown`` and is always the
    source for a (re-)clean, so repeated runs never re-clean earlier output.
    """
    state = conversion.get("clean_state") or "raw"
    if state == "edited":
        return {
            "markdown": conversion["markdown"],
            "report": {},
            "changed": False,
            "skipped": "edited",
        }
    if state == "cleaned" and not force:
        return {
            "markdown": conversion["markdown"],
            "report": conversion.get("clean_report") or {},
            "changed": False,
        }
    source = conversion.get("raw_markdown") or conversion["markdown"]
    layout_meta = (conversion.get("metadata") or {}).get("layout") or {}
    content_list = None
    middle_json = None
    if app_config.app.heading_hierarchy_mode == "auto":
        content_list = _load_json_file(layout_meta.get("content_list_path"))
        middle_json = _load_json_file(layout_meta.get("middle_json_path"))
    hierarchy_report: dict = {}
    if content_list:
        repaired = repair_heading_hierarchy(
            source,
            content_list,
            middle_json,
            config=HeadingHierarchyConfig(),
        )
        source = repaired.markdown
        hierarchy_report = repaired.report
    result = clean_markdown(
        source,
        extension=str(file_info.get("extension") or ""),
        config=_clean_config(layout_mode=bool(content_list)),
    )
    result.report["hierarchy"] = hierarchy_report
    result.report["counts"]["heading_level_adjusted"] = hierarchy_report.get(
        "adjusted_levels", 0
    )
    db.mark_conversion_cleaned(
        file_id,
        result.markdown,
        result.report,
        CLEANER_VERSION,
        source,
    )
    conversion["markdown"] = result.markdown
    conversion["clean_state"] = "cleaned"
    changed = (
        result.markdown != source
        or any(result.report["counts"].values())
    )
    return {
        "markdown": result.markdown,
        "report": result.report,
        "changed": changed,
    }


def _execute_chunk(file_id: str, markdown: str, file_info: dict,
                   chunk_size: int, overlap: int,
                   max_parent_size: int, parent_lookback_tokens: int,
                   child_lookback_tokens: int | None) -> dict:
    """Run the chunker and persist children (SQLite) + stage parents.

    Shared by ``chunk_document`` and ``ingest_full_pipeline`` so pipeline
    changes only need to happen in one place.

    Staged publishing (D11): parent blocks are NOT written to MySQL here.
    They are saved to SQLite ``publish_staging`` and committed only after
    Milvus vectors are verified in ``_execute_ingest`` — eliminating the
    window where MySQL content advances before its vectors are ready.
    """
    db.update_file_status(file_id, "chunking")
    db.clear_chunks(file_id)

    tree = run_chunker(
        markdown, chunk_size, overlap, max_parent_size,
        parent_lookback_tokens, child_lookback_tokens)

    tenant_id = file_info.get("tenant_id", "default")
    kb_id = file_info.get("kb_id", "default")
    product_id = file_info.get("product_id", "")
    tags: dict[str, str] = {}
    if product_id:
        tags["product_id"] = product_id
    parent_rows = []
    chunk_entries: list[dict] = []
    for parent in tree.parents:
        parent_hash = dedup.parent_content_hash(parent.content)
        parent_images = parent.images or []
        parent_rows.append({
            "parent_id": parent.parent_id,
            "title": parent.title or "",
            "content": parent.content,
            "source_type": "document",
            "source_id": file_id,
            "source_uri": file_info.get("stored_path"),
            "category": file_info.get("mushroom_type") or None,
            "tags": {**tags, "images": parent_images} if parent_images else (tags or None),
            "summary": parent.content[:800],
        })
        for child in parent.children:
            chunk_entries.append({
                "file_id": file_id,
                "parent_id": parent.parent_id,
                "child_content": child.content,
                "chunk_index": child.index,
                "child_size": child.size,
                "parent_size": parent.size,
                "parent_hash": parent_hash,
                "child_hash": dedup.child_content_hash(child.content),
            })
    db.insert_chunks_batch(chunk_entries)
    # Stage parent metadata for deferred commit during ingest (D11).
    db.save_staging(file_id, json.dumps(parent_rows, ensure_ascii=False))
    db.update_file_status(file_id, "chunked")

    return {"parent_count": tree.parent_count, "child_count": tree.child_count}


def _execute_ingest(file_id: str, file_info: dict) -> tuple[int, int]:
    """Staged publish (D11): embed → insert-new → verify → MySQL → cleanup-old.

    Shared by ``ingest_document`` and ``ingest_full_pipeline``.

    Write order guarantees:
    1. New-version vectors are inserted into Milvus first.
    2. Vector count is verified against expected chunk count.
    3. Only then are MySQL parent blocks upserted (advancing doc_version).
    4. Old-version vectors are cleaned asynchronously after MySQL commit.

    If step 1-2 fails, old vectors remain intact and MySQL is unchanged —
    the system stays consistent at version N. If step 4 fails, stale
    vectors are invisible to retrieval (MySQL serves current version) and
    are converged by the reconciliation task.

    State machine (D12): every run creates an ``ingestion_jobs`` record that
    tracks progress through explicit states. A failure BEFORE ``switched``
    marks the job ``failed`` (safe to retry); AFTER ``switched`` marks it
    ``committed_failed`` (must forward-fix, never rollback).
    """
    chunks = db.get_chunks(file_id)
    if not chunks:
        raise TaskError("No chunks found. Chunk first.")

    job_id = db.create_job(file_id, action="publish", expected_chunks=len(chunks))
    db.update_job(job_id, db.JOB_STATE_EMBEDDING)

    try:
        tenant_id = file_info.get("tenant_id", "default")
        kb_id = file_info.get("kb_id", "default")

        staging_json = db.get_staging(file_id)
        if staging_json:
            staged_parents = json.loads(staging_json)
        else:
            staged_parents = _reconstruct_parents_from_chunks(chunks, file_info)

        unique_parent_ids = [p["parent_id"] for p in staged_parents]

        staged_sha256: dict[str, str] = {}
        for pr in staged_parents:
            raw = hashlib.sha256(pr["content"].encode("utf-8")).hexdigest()
            staged_sha256[pr["parent_id"]] = raw

        current_state = mysql_client.get_parent_versions_and_hashes(
            unique_parent_ids, tenant_id=tenant_id, kb_id=kb_id,
        ) if unique_parent_ids else {}

        target_versions: dict[str, int] = {}
        for pid in unique_parent_ids:
            cur = current_state.get(pid)
            new_sha = staged_sha256.get(pid)
            if cur is None:
                target_versions[pid] = 1
            elif new_sha and cur["content_sha256"] != new_sha:
                target_versions[pid] = cur["doc_version"] + 1
            else:
                target_versions[pid] = cur["doc_version"]

        chunks, skipped_ids = dedup.filter_chunks(chunks, file_id)

        child_contents = [c["child_content"] for c in chunks]
        parent_ids = [c["parent_id"] for c in chunks]
        chunk_indexes = [c["chunk_index"] for c in chunks]
        chunk_ids = [c["id"] for c in chunks]
        doc_versions = [target_versions.get(c["parent_id"], 1) for c in chunks]

        metadata = {
            "source": file_info["id"],
            "mushroom_type": file_info.get("mushroom_type", ""),
            "product_id": file_info.get("product_id", ""),
            "category": "",
            "tenant_id": tenant_id, "kb_id": kb_id,
        }
        count = insert_chunks(child_contents, parent_ids, chunk_indexes,
                              metadata, doc_version=doc_versions,
                              chunk_ids=chunk_ids)

        expected = len(chunks)
        # Strict verification: Milvus must accept exactly the rows submitted.
        # The old `count < expected` check let an ALL-deduped document
        # (expected == 0, count == 0) pass as "success" while nothing was
        # written, hiding the skip from logs/UI and confusing re-ingest
        # debugging (MySQL advanced, Milvus stayed empty).
        if count != expected:
            raise TaskError(
                f"Vector verification failed for {file_id}: "
                f"expected {expected} but Milvus accepted {count}"
            )
        if count == 0 and skipped_ids:
            _log.warning(
                "File %s: all %d chunk(s) dedup-skipped, 0 vectors inserted "
                "(identical content already served by another 'done' file).",
                file_id, len(skipped_ids),
            )

        db.update_job(
            job_id, db.JOB_STATE_VECTOR_VERIFIED,
            indexed_chunks=count,
            target_version=max(target_versions.values()) if target_versions else 1,
        )
    except Exception as pre_err:
        db.update_job(job_id, db.JOB_STATE_FAILED, error=str(pre_err))
        raise

    # Vectors verified → safe to advance MySQL (D11/D12 critical switch point).
    if staged_parents:
        db.update_job(job_id, db.JOB_STATE_SWITCHING)
        mysql_client.insert_parent_blocks(
            staged_parents, tenant_id=tenant_id, kb_id=kb_id,
        )
        db.update_job(job_id, db.JOB_STATE_SWITCHED)

        # Post-switch cleanup: failures here are forward-fix only.
        keep_pids = {pr["parent_id"] for pr in staged_parents}
        try:
            db.update_job(job_id, db.JOB_STATE_CLEANUP_PENDING)
            _forward_fix_cleanup(
                file_info,
                exclude_parent_ids=keep_pids,
                keep_chunk_ids=set(chunk_ids),
            )
        except Exception as cleanup_err:
            _log.warning("Post-switch cleanup failed for %s (job %s)", file_id, job_id, exc_info=True)
            db.update_job(job_id, db.JOB_STATE_COMMITTED_FAILED,
                          error=f"post-switch cleanup: {cleanup_err}")
            # Do NOT re-raise: new content is already live; reconciliation will converge.

    if skipped_ids:
        db.mark_chunks_dedup(skipped_ids, True)
    db.update_file_status(file_id, "done")
    db.update_job(job_id, db.JOB_STATE_DONE, indexed_chunks=count)

    # (inserted_count, dedup_skipped_count): callers surface the skipped count
    # to the UI so a fully-deduped document isn't reported as a normal ingest.
    return count, len(skipped_ids)


def _reconstruct_parents_from_chunks(
    chunks: list[dict], file_info: dict,
) -> list[dict]:
    """Rebuild approximate parent blocks when publish_staging is absent.

    Used after partial chunk edits (merge/delete via ``update_chunks``) that
    clear staging. Parent content is approximated by joining children in
    index order — sufficient for MySQL context retrieval and hash comparison.
    """
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for c in chunks:
        pid = c.get("parent_id") or ""
        if not pid:
            continue
        if pid not in groups:
            groups[pid] = []
            order.append(pid)
        groups[pid].append(c)

    source_uri = file_info.get("stored_path", "")
    category = file_info.get("mushroom_type") or None
    product_id = file_info.get("product_id", "")
    tags: dict[str, str] = {}
    if product_id:
        tags["product_id"] = product_id

    parents: list[dict] = []
    for pid in order:
        rows = sorted(groups[pid], key=lambda c: c["chunk_index"])
        content = "\n\n".join(r["child_content"] for r in rows)
        first_line = content.split("\n", 1)[0].strip()
        title = first_line[:512] if first_line else ""
        parents.append({
            "parent_id": pid,
            "title": title,
            "content": content,
            "summary": content[:800],
            "source_type": "document",
            "source_id": file_info["id"],
            "source_uri": source_uri,
            "category": category,
            "tags": tags or None,
        })
    return parents


def _execute_convert(file_id: str, file_info: dict) -> dict:
    """Convert one document and persist its authoritative Markdown."""
    db.update_file_status(file_id, "converting")
    file_path = file_info["stored_path"]
    if not os.path.isabs(file_path):
        file_path = str(_BASE / file_path)

    result = convert(file_path, artifact_key=file_id)
    save_markdown(file_id, result.markdown)
    db.insert_conversion(file_id, result.markdown, result.metadata, "done")
    db.update_file_status(file_id, "converted")
    return {"file_id": file_id, "status": "converted", "metadata": result.metadata}


@app.task(bind=True, name="convert_document")
def convert_document(self, file_id: str):
    """Convert a document to Markdown (MinerU / built-in plain-text path)."""
    self.update_state(state="PROGRESS", meta={"step": "converting", "pct": 0})

    file_info = db.get_file(file_id)
    if not file_info:
        raise TaskError("File not found")

    try:
        convert_result = _execute_convert(file_id, file_info)
        self.update_state(
            state="PROGRESS",
            meta={"step": "converting", "pct": 100, "status": "done"},
        )
        return convert_result
    except Exception as e:
        _log.exception("Conversion failed for file %s", file_id)
        db.update_file_status(file_id, "failed", str(e))
        raise


@app.task(bind=True, name="clean_document")
def clean_document(self, file_id: str):
    """Clean heading structure after conversion, before chunking."""
    self.update_state(state="PROGRESS", meta={"step": "cleaning", "pct": 0})

    file_info = db.get_file(file_id)
    if not file_info:
        raise TaskError("File not found")

    conversion = db.get_conversion(file_id)
    if not conversion:
        raise TaskError("No conversion found. Convert first.")

    if (conversion.get("clean_state") or "raw") == "edited":
        self.update_state(state="PROGRESS", meta={
            "step": "cleaning", "pct": 100,
            "changed": False, "skipped": "edited",
        })
        return {
            "file_id": file_id, "status": "edited",
            "changed": False, "skipped": "edited",
        }

    try:
        db.update_file_status(file_id, "cleaning")
        result = _ensure_clean(file_id, conversion, file_info, force=True)
        db.update_file_status(file_id, "cleaned")
        self.update_state(state="PROGRESS", meta={
            "step": "cleaning", "pct": 100,
            "changed": result["changed"], "report": result["report"],
        })
        _log.info("Cleaning complete for %s (changed=%s)",
                  file_info["name"], result["changed"])
        return {"file_id": file_id, "status": "cleaned", **result}
    except Exception as e:
        _log.exception("Cleaning failed for file %s", file_id)
        db.update_file_status(file_id, "failed", str(e))
        raise


@app.task(bind=True, name="chunk_document")
def chunk_document(self, file_id: str, chunk_size: int = 300, overlap: int = 40,
                   max_parent_size: int = 2000, parent_lookback_tokens: int = 80,
                   child_lookback_tokens: int | None = None):
    """Split converted Markdown into parent-child chunks."""
    self.update_state(state="PROGRESS", meta={"step": "chunking", "pct": 0})

    file_info = db.get_file(file_id)
    if not file_info:
        raise TaskError("File not found")

    conversion = db.get_conversion(file_id)
    if not conversion:
        raise TaskError("No conversion found. Convert first.")

    try:
        db.update_file_status(file_id, "cleaning")
        cleaned = _ensure_clean(file_id, conversion, file_info)
        db.update_file_status(
            file_id, "cleaned" if cleaned["changed"] else "converted"
        )
        db.clear_chunks(file_id)

        result = _execute_chunk(
            file_id,
            cleaned["markdown"],
            file_info,
            chunk_size,
            overlap,
            max_parent_size,
            parent_lookback_tokens,
            child_lookback_tokens,
        )
        self.update_state(state="PROGRESS", meta={
            "step": "chunking", "pct": 100,
            "parent_count": result["parent_count"],
            "child_count": result["child_count"],
        })
        _log.info("Chunking complete for %s: %d parents, %d children",
                  file_info["name"], result["parent_count"], result["child_count"])
        return {"file_id": file_id, **result}
    except Exception as e:
        _log.exception("Chunking failed for file %s", file_id)
        db.update_file_status(file_id, "failed", str(e))
        raise


@app.task(bind=True, name="ingest_document")
def ingest_document(self, file_id: str):
    """Embed chunks and insert into Milvus mushroom_knowledge."""
    self.update_state(state="PROGRESS", meta={"step": "embedding", "pct": 0})

    file_info = db.get_file(file_id)
    if not file_info:
        raise TaskError("File not found")

    try:
        db.update_file_status(file_id, "ingesting")
        source = file_info["id"]
        count, dedup_skipped = _execute_ingest(file_id, file_info)

        # self.update_state may fail on Redis transient issues; never let
        # that roll back the successful ingestion.
        try:
            self.update_state(state="PROGRESS", meta={"step": "done", "pct": 100, "inserted": count})
        except Exception:
            pass

        _log.info("Ingestion complete for %s: %d chunks inserted", source, count)
        return {"file_id": file_id, "inserted": count, "dedup_skipped": dedup_skipped}
    except Exception as e:
        _log.exception("Ingestion failed for file %s", file_id)
        # If the file is already marked done, the vectors are in Milvus and
        # only the final Redis progress write failed — report success, not
        # failure, so the frontend doesn't show an error for data that exists.
        current = db.get_file(file_id)
        if current and current.get("status") == "done":
            return {
                "file_id": file_id,
                "inserted": locals().get("count", 0),
                "dedup_skipped": locals().get("dedup_skipped", 0),
            }
        if current:
            db.update_file_status(file_id, "failed", str(e))
        raise


@app.task(bind=True, name="ingest_image")
def ingest_image(self, file_id: str):
    """Process image: CLIP embedding + insert into Milvus mushroom_images."""
    self.update_state(state="PROGRESS", meta={"step": "processing", "pct": 0})

    file_info = db.get_file(file_id)
    if not file_info:
        raise TaskError("File not found")

    try:
        db.update_file_status(file_id, "ingesting")
        file_path = file_info["stored_path"]
        if not os.path.isabs(file_path):
            file_path = str(_BASE / file_path)

        source = file_info["id"]
        mushroom_type = file_info.get("mushroom_type", "")
        product_id = file_info.get("product_id", "")

        self.update_state(state="PROGRESS", meta={"step": "deleting_old", "pct": 5})
        delete_image_by_source(source)
        if db.count_files_by_name(file_info["name"]) <= 1:
            delete_image_by_source(file_info["name"])

        self.update_state(state="PROGRESS", meta={"step": "embedding", "pct": 30})
        metadata = {"source": source, "mushroom_type": mushroom_type, "product_id": product_id}
        result = run_image_insert(file_path, metadata)
        db.update_file_output_path(file_id, result.stored_path)

        db.update_file_status(file_id, "done")
        self.update_state(state="PROGRESS", meta={"step": "done", "pct": 100, "image_id": result.image_id})
        _log.info("Image ingestion complete for %s: id=%s", source, result.image_id)
        return {"file_id": file_id, "image_id": result.image_id}
    except Exception as e:
        _log.exception("Image ingestion failed for file %s", file_id)
        db.update_file_status(file_id, "failed", str(e))
        raise


@app.task(bind=True, name="ingest_full_pipeline")
def ingest_full_pipeline(self, file_id: str, chunk_size: int = 300, overlap: int = 40,
                         max_parent_size: int = 2000, parent_lookback_tokens: int = 80,
                         child_lookback_tokens: int | None = None):
    """One-click full pipeline: convert -> chunk -> ingest."""
    self.update_state(state="PROGRESS", meta={"step": "converting", "pct": 0})

    file_info = db.get_file(file_id)
    if not file_info:
        raise TaskError("File not found")

    file_type = file_info["type"]

    try:
        if file_type == "image":
            # Delegate to the dedicated image ingest task safely.
            result = ingest_image.delay(file_id)
            return {"delegated": True, "task_id": result.id, "step": "ingest_image"}
        else:
            # ---- convert ----
            self.update_state(state="PROGRESS", meta={"step": "converting", "pct": 5})
            convert_result = _execute_convert(file_id, file_info)

            # ---- clean ----
            self.update_state(state="PROGRESS", meta={"step": "cleaning", "pct": 20})
            conversion = db.get_conversion(file_id)
            if not conversion:
                raise TaskError("No conversion found. Convert first.")
            cleaned = _ensure_clean(file_id, conversion, file_info)
            self.update_state(state="PROGRESS", meta={"step": "cleaning", "pct": 35})
            if cleaned["changed"]:
                db.update_file_status(file_id, "cleaned")

            # ---- chunk ----
            self.update_state(state="PROGRESS", meta={"step": "chunking", "pct": 45})
            chunk_result = _execute_chunk(
                file_id, cleaned["markdown"], file_info,
                chunk_size, overlap, max_parent_size,
                parent_lookback_tokens, child_lookback_tokens)

            # ---- ingest ----
            self.update_state(state="PROGRESS", meta={"step": "embedding", "pct": 65})
            ingest_result = _execute_ingest(file_id, file_info)
            insert_count, dedup_skipped = ingest_result

            # self.update_state may fail on transient Redis errors; never let
            # that roll back a successful ingestion.
            try:
                self.update_state(state="PROGRESS", meta={"step": "done", "pct": 100})
            except Exception:
                pass

            _log.info("Full pipeline complete for %s: %d parents, %d children, %d inserted",
                      file_id, chunk_result["parent_count"], chunk_result["child_count"], insert_count)
            return {"file_id": file_id,
                    "conversion": convert_result,
                    "chunks": {"file_id": file_id, **chunk_result},
                    "ingestion": {"file_id": file_id, "inserted": insert_count,
                                  "dedup_skipped": dedup_skipped}}
    except Exception as e:
        _log.exception("Full pipeline failed for file %s", file_id)
        # If the file is already done, the vectors are in Milvus and only the
        # final Redis progress write failed — report success, not failure.
        current = db.get_file(file_id)
        if current and current.get("status") == "done":
            return {
                "file_id": file_id,
                "inserted": locals().get("insert_count", 0),
                "dedup_skipped": locals().get("dedup_skipped", 0),
            }
        if current:
            db.update_file_status(file_id, "failed", str(e))
        raise


@app.task(bind=True, name="reembed_merged_chunk")
def reembed_merged_chunk(self, file_id: str, chunk_id: str):
    """Re-embed a single merged chunk so Milvus matches SQLite post-merge."""
    self.update_state(state="PROGRESS", meta={"step": "embedding", "pct": 0})

    file_info = db.get_file(file_id)
    if not file_info:
        raise TaskError("File not found")

    chunks = db.get_chunks(file_id)
    target = next((c for c in chunks if c["id"] == chunk_id), None)
    if not target:
        raise TaskError("Chunk not found")
    tenant_id = file_info.get("tenant_id", "default")
    kb_id = file_info.get("kb_id", "default")
    versions = mysql_client.get_parent_versions(
        [target["parent_id"]], tenant_id=tenant_id, kb_id=kb_id,
    )

    try:
        # Upsert replaces any prior vector for this chunk id in-place.
        metadata = {
            "source": file_info["id"],
            "mushroom_type": file_info.get("mushroom_type", ""),
            "product_id": file_info.get("product_id", ""),
            "category": "",
            "tenant_id": tenant_id,
            "kb_id": kb_id,
        }
        count = insert_chunks(
            [target["child_content"]],
            [target["parent_id"]],
            [target["chunk_index"]],
            metadata,
            doc_version=versions.get(target["parent_id"], 1),
            chunk_ids=[chunk_id],
        )
        self.update_state(state="PROGRESS", meta={"step": "done", "pct": 100})
        return {"file_id": file_id, "chunk_id": chunk_id, "inserted": count}
    except Exception as e:
        _log.exception("Re-embed failed for chunk %s in file %s", chunk_id, file_id)
        raise


@app.task(bind=True, name="purge_file_vectors")
def purge_file_vectors(self, file_id: str, file_type: str, name: str,
                       name_unique: bool, tenant_id: str = "default",
                       kb_id: str = "default"):
    """Purge a deleted file's vectors from its Milvus collection.

    The DELETE endpoint deletes the SQLite row + local artifacts synchronously
    so the file vanishes from the UI instantly; the Milvus delete + flush + load
    (which can take seconds) runs here in the background. ``name_unique`` is
    captured by the endpoint BEFORE the SQLite row disappears — legacy rows
    keyed by the filename are purged only when this name was unique at deletion
    time, so a same-named sibling's vectors are never wiped.
    """
    coll = (app_config.milvus.text_collection if file_type == "text"
            else app_config.milvus.image_collection)
    try:
        total = mc.purge_file_vectors(
            file_id, file_type, name, name_unique,
            tenant_id=tenant_id, kb_id=kb_id,
        )
        # Soft-delete parent blocks in MySQL for text files.
        if file_type == "text":
            mysql_client.delete_parents_by_source(
                file_id, tenant_id=tenant_id, kb_id=kb_id,
            )
        _log.info("Purged %d Milvus rows for deleted file %s", total, file_id)
    except Exception:
        _log.warning("Milvus purge failed for deleted file %s", file_id, exc_info=True)


# ── Wiki publish (ROADMAP P1-T5) ──────────────────────────────────

@app.task(bind=True, name="publish_wiki_page")
def publish_wiki_page(self, page_id: str, revision_id: int):
    """Publish a wiki page revision: slice → embed → insert → delete-old → published.

    On failure ``publish_revision`` flips the page to ``publish_failed`` and
    re-raises, so Celery records FAILURE (never a false SUCCESS — see TaskError).
    """
    from src.wiki.publish import publish_revision

    vectors = publish_revision(page_id, revision_id)
    return {"page_id": page_id, "revision_id": revision_id, "vectors": vectors}


@app.task(bind=True, name="cleanup_wiki_page_vectors")
def cleanup_wiki_page_vectors(self, page_id: str, keep_revision_id: int | None = None):
    """Converge leftover old-revision vectors (delete-old failure, ROADMAP D9).

    Idempotent and re-runnable. ``keep_revision_id=None`` purges all of a page's
    vectors (used when returning to draft after a failed publish).
    """
    from src.wiki.publish import cleanup_page_vectors

    deleted = cleanup_page_vectors(page_id, keep_revision_id)
    return {"page_id": page_id, "deleted": deleted}


@app.task(bind=True, name="cleanup_expired_trash")
def cleanup_expired_trash(self, retention_days: int = 30):
    """Permanently delete trash pages past the retention window (ROADMAP P3-T2).

    Idempotent; also invoked once on API startup.
    """
    from src.wiki.trash import purge_expired_trash

    return {"purged": purge_expired_trash(retention_days)}


@app.task(name="reconcile_consistency")
def reconcile_consistency() -> dict:
    """Periodic reconciliation between MySQL parent blocks and Milvus vectors.

    First makes ``metadata`` authoritative for tenant/knowledge-base scope,
    then checks for these types of drift:
    1. Missing: MySQL has an active parent but Milvus has no vectors for it.
    2. Stale: Milvus has vectors with doc_version < MySQL's current version.
    3. Orphan: Milvus has vectors for a parent that is deleted in MySQL.
    4. Stale scope: an older tenant/kb still has active rows after metadata moved.

    Repairs are batched and best-effort; failures are logged, not raised,
    so one bad document doesn't prevent the rest from being checked.
    """
    _log.info("Starting MySQL ↔ Milvus reconciliation")
    stats = {
        "checked": 0, "missing": 0, "stale": 0, "orphan": 0, "stale_scope": 0,
    }
    # Recover any ingestion jobs stuck mid-publish before scanning for drift.
    try:
        job_stats = _recover_stale_jobs()
        stats["jobs_recovered"] = sum(job_stats.values())
    except Exception:
        _log.warning("Job recovery during reconciliation failed", exc_info=True)
    try:
        # Get all active parents grouped by source_id
        all_parents = mysql_client.get_all_active_parents()
        by_source: dict[str, list[dict]] = {}
        for p in all_parents:
            sid = p.get("source_id") or ""
            by_source.setdefault(sid, []).append(p)
        stats["checked"] = len(all_parents)

        collection_name = app_config.milvus.text_collection

        for source_id, parents in by_source.items():
            if not source_id:
                continue
            authority = db.get_file(source_id)
            if authority:
                authority_tenant = str(authority.get("tenant_id") or "default")
                authority_kb = str(authority.get("kb_id") or "default")
                stats["stale_scope"] += mysql_client.delete_parents_outside_scope(
                    source_id,
                    tenant_id=authority_tenant,
                    kb_id=authority_kb,
                )
                parents = [
                    p for p in parents
                    if str(p["tenant_id"]) == authority_tenant
                    and str(p["kb_id"]) == authority_kb
                ]
                if not parents:
                    continue
            allowed: set[tuple[str, str, str]] = {
                (str(p["tenant_id"]), str(p["kb_id"]), str(p["parent_id"]))
                for p in parents
            }

            # Reconciliation must also inspect scopes left behind by older
            # metadata moves. Each query remains scoped to one tenant/kb pair.
            known_scopes = [
                {"tenant_id": str(p["tenant_id"]), "kb_id": str(p["kb_id"])}
                for p in parents
            ]
            known_pairs = {
                (str(p["tenant_id"]), str(p["kb_id"])) for p in parents
            }
            for recorded in mysql_client.get_parent_scopes_by_source(source_id):
                tenant_id = str(recorded.get("tenant_id") or "")
                kb_id = str(recorded.get("kb_id") or "")
                if tenant_id and kb_id and (tenant_id, kb_id) not in known_pairs:
                    known_scopes.append({"tenant_id": tenant_id, "kb_id": kb_id})
                    known_pairs.add((tenant_id, kb_id))

            # Check each authoritative parent in its own scope.
            for p in parents:
                pid = p["parent_id"]
                ver = p["doc_version"]
                expr = mx.and_expr(
                    mx.eq("parent_id", pid),
                    mx.eq("tenant_id", p["tenant_id"]),
                    mx.eq("kb_id", p["kb_id"]),
                )
                try:
                    found = mc.query_by_expr(expr, collection_name)
                except Exception:
                    _log.warning("Reconcile query failed for parent %s", pid, exc_info=True)
                    continue

                if not found:
                    stats["missing"] += 1
                    _log.info("Reconcile: missing vectors for parent %s (v%s)", pid, ver)
                    continue

                stale_rows = [r for r in found if r.get("doc_version", 0) < ver]
                if stale_rows:
                    stats["stale"] += len(stale_rows)
                    try:
                        stale_ids = [r["id"] for r in stale_rows]
                        mc.delete_by_ids(
                            stale_ids, collection_name,
                        )
                        _log.info("Reconcile: cleaned %d stale vectors for %s", len(stale_ids), pid)
                    except Exception:
                        _log.warning("Reconcile: stale cleanup failed for %s", pid, exc_info=True)

            # Remove rows whose exact tenant/kb/parent triple is no longer active.
            for scope in known_scopes:
                src_expr = mx.and_expr(
                    mx.eq("source", source_id),
                    mx.eq("tenant_id", scope["tenant_id"]),
                    mx.eq("kb_id", scope["kb_id"]),
                )
                try:
                    rows = mc.query_by_expr(src_expr, collection_name)
                    invalid = [
                        row for row in rows
                        if (scope["tenant_id"], scope["kb_id"], str(row.get("parent_id") or ""))
                        not in allowed
                    ]
                    if not invalid:
                        continue
                    stats["orphan"] += len(invalid)
                    mc.delete_by_ids(
                        [row["id"] for row in invalid], collection_name,
                    )
                    _log.info(
                        "Reconcile: cleaned %d out-of-scope/orphan vectors for source %s",
                        len(invalid), source_id,
                    )
                except Exception:
                    _log.warning(
                        "Reconcile orphan check failed for source %s in (%s, %s)",
                        source_id, scope["tenant_id"], scope["kb_id"],
                        exc_info=True,
                    )

    except Exception:
        _log.exception("Reconciliation task failed")
        raise

    _log.info("Reconciliation complete: %+s", stats)
    return stats


def get_task_status(task_id: str) -> dict:
    """Get the current status of a Celery task."""
    result = AsyncResult(task_id, app=app)
    try:
        state = result.state
    except ValueError:
        # Clean up the corrupted result from Redis so it does not
        # crash future workers that try to decode it.
        try:
            r = redis_lib.from_url(app_config.redis_url, decode_responses=False)
            r.delete(f"celery-task-meta-{task_id}")
            r.close()
        except Exception:
            pass
        return {"task_id": task_id, "state": "FAILURE", "step": "failed", "pct": 0,
                "error": "Task result could not be decoded (may contain a non-JSON-serializable exception)"}

    response: dict = {"task_id": task_id, "state": state, "step": "", "pct": 0}

    if state == "PROGRESS":
        info = result.info or {}
        response["step"] = info.get("step", "")
        response["pct"] = info.get("pct", 0)
        response["result"] = info
    elif state == "SUCCESS":
        response["step"] = "done"
        response["pct"] = 100
        res = result.result if not isinstance(result.result, Exception) else None
        response["result"] = res
        # Defense in depth: some legacy task paths returned {"error": ...} from
        # a "successful" task (normal return → Celery records SUCCESS, overwriting
        # the earlier FAILURE state). Treat that as a failure so the frontend
        # surfaces the real error instead of "完成".
        if isinstance(res, dict) and res.get("error"):
            response["state"] = "FAILURE"
            response["step"] = "failed"
            response["error"] = str(res["error"])
    elif state == "FAILURE":
        response["step"] = "failed"
        response["error"] = str(result.info) if result.info else "Unknown error"

    return response
