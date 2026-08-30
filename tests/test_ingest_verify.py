"""Tests for the staged-publish vector verification in _execute_ingest.

Covers the regression where an ALL-deduped document (every parent matches a
``done`` file already in Milvus) sailed through as "success" with 0 vectors
written, leaving MySQL advanced while Milvus stayed empty.
"""
from __future__ import annotations

import json
import logging

import pytest

import tasks.celery_worker as cw


def _chunk_row(index: int, parent_id: str = "p") -> dict:
    return {
        "id": f"chunk-{index}",
        "file_id": "f",
        "parent_id": parent_id,
        "child_content": f"child {index}",
        "chunk_index": index,
        "child_size": 7,
        "parent_size": 10,
        "parent_hash": "0" * 64,
        "child_hash": "1" * 64,
    }


def _staged_parent(parent_id: str = "p") -> dict:
    return {
        "parent_id": parent_id,
        "title": "T",
        "content": "parent content",
        "source_type": "document",
        "source_id": "f",
        "category": None,
        "tags": None,
        "summary": "parent content",
    }


@pytest.fixture
def patched(monkeypatch):
    """Controllable fakes for every external dependency of _execute_ingest."""
    calls = {
        "chunks": [],
        "dedup_result": ([], []),
        "insert_count": 0,
        "mysql_blocks": [],
        "filtered_ids": None,
    }

    def _fake_insert(*args, **kwargs):
        calls["filtered_ids"] = kwargs.get("chunk_ids")
        return calls["insert_count"]

    monkeypatch.setattr(cw.db, "get_chunks", lambda file_id: calls["chunks"])
    monkeypatch.setattr(cw.db, "create_job", lambda file_id, **kw: "job-1")
    monkeypatch.setattr(cw.db, "update_job", lambda *a, **kw: None)
    monkeypatch.setattr(cw.db, "get_staging",
                        lambda file_id: json.dumps([_staged_parent()]))
    monkeypatch.setattr(cw.dedup, "filter_chunks",
                        lambda chunks, file_id: calls["dedup_result"])
    monkeypatch.setattr(cw, "insert_chunks", _fake_insert)
    monkeypatch.setattr(cw.mysql_client, "get_parent_versions_and_hashes",
                        lambda *a, **kw: {})
    monkeypatch.setattr(cw.mysql_client, "insert_parent_blocks",
                        lambda blocks, **kw: calls["mysql_blocks"].append(blocks))
    monkeypatch.setattr(cw, "_forward_fix_cleanup", lambda *a, **kw: None)
    monkeypatch.setattr(cw.db, "mark_chunks_dedup", lambda *a, **kw: None)
    monkeypatch.setattr(cw.db, "update_file_status", lambda *a, **kw: None)
    return calls


def _run_ingest(calls):
    return cw._execute_ingest("f", {
        "id": "f", "type": "text", "name": "a.md",
        "tenant_id": "default", "kb_id": "default",
        "product_id": "", "mushroom_type": "",
    })


def test_partial_insert_raises_and_keeps_mysql(patched):
    """Milvus accepting fewer rows than submitted must fail the publish."""
    patched["chunks"] = [_chunk_row(0), _chunk_row(1)]
    patched["dedup_result"] = ([_chunk_row(0), _chunk_row(1)], [])
    patched["insert_count"] = 1  # Milvus silently dropped one row

    with pytest.raises(cw.TaskError, match="expected 2 but Milvus accepted 1"):
        _run_ingest(patched)

    # MySQL must NOT advance when Milvus verification fails.
    assert patched["mysql_blocks"] == []


def test_all_deduped_reports_zero_instead_of_silent_success(patched, caplog):
    """Fully dedup-skipped document: completes (design), but warns clearly."""
    rows = [_chunk_row(0), _chunk_row(1)]
    patched["chunks"] = rows
    patched["dedup_result"] = ([], [r["id"] for r in rows])
    patched["insert_count"] = 0

    with caplog.at_level(logging.WARNING):
        count, dedup_skipped = _run_ingest(patched)

    assert count == 0
    assert dedup_skipped == 2
    assert patched["filtered_ids"] == []  # nothing was sent to Milvus
    assert len(patched["mysql_blocks"]) == 1
    assert "dedup-skipped" in caplog.text


def test_forward_fix_cleanup_deletes_stale_ids_by_pk_only(monkeypatch):
    """Stale cleanup deletes by primary key; Milvus ignores scope predicates."""
    from src import milvus_client as mc

    delete_calls = []

    def fake_query_by_expr(expr, collection_name):
        return [{"id": "same-id"}]

    def fake_delete_by_ids(ids, collection_name, **kwargs):
        delete_calls.append((list(ids), collection_name, kwargs))
        return len(ids)

    monkeypatch.setattr(mc, "query_by_expr", fake_query_by_expr)
    monkeypatch.setattr(mc, "delete_by_ids", fake_delete_by_ids)
    monkeypatch.setattr(cw.db, "count_files_by_name", lambda *a, **k: 2)
    monkeypatch.setattr(
        cw.mysql_client, "get_parent_scopes_by_source", lambda *a, **k: [])
    monkeypatch.setattr(
        cw.mysql_client, "delete_parents_by_source_scopes", lambda *a, **k: None)
    monkeypatch.setattr(cw.db, "clear_staging", lambda *a, **k: None)
    monkeypatch.setattr(cw.db, "clear_previous_scope", lambda *a, **k: None)

    cw._forward_fix_cleanup(
        {
            "id": "f",
            "name": "a.md",
            "tenant_id": "current",
            "kb_id": "current",
            "previous_tenant_id": "old",
            "previous_kb_id": "old",
        },
        exclude_parent_ids=set(),
        keep_chunk_ids={"same-id"},
    )

    assert len(delete_calls) == 1
    ids, _, kwargs = delete_calls[0]
    assert ids == ["same-id"]
    assert kwargs == {}


def test_delete_by_ids_pk_only(monkeypatch):
    """Milvus delete acts on the primary key, so no scope predicates are added."""
    from src import milvus_client as mc

    calls = []

    def fake_delete_by_expr(expr, collection_name):
        calls.append((expr, collection_name))
        return 1

    monkeypatch.setattr(mc, "delete_by_expr", fake_delete_by_expr)
    count = mc.delete_by_ids(["same-id"], "mushroom_knowledge")

    assert count == 1
    expr, _ = calls[0]
    assert 'id in ["same-id"]' in expr
    assert 'tenant_id == "old"' not in expr
    assert 'kb_id == "old"' not in expr


def test_insert_text_entities_uses_upsert(monkeypatch):
    """Re-ingest must upsert chunk ids so old vectors are replaced in place."""
    from src import milvus_client as mc

    class _Result:
        upsert_count = 2

    class _FakeCollection:
        def __init__(self):
            self.upserted = None
            self.flushed = False

        def upsert(self, entities):
            self.upserted = entities
            return _Result()

        def flush(self):
            self.flushed = True

    fake = _FakeCollection()
    monkeypatch.setattr(mc, "get_text_collection", lambda: fake)
    count = mc.insert_text_entities([{"id": "a"}, {"id": "b"}])

    assert count == 2
    assert fake.upserted == [{"id": "a"}, {"id": "b"}]
    assert fake.flushed is True
