"""Scoped API key tests (ROADMAP P4-T7): issue, grant role, revoke."""
from __future__ import annotations

import hashlib
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
from src.wiki.models import ApiKey  # noqa: E402
from src.wiki.seed import ensure_seed_data  # noqa: E402

_TRUNCATE = (
    "TRUNCATE TABLE audit_logs, space_members, links, comments, attachments, "
    "api_keys, revisions, pages, spaces, users, roles RESTART IDENTITY CASCADE"
)


@pytest.fixture(autouse=True)
def _clean(wiki_engine):
    from sqlalchemy import text

    with wdb.get_engine().begin() as conn:
        conn.execute(text(_TRUNCATE))
    with wdb.session_scope() as s:
        ensure_seed_data(s)
    yield


@pytest.fixture
def client(monkeypatch):
    from src.config import config

    monkeypatch.setattr(config.security, "api_key", "super-key")
    return TestClient(main.app)


def _space_id(client):
    return next(s["id"] for s in client.get("/api/wiki/spaces", headers={"X-API-Key": "super-key"}).json() if s["slug"] == "default")


def _issue(client, space_id, role, name="k"):
    r = client.post("/api/wiki/api-keys", json={"name": name, "space_id": space_id, "role": role},
                    headers={"X-API-Key": "super-key"})
    assert r.status_code == 200, r.text
    return r.json()


def test_scoped_key_grants_role(wiki_engine, client):
    sid = _space_id(client)
    created = _issue(client, sid, "reader")
    rkey = created["key"]
    assert rkey.startswith("sk_")
    h = {"X-API-Key": rkey}
    # reader: read OK, write 403
    assert client.get("/api/wiki/pages", params={"space_id": sid}, headers=h).status_code == 200
    assert client.post("/api/wiki/pages", json={"space_id": sid, "title": "x"}, headers=h).status_code == 403
    # only the hash is stored, never the plaintext
    with wdb.session_scope() as s:
        row = s.execute(select(ApiKey)).scalar_one()
        assert row.key_hash == hashlib.sha256(rkey.encode("utf-8")).hexdigest()
        assert row.key_hash != rkey


def test_editor_key_can_write(wiki_engine, client):
    sid = _space_id(client)
    created = _issue(client, sid, "editor")
    h = {"X-API-Key": created["key"]}
    assert client.post("/api/wiki/pages", json={"space_id": sid, "title": "由 key 创建"}, headers=h).status_code == 200


def test_revoke_disables_key(wiki_engine, client):
    sid = _space_id(client)
    created = _issue(client, sid, "reader")
    h = {"X-API-Key": created["key"]}
    assert client.get("/api/wiki/pages", params={"space_id": sid}, headers=h).status_code == 200
    assert client.delete(f"/api/wiki/api-keys/{created['id']}", headers={"X-API-Key": "super-key"}).status_code == 200
    # revoked key → 401 (no longer matches an active row)
    assert client.get("/api/wiki/pages", params={"space_id": sid}, headers=h).status_code == 401


def test_global_key_still_superuser(wiki_engine, client):
    # the global API_KEY still bypasses ACL (superuser path preserved)
    r = client.post("/api/wiki/pages", json={"space_id": _space_id(client), "title": "s"},
                    headers={"X-API-Key": "super-key"})
    assert r.status_code == 200


def test_list_keys_superuser_only(wiki_engine, client):
    sid = _space_id(client)
    _issue(client, sid, "reader")
    assert client.get("/api/wiki/api-keys", headers={"X-API-Key": "super-key"}).status_code == 200
