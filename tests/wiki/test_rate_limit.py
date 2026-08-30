"""Phase 4 tests: request-id/version headers (P4-T1) + login rate limit (P4-T2)."""
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


class FakeRedis:
    """In-memory stand-in for the rate-limiter redis."""

    def __init__(self):
        self.data: dict = {}

    def get(self, key):
        return self.data.get(key)

    def incr(self, key):
        # redis (decode_responses=True) stores/returns strings
        self.data[key] = str(int(self.data.get(key, 0)) + 1)
        return self.data[key]

    def expire(self, key, ttl):
        pass

    def delete(self, key):
        self.data.pop(key, None)


def _register(client, username="alice", password="pw123456"):
    assert client.post("/api/wiki/auth/register", json={"username": username, "password": password}).status_code == 200


def test_request_id_and_version_headers(wiki_engine):
    with TestClient(main.app) as client:
        r = client.get("/api/wiki/spaces")
        assert r.status_code == 200
        assert r.headers.get("X-Request-Id")  # correlation id present
        assert r.headers.get("X-API-Version") == "v1"


def test_login_lockout_after_max_attempts(wiki_engine, monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr("src.wiki.api._rate_limiter_redis", lambda: fake)
    with TestClient(main.app) as client:
        _register(client)
        # max attempts all fail with 401
        for _ in range(5):
            assert client.post("/api/wiki/auth/login", json={"username": "alice", "password": "wrong1"}).status_code == 401
        # next attempt is rate-limited (429)
        assert client.post("/api/wiki/auth/login", json={"username": "alice", "password": "wrong1"}).status_code == 429


def test_login_success_clears_counter(wiki_engine, monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr("src.wiki.api._rate_limiter_redis", lambda: fake)
    with TestClient(main.app) as client:
        _register(client)
        assert client.post("/api/wiki/auth/login", json={"username": "alice", "password": "wrong1"}).status_code == 401
        assert fake.data.get("wiki:login_fail:alice") == "1"
        assert client.post("/api/wiki/auth/login", json={"username": "alice", "password": "pw123456"}).status_code == 200
        assert "wiki:login_fail:alice" not in fake.data  # success clears the counter


def test_rate_limit_degrades_open_when_redis_down(wiki_engine, monkeypatch):
    def boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr("src.wiki.api._rate_limiter_redis", boom)
    with TestClient(main.app) as client:
        _register(client)
        # redis unavailable → login still works (fail-lenient)
        assert client.post("/api/wiki/auth/login", json={"username": "alice", "password": "pw123456"}).status_code == 200


# ── IP generic rate limit + idempotency (P4-T2) ────────────────────

def test_ip_rate_limit_helper(wiki_engine, monkeypatch):
    from src import rate_limit

    fake = FakeRedis()
    monkeypatch.setattr(rate_limit, "redis_client", lambda: fake)
    # budget of 3 per minute: first 3 pass, 4th is limited
    assert rate_limit.ip_rate_limit_hit("1.2.3.4", 3) is False
    assert rate_limit.ip_rate_limit_hit("1.2.3.4", 3) is False
    assert rate_limit.ip_rate_limit_hit("1.2.3.4", 3) is False
    assert rate_limit.ip_rate_limit_hit("1.2.3.4", 3) is True


def test_ip_rate_limit_429_with_exempt_paths(wiki_engine, monkeypatch):
    from src.config import config

    monkeypatch.setattr(config.security, "ip_rate_limit_per_minute", 10)
    monkeypatch.setattr("src.rate_limit.ip_rate_limit_hit", lambda ip, limit: True)
    with TestClient(main.app) as client:
        assert client.get("/api/wiki/spaces").status_code == 429
        assert client.get("/health").status_code in (200, 503)  # exempt path unaffected


def test_create_page_idempotency(wiki_engine, monkeypatch):
    from src import rate_limit

    stored = {}

    def fake_lookup(key, scope=""):
        return stored.get((scope, key))

    def fake_store(key, scope, resource_id):
        stored[(scope, key)] = resource_id

    monkeypatch.setattr(rate_limit, "idempotency_lookup", fake_lookup)
    monkeypatch.setattr(rate_limit, "idempotency_store", fake_store)
    with TestClient(main.app) as client:
        sid = client.get("/api/wiki/spaces").json()[0]["id"]
        h = {"Idempotency-Key": "create-page-1"}
        r1 = client.post("/api/wiki/pages", json={"space_id": sid, "title": "幂等页"}, headers=h)
        assert r1.status_code == 200, r1.text
        pid1 = r1.json()["id"]
        # same key again → returns the already-created page, no duplicate
        r2 = client.post("/api/wiki/pages", json={"space_id": sid, "title": "幂等页"}, headers=h)
        assert r2.json()["id"] == pid1
        # no key → a normal new page
        r3 = client.post("/api/wiki/pages", json={"space_id": sid, "title": "另一个"})
        assert r3.json()["id"] != pid1


def test_idempotency_scoped_by_space(wiki_engine, monkeypatch):
    """The same Idempotency-Key in a different space creates a new page, not a
    cross-space collision (P4-T2 修复)."""
    from src import rate_limit

    stored = {}

    def fake_lookup(key, scope=""):
        return stored.get((scope, key))

    def fake_store(key, scope, resource_id):
        stored[(scope, key)] = resource_id

    monkeypatch.setattr(rate_limit, "idempotency_lookup", fake_lookup)
    monkeypatch.setattr(rate_limit, "idempotency_store", fake_store)
    with TestClient(main.app) as client:
        sid = client.get("/api/wiki/spaces").json()[0]["id"]
        sid2 = client.post("/api/wiki/spaces", json={"slug": "second", "name": "S2"}).json()["id"]
        h = {"Idempotency-Key": "k"}
        p1 = client.post("/api/wiki/pages", json={"space_id": sid, "title": "A"}, headers=h).json()
        p2 = client.post("/api/wiki/pages", json={"space_id": sid2, "title": "B"}, headers=h).json()
        assert p2["id"] != p1["id"]  # cross-space key does not collide
