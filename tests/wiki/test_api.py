"""Wiki API tests (ROADMAP P1-T2): pages / revisions / spaces CRUD.

Runs against the real wiki test Postgres (conftest points POSTGRES_DB at
wiki_test; the wiki_engine fixture creates + migrates + seeds it).
"""
from __future__ import annotations

import sys
import types

# Avoid pulling the heavy converter/embedder chain into main's import (same trick as
# test_main.py): main.py only needs SUPPORTED_EXTENSIONS at import time.
_fake_converter = types.ModuleType("src.converter")
_fake_converter.SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".html", ".md", ".txt"}
sys.modules["src.converter"] = _fake_converter

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src import db  # noqa: E402
import main  # noqa: E402


def _seed_converted_file(name: str = "my-doc.md", markdown: str = "# 转换内容") -> str:
    """A pipeline file with a finished conversion in the shared test SQLite."""
    info = db.insert_file(name, f"/tmp/{name}", 100, "text", ".md")
    db.insert_conversion(info["id"], markdown, {}, "done")
    return info["id"]


@pytest.fixture(autouse=True)
def _clean_wiki_tables(wiki_engine):
    """API tests commit (get_db commits), so wipe wiki tables before each test.

    Without this, pages/revisions created by one API test leak into the next —
    the TestClient startup re-seeds system user + default space each time, so
    tests always have seed data available.
    """
    from sqlalchemy import text

    from src.wiki import database as wdb

    with wdb.get_engine().begin() as conn:
        conn.execute(text(
            "TRUNCATE TABLE audit_logs, space_members, links, comments, attachments, "
            "revisions, pages, spaces, users, roles RESTART IDENTITY CASCADE"
        ))
    yield


def _space_id(client, slug: str = "default") -> str:
    for sp in client.get("/api/wiki/spaces").json():
        if sp["slug"] == slug:
            return sp["id"]
    raise AssertionError(f"space {slug!r} not seeded")


def test_seed_is_idempotent(wiki_engine):
    from src.wiki import database as wdb
    from src.wiki.seed import ensure_seed_data

    with wdb.session_scope() as s:
        first = ensure_seed_data(s)
    with wdb.session_scope() as s:
        second = ensure_seed_data(s)
    assert first["users"] == 1 and first["spaces"] == 1 and first["roles"] == 3
    assert second == {"users": 0, "roles": 0, "spaces": 0}


def test_create_and_update_page(wiki_engine):
    with TestClient(main.app) as client:
        sid = _space_id(client)
        r = client.post(
            "/api/wiki/pages",
            json={"space_id": sid, "title": "首页", "content": "# 第一版"},
        )
        assert r.status_code == 200, r.text
        page = r.json()
        pid = page["id"]
        assert page["status"] == "draft"
        assert page["content"] == "# 第一版"
        assert page["current_revision_id"] is None  # not published (D10)

        # update → revision 2, published pointer still None
        r2 = client.put(f"/api/wiki/pages/{pid}", json={"content": "# 第二版", "note": "改了一版"})
        assert r2.status_code == 200, r2.text
        assert r2.json()["revision_id"] == 2

        # page now serves latest content
        assert client.get(f"/api/wiki/pages/{pid}").json()["content"] == "# 第二版"

        # history newest-first, and pinned revision fetch
        revs = client.get(f"/api/wiki/pages/{pid}/revisions").json()
        assert [v["revision_id"] for v in revs] == [2, 1]
        assert client.get(f"/api/wiki/pages/{pid}/revisions/1").json()["content_md"] == "# 第一版"


def test_page_tree(wiki_engine):
    with TestClient(main.app) as client:
        sid = _space_id(client)
        root = client.post("/api/wiki/pages", json={"space_id": sid, "title": "根页"}).json()
        client.post(
            "/api/wiki/pages",
            json={"space_id": sid, "title": "子页", "parent_page_id": root["id"]},
        )
        tree = client.get(f"/api/wiki/pages?space_id={sid}").json()
        assert len(tree) == 1
        assert tree[0]["title"] == "根页"
        assert [c["title"] for c in tree[0]["children"]] == ["子页"]


def test_cross_space_parent_rejected(wiki_engine):
    with TestClient(main.app) as client:
        sid = _space_id(client)
        # a parent from a different (nonexistent) space is rejected
        r = client.post(
            "/api/wiki/pages",
            json={"space_id": sid, "title": "x", "parent_page_id": "not-a-page"},
        )
        assert r.status_code == 404


