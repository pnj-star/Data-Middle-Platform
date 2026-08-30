"""Wiki Milvus access-layer unit tests — pure logic, no Milvus connection.

The live Milvus path (insert / pre-filter search / delete) is verified
manually during P1-T1; keeping the pytest suite free of a Milvus dependency
matches how the legacy pipeline tests stub it. These tests cover the
deterministic, connection-free parts: row ids, expr construction, and
schema/output-field consistency.
"""
from __future__ import annotations

from src import milvus_expr as mx
from src.wiki import milvus as wm


def test_make_row_id():
    assert wm.make_row_id("page1", 3, 7) == "page1:3:7"


def test_in_int_expr():
    """INT-field pre-filter (revision_id) uses ints, not quoted strings."""
    assert mx.in_int_expr("revision_id", [1, 2]) == "revision_id in [1, 2]"
    assert "revision_id in [1, 2]" != mx.in_expr("revision_id", ["1", "2"])


def test_wiki_fields_match_schema():
    """Every read output field exists in the schema; embedding is the anns_field."""
    schema_fields = {f.name for f in wm._schema(512).fields}
    assert set(wm._WIKI_FIELDS) <= schema_fields
    assert "embedding" not in wm._WIKI_FIELDS


def test_id_prefix_sep_in_pk():
    """The row-id separator must be safe inside Milvus PK strings."""
    assert ":" not in wm.wiki_collection_name()
