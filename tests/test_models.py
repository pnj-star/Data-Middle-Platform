"""Pydantic model validation tests (no mocks needed)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src import models


class TestIngestFullRequest:
    def test_defaults(self):
        req = models.IngestFullRequest()
        assert req.chunk_size == 300
        assert req.overlap == 40
        assert req.max_parent_size == 2000
        assert req.parent_lookback_tokens == 80
        assert req.child_lookback_tokens is None

    @pytest.mark.parametrize("field,value", [
        ("chunk_size", 99),
        ("chunk_size", 2001),
        ("overlap", -1),
        ("overlap", 201),
        ("max_parent_size", 499),
        ("max_parent_size", 8001),
        ("parent_lookback_tokens", -1),
        ("parent_lookback_tokens", 501),
        ("child_lookback_tokens", -1),
        ("child_lookback_tokens", 501),
    ])
    def test_out_of_range_rejected(self, field, value):
        with pytest.raises(ValidationError):
            models.IngestFullRequest(**{field: value})

    def test_boundary_values_accepted(self):
        req = models.IngestFullRequest(chunk_size=300, overlap=40, parent_lookback_tokens=0)
        assert req.chunk_size == 300


class TestChunkUpdateRequest:
    def test_valid_actions(self):
        assert models.ChunkUpdateRequest(action="delete", chunk_ids=["a"]).action == "delete"
        assert models.ChunkUpdateRequest(action="merge", chunk_ids=["a", "b"]).action == "merge"

    def test_invalid_action(self):
        with pytest.raises(ValidationError):
            models.ChunkUpdateRequest(action="rename", chunk_ids=["a"])


class TestSaveResponse:
    def test_new_fields(self):
        r = models.SaveResponse(saved=True, count=2, merged_id="m", task_id="t", reingest=True)
        assert r.count == 2
        assert r.merged_id == "m"
        assert r.task_id == "t"
        assert r.reingest is True

    def test_defaults(self):
        r = models.SaveResponse()
        assert r.count == 0
        assert r.merged_id is None
        assert r.task_id is None
        assert r.reingest is False


class TestParentChunkResponse:
    def test_parent_id_optional(self):
        p = models.ParentChunkResponse(title="t", content="c", size=1, children=[])
        assert p.parent_id == ""
