"""Space & member management API tests (Phase 2, P2-T4 extension).

These give Phase 2's "A reads/writes X, B reads Y" exit criterion a real product
entry point: an owner assigns roles via the API instead of raw SQL.
"""
from __future__ import annotations

import sys
import types

_fake_converter = types.ModuleType("src.converter")
_fake_converter.SUPPORTED_EXTENSIONS = {".pdf", ".md"}
sys.modules["src.converter"] = _fake_converter

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

import main  # noqa: E402
from src.wiki import database as wdb  # noqa: E402
from src.wiki.models import SpaceMember  # noqa: E402
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


def _client(monkeypatch):
    from src.config import config

    monkeypatch.setattr(config.security, "api_key", "super-key")
    return TestClient(main.app)


def _user(client, username):
    assert client.post("/api/wiki/auth/register", json={"username": username, "password": "pw123456"}).status_code == 200
    tok = client.post("/api/wiki/auth/login", json={"username": username, "password": "pw123456"}).json()["access_token"]
    return tok


def test_owner_creates_space_and_assigns_member(wiki_engine, monkeypatch):
    with _client(monkeypatch) as client:
        owner_tok = _user(client, "owner1")
        member_tok = _user(client, "member1")
        auth = {"Authorization": f"Bearer {owner_tok}"}

        # owner creates a space → auto-owner membership
        r = client.post("/api/wiki/spaces", json={"slug": "team-a", "name": "A 团队"}, headers=auth)
        assert r.status_code == 200, r.text
        sid = r.json()["id"]

        members = client.get(f"/api/wiki/spaces/{sid}/members", headers=auth).json()
        assert any(m["username"] == "owner1" and m["role"] == "owner" for m in members)

        # owner assigns member1 as editor
        uid = client.get("/api/wiki/auth/me", headers={"Authorization": f"Bearer {member_tok}"}).json()["id"]
        r2 = client.post(f"/api/wiki/spaces/{sid}/members", json={"user_id": uid, "role": "editor"}, headers=auth)
        assert r2.status_code == 200, r2.text
        assert r2.json()["role"] == "editor" and r2.json()["username"] == "member1"

        # member1 can now read AND write in team-a
        assert client.get("/api/wiki/pages", params={"space_id": sid}, headers={"Authorization": f"Bearer {member_tok}"}).status_code == 200
        assert client.post("/api/wiki/pages", json={"space_id": sid, "title": "由成员创建"}, headers={"Authorization": f"Bearer {member_tok}"}).status_code == 200


def test_non_owner_cannot_manage_members(wiki_engine, monkeypatch):
    with _client(monkeypatch) as client:
        owner_tok = _user(client, "owner2")
        reader_tok = _user(client, "reader2")
        victim = client.get("/api/wiki/auth/me", headers={"Authorization": f"Bearer {reader_tok}"}).json()
        auth = {"Authorization": f"Bearer {owner_tok}"}

        sid = client.post("/api/wiki/spaces", json={"slug": "team-b", "name": "B"}, headers=auth).json()["id"]
        # make reader2 a member (reader) so they can see the space
        client.post(f"/api/wiki/spaces/{sid}/members", json={"user_id": victim["id"], "role": "reader"}, headers=auth)

        # reader2 cannot add members
        other = client.post("/api/wiki/auth/register", json={"username": "someone", "password": "pw123456"}).json()
        r = client.post(f"/api/wiki/spaces/{sid}/members", json={"user_id": other["id"], "role": "reader"},
                        headers={"Authorization": f"Bearer {reader_tok}"})
        assert r.status_code == 403


def test_owner_cannot_remove_self(wiki_engine, monkeypatch):
    with _client(monkeypatch) as client:
        tok = _user(client, "solo")
        auth = {"Authorization": f"Bearer {tok}"}
        me = client.get("/api/wiki/auth/me", headers=auth).json()
        sid = client.post("/api/wiki/spaces", json={"slug": "solo", "name": "S"}, headers=auth).json()["id"]
        assert client.delete(f"/api/wiki/spaces/{sid}/members/{me['id']}", headers=auth).status_code == 400


def test_duplicate_slug_rejected(wiki_engine, monkeypatch):
    with _client(monkeypatch) as client:
        tok = _user(client, "dup")
        auth = {"Authorization": f"Bearer {tok}"}
        assert client.post("/api/wiki/spaces", json={"slug": "same", "name": "1"}, headers=auth).status_code == 200
        assert client.post("/api/wiki/spaces", json={"slug": "same", "name": "2"}, headers=auth).status_code == 409


def test_invalid_token_is_explicit_401(wiki_engine, monkeypatch):
    with _client(monkeypatch) as client:
        assert client.get("/api/wiki/spaces", headers={"Authorization": "Bearer not.a.token"}).status_code == 401
