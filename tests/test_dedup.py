"""Tests for the content-fingerprint dedup module (src.dedup)."""
from __future__ import annotations

import pytest

from src import db, dedup


@pytest.fixture(autouse=True)
def setup_db():
    """Initialize the database and clean up after each test (same as test_db)."""
    db.init_db()
    yield


def _add_file(name: str, status: str = "uploaded") -> dict:
    return db.insert_file(name, f"/x/{name}", 100, "text", ".md")


def _chunk(file_id: str, parent: str, child: str, index: int = 0, parent_id: str = "p") -> dict:
    """One chunk row with hashes, mirroring what the celery chunk step writes."""
    return {
        "file_id": file_id,
        "parent_id": parent_id,
        "parent_title": "T",
        "parent_content": parent,
        "child_content": child,
        "chunk_index": index,
        "child_size": len(child),
        "parent_size": len(parent),
        "parent_hash": dedup.parent_content_hash(parent),
        "child_hash": dedup.child_content_hash(child),
    }


class TestNormalize:
    def test_drops_whitespace_and_folds_case(self):
        assert dedup.normalize_text("Anti-counterfeiting Description") == \
            dedup.normalize_text("Anti-counterfeitingDescription")
        assert dedup.normalize_text("  A B\nC\tD  ") == "abcd"

    def test_nfkc_fullwidth_to_halfwidth(self):
        assert dedup.normalize_text("声明：ＡＢＣ０１２（１）") == "声明:abc012(1)"
        assert dedup.normalize_text("　全角空格") == "全角空格"

    def test_hash_deterministic_and_distinct(self):
        a = dedup.parent_content_hash("Hello World")
        assert a == dedup.parent_content_hash("Hello World")
        assert a != dedup.parent_content_hash("Hello Worlds")


