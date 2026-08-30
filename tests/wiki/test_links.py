"""Page links / backlinks tests (ROADMAP P3-T4)."""
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


def _mk(client, title):
    sid = client.get("/api/wiki/spaces").json()[0]["id"]
    return client.post("/api/wiki/pages", json={"space_id": sid, "title": title}).json()


def test_links_and_backlinks(wiki_engine):
    with TestClient(main.app) as client:
        a = _mk(client, "页面A")
        b = _mk(client, "页面B")
        client.put(f"/api/wiki/pages/{a['id']}", json={"content": "引用 [[页面B]] 和 [[未来页面]]"})

        links = client.get(f"/api/wiki/pages/{a['id']}/links").json()
        by_label = {l["label"]: l for l in links}
        assert by_label["页面B"]["target_page_id"] == b["id"]
        assert by_label["未来页面"]["target_page_id"] is None  # forward reference
        assert by_label["未来页面"]["target_title"] == "未来页面"

        backlinks = client.get(f"/api/wiki/pages/{b['id']}/backlinks").json()
        assert any(x["page_id"] == a["id"] and x["title"] == "页面A" for x in backlinks)


def test_links_rebuild_on_update(wiki_engine):
    with TestClient(main.app) as client:
        a = _mk(client, "页面A")
        b = _mk(client, "页面B")
        client.put(f"/api/wiki/pages/{a['id']}", json={"content": "引用 [[页面B]]"})
        assert len(client.get(f"/api/wiki/pages/{a['id']}/links").json()) == 1
        # removing the reference rebuilds links to empty
        client.put(f"/api/wiki/pages/{a['id']}", json={"content": "不再引用"})
        assert client.get(f"/api/wiki/pages/{a['id']}/links").json() == []
