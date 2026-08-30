"""Unit tests for the db module."""
from __future__ import annotations


import pytest

from src import db
from src.config import config


@pytest.fixture(autouse=True)
def setup_db():
    """Initialize the database and clean up after each test."""
    db.init_db()
    yield


class TestFileCRUD:
    def test_insert_and_get_file(self):
        info = db.insert_file("test.pdf", "/tmp/test.pdf", 1024, "text", ".pdf")
        assert info["name"] == "test.pdf"
        assert info["size"] == 1024
        assert info["status"] == "uploaded"

        fetched = db.get_file(info["id"])
        assert fetched is not None
        assert fetched["name"] == "test.pdf"

    def test_list_files_pagination(self):
        # Insert 3 files
        db.insert_file("a.pdf", "/tmp/a.pdf", 100, "text", ".pdf")
        db.insert_file("b.pdf", "/tmp/b.pdf", 200, "text", ".pdf")
        db.insert_file("c.pdf", "/tmp/c.pdf", 300, "text", ".pdf")

        files, total = db.list_files(page=1, limit=2)
        assert len(files) == 2
        assert total == 3

    def test_list_files_filter_by_status(self):
        db.insert_file("a.pdf", "/tmp/a.pdf", 100, "text", ".pdf")
        files, total = db.list_files(status="uploaded")
        assert total >= 1
        for f in files:
            assert f["status"] == "uploaded"

    def test_list_files_filter_by_type(self):
        db.insert_file("img.jpg", "/tmp/img.jpg", 100, "image", ".jpg")
        db.insert_file("doc.pdf", "/tmp/doc.pdf", 100, "text", ".pdf")
        files, total = db.list_files(file_type="image")
        for f in files:
            assert f["type"] == "image"

    def test_update_file_status(self):
        info = db.insert_file("test.pdf", "/tmp/test.pdf", 100, "text", ".pdf")
        db.update_file_status(info["id"], "converting")
        fetched = db.get_file(info["id"])
        assert fetched["status"] == "converting"

    def test_update_file_status_with_error(self):
        info = db.insert_file("test.pdf", "/tmp/test.pdf", 100, "text", ".pdf")
        db.update_file_status(info["id"], "failed", "Something went wrong")
        fetched = db.get_file(info["id"])
        assert fetched["status"] == "failed"
        assert fetched["error"] == "Something went wrong"

    def test_error_cleared_on_success(self):
        info = db.insert_file("test.pdf", "/tmp/test.pdf", 100, "text", ".pdf")
        db.update_file_status(info["id"], "failed", "boom")
        db.update_file_status(info["id"], "converted")
        fetched = db.get_file(info["id"])
        assert fetched["status"] == "converted"
        assert fetched["error"] is None

    def test_update_file_output_path(self):
        info = db.insert_file("img.jpg", "/tmp/img.jpg", 100, "image", ".jpg")
        db.update_file_output_path(info["id"], "/tmp/outputs/images/x.jpg")
        fetched = db.get_file(info["id"])
        assert fetched["output_path"] == "/tmp/outputs/images/x.jpg"

    def test_update_file_output_path(self):
        info = db.insert_file("test.pdf", "/tmp/test.pdf", 100, "text", ".pdf")
        db.update_file_output_path(info["id"], "/tmp/outputs/images/x.jpg")
        fetched = db.get_file(info["id"])
        assert fetched["output_path"] == "/tmp/outputs/images/x.jpg"

    def test_delete_file(self):
        info = db.insert_file("test.pdf", "/tmp/test.pdf", 100, "text", ".pdf")
        db.delete_file(info["id"])
        assert db.get_file(info["id"]) is None

    def test_get_nonexistent_file(self):
        assert db.get_file("nonexistent") is None


class TestConversionCRUD:
    def test_insert_and_get_conversion(self):
        info = db.insert_file("test.pdf", "/tmp/test.pdf", 100, "text", ".pdf")
        db.insert_conversion(info["id"], "# Hello\nWorld", {"title": "Hello"}, "done")
        conv = db.get_conversion(info["id"])
        assert conv is not None
        assert "# Hello" in conv["markdown"]
        assert conv["metadata"]["title"] == "Hello"

    def test_update_conversion_markdown(self):
        info = db.insert_file("test.pdf", "/tmp/test.pdf", 100, "text", ".pdf")
        db.insert_conversion(info["id"], "Original", {})
        db.update_conversion_markdown(info["id"], "Updated")
        conv = db.get_conversion(info["id"])
        assert conv["markdown"] == "Updated"

    def test_update_conversion_raw_target(self):
        info = db.insert_file("test.pdf", "/tmp/test.pdf", 100, "text", ".pdf")
        db.insert_conversion(info["id"], "Original", {})
        db.update_conversion_markdown(info["id"], "Raw Edited", target="raw")
        conv = db.get_conversion(info["id"])
        assert conv["raw_markdown"] == "Raw Edited"
        assert conv["markdown"] == "Raw Edited"
        assert conv["clean_state"] == "edited"
        assert db.get_clean_flags_batch([info["id"]])[info["id"]] is False
        db.update_conversion_markdown(info["id"], "Clean Edited", target="cleaned")
        conv = db.get_conversion(info["id"])
        assert conv["markdown"] == "Clean Edited"
        assert conv["raw_markdown"] == "Raw Edited"
        assert conv["clean_state"] == "cleaned"
        assert db.get_clean_flags_batch([info["id"]])[info["id"]] is True

    def test_get_nonexistent_conversion(self):
        assert db.get_conversion("nonexistent") is None