def test_delete_page(wiki_engine):
    with TestClient(main.app) as client:
        sid = _space_id(client)
        p = client.post("/api/wiki/pages", json={"space_id": sid, "title": "待删"}).json()
        assert client.delete(f"/api/wiki/pages/{p['id']}").status_code == 200
        assert client.get(f"/api/wiki/pages/{p['id']}").status_code == 404
        assert client.get(f"/api/wiki/pages/{p['id']}/revisions").status_code == 404


def test_soft_delete_keeps_vectors(wiki_engine, monkeypatch):
    """Soft delete (P3-T2) does NOT purge vectors — restore stays instant."""
    import main
    from fastapi.testclient import TestClient

    purged = []
    monkeypatch.setattr("src.wiki.api.wm.delete_by_expr", lambda expr: purged.append(expr) or 0)
    with TestClient(main.app) as client:
        sid = _space_id(client)
        page = client.post("/api/wiki/pages", json={"space_id": sid, "title": "删"}).json()
        assert client.delete(f"/api/wiki/pages/{page['id']}").status_code == 200
    assert purged == []  # vectors kept; only trash purge (P3-T2) removes them


def test_not_found(wiki_engine):
    with TestClient(main.app) as client:
        assert client.get("/api/wiki/pages/does-not-exist").status_code == 404


def test_api_key_guard(wiki_engine, monkeypatch):
    """Once API_KEY is configured, wiki endpoints require auth (ACL, P2-T2)."""
    from src.config import config

    monkeypatch.setattr(config.security, "api_key", "secret")
    with TestClient(main.app) as client:
        # reads and writes are both 401 without credentials
        assert client.get("/api/wiki/spaces").status_code == 401
        assert client.post("/api/wiki/pages", json={"space_id": "x", "title": "t"}).status_code == 401
        # correct API key = superuser → passes auth, proceeds to business logic (404 here)
        assert (
            client.post(
                "/api/wiki/pages",
                json={"space_id": "x", "title": "t"},
                headers={"X-API-Key": "secret"},
            ).status_code
            == 404
        )


# ── Import from converted file (P1-T3) ─────────────────────────────

def test_import_from_converted_file(wiki_engine):
    fid = _seed_converted_file("my-doc.md", "# 转换内容")
    with TestClient(main.app) as client:
        sid = _space_id(client)
        r = client.post(f"/api/wiki/import-from-file/{fid}", json={"space_id": sid})
        assert r.status_code == 200, r.text
        page = r.json()
        assert page["title"] == "my-doc"
        assert page["content"] == "# 转换内容"
        assert page["source_file_name"] == "my-doc.md"
        assert page["source_file_extension"] == "md"
        assert page["status"] == "draft"
        assert page["current_revision_id"] is None  # not published (D10)


def test_import_is_idempotent(wiki_engine):
    fid = _seed_converted_file()
    with TestClient(main.app) as client:
        sid = _space_id(client)
        assert client.post(f"/api/wiki/import-from-file/{fid}", json={"space_id": sid}).status_code == 200
        assert client.post(f"/api/wiki/import-from-file/{fid}", json={"space_id": sid}).status_code == 409


def test_import_unconverted_file_rejected(wiki_engine):
    info = db.insert_file("x.md", "/tmp/x.md", 10, "text", ".md")  # no conversion
    with TestClient(main.app) as client:
        sid = _space_id(client)
        assert client.post(f"/api/wiki/import-from-file/{info['id']}", json={"space_id": sid}).status_code == 409


def test_import_image_file_rejected(wiki_engine):
    info = db.insert_file("img.png", "/tmp/img.png", 10, "image", ".png")
    with TestClient(main.app) as client:
        sid = _space_id(client)
        assert client.post(f"/api/wiki/import-from-file/{info['id']}", json={"space_id": sid}).status_code == 400


def test_import_not_found(wiki_engine):
    with TestClient(main.app) as client:
        assert client.post("/api/wiki/import-from-file/nope", json={"space_id": "s"}).status_code == 404


def test_import_invalid_parent(wiki_engine):
    fid = _seed_converted_file()
    with TestClient(main.app) as client:
        sid = _space_id(client)
        r = client.post(
            f"/api/wiki/import-from-file/{fid}",
            json={"space_id": sid, "parent_page_id": "not-exist"},
        )
        assert r.status_code == 400
