"""Wiki space-level ACL tests (ROADMAP Phase 2, P2-T2).

ACL activates once API_KEY is configured (dev mode keeps anonymous access for
the older tests). These tests pin API_KEY to a value and exercise the matrix:
superuser bypass / reader / editor / no-access / anonymous.
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
from src.wiki.models import Role, Space, SpaceMember, User  # noqa: E402
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
    # roles (owner/editor/reader) + system + default space
    with wdb.session_scope() as s:
        ensure_seed_data(s)
    yield


def _client(monkeypatch):
    from src.config import config

    monkeypatch.setattr(config.security, "api_key", "super-key")
    return TestClient(main.app)


def _token(client, username, password="pw123456"):
    r = client.post("/api/wiki/auth/register", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    tok = client.post("/api/wiki/auth/login", json={"username": username, "password": password}).json()["access_token"]
    return tok


def _member(username: str, space_slug: str, role: str) -> None:
    with wdb.session_scope() as s:
        uid = s.execute(select(User.id).where(User.username == username)).scalar_one()
        sid = s.execute(select(Space.id).where(Space.slug == space_slug)).scalar_one()
        rid = s.execute(select(Role.id).where(Role.name == role)).scalar_one()
        s.add(SpaceMember(user_id=uid, space_id=sid, role_id=rid))


def _space(slug: str) -> str:
    with wdb.session_scope() as s:
        sp = s.execute(select(Space).where(Space.slug == slug)).scalar_one()
        return sp.id


def test_reader_reads_not_writes(wiki_engine, monkeypatch):
    with _client(monkeypatch) as client:
        tok = _token(client, "reader1")
        _member("reader1", "default", "reader")
        _space("default")
        auth = {"Authorization": f"Bearer {tok}"}

        assert client.get("/api/wiki/pages", params={"space_id": _space("default")}, headers=auth).status_code == 200
        assert client.post("/api/wiki/pages", json={"space_id": _space("default"), "title": "x"}, headers=auth).status_code == 403
        page = client.post("/api/wiki/pages", json={"space_id": _space("default"), "title": "x"}, headers={"X-API-Key": "super-key"}).json()
        assert client.put(f"/api/wiki/pages/{page['id']}", json={"content": "y"}, headers=auth).status_code == 403
        assert client.post(f"/api/wiki/pages/{page['id']}/publish", headers=auth).status_code == 403
        assert client.delete(f"/api/wiki/pages/{page['id']}", headers=auth).status_code == 403


def test_editor_can_write(wiki_engine, monkeypatch):
    with _client(monkeypatch) as client:
        tok = _token(client, "editor1")
        _member("editor1", "default", "editor")
        auth = {"Authorization": f"Bearer {tok}"}
        r = client.post("/api/wiki/pages", json={"space_id": _space("default"), "title": "可写"}, headers=auth)
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        assert client.put(f"/api/wiki/pages/{pid}", json={"content": "c"}, headers=auth).status_code == 200


def test_no_membership_forbidden(wiki_engine, monkeypatch):
    with _client(monkeypatch) as client:
        tok = _token(client, "outsider")
        auth = {"Authorization": f"Bearer {tok}"}
        assert client.get("/api/wiki/pages", params={"space_id": _space("default")}, headers=auth).status_code == 403


def test_anonymous_401_when_acl_active(wiki_engine, monkeypatch):
    with _client(monkeypatch) as client:
        assert client.get("/api/wiki/pages", params={"space_id": _space("default")}).status_code == 401


def test_superuser_api_key_bypasses(wiki_engine, monkeypatch):
    with _client(monkeypatch) as client:
        h = {"X-API-Key": "super-key"}
        assert client.get("/api/wiki/pages", params={"space_id": _space("default")}, headers=h).status_code == 200
        r = client.post("/api/wiki/pages", json={"space_id": _space("default"), "title": "s"}, headers=h)
        assert r.status_code == 200


def test_spaces_list_scoped(wiki_engine, monkeypatch):
    with wdb.session_scope() as s:
        s.add(Space(slug="second", name="Second"))
    with _client(monkeypatch) as client:
        tok = _token(client, "scoped")
        _member("scoped", "default", "reader")
        auth = {"Authorization": f"Bearer {tok}"}
        mine = client.get("/api/wiki/spaces", headers=auth).json()
        assert [sp["slug"] for sp in mine] == ["default"]  # second not visible
        all_spaces = client.get("/api/wiki/spaces", headers={"X-API-Key": "super-key"}).json()
        assert {"default", "second"} <= {sp["slug"] for sp in all_spaces}


def test_search_scoped_to_member_spaces(wiki_engine, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "src.wiki.api.wm.search",
        lambda vec, filter_expr="", limit=20, offset=0: seen.update(expr=filter_expr) or [],
    )
    monkeypatch.setattr("src.embedder.get_embedding", lambda q: [0.1] * 512)
    # publish a page so there's a revision to search
    from src.wiki.models import Page, Revision

    with wdb.session_scope() as s:
        u = s.execute(select(User).where(User.username == "system")).scalar_one()
        sid = _space("default")
        pg = Page(space_id=sid, title="P", status="published", created_by=u.id, updated_by=u.id)
        s.add(pg)
        s.flush()
        rv = Revision(page_id=pg.id, revision_id=1, content_md="c")
        s.add(rv)
        s.flush()
        pg.current_revision_id = rv.id

    with _client(monkeypatch) as client:
        tok = _token(client, "searcher")
        _member("searcher", "default", "reader")
        auth = {"Authorization": f"Bearer {tok}"}
        client.get("/api/wiki/search", params={"q": "x"}, headers=auth)
        assert f'space_id in ["{_space("default")}"]' in seen["expr"]  # pre-filter scoped to member space