class TestFilterChunks:
    def test_skips_whole_parent_already_in_milvus(self):
        f1 = _add_file("a.md", status="done")
        f2 = _add_file("b.md")
        db.insert_chunks_batch([_chunk(f1["id"], "Shared Section", "c1", index=0)])
        db.insert_chunks_batch([_chunk(f1["id"], "Shared Section", "c2", index=1)])
        db.update_file_status(f1["id"], "done")

        # b has the same parent section; every row must be skipped.
        db.insert_chunks_batch([
            _chunk(f2["id"], "Shared Section", "d1", index=0),
            _chunk(f2["id"], "Shared Section", "d2", index=1),
        ])
        rows = db.get_chunks(f2["id"])
        kept, skipped = dedup.filter_chunks(rows, f2["id"])
        assert kept == []
        assert sorted(skipped) == sorted(r["id"] for r in rows)

    def test_normalization_variants_collide(self):
        """Whitespace/full-width variants of the same section dedup against each other."""
        f1 = _add_file("a.md", status="done")
        f2 = _add_file("b.md")
        db.insert_chunks_batch([_chunk(f1["id"], "Anti-counterfeiting Description", "c1")])
        db.update_file_status(f1["id"], "done")
        db.insert_chunks_batch([_chunk(f2["id"], "Anti-counterfeitingDescription", "d1")])
        rows = db.get_chunks(f2["id"])
        kept, skipped = dedup.filter_chunks(rows, f2["id"])
        assert kept == []
        assert len(skipped) == 1

    def test_intra_file_duplicate_parent_skipped(self):
        f1 = _add_file("a.md")
        db.insert_chunks_batch([
            _chunk(f1["id"], "Same Section", "c1", index=0, parent_id="p1"),
            _chunk(f1["id"], "Same Section", "c2", index=1, parent_id="p1"),
            _chunk(f1["id"], "Same Section", "c3", index=0, parent_id="p2"),  # duplicate parent
        ])
        rows = db.get_chunks(f1["id"])
        kept, skipped = dedup.filter_chunks(rows, f1["id"])
        assert len(kept) == 2          # first parent's two children
        assert len(skipped) == 1       # duplicate parent's single child
        assert len({row["parent_id"] for row in kept}) == 1

    def test_child_dedup_within_same_parent_only(self):
        f1 = _add_file("a.md")
        db.insert_chunks_batch([
            _chunk(f1["id"], "Parent One", "repeated paragraph", index=0, parent_id="p1"),
            _chunk(f1["id"], "Parent One", "repeated paragraph", index=1, parent_id="p1"),
        ])
        rows = db.get_chunks(f1["id"])
        kept, skipped = dedup.filter_chunks(rows, f1["id"])
        assert len(kept) == 1
        assert len(skipped) == 1

    def test_same_child_under_different_parents_both_kept(self):
        """Child dedup is intra-parent only — never drops a parent's context."""
        f1 = _add_file("a.md")
        db.insert_chunks_batch([
            _chunk(f1["id"], "Parent One", "shared paragraph", index=0, parent_id="p1"),
            _chunk(f1["id"], "Parent Two", "shared paragraph", index=0, parent_id="p2"),
        ])
        rows = db.get_chunks(f1["id"])
        kept, skipped = dedup.filter_chunks(rows, f1["id"])
        assert len(kept) == 2
        assert skipped == []

    def test_reingest_does_not_dedup_against_own_stale_rows(self):
        """The file being re-ingested is excluded from the seen-parent base."""
        f1 = _add_file("a.md", status="done")
        db.insert_chunks_batch([_chunk(f1["id"], "Own Section", "c1")])
        db.update_file_status(f1["id"], "done")
        # Re-ingest: same rows must be kept (its own previous copy is being replaced).
        rows = db.get_chunks(f1["id"])
        kept, skipped = dedup.filter_chunks(rows, f1["id"])
        assert len(kept) == 1
        assert skipped == []

    def test_dedup_does_not_cross_tenant_or_kb(self):
        owner = db.insert_file(
            "owner.md", "/x/owner.md", 10, "text", ".md",
            tenant_id="tenant-a", kb_id="kb-1",
        )
        other_tenant = db.insert_file(
            "other.md", "/x/other.md", 10, "text", ".md",
            tenant_id="tenant-b", kb_id="kb-1",
        )
        other_kb = db.insert_file(
            "other-kb.md", "/x/other-kb.md", 10, "text", ".md",
            tenant_id="tenant-a", kb_id="kb-2",
        )
        db.insert_chunks_batch([_chunk(owner["id"], "Shared Section", "c1")])
        db.update_file_status(owner["id"], "done")

        for scoped_file in (other_tenant, other_kb):
            db.insert_chunks_batch([
                _chunk(scoped_file["id"], "Shared Section", "scoped-child"),
            ])
            rows = db.get_chunks(scoped_file["id"])
            kept, skipped = dedup.filter_chunks(rows, scoped_file["id"])
            assert len(kept) == 1
            assert skipped == []

    def test_dedup_scopes_by_product_within_same_kb(self):
        f1 = db.insert_file("a.md", "/x/a.md", 10, "text", ".md",
                            product_id="p1", tenant_id="t1", kb_id="kb1")
        f2 = db.insert_file("b.md", "/x/b.md", 10, "text", ".md",
                            product_id="p2", tenant_id="t1", kb_id="kb1")
        db.insert_chunks_batch([_chunk(f1["id"], "Shared Section", "c1")])
        db.update_file_status(f1["id"], "done")
        db.insert_chunks_batch([_chunk(f2["id"], "Shared Section", "d1")])
        rows = db.get_chunks(f2["id"])
        kept, skipped = dedup.filter_chunks(rows, f2["id"])
        assert len(kept) == 1
        assert skipped == []


class TestInvalidateDependents:
    def test_marks_dependent_files_chunked(self):
        f1 = _add_file("a.md", status="done")
        f2 = _add_file("b.md", status="done")
        ph = dedup.parent_content_hash("Shared Sec")
        db.insert_chunks_batch([_chunk(f1["id"], "Shared Sec", "c1")])
        db.insert_chunks_batch([_chunk(f2["id"], "Shared Sec", "d1")])
        db.update_file_status(f1["id"], "done")
        db.update_file_status(f2["id"], "done")
        # f2's chunk was deduped against f1's copy.
        db.mark_chunks_dedup([db.get_chunks(f2["id"])[0]["id"]], True)

        assert db.get_file(f2["id"])["status"] == "done"
        affected = dedup.invalidate_dependents(f1["id"])
        assert affected == [f2["id"]]
        assert db.get_file(f2["id"])["status"] == "chunked"
        # f2 is not affected by f1 when f2 has no deduped chunks against f1.
        assert ph
        db.update_file_status(f2["id"], "done")
        assert dedup.invalidate_dependents(f2["id"]) == []

    def test_no_dependents_when_none(self):
        f1 = _add_file("a.md", status="done")
        assert dedup.invalidate_dependents(f1["id"]) == []
