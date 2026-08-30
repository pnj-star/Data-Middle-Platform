"""Page-to-page links / backlinks (ROADMAP P3-T4).

Wiki-style `[[target]]` references are parsed from page markdown when a page is
saved. A link to an existing page resolves to its id; a link whose target does
not exist yet is kept as a forward reference (target_slug). Backlinks are the
reverse: pages whose links point at a given page.
"""
from __future__ import annotations

import re

from sqlalchemy import delete, select

from src.wiki.models import Link, Page

# Matches `[[target]]`; the target may contain anything except `]]`.
_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def extract_links(markdown: str | None) -> list[str]:
    """Ordered, deduped list of `[[target]]` references in markdown."""
    if not markdown:
        return []
    seen: list[str] = []
    for m in _LINK_RE.findall(markdown):
        target = m.strip()
        if target and target not in seen:
            seen.append(target)
    return seen


def rebuild_links(session, page: Page, content: str) -> None:
    """Replace a page's outgoing links with ones parsed from `content`.

    Resolution order: exact title, then slug, within the page's own space.
    Unresolved targets become forward references (target_slug only).
    """
    session.execute(delete(Link).where(Link.source_page_id == page.id))
    for target in extract_links(content):
        resolved = session.execute(
            select(Page).where(
                Page.space_id == page.space_id,
                Page.deleted_at.is_(None),
                (Page.title == target) | (Page.slug == target),
            )
        ).scalars().first()
        session.add(
            Link(
                source_page_id=page.id,
                target_page_id=resolved.id if resolved else None,
                target_slug=None if resolved else target,
                label=target,
            )
        )
