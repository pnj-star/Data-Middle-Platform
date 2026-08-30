"""Comment tests (ROADMAP P3-T5): create / list / reply / delete + ACL."""
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


def _page_id(client):
    sid = client.get("/api/wiki/spaces").json()[0]["id"]
    return client.post("/api/wiki/pages", json={"space_id": sid, "title": "评论页"}).json()["id"]


def test_comment_create_list_reply_delete(wiki_engine):
    with TestClient(main.app) as client:
        pid = _page_id(client)
        r = client.post(f"/api/wiki/pages/{pid}/comments", json={"content": "第一条评论"})
        assert r.status_code == 200, r.text
        c1 = r.json()
        assert c1["username"] == "system"  # dev mode → system author
        assert c1["content"] == "第一条评论"

        r2 = client.post(f"/api/wiki/pages/{pid}/comments", json={"content": "回复", "parent_comment_id": c1["id"]})
        assert r2.status_code == 200
        assert r2.json()["parent_comment_id"] == c1["id"]

        listed = client.get(f"/api/wiki/pages/{pid}/comments").json()
        assert [c["content"] for c in listed] == ["第一条评论", "回复"]

        # deleting the parent cascades to the reply
        assert client.delete(f"/api/wiki/comments/{c1['id']}").status_code == 200
        assert client.get(f"/api/wiki/pages/{pid}/comments").json() == []


def test_comment_empty_rejected(wiki_engine):
    with TestClient(main.app) as client:
        pid = _page_id(client)
        assert client.post(f"/api/wiki/pages/{pid}/comments", json={"content": "   "}).status_code == 400


def test_comment_delete_recurses_grandchildren(wiki_engine):
    """Deleting a comment clears replies at any depth (P3 修复 item 3)."""
    with TestClient(main.app) as client:
        pid = _page_id(client)
        top = client.post(f"/api/wiki/pages/{pid}/comments", json={"content": "top"}).json()
        mid = client.post(f"/api/wiki/pages/{pid}/comments", json={"content": "mid", "parent_comment_id": top["id"]}).json()
        client.post(f"/api/wiki/pages/{pid}/comments", json={"content": "leaf", "parent_comment_id": mid["id"]}).json()
        assert client.delete(f"/api/wiki/comments/{top['id']}").status_code == 200
        assert client.get(f"/api/wiki/pages/{pid}/comments").json() == []


def test_comment_delete_permissions(wiki_engine, monkeypatch):
    from src.config import config

    monkeypatch.setattr(config.security, "api_key", "super-key")
    with TestClient(main.app) as client:
        # owner creates page; alice + bob join as reader
        assert client.post("/api/wiki/auth/register", json={"username": "alice", "password": "pw123456"}).status_code == 200
        assert client.post("/api/wiki/auth/register", json={"username": "bob", "password": "pw123456"}).status_code == 200
        at = client.post("/api/wiki/auth/login", json={"username": "alice", "password": "pw123456"}).json()["access_token"]
        bt = client.post("/api/wiki/auth/login", json={"username": "bob", "password": "pw123456"}).json()["access_token"]
        aid = client.get("/api/wiki/auth/me", headers={"Authorization": f"Bearer {at}"}).json()["id"]
        sid = client.get("/api/wiki/spaces", headers={"X-API-Key": "super-key"}).json()
        sid = next(s["id"] for s in sid if s["slug"] == "default")
        for uid in (aid, client.get("/api/wiki/auth/me", headers={"Authorization": f"Bearer {bt}"}).json()["id"]):
            client.post(f"/api/wiki/spaces/{sid}/members", json={"user_id": uid, "role": "reader"},
                        headers={"X-API-Key": "super-key"})

        pid = client.post("/api/wiki/pages", json={"space_id": sid, "title": "P"}, headers={"X-API-Key": "super-key"}).json()["id"]
        c = client.post(f"/api/wiki/pages/{pid}/comments", json={"content": "alice 的评论"},
                        headers={"Authorization": f"Bearer {at}"}).json()
        # bob (reader) cannot delete alice's comment
        assert client.delete(f"/api/wiki/comments/{c['id']}", headers={"Authorization": f"Bearer {bt}"}).status_code == 403
        # alice (author) can
        assert client.delete(f"/api/wiki/comments/{c['id']}", headers={"Authorization": f"Bearer {at}"}).status_code == 200
