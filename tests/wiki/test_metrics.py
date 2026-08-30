"""Prometheus metrics tests (ROADMAP P4-T3)."""
from __future__ import annotations

import sys
import types

_fake_converter = types.ModuleType("src.converter")
_fake_converter.SUPPORTED_EXTENSIONS = {".pdf", ".md"}
sys.modules["src.converter"] = _fake_converter

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from src.wiki import database as wdb  # noqa: E402
from src.wiki.seed import ensure_seed_data  # noqa: E402

_TRUNCATE = (
    "TRUNCATE TABLE audit_logs, space_members, links, comments, attachments, "
    "revisions, pages, spaces, users, roles RESTART IDENTITY CASCADE"
)


@pytest.fixture(autouse=True)
def _clean(wiki_engine):
    from sqlalchemy import text

    with wdb.get_engine().begin() as conn:
        conn.execute(text(_TRUNCATE))
    with wdb.session_scope() as s:
        ensure_seed_data(s)
    yield


def test_metrics_endpoint(wiki_engine):
    with TestClient(main.app) as client:
        client.get("/api/wiki/spaces")  # generate a recorded request
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]
        body = r.text
        assert "wiki_http_requests_total" in body
        assert "wiki_http_request_duration_seconds" in body
        assert "wiki_celery_queue_length" in body
        # the earlier /api/wiki/spaces call was recorded
        assert "/api/wiki/spaces" in body


def test_metric_path_normalizes_ids(wiki_engine):
    from src import metrics

    # a page id must not become its own label value (bounded cardinality)
    assert metrics.metric_path("/api/wiki/pages/0123456789abcdef0123456789abcdef") == "/api/wiki/pages/{id}"
    assert metrics.metric_path("/api/wiki/spaces") == "/api/wiki/spaces"
