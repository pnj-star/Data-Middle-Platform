"""Wiki audit-log tests (ROADMAP Phase 2, P2-T3)."""
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


def _register(client, username="alice", password="pw123456"):
    assert client.post("/api/wiki/auth/register", json={"username": username, "password": password}).status_code == 200
    return client.post("/api/wiki/auth/login", json={"username": username, "password": password}).json()["access_token"]


def test_page_create_is_audited(wiki_engine):
    with TestClient(main.app) as client:
        tok = _register(client, "alice")
        auth = {"Authorization": f"Bearer {tok}"}
        sid = client.get("/api/wiki/spaces", headers=auth).json()[0]["id"]
        page = client.post("/api/wiki/pages", json={"space_id": sid, "title": "审计页"}, headers=auth).json()

        audit = client.get("/api/wiki/audit", headers=auth).json()
        creates = [a for a in audit if a["action"] == "page.create"]
        assert creates
        c = creates[0]
        assert c["target_id"] == page["id"]
        assert c["user_id"]  # the logged-in user, not system
        assert c["ip"]  # source ip recorded
        assert c["result"] == "success"


def test_login_success_and_failure_audited(wiki_engine):
    with TestClient(main.app) as client:
        _register(client, "bob")
        client.post("/api/wiki/auth/login", json={"username": "bob", "password": "wrong1"})
        client.post("/api/wiki/auth/login", json={"username": "bob", "password": "pw123456"})

        audit = client.get("/api/wiki/audit?action=auth.login").json()
        failures = [a for a in audit if a["result"] == "failure"]
        successes = [a for a in audit if a["result"] == "success"]
        assert failures  # bad-password attempt recorded with result=failure
        assert successes
        assert failures[0]["detail"]["username"] == "bob"


def test_audit_admin_only(wiki_engine, monkeypatch):
    from src.config import config

    monkeypatch.setattr(config.security, "api_key", "super-key")
    with TestClient(main.app) as client:
        tok = _register(client, "carol")
        auth = {"Authorization": f"Bearer {tok}"}
        # a normal JWT user cannot view the audit trail
        assert client.get("/api/wiki/audit", headers=auth).status_code == 403
        # superuser (API key) can
        assert client.get("/api/wiki/audit", headers={"X-API-Key": "super-key"}).status_code == 200


def test_publish_and_delete_audited(wiki_engine, monkeypatch):
    with TestClient(main.app) as client:
        tok = _register(client, "dave")
        auth = {"Authorization": f"Bearer {tok}"}
        sid = client.get("/api/wiki/spaces", headers=auth).json()[0]["id"]
        page = client.post("/api/wiki/pages", json={"space_id": sid, "title": "P"}, headers=auth).json()

        monkeypatch.setattr("src.wiki.api._dispatch_wiki_task", lambda *a, **k: types.SimpleNamespace(id="t"))
        assert client.post(f"/api/wiki/pages/{page['id']}/publish", headers=auth).status_code == 200
        assert client.delete(f"/api/wiki/pages/{page['id']}", headers=auth).status_code == 200

        audit = client.get("/api/wiki/audit", headers=auth).json()
        actions = [a["action"] for a in audit]
        assert "page.publish" in actions
        assert "page.trash" in actions  # delete is now a soft-delete (P3-T2)


def test_publish_broker_failure_is_audited_failure(wiki_engine, monkeypatch):
    """Publish dispatch failure appends a failure audit (P2-T4 修复)."""
    with TestClient(main.app) as client:
        tok = _register(client, "erik")
        auth = {"Authorization": f"Bearer {tok}"}
        sid = client.get("/api/wiki/spaces", headers=auth).json()[0]["id"]
        page = client.post("/api/wiki/pages", json={"space_id": sid, "title": "P"}, headers=auth).json()

        def boom(*a, **k):
            raise RuntimeError("redis down")

        monkeypatch.setattr("src.wiki.api._dispatch_wiki_task", boom)
        assert client.post(f"/api/wiki/pages/{page['id']}/publish", headers=auth).status_code == 503

        audit = client.get("/api/wiki/audit?action=page.publish", headers=auth).json()
        failures = [a for a in audit if a["result"] == "failure"]
        assert failures  # the earlier success row is corrected by a failure row
        assert failures[0]["detail"]["error"] == "broker_unavailable"
