"""Wiki test-DB bootstrap sanity checks (ROADMAP P0-T6).

These exercise the real-Postgres fixtures: schema migrated to head, table set
complete, and per-test transaction isolation.
"""
from __future__ import annotations

from sqlalchemy import inspect, select

from src.wiki.models import Page, Space, User


EXPECTED_TABLES = {
    "spaces", "pages", "revisions", "users", "roles", "space_members",
    "attachments", "links", "comments", "audit_logs",
}


def test_wiki_tables_exist(wiki_engine):
    """Migrated test DB has all 10 wiki tables (not SQLite shims)."""
    names = set(inspect(wiki_engine).get_table_names())
    missing = EXPECTED_TABLES - names
    assert not missing, f"missing tables: {missing}"
    # dialect is real Postgres
    assert inspect(wiki_engine).dialect.name == "postgresql"


def test_session_insert(wiki_session):
    """Insert + flush yields an id; client-side defaults apply."""
    u = User(username="t1")
    wiki_session.add(u)
    wiki_session.flush()
    assert u.id and len(u.id) == 32
    assert u.status == "active" and u.provider == "local"


def test_session_rollback_isolation(wiki_session):
    """Writes from another test are not visible — no cross-test pollution."""
    users = wiki_session.execute(select(User.username)).scalars().all()
    assert "t1" not in users


def test_full_chain(wiki_session):
    """user → space → page → revision → publish pointer round-trip."""
    u = User(username="chain")
    wiki_session.add(u)
    wiki_session.flush()
    # unique slug — the seeded default space may already exist from other tests
    sp = Space(slug="chain-space", name="Chain", owner_user_id=u.id)
    wiki_session.add(sp)
    wiki_session.flush()
    p = Page(space_id=sp.id, title="首页")
    wiki_session.add(p)
    wiki_session.flush()
    from src.wiki.models import Revision

    r = Revision(page_id=p.id, revision_id=1, content_md="# 首页")
    wiki_session.add(r)
    wiki_session.flush()
    p.current_revision_id = r.id
    wiki_session.flush()

    row = wiki_session.execute(
        select(Page.title, Revision.revision_id, Revision.content_md)
        .join(Revision, Revision.id == Page.current_revision_id)
    ).one()
    assert row == ("首页", 1, "# 首页")


def test_session_scope_commit_and_rollback(wiki_engine):
    """session_scope commits on success, rolls back on error (P1-T1)."""
    from sqlalchemy import select

    from src.wiki import database as wdb
    from src.wiki.models import User

    with wdb.session_scope() as s:
        s.add(User(username="scope_commit"))
    try:
        with wdb.session_scope() as s:
            s.add(User(username="scope_rollback"))
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    with wdb.session_scope() as s:
        names = s.execute(select(User.username)).scalars().all()
    assert "scope_commit" in names
    assert "scope_rollback" not in names


def test_get_db_dependency(wiki_engine):
    """FastAPI get_db dependency yields a working session (P1-T1)."""
    from sqlalchemy import text

    from src.wiki import database as wdb

    gen = wdb.get_db()
    session = next(gen)
    assert session.execute(text("SELECT 1")).scalar() == 1
    gen.close()
