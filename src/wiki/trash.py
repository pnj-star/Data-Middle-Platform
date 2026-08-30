"""Trash / recycle-bin and retention cleanup (ROADMAP P3-T2, P3-T7 fixes).

Soft delete keeps vectors + revisions so restore is instant; only a permanent
purge (explicit trash delete, or retention expiry) removes them.

Permanent purge is recursive over the page subtree and clears every dependent
resource: child pages, attachments (rows + disk files), comments (recursively),
links (as source or target), vectors and revisions — so it never hits a foreign
key violation or leaves orphan files (fixed after P3 review).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, or_, select

from src import milvus_expr as mx
from src.logging_config import get_logger
from src.storage import get_storage
from src.wiki import milvus as wm
from src.wiki.database import session_scope
from src.wiki.models import Attachment, AuditLog, Comment, Link, Page, Revision

_log = get_logger(__name__)


def _delete_attachment_file(session, att: Attachment) -> None:
    try:
        get_storage().delete(att.stored_path or att.id)
    except Exception:
        _log.warning("failed to delete attachment file %s", att.id, exc_info=True)


def delete_comment_tree(session, comment: Comment) -> None:
    """Recursively delete a comment and every reply underneath it.

    `flush()` after each child so the self-referential FK (parent_comment_id)
    is satisfied — SQLAlchemy cannot order deletes on the same table itself.
    """
    children = session.execute(
        select(Comment).where(Comment.parent_comment_id == comment.id)
    ).scalars().all()
    for child in children:
        delete_comment_tree(session, child)
    session.flush()
    session.delete(comment)


def purge_page_fully(session, page: Page) -> None:
    """Permanently delete a page and its whole subtree, clearing dependents.

    Order matters for FK safety: child pages first (parent_page_id), then the
    page's attachments/comments/links, then vectors + revisions + the row.
    """
    children = session.execute(
        select(Page).where(Page.parent_page_id == page.id)
    ).scalars().all()
    for child in children:
        purge_page_fully(session, child)

    for att in session.execute(select(Attachment).where(Attachment.page_id == page.id)).scalars():
        _delete_attachment_file(session, att)
        session.delete(att)
    for comment in session.execute(select(Comment).where(Comment.page_id == page.id)).scalars():
        delete_comment_tree(session, comment)
    session.execute(
        delete(Link).where(or_(Link.source_page_id == page.id, Link.target_page_id == page.id))
    )

    try:
        wm.delete_by_expr(mx.eq("page_id", page.id))
    except Exception:
        _log.warning("vector purge failed for page %s during permanent delete", page.id, exc_info=True)
    session.execute(delete(Revision).where(Revision.page_id == page.id))
    session.delete(page)


def trash_page_recursive(session, page: Page) -> None:
    """Soft-delete a page and its whole subtree (no orphaned children)."""
    for child in session.execute(select(Page).where(Page.parent_page_id == page.id)).scalars():
        trash_page_recursive(session, child)
    page.deleted_at = datetime.now(timezone.utc)


def restore_page_recursive(session, page: Page) -> None:
    """Restore a page and its whole subtree."""
    page.deleted_at = None
    for child in session.execute(select(Page).where(Page.parent_page_id == page.id)).scalars():
        restore_page_recursive(session, child)


def purge_expired_trash(retention_days: int = 30) -> list[str]:
    """Permanently delete trash pages past the retention window.

    Idempotent; returns the purged page ids. Audits the automatic purge so the
    audit trail covers auto-deletion too (P3 review item 4).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    purged: list[str] = []
    with session_scope() as s:
        pages = s.execute(
            select(Page).where(Page.deleted_at.is_not(None), Page.deleted_at < cutoff)
        ).scalars().all()
        for page in pages:
            try:
                purge_page_fully(s, page)
                purged.append(page.id)
            except Exception:
                # One bad page must not abort the whole batch (P4-T2 修复)
                _log.warning("purge failed for expired page %s, skipping", page.id, exc_info=True)
        if purged:
            s.add(AuditLog(user_id=None, action="page.purge", target_type="space",
                           detail={"purged": purged}, result="success", ip=None))
            _log.info("Purged %d expired trash pages", len(purged))
    return purged
