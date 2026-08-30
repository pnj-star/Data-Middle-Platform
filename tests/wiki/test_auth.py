"""Wiki auth tests (ROADMAP Phase 2, P2-T1): register / login / me."""
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
from src.wiki.models import User  # noqa: E402

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


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    """Auth tests exercise authentication logic, not the P4-T2 rate limiter —
    pin a no-op limiter so real-Redis counters from other runs can't interfere."""
    fake = types.SimpleNamespace(
        get=lambda k: None, incr=lambda k: 1, expire=lambda k, t: None, delete=lambda k: None
    )
    monkeypatch.setattr("src.wiki.api._rate_limiter_redis", lambda: fake)


def _register(client, username="alice", password="secret1"):
    r = client.post("/api/wiki/auth/register", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def test_register_and_login(wiki_engine):
    with TestClient(main.app) as client:
        user = _register(client)
        assert user["username"] == "alice"
        assert "password_hash" not in user  # never leak the hash
        assert user["provider"] == "local"

        r = client.post("/api/wiki/auth/login", json={"username": "alice", "password": "secret1"})
        assert r.status_code == 200
        assert r.json()["token_type"] == "bearer" and r.json()["access_token"]


def test_register_duplicate_username(wiki_engine):
    with TestClient(main.app) as client:
        _register(client)
        r = client.post("/api/wiki/auth/register", json={"username": "alice", "password": "other1"})
        assert r.status_code == 409


def test_register_rejects_bad_username(wiki_engine):
    with TestClient(main.app) as client:
        # spaces / too short are rejected by the schema
        assert client.post("/api/wiki/auth/register", json={"username": "a b", "password": "secret1"}).status_code == 422
        assert client.post("/api/wiki/auth/register", json={"username": "ab", "password": "secret1"}).status_code == 422


def test_login_wrong_password_and_unknown_user(wiki_engine):
    with TestClient(main.app) as client:
        _register(client)
        assert client.post("/api/wiki/auth/login", json={"username": "alice", "password": "wrong1"}).status_code == 401
        assert client.post("/api/wiki/auth/login", json={"username": "nobody", "password": "secret1"}).status_code == 401


def test_me_requires_token(wiki_engine):
    with TestClient(main.app) as client:
        token = client.post("/api/wiki/auth/login", json={"username": _register(client)["username"], "password": "secret1"}).json()["access_token"]
        assert client.get("/api/wiki/auth/me").status_code == 401
        assert client.get("/api/wiki/auth/me", headers={"Authorization": "Bearer invalid.token.x"}).status_code == 401
        r = client.get("/api/wiki/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["username"] == "alice"


def test_auth_disabled_without_secret(wiki_engine, monkeypatch):
    from src.config import config

    monkeypatch.setattr(config.security, "jwt_secret", "")
    with TestClient(main.app) as client:
        assert client.post("/api/wiki/auth/register", json={"username": "xyz", "password": "secret1"}).status_code == 503
        assert client.post("/api/wiki/auth/login", json={"username": "xyz", "password": "secret1"}).status_code == 503


def test_registration_can_be_disabled(wiki_engine, monkeypatch):
    from src.config import config

    monkeypatch.setattr(config.security, "allow_registration", False)
    with TestClient(main.app) as client:
        assert client.post("/api/wiki/auth/register", json={"username": "xyz", "password": "secret1"}).status_code == 403


def test_password_is_hashed(wiki_engine):
    with TestClient(main.app) as client:
        _register(client)
        with wdb.session_scope() as s:
            from sqlalchemy import select

            u = s.execute(select(User).where(User.username == "alice")).scalar_one()
            assert u.password_hash and u.password_hash != "secret1"
            assert u.password_hash.startswith("$2")  # bcrypt prefix
