"""Trash / recycle-bin tests (ROADMAP P3-T2): soft delete, restore, purge,
retention cleanup, and search exclusion."""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone

_fake_converter = types.ModuleType("src.converter")
_fake_converter.SUPPORTED_EXTENSIONS = {".pdf", ".md"}
sys.modules["src.converter"] = _fake_converter

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import update  # noqa: E402

import main  # noqa: E402
from src.wiki import database as wdb  # noqa: E402
from src.wiki import trash  # noqa: E402
from src.wiki.models import Page  # noqa: E402
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


def _page(client):
    sid = client.get("/api/wiki/spaces").json()[0]["id"]
    return client.post("/api/wiki/pages", json={"space_id": sid, "title": "回收页"}).json()


def test_soft_delete_and_restore(wiki_engine):
    with TestClient(main.app) as client:
        page = _page(client)
        pid = page["id"]
        assert client.delete(f"/api/wiki/pages/{pid}").status_code == 200
        # gone from normal access + tree, present in trash
        assert client.get(f"/api/wiki/pages/{pid}").status_code == 404
        trash_list = client.get("/api/wiki/trash").json()
        assert any(t["id"] == pid for t in trash_list)
        # restore
        assert client.post(f"/api/wiki/trash/{pid}/restore").status_code == 200
        assert client.get(f"/api/wiki/pages/{pid}").status_code == 200
        assert client.get("/api/wiki/trash").json() == []


def test_double_trash_rejected(wiki_engine):
    with TestClient(main.app) as client:
        page = _page(client)
        pid = page["id"]
        assert client.delete(f"/api/wiki/pages/{pid}").status_code == 200
        assert client.delete(f"/api/wiki/pages/{pid}").status_code == 400


def test_purge_trash_page(wiki_engine, monkeypatch):
    purged = []
    monkeypatch.setattr("src.wiki.trash.wm.delete_by_expr", lambda expr: purged.append(expr) or 0)
    with TestClient(main.app) as client:
        page = _page(client)
        pid = page["id"]
        client.delete(f"/api/wiki/pages/{pid}")
        assert client.delete(f"/api/wiki/trash/{pid}").status_code == 200
        assert client.get("/api/wiki/trash").json() == []
        assert client.get(f"/api/wiki/pages/{pid}").status_code == 404
    assert purged and f'page_id == "{pid}"' in purged[0]


def test_purge_expired_trash(wiki_engine):
    with TestClient(main.app) as client:
        page = _page(client)
        pid = page["id"]
        client.delete(f"/api/wiki/pages/{pid}")
        # backdate the soft-delete so it's past retention
        with wdb.session_scope() as s:
            s.execute(
                update(Page).where(Page.id == pid).values(
                    deleted_at=datetime.now(timezone.utc) - timedelta(days=40)
                )
            )
        assert pid in trash.purge_expired_trash(retention_days=30)
        assert client.get("/api/wiki/trash").json() == []


def test_search_excludes_deleted(wiki_engine, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "src.wiki.api.wm.search",
        lambda vec, filter_expr="", limit=20, offset=0: seen.update(expr=filter_expr) or [],
    )
    monkeypatch.setattr("src.embedder.get_embedding", lambda q: [0.1] * 512)
    with TestClient(main.app) as client:
        page = _page(client)
        pid = page["id"]
        # publish it so it's searchable, then soft-delete
        monkeypatch.setattr("src.wiki.api._dispatch_wiki_task", lambda *a, **k: types.SimpleNamespace(id="t"))
        client.post(f"/api/wiki/pages/{pid}/publish")
        with wdb.session_scope() as s:
            s.execute(update(Page).where(Page.id == pid).values(
                status="published", deleted_at=datetime.now(timezone.utc),
            ))
        body = client.get("/api/wiki/search", params={"q": "x"}).json()
        # published-revision set excludes deleted pages → empty result
        assert body == {"query": "x", "results": [], "total": 0}


def test_purge_clears_dependents(wiki_engine, monkeypatch):
    """purge with attachments/comments/links/children must not FK-500 (P3 修复)."""
    import shutil
    from pathlib import Path

    from src.config import config as app_config

    shutil.rmtree(app_config.attachments_dir_abs, ignore_errors=True)
    purged_expr = []
    monkeypatch.setattr("src.wiki.trash.wm.delete_by_expr", lambda expr: purged_expr.append(expr) or 0)
    with TestClient(main.app) as client:
        sid = client.get("/api/wiki/spaces").json()[0]["id"]
        parent = client.post("/api/wiki/pages", json={"space_id": sid, "title": "父"}).json()
        child = client.post("/api/wiki/pages", json={"space_id": sid, "title": "子", "parent_page_id": parent["id"]}).json()
        # attachment (with a real file on disk)
        att = client.post(f"/api/wiki/pages/{parent['id']}/attachments", files={"file": ("a.txt", b"data", "text/plain")}).json()
        # comment
        client.post(f"/api/wiki/pages/{parent['id']}/comments", json={"content": "评论"})
        # another page links TO parent
        x = client.post("/api/wiki/pages", json={"space_id": sid, "title": "X"}).json()
        client.put(f"/api/wiki/pages/{x['id']}", json={"content": "见 [[父]]"})
        assert client.get(f"/api/wiki/pages/{parent['id']}/backlinks").json()

        client.delete(f"/api/wiki/pages/{parent['id']}")  # soft-delete subtree
        assert client.delete(f"/api/wiki/trash/{parent['id']}").status_code == 200

        # subtree gone, dependents gone, disk file gone, links cleared
        assert client.get(f"/api/wiki/pages/{parent['id']}").status_code == 404
        assert client.get(f"/api/wiki/pages/{child['id']}").status_code == 404
        assert not Path(app_config.attachments_dir_abs, att["id"]).exists()
        assert client.get(f"/api/wiki/pages/{x['id']}/links").json() == []
    assert purged_expr  # vector purge was invoked


def test_soft_delete_and_restore_subtree(wiki_engine):
    with TestClient(main.app) as client:
        sid = client.get("/api/wiki/spaces").json()[0]["id"]
        parent = client.post("/api/wiki/pages", json={"space_id": sid, "title": "父"}).json()
        child = client.post("/api/wiki/pages", json={"space_id": sid, "title": "子", "parent_page_id": parent["id"]}).json()
        # delete parent → whole subtree trashed (no orphans)
        client.delete(f"/api/wiki/pages/{parent['id']}")
        assert client.get(f"/api/wiki/pages/{parent['id']}").status_code == 404
        assert client.get(f"/api/wiki/pages/{child['id']}").status_code == 404
        trash = client.get("/api/wiki/trash").json()
        assert {t["id"] for t in trash} == {parent["id"], child["id"]}
        # restore parent → subtree restored
        assert client.post(f"/api/wiki/trash/{parent['id']}/restore").status_code == 200
        assert client.get(f"/api/wiki/pages/{parent['id']}").status_code == 200
        assert client.get(f"/api/wiki/pages/{child['id']}").status_code == 200


def test_trashed_page_attachment_isolated(wiki_engine):
    with TestClient(main.app) as client:
        pid = _page(client)["id"]
        att = client.post(f"/api/wiki/pages/{pid}/attachments", files={"file": ("a.txt", b"data", "text/plain")}).json()
        client.delete(f"/api/wiki/pages/{pid}")
        # attachment of a trashed page is not downloadable/removable
        assert client.get(f"/api/wiki/attachments/{att['id']}/download").status_code == 404
        assert client.delete(f"/api/wiki/attachments/{att['id']}").status_code == 404
