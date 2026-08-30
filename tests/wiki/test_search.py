"""Wiki search API tests (ROADMAP P1-T6).

Pre-filter semantics are the core: only published pages' current revisions are
searchable; the published-revision set is fetched from Postgres and passed to
Milvus as `revision_id in [...]` BEFORE ranking (D9/D10).
"""
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
from src.wiki.models import Page, Revision, Space, User  # noqa: E402

_TRUNCATE = (
    "TRUNCATE TABLE audit_logs, space_members, links, comments, attachments, "
    "revisions, pages, spaces, users, roles RESTART IDENTITY CASCADE"
)


@pytest.fixture(autouse=True)
def _clean(wiki_engine):
    from sqlalchemy import text

    with wdb.get_engine().begin() as conn:
        conn.execute(text(_TRUNCATE))
    yield


def _seed_published(wiki_engine):
    """A page in `published` state whose current_revision points at rev 1."""
    with wdb.session_scope() as s:
        u = User(username="s_usr")
        s.add(u)
        s.flush()
        sp = Space(slug="s-space", name="S", owner_user_id=u.id)
        s.add(sp)
        s.flush()
        p = Page(space_id=sp.id, title="已发布页", status="published", created_by=u.id, updated_by=u.id)
        s.add(p)
        s.flush()
        r = Revision(page_id=p.id, revision_id=1, content_md="# 内容", editor_user_id=u.id)
        s.add(r)
        s.flush()
        p.current_revision_id = r.id
        return p.id, r.revision_id, sp.id


def test_search_empty_when_nothing_published(wiki_engine, monkeypatch):
    """No published pages → empty result, never touches Milvus."""
    touched = []
    monkeypatch.setattr("src.wiki.api.wm.search", lambda *a, **k: touched.append(1) or [])
    with TestClient(main.app) as client:
        r = client.get("/api/wiki/search", params={"q": "test"})
    assert r.status_code == 200
    assert r.json() == {"query": "test", "results": [], "total": 0}
    assert touched == []  # pre-filter short-circuit


def test_search_prefilter_and_page_title(wiki_engine, monkeypatch):
    pid, revno, sid = _seed_published(wiki_engine)
    seen = {}

    def fake_search(vec, filter_expr="", limit=20, offset=0):
        seen["expr"] = filter_expr
        return [{
            "page_id": pid, "revision_id": revno, "space_id": sid,
            "content": "命中内容", "parent_title": "节", "chunk_index": 0,
        }]

    monkeypatch.setattr("src.wiki.api.wm.search", fake_search)
    monkeypatch.setattr("src.embedder.get_embedding", lambda q: [0.1] * 512)
    with TestClient(main.app) as client:
        body = client.get("/api/wiki/search", params={"q": "query"}).json()

    # D11: composite (page_id, revision_id) pre-filter, not bare rev_ids.
    assert f"revision_id == {revno}" in seen["expr"]
    assert "page_id in [" in seen["expr"]
    assert body["results"][0]["page_id"] == pid
    assert body["results"][0]["page_title"] == "已发布页"  # enriched from Postgres


def test_search_space_filter(wiki_engine, monkeypatch):
    _pid, _revno, sid = _seed_published(wiki_engine)
    seen = {}
    monkeypatch.setattr(
        "src.wiki.api.wm.search",
        lambda vec, filter_expr="", limit=20, offset=0: seen.update(expr=filter_expr) or [],
    )
    monkeypatch.setattr("src.embedder.get_embedding", lambda q: [0.1] * 512)
    with TestClient(main.app) as client:
        client.get("/api/wiki/search", params={"q": "x", "space_id": sid})
    assert f'space_id == "{sid}"' in seen["expr"]


def test_search_keyword_filter(wiki_engine, monkeypatch):
    _pid, _revno, _sid = _seed_published(wiki_engine)
    seen = {}
    monkeypatch.setattr(
        "src.wiki.api.wm.search",
        lambda vec, filter_expr="", limit=20, offset=0: seen.update(expr=filter_expr) or [],
    )
    monkeypatch.setattr("src.embedder.get_embedding", lambda q: [0.1] * 512)
    with TestClient(main.app) as client:
        client.get("/api/wiki/search", params={"q": "x", "keyword": "敏感词"})
    assert 'content like "%敏感词%"' in seen["expr"]


def test_search_rejects_empty_query(wiki_engine, monkeypatch):
    with TestClient(main.app) as client:
        assert client.get("/api/wiki/search", params={"q": ""}).status_code == 422
