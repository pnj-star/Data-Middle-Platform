"""API-level tests for main.py using FastAPI TestClient with mocked deps."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import types

# Provide a fake src.converter module with only the constant main.py needs at
# import time, so the tests stay independent of the real engine chain.
_fake_converter = types.ModuleType("src.converter")
_fake_converter.SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".html", ".md", ".txt"}
sys.modules["src.converter"] = _fake_converter

# The real tasks.celery_worker imports sentence-transformers / cn-clip.
# Provide a fake so trigger endpoints can resolve *.delay without pulling those
# in. Every task fakes a .delay() returning a SimpleNamespace(id=...).
def _fake_task():
    return types.SimpleNamespace(delay=lambda *a, **k: types.SimpleNamespace(id="fake-task-id"))

_fake_tasks = types.ModuleType("tasks")
_fake_tasks.__path__ = []
_fake_cw = types.ModuleType("tasks.celery_worker")
_fake_cw.convert_document = _fake_task()
_fake_cw.chunk_document = _fake_task()
_fake_cw.ingest_document = _fake_task()
_fake_cw.ingest_image = _fake_task()
_fake_cw.ingest_full_pipeline = _fake_task()
_fake_cw.reembed_merged_chunk = _fake_task()
sys.modules["tasks"] = _fake_tasks
sys.modules["tasks.celery_worker"] = _fake_cw

import pytest
from fastapi.testclient import TestClient

from src import db
from src.chunker import count_tokens
from src.config import config
import main


@pytest.fixture
def client(monkeypatch, tmp_path):
    """TestClient with dirs pointed at a temp location (no real-dir pollution)."""
    monkeypatch.setattr(config.app, "upload_dir", str(tmp_path / "uploads"))
    monkeypatch.setattr(config.app, "output_dir", str(tmp_path / "outputs"))
    with TestClient(main.app) as c:
        yield c


def _seed_done_file(name: str = "doc.md") -> dict:
    """A text file in 'done' state with 3 chunks across 2 parents."""
    info = db.insert_file(name, f"/x/{name}", 100, "text", ".md")
    db.insert_conversion(info["id"], "# T\ncontent", {}, "done")
    db.insert_chunks_batch([
        {"file_id": info["id"], "parent_id": "p1", "child_content": "child one",
         "chunk_index": 0, "child_size": 9, "parent_size": 2},
        {"file_id": info["id"], "parent_id": "p1", "child_content": "child two",
         "chunk_index": 1, "child_size": 9, "parent_size": 2},
        {"file_id": info["id"], "parent_id": "p2", "child_content": "child three",
         "chunk_index": 0, "child_size": 11, "parent_size": 3},
    ])
    db.update_file_status(info["id"], "done")
    return info


class TestAuth:
    def test_api_key_guard_blocks(self, client, monkeypatch):
        monkeypatch.setattr(config.security, "api_key", "secret")
        r = client.delete("/api/files/does-not-exist")
        assert r.status_code == 401

    def test_api_key_guard_allows_valid(self, client, monkeypatch):
        monkeypatch.setattr(config.security, "api_key", "secret")
        r = client.delete(
            "/api/files/does-not-exist",
            headers={"X-API-Key": "secret"},
        )
        assert r.status_code == 404

    def test_api_key_off_by_default(self, client):
        assert config.security.api_key == ""
        r = client.delete("/api/files/does-not-exist")
        assert r.status_code == 404


class TestUpload:
    def test_upload_oversized_returns_413(self, client, monkeypatch):
        monkeypatch.setattr(config.app, "max_file_size_mb", 1)
        r = client.post(
            "/api/files/upload",
            files={"files": ("big.txt", b"x" * (1024 * 1024 + 10), "text/plain")},
        )
        assert r.status_code == 413
        from pathlib import Path
        leftover = list(Path(config.upload_dir_abs).iterdir())
        assert leftover == []

    def test_upload_success(self, client):
        r = client.post(
            "/api/files/upload",
            files={"files": ("hello.txt", b"hello world", "text/plain")},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["files"]) == 1
        assert body["files"][0]["name"] == "hello world".replace("hello world", "hello.txt")

    def _upload(self, client, name: str, content: bytes, **data):
        return client.post(
            "/api/files/upload",
            files={"files": (name, content, "application/octet-stream")},
            data=data,
        )

    def test_upload_same_content_same_scope_conflicts(self, client):
        r1 = self._upload(client, "hello.txt", b"hello world",
                          tenant_id="t1", kb_id="kb1", product_id="p1")
        assert r1.status_code == 200
        r2 = self._upload(client, "hello-copy.txt", b"hello world",
                          tenant_id="t1", kb_id="kb1", product_id="p1")
        assert r2.status_code == 409
        assert r2.json()["existing"]["id"] == r1.json()["files"][0]["id"]
        upload_dir = Path(config.upload_dir_abs)
        assert len(list(upload_dir.iterdir())) == 1
        _, total = db.list_files(tenant_id="t1", kb_id="kb1")
        assert total == 1

    def test_upload_same_content_different_product_allowed(self, client):
        r1 = self._upload(client, "hello.txt", b"same bytes",
                          tenant_id="t1", kb_id="kb1", product_id="p1")
        r2 = self._upload(client, "hello-copy.txt", b"same bytes",
                          tenant_id="t1", kb_id="kb1", product_id="p2")
        assert r1.status_code == 200
        assert r2.status_code == 200
        id1 = r1.json()["files"][0]["id"]
        id2 = r2.json()["files"][0]["id"]
        assert db.get_file(id1)["stored_path"] == db.get_file(id2)["stored_path"]
        upload_dir = Path(config.upload_dir_abs)
        assert len(list(upload_dir.iterdir())) == 1

    def test_upload_force_creates_shared_logical_record(self, client):
        r1 = self._upload(client, "hello.txt", b"same bytes")
        assert r1.status_code == 200
        r2 = self._upload(client, "hello-copy.txt", b"same bytes", force="true")
        assert r2.status_code == 200
        id1 = r1.json()["files"][0]["id"]
        id2 = r2.json()["files"][0]["id"]
        assert id1 != id2
        assert db.get_file(id1)["stored_path"] == db.get_file(id2)["stored_path"]
        upload_dir = Path(config.upload_dir_abs)
        assert len(list(upload_dir.iterdir())) == 1

    def test_upload_stored_name_uses_sha256(self, client):
        content = b"hello world"
        r = self._upload(client, "hello.txt", content)
        assert r.status_code == 200
        info = db.get_file(r.json()["files"][0]["id"])
        expected = hashlib.sha256(content).hexdigest()
        assert Path(info["stored_path"]).name == f"{expected}.txt"


class TestDuplicateSubmitGuard:
    def test_convert_rejects_when_already_converting(self, client):
        info = db.insert_file("a.pdf", "/x/a.pdf", 100, "text", ".pdf")
        db.update_file_status(info["id"], "converting")
        r = client.post(f"/api/convert/{info['id']}")
        assert r.status_code == 409

    def test_chunk_rejects_when_already_chunking(self, client):
        info = db.insert_file("b.md", "/x/b.md", 100, "text", ".md")
        db.update_file_status(info["id"], "chunking")
        r = client.post(f"/api/chunks/{info['id']}", json={})
        assert r.status_code == 409

    def test_ingest_rejects_when_already_ingesting(self, client):
        info = db.insert_file("c.md", "/x/c.md", 100, "text", ".md")
        db.update_file_status(info["id"], "ingesting")
        r = client.post(f"/api/ingest/{info['id']}")
        assert r.status_code == 409

    def test_ingest_full_rejects_when_processing(self, client):
        info = db.insert_file("d.pdf", "/x/d.pdf", 100, "text", ".pdf")
        db.update_file_status(info["id"], "converting")
        r = client.post(f"/api/ingest-full/{info['id']}", json={})
        assert r.status_code == 409

    def test_convert_allows_when_not_converting(self, client):
        info = db.insert_file("e.md", "/x/e.md", 100, "text", ".md")
        db.update_file_status(info["id"], "uploaded")
        r = client.post(f"/api/convert/{info['id']}")
        assert r.status_code == 200
        assert r.json()["task_id"] == "fake-task-id"
        assert db.get_file(info["id"])["status"] == "converting"


class TestConfigEndpoint:
    def test_api_config(self, client):
        r = client.get("/api/config")
        assert r.status_code == 200
        body = r.json()
        assert body["max_file_size_mb"] == config.app.max_file_size_mb
        assert body["api_key_required"] == bool(config.security.api_key)


class TestConvertedEdit:
    def test_update_converted_preserves_stale_data(self, client, mock_milvus):
        info = _seed_done_file()
        r = client.put(f"/api/converted/{info['id']}", json={"markdown": "# New\ncontent"})
        assert r.status_code == 200
        fetched = db.get_file(info["id"])
        assert fetched["status"] == "converted"
        assert fetched["error"] is None
        assert db.get_chunk_count(info["id"]) > 0
        assert not mock_milvus["delete_by_ids"]

    def test_update_converted_raw_target(self, client, mock_milvus):
        info = _seed_done_file()
        r = client.put(f"/api/converted/{info['id']}", json={"markdown": "# Raw Edited", "target": "raw"})
        assert r.status_code == 200
        conv = db.get_conversion(info["id"])
        assert conv["raw_markdown"] == "# Raw Edited"
        assert conv["markdown"] == "# Raw Edited"
        r = client.put(f"/api/converted/{info['id']}", json={"markdown": "# Clean Edited", "target": "cleaned"})
        assert r.status_code == 200
        conv = db.get_conversion(info["id"])
        assert conv["raw_markdown"] == "# Raw Edited"
        assert conv["markdown"] == "# Clean Edited"
        assert not mock_milvus["delete_by_ids"]

    def test_update_missing_conversion_returns_404(self, client):
        info = db.insert_file("missing.md", "/x/missing.md", 10, "text", ".md")
        r = client.put(f"/api/converted/{info['id']}", json={"markdown": "text"})
        assert r.status_code == 404


class TestChunkTree:
    def test_chunk_tree_prefers_staged_parent_content(self, client):
        info = _seed_done_file()
        db.update_file_status(info["id"], "chunked")
        db.save_staging(info["id"], json.dumps([
            {
                "parent_id": "p1",
                "title": "Staged parent",
                "content": "authoritative parent content",
                "source_type": "document",
                "source_id": info["id"],
            }
        ], ensure_ascii=False))

        r = client.get(f"/api/chunks/{info['id']}")
        assert r.status_code == 200
        parent = r.json()["parents"][0]
        assert parent["title"] == "Staged parent"
        assert parent["content"] == "authoritative parent content"
        assert parent["size"] == count_tokens("authoritative parent content")
        assert len(parent["children"]) == 2

    def test_chunk_tree_falls_back_to_children_for_legacy_rows(self, client):
        info = _seed_done_file()
        db.update_file_status(info["id"], "chunked")

        r = client.get(f"/api/chunks/{info['id']}")
        assert r.status_code == 200
        parent = next(p for p in r.json()["parents"] if p["parent_id"] == "p1")
        assert parent["content"] == "child one\n\nchild two"
        assert parent["size"] == count_tokens("child one\n\nchild two")


class TestChunkEdit:
    def test_delete_chunks_defers_milvus_to_ingest(self, client, mock_milvus):
        info = _seed_done_file()
        chunks = db.get_chunks(info["id"])
        c1 = chunks[0]["id"]
        r = client.put(f"/api/chunks/{info['id']}", json={"action": "delete", "chunk_ids": [c1]})
        assert r.status_code == 200
        assert db.get_chunk_count(info["id"]) == 2
        assert db.get_file(info["id"])["status"] == "chunked"
        assert not mock_milvus["delete_by_ids"]

    def test_delete_rejects_foreign_ids(self, client, mock_milvus):
        info = _seed_done_file()
        r = client.put(
            f"/api/chunks/{info['id']}",
            json={"action": "delete", "chunk_ids": ["not-a-real-id"]},
        )
        assert r.status_code == 400

    def test_merge_chunks_defers_milvus_to_ingest(self, client, mock_milvus):
        info = _seed_done_file()
        chunks = db.get_chunks(info["id"])
        c1, c2 = chunks[0], chunks[1]
        r = client.put(
            f"/api/chunks/{info['id']}",
            json={"action": "merge", "chunk_ids": [c1["id"], c2["id"]]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["merged_id"] == c1["id"]
        remaining = db.get_chunks(info["id"])
        survivor = next(c for c in remaining if c["id"] == c1["id"])
        assert survivor["child_content"] == c1["child_content"] + "\n\n" + c2["child_content"]
        assert db.get_chunk_count(info["id"]) == 2
        assert db.get_file(info["id"])["status"] == "chunked"
        assert not mock_milvus["delete_by_ids"]

    def test_merge_requires_two(self, client):
        info = _seed_done_file()
        chunks = db.get_chunks(info["id"])
        r = client.put(
            f"/api/chunks/{info['id']}",
            json={"action": "merge", "chunk_ids": [chunks[0]["id"]]},
        )
        assert r.status_code == 400


class TestFileMetaEdit:
    def test_patch_updates_metadata(self, client):
        info = _seed_done_file()
        r = client.put(
            f"/api/files/{info['id']}/meta",
            json={
                "mushroom_type": "shiitake",
                "product_id": "p-1",
                "tenant_id": "tenant-a",
                "kb_id": "kb-a",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["mushroom_type"] == "shiitake"
        assert body["product_id"] == "p-1"
        assert body["tenant_id"] == "tenant-a"
        assert body["kb_id"] == "kb-a"

    def test_patch_done_file_marks_for_reingest(self, client):
        info = _seed_done_file()
        db.save_staging(info["id"], "[]")
        r = client.put(f"/api/files/{info['id']}/meta", json={"tenant_id": "tenant-b"})
        assert r.status_code == 200
        assert db.get_file(info["id"])["status"] == "chunked"
        assert db.get_staging(info["id"]) is None

    def test_patch_noop_returns_unchanged(self, client):
        info = _seed_done_file()
        before = db.get_file(info["id"])
        r = client.put(f"/api/files/{info['id']}/meta", json={})
        assert r.status_code == 200
        after = db.get_file(info["id"])
        assert after["status"] == before["status"]

    def test_patch_missing_file_404(self, client):
        r = client.put("/api/files/missing/meta", json={"tenant_id": "x"})
        assert r.status_code == 404


class TestDelete:
    def test_delete_unknown_returns_404(self, client):
        r = client.delete("/api/files/missing")
        assert r.status_code == 404

    def test_delete_removes_row_and_enqueues_purge(self, client, monkeypatch):
        info = _seed_done_file()
        calls = []
        monkeypatch.setattr(main, "_dispatch_task", lambda name, *args: calls.append((name, args)))
        r = client.delete(f"/api/files/{info['id']}")
        assert r.status_code == 200
        assert r.json()["deleted"] is True
        assert db.get_file(info["id"]) is None
        assert calls[0][0] == "purge_file_vectors"
        assert calls[0][1][0] == info["id"]
        assert calls[0][1][3] is True

    def test_delete_skips_legacy_name_when_shared(self, client, monkeypatch):
        first = _seed_done_file("shared.md")
        db.insert_file("shared.md", "/x/shared-2.md", 10, "text", ".md")
        calls = []
        monkeypatch.setattr(main, "_dispatch_task", lambda name, *args: calls.append((name, args)))
        r = client.delete(f"/api/files/{first['id']}")
        assert r.status_code == 200
        assert calls[0][1][3] is False

    def test_delete_shared_stored_file_keeps_physical_object(self, client, monkeypatch):
        monkeypatch.setattr(main, "_dispatch_task", lambda name, *args: None)

        def _upload(force: bool = False):
            data = {"force": "true"} if force else None
            return client.post(
                "/api/files/upload",
                files={"files": ("dup.txt", b"shared bytes", "application/octet-stream")},
                data=data,
            )

        r1 = _upload()
        r2 = _upload(force=True)
        id1 = r1.json()["files"][0]["id"]
        id2 = r2.json()["files"][0]["id"]
        stored = db.get_file(id1)["stored_path"]
        assert Path(stored).exists()
        client.delete(f"/api/files/{id1}")
        assert Path(stored).exists()
        client.delete(f"/api/files/{id2}")
        assert not Path(stored).exists()
