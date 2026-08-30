"""Wiki publish flow tests (ROADMAP P1-T5).

Milvus + embedder are mocked; Postgres is real (wiki_engine). Covers the state
machine, insert-then-delete, pointer advance, failure path, stale recovery and
the cleanup task.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

# Avoid main's heavy converter/embedder import (same trick as test_api.py).
_fake_converter = types.ModuleType("src.converter")
_fake_converter.SUPPORTED_EXTENSIONS = {".pdf", ".md"}
sys.modules["src.converter"] = _fake_converter

import pytest  # noqa: E402
from sqlalchemy import select, update  # noqa: E402

from src.wiki import database as wdb  # noqa: E402
from src.wiki import publish  # noqa: E402
from src.wiki.models import (  # noqa: E402
    PAGE_STATUS_DRAFT,
    PAGE_STATUS_PUBLISHED,
    PAGE_STATUS_PUBLISH_FAILED,
    PAGE_STATUS_PUBLISHING,
    Page,
    Revision,
    Space,
    User,
)

_TRUNCATE = (
    "TRUNCATE TABLE audit_logs, space_members, links, comments, attachments, "
    "revisions, pages, spaces, users, roles RESTART IDENTITY CASCADE"
)


@pytest.fixture(autouse=True)
def _clean(wiki_engine):
    """publish_revision commits via session_scope — isolate between tests."""
    from sqlalchemy import text

    with wdb.get_engine().begin() as conn:
        conn.execute(text(_TRUNCATE))
    yield


def _seed_page(title="P", content="# 第一版"):
    """page + two revisions (rev 2 supersedes rev 1). Returns (page, rev2, space_id)."""
    with wdb.session_scope() as s:
        u = User(username="p_usr")
        s.add(u)
        s.flush()
        sp = Space(slug="p-space", name="S", owner_user_id=u.id)
        s.add(sp)
        s.flush()
        page = Page(space_id=sp.id, title=title, created_by=u.id, updated_by=u.id)
        s.add(page)
        s.flush()
        r1 = Revision(page_id=page.id, revision_id=1, content_md=content, editor_user_id=u.id)
        s.add(r1)
        s.flush()
        r2 = Revision(page_id=page.id, revision_id=2, content_md=content + "\n\n## 更新部分", editor_user_id=u.id)
        s.add(r2)
        s.flush()
        return page.id, r2.id, sp.id


def _mock_side(monkeypatch):
    """Mock embedder + milvus, returning recording lists."""
    inserted, deleted, counted = [], [], []

    def fake_embeddings(texts):
        return [[0.1] * 512 for _ in texts]

    monkeypatch.setattr(publish.embedder, "get_embeddings", fake_embeddings)
    monkeypatch.setattr(publish.wm, "insert_vectors", lambda ents: inserted.extend(ents) or len(ents))
    monkeypatch.setattr(publish.wm, "delete_by_expr", lambda expr: deleted.append(expr) or 0)
    monkeypatch.setattr(publish.wm, "count", lambda expr: counted.append(expr) or 0)
    return inserted, deleted, counted


def test_publish_success_insert_then_delete(wiki_engine, monkeypatch):
    pid, rev2_row_id, sid = _seed_page()
    inserted, deleted, _ = _mock_side(monkeypatch)

    n = publish.publish_revision(pid, 2)

    assert n == len(inserted) > 0
    # entities carry the wiki traceability fields (D3/D14)
    assert all(e["page_id"] == pid and e["revision_id"] == 2 and e["space_id"] == sid for e in inserted)
    assert all("content" in e and "parent_title" in e and "chunk_index" in e for e in inserted)
    # insert-then-delete (D9): clear-own (idempotent) then delete-old (other revs)
    assert deleted and any("revision_id in [2]" in d for d in deleted)
    assert deleted and any("revision_id != 2" in d for d in deleted)

    with wdb.session_scope() as s:
        page = s.get(Page, pid)
        assert page.status == PAGE_STATUS_PUBLISHED
        assert page.current_revision_id == rev2_row_id  # pointer advanced (D10)


def test_publish_failure_marks_publish_failed(wiki_engine, monkeypatch):
    pid, _row, _sid = _seed_page()

    def boom(_):
        raise RuntimeError("embed failed")

    monkeypatch.setattr(publish.embedder, "get_embeddings", boom)
    monkeypatch.setattr(publish.wm, "insert_vectors", lambda e: 0)
    monkeypatch.setattr(publish.wm, "delete_by_expr", lambda e: 0)
    monkeypatch.setattr(publish.wm, "count", lambda e: 0)

    with pytest.raises(RuntimeError, match="embed failed"):
        publish.publish_revision(pid, 2)
    with wdb.session_scope() as s:
        assert s.get(Page, pid).status == PAGE_STATUS_PUBLISH_FAILED


def test_recover_stale_publish_vectors_present(wiki_engine, monkeypatch):
    pid, rev2_row_id, _sid = _seed_page()
    inserted, deleted, counted = _mock_side(monkeypatch)

    with wdb.session_scope() as s:
        s.execute(
            update(Page).where(Page.id == pid).values(
                status=PAGE_STATUS_PUBLISHING,
                updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
            )
        )
    counted.clear()  # next count() returns 1 → vectors present

    monkeypatch.setattr(publish.wm, "count", lambda expr: 1)
    recovered = publish.recover_stale_publishes(stale_seconds=3600)

    assert pid in recovered
    with wdb.session_scope() as s:
        page = s.get(Page, pid)
        assert page.status == PAGE_STATUS_PUBLISHED
        assert page.current_revision_id == rev2_row_id  # publish actually completed


def test_recover_stale_publish_no_vectors(wiki_engine, monkeypatch):
    pid, _rev2_row_id, _sid = _seed_page()
    _, deleted, _ = _mock_side(monkeypatch)

    with wdb.session_scope() as s:
        s.execute(
            update(Page).where(Page.id == pid).values(
                status=PAGE_STATUS_PUBLISHING,
                updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
            )
        )
    # count stays 0 → no vectors → back to draft + purge partials
    recovered = publish.recover_stale_publishes(stale_seconds=3600)

    assert pid in recovered
    with wdb.session_scope() as s:
        assert s.get(Page, pid).status == PAGE_STATUS_DRAFT
    assert deleted and f'page_id == "{pid}"' in deleted[-1]  # partial-vector purge


def test_cleanup_page_vectors(wiki_engine, monkeypatch):
    _, deleted, _ = _mock_side(monkeypatch)
    publish.cleanup_page_vectors("pid", keep_revision_id=5)
    assert "revision_id != 5" in deleted[0]
    publish.cleanup_page_vectors("pid")
    assert deleted[1] == 'page_id == "pid"'


def test_publish_delete_old_retries_cleanup(wiki_engine, monkeypatch):
    """delete-old failure triggers an in-process cleanup retry (P1-T5 修复)."""
    pid, _row, _sid = _seed_page()
    calls = []

    def flaky(expr):
        calls.append(expr)
        if len(calls) == 2:  # the delete-old (!=2) call fails once; clear-own (in[2]) succeeds
            raise RuntimeError("milvus hiccup")
        return 0

    monkeypatch.setattr(publish.embedder, "get_embeddings", lambda t: [[0.1] * 512 for _ in t])
    monkeypatch.setattr(publish.wm, "insert_vectors", lambda e: len(e))
    monkeypatch.setattr(publish.wm, "delete_by_expr", flaky)
    monkeypatch.setattr(publish.wm, "count", lambda e: 0)

    n = publish.publish_revision(pid, 2)
    assert n > 0
    # calls: clear-own (in [2]) → delete-old (!=2, raised) → cleanup retry (!=2)
    assert len(calls) == 3
    assert any("revision_id != 2" in c for c in calls[1:])  # delete-old retried
    with wdb.session_scope() as s:
        assert s.get(Page, pid).status == PAGE_STATUS_PUBLISHED  # publish still succeeds


def test_publish_intra_page_dedup(wiki_engine, monkeypatch):
    """Duplicate knowledge unit inside a page is embedded once (P3-T7, D7)."""
    content = "# 标题\n\n## A\n同一段重复内容\n\n## B\n同一段重复内容"
    pid, _row, _sid = _seed_page(content=content)
    inserted, _, _ = _mock_side(monkeypatch)

    n = publish.publish_revision(pid, 2)
    assert n == len(inserted) >= 1
    contents = [e["content"] for e in inserted]
    assert len(contents) == len(set(contents))  # the repeated unit stored only once


def test_publish_is_idempotent(wiki_engine, monkeypatch):
    """Re-publishing the same revision clears its own vectors first (P4-T6)."""
    pid, _row, _sid = _seed_page()
    inserted, deleted, _ = _mock_side(monkeypatch)

    publish.publish_revision(pid, 2)
    first_ids = [e["id"] for e in inserted]
    publish.publish_revision(pid, 2)  # re-run with multiple workers safe
    second_ids = [e["id"] for e in inserted[len(first_ids):]]

    assert any("revision_id in [2]" in d for d in deleted)  # own-revision cleared before insert
    assert any("revision_id != 2" in d for d in deleted)    # old-version cleanup still runs
    assert len(first_ids) == len(set(first_ids))            # within one run ids are unique
    assert first_ids == second_ids                          # re-run reproduces identical ids


def test_publish_api_dispatches_and_conflicts(wiki_engine, monkeypatch):
    import main
    from fastapi.testclient import TestClient

    calls = []
    monkeypatch.setattr(
        "src.wiki.api._dispatch_wiki_task",
        lambda name, *a, **k: calls.append((name, a)) or SimpleNamespace(id="t1"),
    )
    with TestClient(main.app) as client:
        spaces = client.get("/api/wiki/spaces").json()
        sid = spaces[0]["id"]
        page = client.post("/api/wiki/pages", json={"space_id": sid, "title": "可发布", "content": "# 内容"}).json()

        r = client.post(f"/api/wiki/pages/{page['id']}/publish")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "publishing" and body["task_id"] == "t1"
        assert calls and calls[0] == ("publish_wiki_page", (page["id"], 1))

        # already publishing → 409
        assert client.post(f"/api/wiki/pages/{page['id']}/publish").status_code == 409


def test_publish_api_empty_page_rejected(wiki_engine, monkeypatch):
    import main
    from fastapi.testclient import TestClient

    monkeypatch.setattr("src.wiki.api._dispatch_wiki_task", lambda *a, **k: SimpleNamespace(id="x"))
    with TestClient(main.app) as client:
        spaces = client.get("/api/wiki/spaces").json()
        page = client.post("/api/wiki/pages", json={"space_id": spaces[0]["id"], "title": "空"}).json()
        # content is "" → the page still has a revision (rev 1 with empty md).
        # Force "no revision": delete revisions via direct DB, then publish → 400.
        with wdb.session_scope() as s:
            from sqlalchemy import delete

            s.execute(delete(Revision).where(Revision.page_id == page["id"]))
        assert client.post(f"/api/wiki/pages/{page['id']}/publish").status_code == 400