class TestChunkCRUD:
    def test_insert_and_get_chunks(self):
        info = db.insert_file("test.pdf", "/tmp/test.pdf", 100, "text", ".pdf")
        db.insert_chunks_batch([{"file_id": info["id"], "parent_id": "p1", "child_content": "Child content", "chunk_index": 0, "child_size": 13}])
        chunks = db.get_chunks(info["id"])
        assert len(chunks) == 1
        assert chunks[0]["child_content"] == "Child content"
        assert chunks[0]["chunk_index"] == 0

    def test_insert_chunks_batch_persists_parent_id(self):
        info = db.insert_file("test.pdf", "/tmp/test.pdf", 100, "text", ".pdf")
        entries = [
            {"file_id": info["id"], "parent_id": "pid-1", "parent_title": "T1", "parent_content": "P1",
             "child_content": "C1", "chunk_index": 0, "child_size": 2, "parent_size": 2},
            {"file_id": info["id"], "parent_id": "pid-1", "parent_title": "T1", "parent_content": "P1",
             "child_content": "C2", "chunk_index": 1, "child_size": 2, "parent_size": 2},
        ]
        count = db.insert_chunks_batch(entries)
        assert count == 2
        chunks = db.get_chunks(info["id"])
        assert len(chunks) == 2
        assert {c["parent_id"] for c in chunks} == {"pid-1"}

    def test_get_chunk_count(self):
        info = db.insert_file("test.pdf", "/tmp/test.pdf", 100, "text", ".pdf")
        db.insert_chunks_batch([{"file_id": info["id"], "parent_id": "p1", "child_content": "C1", "chunk_index": 0, "child_size": 2}])
        db.insert_chunks_batch([{"file_id": info["id"], "parent_id": "p1", "child_content": "C2", "chunk_index": 1, "child_size": 2}])
        assert db.get_chunk_count(info["id"]) == 2

    def test_get_chunk_counts_batch(self):
        f1 = db.insert_file("a.pdf", "/tmp/a.pdf", 100, "text", ".pdf")
        f2 = db.insert_file("b.pdf", "/tmp/b.pdf", 100, "text", ".pdf")
        db.insert_chunks_batch([
            {"file_id": f1["id"], "parent_id": "p1",
             "child_content": "C", "chunk_index": 0, "child_size": 1},
            {"file_id": f2["id"], "parent_id": "p1",
             "child_content": "C1", "chunk_index": 0, "child_size": 1},
            {"file_id": f2["id"], "parent_id": "p1",
             "child_content": "C2", "chunk_index": 1, "child_size": 1},
        ])
        counts = db.get_chunk_counts_batch([f1["id"], f2["id"]])
        assert counts[f1["id"]] == 1
        assert counts[f2["id"]] == 2

    def test_delete_chunks(self):
        info = db.insert_file("test.pdf", "/tmp/test.pdf", 100, "text", ".pdf")
        db.insert_chunks_batch([{"file_id": info["id"], "parent_id": "p1", "child_content": "C1", "chunk_index": 0, "child_size": 2}])
        db.insert_chunks_batch([{"file_id": info["id"], "parent_id": "p1", "child_content": "C2", "chunk_index": 1, "child_size": 2}])
        chunks = db.get_chunks(info["id"])
        assert len(chunks) == 2
        db.delete_chunks(info["id"], [chunks[1]["id"]])
        chunks = db.get_chunks(info["id"])
        assert len(chunks) == 1

    def test_clear_chunks(self):
        info = db.insert_file("test.pdf", "/tmp/test.pdf", 100, "text", ".pdf")
        db.insert_chunks_batch([{"file_id": info["id"], "parent_id": "p1", "child_content": "C1", "chunk_index": 0, "child_size": 2}])
        db.insert_chunks_batch([{"file_id": info["id"], "parent_id": "p1", "child_content": "C2", "chunk_index": 1, "child_size": 2}])
        db.clear_chunks(info["id"])
        assert db.get_chunk_count(info["id"]) == 0

    def test_update_chunk_content(self):
        info = db.insert_file("test.pdf", "/tmp/test.pdf", 100, "text", ".pdf")
        db.insert_chunks_batch([{"file_id": info["id"], "parent_id": "p1", "child_content": "old", "chunk_index": 0, "child_size": 3}])
        cid = db.get_chunks(info["id"])[0]["id"]
        db.update_chunk_content(info["id"], cid, "new content here")
        chunks = db.get_chunks(info["id"])
        assert chunks[0]["child_content"] == "new content here"
        assert chunks[0]["child_size"] == len("new content here")
