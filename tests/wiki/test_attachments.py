"""Attachment tests (ROADMAP P3-T3): upload / list / download / delete + ACL."""
from __future__ import annotations

import shutil
import sys
import types

_fake_converter = types.ModuleType("src.converter")
_fake_converter.SUPPORTED_EXTENSIONS = {".pdf", ".md"}
sys.modules["src.converter"] = _fake_converter

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

import main  # noqa: E402
from src.config import config as app_config  # noqa: E402
from src.wiki import database as wdb  # noqa: E402
from src.wiki.models import Attachment  # noqa: E402
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
    shutil.rmtree(app_config.attachments_dir_abs, ignore_errors=True)  # drop test files


def _page_id(client):
    sid = client.get("/api/wiki/spaces").json()[0]["id"]
    return client.post("/api/wiki/pages", json={"space_id": sid, "title": "附件页"}).json()["id"]


def test_upload_list_download_delete(wiki_engine):
    with TestClient(main.app) as client:
        pid = _page_id(client)
        r = client.post(
            f"/api/wiki/pages/{pid}/attachments",
            files={"file": ("报告.txt", b"hello attachment", "text/plain")},
        )
        assert r.status_code == 200, r.text
        att = r.json()
        assert att["original_name"] == "报告.txt"
        assert att["size"] == len(b"hello attachment")
        assert att["mime_type"] == "text/plain"

        listed = client.get(f"/api/wiki/pages/{pid}/attachments").json()
        assert [a["id"] for a in listed] == [att["id"]]

        dl = client.get(f"/api/wiki/attachments/{att['id']}/download")
        assert dl.status_code == 200
        assert dl.content == b"hello attachment"
        # non-ASCII filename is RFC-5987 encoded (filename*=UTF-8''...), so just
        # require a content-disposition attachment header
        assert "attachment" in (dl.headers.get("content-disposition") or "")

        assert client.delete(f"/api/wiki/attachments/{att['id']}").status_code == 200
        assert client.get(f"/api/wiki/pages/{pid}/attachments").json() == []


def test_upload_requires_write(wiki_engine, monkeypatch):
    from src.config import config

    monkeypatch.setattr(config.security, "api_key", "super-key")
    with TestClient(main.app) as client:
        # register + add as reader
        assert client.post("/api/wiki/auth/register", json={"username": "att_reader", "password": "pw123456"}).status_code == 200
        tok = client.post("/api/wiki/auth/login", json={"username": "att_reader", "password": "pw123456"}).json()["access_token"]
        me = client.get("/api/wiki/auth/me", headers={"Authorization": f"Bearer {tok}"}).json()
        sid = client.get("/api/wiki/spaces", headers={"X-API-Key": "super-key"}).json()
        sid = next(s["id"] for s in sid if s["slug"] == "default")
        client.post(f"/api/wiki/spaces/{sid}/members", json={"user_id": me["id"], "role": "reader"},
                    headers={"X-API-Key": "super-key"})
        # reader can list, cannot upload
        pid = client.post("/api/wiki/pages", json={"space_id": sid, "title": "P"},
                          headers={"X-API-Key": "super-key"}).json()["id"]
        auth = {"Authorization": f"Bearer {tok}"}
        assert client.get(f"/api/wiki/pages/{pid}/attachments", headers=auth).status_code == 200
        assert client.post(f"/api/wiki/pages/{pid}/attachments", files={"file": ("a.txt", b"x", "text/plain")},
                           headers=auth).status_code == 403
