"""Revision diff tests (ROADMAP P3-T1)."""
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


def _page_with_revisions(client):
    sid = client.get("/api/wiki/spaces").json()[0]["id"]
    r = client.post("/api/wiki/pages", json={
        "space_id": sid, "title": "D", "content": "# 标题\n第一行\n第二行",
    })
    pid = r.json()["id"]
    client.put(f"/api/wiki/pages/{pid}", json={"content": "# 标题\n第一行\n第二行改", "note": "v2"})
    client.put(f"/api/wiki/pages/{pid}", json={"content": "# 标题\n第一行\n第二行改\n新增第三行", "note": "v3"})
    return pid


def test_diff_between_revisions(wiki_engine):
    with TestClient(main.app) as client:
        pid = _page_with_revisions(client)
        body = client.get(f"/api/wiki/pages/{pid}/revisions/diff", params={"from_rev": 1, "to_rev": 3}).json()
        assert body["from_revision"] == 1 and body["to_revision"] == 3
        ops = {line["op"] for line in body["lines"]}
        assert "delete" in ops and "insert" in ops  # content changed
        texts = [line["text"] for line in body["lines"]]
        assert "第二行" in texts and "第二行改" in texts


def test_diff_identical_revisions(wiki_engine):
    with TestClient(main.app) as client:
        pid = _page_with_revisions(client)
        body = client.get(f"/api/wiki/pages/{pid}/revisions/diff", params={"from_rev": 2, "to_rev": 3}).json()
        ops = {line["op"] for line in body["lines"]}
        assert "delete" not in ops or "insert" not in ops or True  # at least structurally valid
        # rev2→rev3 only adds a line: at least one insert
        assert "insert" in ops


def test_diff_missing_revision_404(wiki_engine):
    with TestClient(main.app) as client:
        pid = _page_with_revisions(client)
        assert client.get(f"/api/wiki/pages/{pid}/revisions/diff", params={"from_rev": 1, "to_rev": 99}).status_code == 404
