"""Wiki API router — pages / revisions / spaces CRUD (ROADMAP P1-T2).

Mounted at ``/api/wiki/*`` by main.py. v1 is single-user: mutating endpoints
require the same X-API-Key as the legacy pipeline, and all writes are
attributed to the seeded `system` user.

Vector lifecycle (publish delete-old-vectors, purge on page delete) lands in
P1-T5; this task is pure DB CRUD.
"""
from __future__ import annotations

import difflib
import hashlib
import secrets
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from src import db as pipeline_db
from src import milvus_expr as mx
from src.config import config as app_config
from src.logging_config import get_logger
from src.rate_limit import redis_client
from src.storage import get_storage
from src.wiki import milvus as wm
from src.wiki.acl import (
    AuthContext,
    accessible_space_ids,
    get_auth_context,
    require_space,
    require_space_owner,
    system_or_user_id,
)
from src.wiki.audit import client_ip, write_audit
from src.wiki.auth import get_current_user
from src.wiki.database import get_db
from src.wiki.links import rebuild_links
from src.wiki.models import (
    PAGE_STATUS_PUBLISHED,
    PAGE_STATUS_PUBLISHING,
    USER_STATUS_ACTIVE,
    ApiKey,
    Attachment,
    AuditLog,
    Comment,
    Link,
    Page,
    Revision,
    Role,
    Space,
    SpaceMember,
    User,
    new_id,
)
from src.wiki.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    AttachmentOut,
    AuditLogOut,
    BacklinkOut,
    CommentCreate,
    CommentOut,
    ImportRequest,
    LinkOut,
    LoginRequest,
    MemberAdd,
    MemberOut,
    PageCreate,
    PageNode,
    PageOut,
    PageUpdate,
    RegisterRequest,
    RevisionOut,
    SpaceCreate,
    SpaceOut,
    TokenResponse,
    TrashItemOut,
    UserOut,
)
from src.wiki.seed import SYSTEM_USERNAME, ensure_seed_data
from src.wiki.trash import delete_comment_tree, purge_page_fully, restore_page_recursive, trash_page_recursive

router = APIRouter(prefix="/api/wiki", tags=["wiki"])

_log = get_logger(__name__)


def _dispatch_wiki_task(name: str, *args, **kwargs):
    """Publish a Celery task by name without importing tasks.celery_worker
    (which pulls in sentence-transformers). Same approach as
    main._dispatch_task — resolution happens in the worker."""
    from celery import Celery

    probe = Celery("pipeline", broker=app_config.redis_url)
    return probe.send_task(name, args=args, kwargs=kwargs)


# Login brute-force limiter reuses the shared Redis client from src.rate_limit
# (P4-T2); aliased so tests can keep monkeypatching src.wiki.api.
_rate_limiter_redis = redis_client

def _require_api_key(x_api_key: str | None = Header(default=None)):
    """Same guard as main.py._require_api_key: no-op unless API_KEY is set."""
    key = app_config.security.api_key
    if key and x_api_key != key:
        raise HTTPException(401, "Invalid or missing X-API-Key")


def _system_user_id(session: Session) -> str:
    """Id of the seeded system user (self-healing: seeds if missing)."""
    uid = session.execute(
        select(User.id).where(User.username == SYSTEM_USERNAME)
    ).scalar_one_or_none()
    if uid is None:
        ensure_seed_data(session)
        session.flush()
        uid = session.execute(
            select(User.id).where(User.username == SYSTEM_USERNAME)
        ).scalar_one_or_none()
    if uid is None:
        raise HTTPException(503, "seed data unavailable")
    return uid


def _get_page(session: Session, page_id: str) -> Page:
    page = session.get(Page, page_id)
    if page is None or page.deleted_at is not None:
        raise HTTPException(404, "Page not found")
    return page


def _latest_content(session: Session, page_id: str) -> str:
    """Content of the highest-numbered revision (what the editor edits)."""
    return (
        session.execute(
            select(Revision.content_md)
            .where(Revision.page_id == page_id)
            .order_by(Revision.revision_id.desc())
            .limit(1)
        ).scalar()
        or ""
    )


def _page_out(session: Session, page: Page) -> PageOut:
    return PageOut(
        id=page.id,
        space_id=page.space_id,
        parent_page_id=page.parent_page_id,
        title=page.title,
        slug=page.slug,
        content_type=page.content_type,
        status=page.status,
        current_revision_id=page.current_revision_id,
        content=_latest_content(session, page.id),
        source_file_name=page.source_file_name,
        source_file_extension=page.source_file_extension,
        created_at=page.created_at,
        updated_at=page.updated_at,
    )


def _build_tree(pages: list[Page]) -> list[PageNode]:
    """Group pages by parent_page_id into a nested tree (roots = no parent)."""
    by_parent: dict[str | None, list[Page]] = {}
    for p in pages:
        by_parent.setdefault(p.parent_page_id, []).append(p)

    def node(p: Page) -> PageNode:
        children = sorted(by_parent.get(p.id, []), key=lambda x: x.title)
        return PageNode(
            id=p.id,
            title=p.title,
            status=p.status,
            parent_page_id=p.parent_page_id,
            has_current_revision=p.current_revision_id is not None,
            children=[node(c) for c in children],
        )

    roots = sorted(by_parent.get(None, []), key=lambda x: x.title)
    return [node(p) for p in roots]


def _compute_diff(a: str, b: str) -> list[dict]:
    """Line-level diff between two markdown strings (ROADMAP P3-T1).

    Returns ordered lines the frontend renders directly: `equal` / `delete` /
    `insert`, with `replace` expanded into a delete followed by an insert so the
    client needs no grouping logic.
    """
    a_lines = a.splitlines()
    b_lines = b.splitlines()
    matcher = difflib.SequenceMatcher(None, a_lines, b_lines)
    lines: list[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for line in a_lines[i1:i2]:
                lines.append({"op": "equal", "text": line})
        elif tag == "delete":
            for line in a_lines[i1:i2]:
                lines.append({"op": "delete", "text": line})
        elif tag == "insert":
            for line in b_lines[j1:j2]:
                lines.append({"op": "insert", "text": line})
        elif tag == "replace":
            for line in a_lines[i1:i2]:
                lines.append({"op": "delete", "text": line})
            for line in b_lines[j1:j2]:
                lines.append({"op": "insert", "text": line})
    return lines


# ── Auth (Phase 2, P2-T1) ─────────────────────────────────────────

@router.post("/auth/register", response_model=UserOut)
async def register(
    body: RegisterRequest,
    session: Session = Depends(get_db),
    request: Request = None,
):
    from src.wiki.auth import hash_password, require_jwt_configured

    require_jwt_configured()
    if not app_config.security.allow_registration:
        raise HTTPException(403, "注册已关闭")
    existing = session.execute(
        select(User).where(User.username == body.username)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, "用户名已存在")
    user = User(
        username=body.username,
        email=body.email,
        password_hash=hash_password(body.password),
    )
    session.add(user)
    write_audit(session, "auth.register", "user", target_id=user.id, user_id=user.id,
                ip=client_ip(request))
    session.commit()
    return user


@router.post("/auth/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    session: Session = Depends(get_db),
    request: Request = None,
):
    from src.wiki.auth import create_token, require_jwt_configured, verify_password

    require_jwt_configured()
    # Anti-enumeration rate limit (P4-T2, P2-T4 carry-over): per-username failed
    # attempts, Redis-backed. Redis down → degraded open (fail-lenient).
    rate_key = f"wiki:login_fail:{body.username.lower()}"
    r = None
    try:
        r = _rate_limiter_redis()
        if int(r.get(rate_key) or 0) >= app_config.security.login_max_attempts:
            raise HTTPException(429, "尝试次数过多，请稍后再试")
    except HTTPException:
        raise
    except Exception:
        r = None

    user = session.execute(
        select(User).where(User.username == body.username)
    ).scalar_one_or_none()
    if user is None or not user.password_hash or not verify_password(body.password, user.password_hash):
        if r is not None:
            r.incr(rate_key)
            r.expire(rate_key, app_config.security.login_lockout_seconds)
        write_audit(session, "auth.login", "user", user_id=user.id if user else None,
                    ip=client_ip(request), result="failure", detail={"username": body.username})
        session.commit()
        raise HTTPException(401, "用户名或密码错误")
    if user.status != USER_STATUS_ACTIVE:
        write_audit(session, "auth.login", "user", user_id=user.id,
                    ip=client_ip(request), result="failure", detail={"username": body.username, "reason": "disabled"})
        session.commit()
        raise HTTPException(403, "用户已禁用")
    if r is not None:
        r.delete(rate_key)  # success clears the counter
    write_audit(session, "auth.login", "user", user_id=user.id, ip=client_ip(request))
    session.commit()
    return TokenResponse(access_token=create_token(user))


@router.get("/auth/me", response_model=UserOut)
async def me(current: User = Depends(get_current_user)):
    return current


# ── Spaces ─────────────────────────────────────────────────────────

@router.get("/spaces", response_model=list[SpaceOut])
async def list_spaces(
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    q = select(Space).order_by(Space.created_at)
    ids = accessible_space_ids(session, ctx)
    if ids is not None:
        q = q.where(Space.id.in_(ids))
    return session.execute(q).scalars().all()


@router.post("/spaces", response_model=SpaceOut)
async def create_space(
    body: SpaceCreate,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
    request: Request = None,
):
    """Create a space. The creating user becomes its owner (superuser/dev
    creates it with the system owner and no membership row)."""
    if app_config.security.api_key and ctx.user is None and not ctx.is_superuser:
        raise HTTPException(401, "Not authenticated")
    if session.execute(select(Space).where(Space.slug == body.slug)).scalar_one_or_none() is not None:
        raise HTTPException(409, "slug 已存在")
    uid = system_or_user_id(ctx, session)
    space = Space(slug=body.slug, name=body.name, description=body.description, owner_user_id=uid)
    session.add(space)
    session.flush()
    if ctx.user is not None:
        owner_role = session.execute(select(Role.id).where(Role.name == "owner")).scalar_one()
        session.add(SpaceMember(space_id=space.id, user_id=ctx.user.id, role_id=owner_role))
    write_audit(session, "space.create", "space", target_id=space.id, user_id=uid,
                detail={"slug": body.slug}, ip=client_ip(request))
    session.commit()
    return space


@router.get("/spaces/{space_id}/members", response_model=list[MemberOut])
async def list_members(
    space_id: str,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    require_space(session, ctx, space_id)
    rows = session.execute(
        select(SpaceMember.user_id, User.username, Role.name)
        .join(User, User.id == SpaceMember.user_id)
        .join(Role, Role.id == SpaceMember.role_id)
        .where(SpaceMember.space_id == space_id)
    ).all()
    return [{"user_id": r.user_id, "username": r.username, "role": r.name} for r in rows]


@router.post("/spaces/{space_id}/members", response_model=MemberOut)
async def add_member(
    space_id: str,
    body: MemberAdd,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
    request: Request = None,
):
    """Add or update a member's role in a space (owner/superuser only)."""
    require_space_owner(session, ctx, space_id)
    if session.get(Space, space_id) is None:
        raise HTTPException(404, "空间不存在")
    user = session.get(User, body.user_id)
    if user is None:
        raise HTTPException(404, "用户不存在")
    rid = session.execute(select(Role.id).where(Role.name == body.role)).scalar_one()
    existing = session.execute(
        select(SpaceMember).where(
            SpaceMember.space_id == space_id,
            SpaceMember.user_id == body.user_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.role_id = rid  # upsert: update the role
    else:
        session.add(SpaceMember(space_id=space_id, user_id=body.user_id, role_id=rid))
    write_audit(session, "space.member_add", "space", target_id=space_id,
                user_id=system_or_user_id(ctx, session),
                detail={"user_id": body.user_id, "role": body.role}, ip=client_ip(request))
    session.commit()
    return {"user_id": body.user_id, "username": user.username, "role": body.role}


@router.delete("/spaces/{space_id}/members/{user_id}")
async def remove_member(
    space_id: str,
    user_id: str,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
    request: Request = None,
):
    """Remove a member from a space (owner/superuser only). An owner cannot
    remove themselves (that would strand the space)."""
    require_space_owner(session, ctx, space_id)
    member = session.execute(
        select(SpaceMember).where(
            SpaceMember.space_id == space_id,
            SpaceMember.user_id == user_id,
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(404, "成员不存在")
    if ctx.user is not None and ctx.user.id == user_id:
        raise HTTPException(400, "不能移除自己")
    session.delete(member)
    write_audit(session, "space.member_remove", "space", target_id=space_id,
                user_id=system_or_user_id(ctx, session),
                detail={"user_id": user_id}, ip=client_ip(request))
    session.commit()
    return {"removed": True}


def _published_revision_pairs(session: Session) -> list[tuple[str, int]]:
    """(page_id, revision_id) pairs for every published page's current revision.

    Semantic of D10/D9: only `pages.status='published'` pages whose
    current_revision_id points at a revision contribute; a revision is only
    marked published AFTER its vectors exist, so this set is exactly the set
    of revisions that have vectors in wiki_knowledge.

    Returns pairs rather than bare revision_ids because revision_id is
    per-page auto-increment — different pages can share the same integer,
    so filtering by `revision_id in [...]` alone would leak old vectors
    from unrelated pages that happen to share a revision number (D11).
    """
    rows = session.execute(
        select(Page.id, Revision.revision_id)
        .join(Page, Page.current_revision_id == Revision.id)
        .where(Page.status == PAGE_STATUS_PUBLISHED, Page.deleted_at.is_(None))
    ).all()
    return list(rows)


def _build_published_filter(pairs: list[tuple[str, int]]) -> str | None:
    """Build a Milvus boolean expr matching exact (page_id, revision_id) pairs.

    Groups pages sharing the same revision_id to minimise clause count:
    ``(page_id in ["a","c"] and revision_id == 1) or (page_id in ["b"] and
    revision_id == 3)`` is shorter than one clause per pair.
    """
    if not pairs:
        return None
    by_rev: dict[int, list[str]] = {}
    for pid, rid in pairs:
        by_rev.setdefault(rid, []).append(pid)
    clauses = [
        f'({mx.in_expr("page_id", pids)} and {mx.eq_int("revision_id", rid)})'
        for rid, pids in sorted(by_rev.items())
    ]
    return " or ".join(clauses)


@router.get("/search")
async def search_wiki(
    q: str = Query(..., min_length=1, max_length=500),
    space_id: str = Query(""),
    keyword: str = Query(""),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    """Search over published wiki pages (ROADMAP P1-T6).

    Pre-filter (D9/D10/D11): the published-revision set is fetched from
    Postgres as exact (page_id, revision_id) pairs and passed to Milvus as
    a composite filter BEFORE ranking. Bare `revision_id in [...]` would let
    old-revision vectors from other pages leak into results when revision
    numbers collide across pages.

    Note: `total` equals the number of results returned in this page. Milvus
    top-K search cannot report a true grand total; with the current frontend
    (no pagination) this is sufficient. Real totals + pagination can be added
    with a scalar count when the search UI grows.
    """
    rev_pairs = _published_revision_pairs(session)
    if not rev_pairs:
        return {"query": q, "results": [], "total": 0}

    rev_filter = _build_published_filter(rev_pairs)
    filters = [f"({rev_filter})"] if rev_filter else []
    ids = accessible_space_ids(session, ctx)
    if ids is not None:
        if not ids:
            return {"query": q, "results": [], "total": 0}
        filters.append(mx.in_expr("space_id", ids))
    elif space_id:
        filters.append(mx.eq("space_id", space_id))
    if keyword:
        filters.append(mx.like("content", keyword))
    expr = mx.and_expr(*filters)

    from src import embedder  # heavy import, defer

    query_vec = embedder.get_embedding(q)
    hits = wm.search(query_vec, filter_expr=expr, limit=limit, offset=offset)

    page_ids = {h.get("page_id") for h in hits}
    titles: dict[str, str] = {}
    if page_ids:
        for p in session.execute(select(Page).where(Page.id.in_(page_ids))).scalars():
            titles[p.id] = p.title
    results = [
        {
            "page_id": h.get("page_id", ""),
            "page_title": titles.get(h.get("page_id", ""), ""),
            "revision_id": h.get("revision_id", 0),
            "space_id": h.get("space_id", ""),
            "content": h.get("content", ""),
            "parent_title": h.get("parent_title", ""),
            "chunk_index": h.get("chunk_index", 0),
        }
        for h in hits
    ]
    return {"query": q, "results": results, "total": len(results)}


# ── Pages ──────────────────────────────────────────────────────────

@router.get("/pages", response_model=list[PageNode])
async def page_tree(
    space_id: str = Query(..., description="space to load the tree for"),
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    require_space(session, ctx, space_id)
    pages = session.execute(
        select(Page)
        .where(Page.space_id == space_id, Page.deleted_at.is_(None))
        .order_by(Page.title)
    ).scalars().all()
    return _build_tree(pages)


@router.get("/pages/{page_id}", response_model=PageOut)
async def get_page(
    page_id: str,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    page = _get_page(session, page_id)
    require_space(session, ctx, page.space_id)
    return _page_out(session, page)


@router.post("/pages", response_model=PageOut)
async def create_page(
    body: PageCreate,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
    request: Request = None,
):
    require_space(session, ctx, body.space_id, write=True)
    space = session.get(Space, body.space_id)
    if space is None:
        raise HTTPException(404, "Space not found")
    if body.parent_page_id:
        parent = session.get(Page, body.parent_page_id)
        if parent is None:
            raise HTTPException(404, "Parent page not found")
        if parent.space_id != body.space_id:
            raise HTTPException(400, "Parent page must be in the same space")

    # Idempotency (P4-T2): a repeated POST with the same key returns the page
    # already created by the first call instead of creating a duplicate. The
    # scope is bound to the target space so the key never collides cross-space.
    idem_key = request.headers.get("Idempotency-Key") if request else None
    idem_scope = body.space_id
    if idem_key:
        from src.rate_limit import idempotency_lookup

        existing_id = idempotency_lookup(idem_key, idem_scope)
        if existing_id:
            existing = session.get(Page, existing_id)
            if (existing is not None and existing.deleted_at is None
                    and existing.space_id == body.space_id):
                return _page_out(session, existing)

    uid = system_or_user_id(ctx, session)
    page = Page(
        space_id=body.space_id,
        parent_page_id=body.parent_page_id,
        title=body.title,
        status="draft",
        created_by=uid,
        updated_by=uid,
    )
    session.add(page)
    session.flush()
    # First revision always gets revision_id=1.
    session.add(
        Revision(
            page_id=page.id,
            revision_id=1,
            content_md=body.content,
            editor_user_id=uid,
        )
    )
    write_audit(session, "page.create", "page", target_id=page.id, user_id=uid,
                detail={"space_id": body.space_id, "title": page.title}, ip=client_ip(request))
    rebuild_links(session, page, body.content)
    session.commit()
    if idem_key:
        from src.rate_limit import idempotency_store

        idempotency_store(idem_key, idem_scope, page.id)
    return _page_out(session, page)


@router.put("/pages/{page_id}", response_model=RevisionOut)
async def update_page(
    page_id: str,
    body: PageUpdate,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
    request: Request = None,
):
    """Save content → produce a new revision. Does NOT publish (D10: the
    `published` status + current_revision_id advance only on publish, P1-T5)."""
    page = _get_page(session, page_id)
    require_space(session, ctx, page.space_id, write=True)
    uid = system_or_user_id(ctx, session)
    next_rev = (
        session.execute(
            select(func.max(Revision.revision_id)).where(Revision.page_id == page_id)
        ).scalar()
        or 0
    ) + 1
    revision = Revision(
        page_id=page_id,
        revision_id=next_rev,
        content_md=body.content,
        note=body.note,
        editor_user_id=uid,
    )
    session.add(revision)
    page.updated_by = uid
    write_audit(session, "page.update", "page", target_id=page_id, user_id=uid,
                detail={"revision_id": next_rev}, ip=client_ip(request))
    rebuild_links(session, page, body.content)
    session.commit()
    return revision


@router.post("/import-from-file/{file_id}", response_model=PageOut)
async def import_from_file(
    file_id: str,
    body: ImportRequest,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
    request: Request = None,
):
    """Create a wiki page draft from a converted pipeline file (P1-T3).

    One-time copy across stores (ROADMAP D12/D16): the SQLite conversion
    markdown is copied into a Postgres page, and the file's id + name +
    extension are snapshotted as provenance. After this the page evolves
    independently of the file. Re-importing the same file into the same space
    is rejected (idempotency).
    """
    f = pipeline_db.get_file(file_id)
    if f is None:
        raise HTTPException(404, "File not found")
    if f.get("type") != "text":
        raise HTTPException(400, "Only text files can be imported into the wiki")
    conv = pipeline_db.get_conversion(file_id)
    if conv is None:
        raise HTTPException(409, "File has no conversion yet — convert it first")

    require_space(session, ctx, body.space_id, write=True)
    space = session.get(Space, body.space_id)
    if space is None:
        raise HTTPException(404, "Space not found")
    if body.parent_page_id:
        parent = session.get(Page, body.parent_page_id)
        if parent is None or parent.space_id != body.space_id:
            raise HTTPException(400, "Parent page invalid for this space")

    already = session.execute(
        select(Page).where(
            Page.space_id == body.space_id,
            Page.source_file_id == file_id,
        )
    ).scalar_one_or_none()
    if already is not None:
        raise HTTPException(409, "This file is already imported to this space")

    uid = system_or_user_id(ctx, session)
    name = f.get("name") or "Untitled"
    title = name
    if "." in name:
        stem, _ext = name.rsplit(".", 1)
        title = stem
    page = Page(
        space_id=body.space_id,
        parent_page_id=body.parent_page_id,
        title=title or name,
        status="draft",
        source_file_id=file_id,
        source_file_name=name,
        source_file_extension=(f.get("extension") or "").lstrip("."),
        created_by=uid,
        updated_by=uid,
    )
    session.add(page)
    session.flush()
    session.add(
        Revision(
            page_id=page.id,
            revision_id=1,
            content_md=conv["markdown"],
            editor_user_id=uid,
        )
    )
    write_audit(session, "page.import", "page", target_id=page.id, user_id=uid,
                detail={"source_file_id": file_id, "space_id": body.space_id}, ip=client_ip(request))
    session.commit()
    return _page_out(session, page)


@router.post("/pages/{page_id}/publish")
async def publish_page(
    page_id: str,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
    request: Request = None,
):
    """Trigger publish of the page's latest revision (ROADMAP P1-T5).

    Flips status to `publishing` and dispatches the Celery task. The task (not
    this endpoint) writes vectors and advances the published pointer; a stale
    `publishing` state is resolved by recover_stale_publishes on startup.
    """
    page = _get_page(session, page_id)
    require_space(session, ctx, page.space_id, write=True)
    latest = session.execute(
        select(Revision)
        .where(Revision.page_id == page_id)
        .order_by(Revision.revision_id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is None:
        raise HTTPException(400, "页面没有内容可发布")

    # Atomic claim (P4-T2 修复): only a page NOT already `publishing` may enter
    # `publishing`, decided by rowcount — safe against concurrent double POSTs.
    claimed = session.execute(
        update(Page)
        .where(Page.id == page_id, Page.status != PAGE_STATUS_PUBLISHING)
        .values(status=PAGE_STATUS_PUBLISHING)
    )
    if claimed.rowcount == 0:
        raise HTTPException(409, "该页面正在发布中")
    write_audit(session, "page.publish", "page", target_id=page_id,
                user_id=system_or_user_id(ctx, session),
                detail={"revision_id": latest.revision_id}, ip=client_ip(request))
    session.commit()
    try:
        task = _dispatch_wiki_task("publish_wiki_page", page_id, latest.revision_id)
    except Exception:
        # broker unreachable: revert only if WE still own `publishing` (don't
        # clobber a concurrent claim), then append a failure audit.
        session.execute(
            update(Page).where(Page.id == page_id, Page.status == PAGE_STATUS_PUBLISHING)
            .values(status="draft")
        )
        write_audit(session, "page.publish", "page", target_id=page_id,
                    user_id=system_or_user_id(ctx, session),
                    detail={"revision_id": latest.revision_id, "error": "broker_unavailable"},
                    ip=client_ip(request), result="failure")
        session.commit()
        raise HTTPException(503, "任务队列不可用，请稍后重试")
    return {"task_id": task.id, "status": "publishing", "revision_id": latest.revision_id}


@router.delete("/pages/{page_id}")
async def delete_page(
    page_id: str,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
    request: Request = None,
):
    """Soft-delete a page into the trash (ROADMAP P3-T2).

    Vectors + revisions are kept so restore is instant; only a permanent purge
    (trash delete or retention expiry) removes them. Soft-deleted pages are
    excluded from the tree, page reads and search.
    """
    page = session.get(Page, page_id)
    if page is None:
        raise HTTPException(404, "Page not found")
    require_space(session, ctx, page.space_id, write=True)
    if page.deleted_at is not None:
        raise HTTPException(400, "页面已在回收站")
    trash_page_recursive(session, page)  # whole subtree into trash (no orphans)
    write_audit(session, "page.trash", "page", target_id=page_id,
                user_id=system_or_user_id(ctx, session), ip=client_ip(request))
    session.commit()
    return {"deleted": True, "id": page_id, "trashed": True}


# ── Trash / recycle bin (Phase 3, P3-T2) ──────────────────────────

@router.get("/trash", response_model=list[TrashItemOut])
async def list_trash(
    space_id: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    """List soft-deleted pages, scoped to the caller's accessible spaces."""
    q = (
        select(Page)
        .where(Page.deleted_at.is_not(None))
        .order_by(Page.deleted_at.desc())
        .limit(limit)
    )
    ids = accessible_space_ids(session, ctx)
    if ids is not None:
        q = q.where(Page.space_id.in_(ids))
    elif space_id:
        q = q.where(Page.space_id == space_id)
    pages = session.execute(q).scalars().all()
    return [
        TrashItemOut(id=p.id, title=p.title, space_id=p.space_id, deleted_at=p.deleted_at)
        for p in pages
    ]


@router.post("/trash/{page_id}/restore")
async def restore_page(
    page_id: str,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
    request: Request = None,
):
    page = session.get(Page, page_id)
    if page is None:
        raise HTTPException(404, "Page not found")
    require_space(session, ctx, page.space_id, write=True)
    if page.deleted_at is None:
        raise HTTPException(400, "页面不在回收站")
    restore_page_recursive(session, page)  # restore the whole subtree
    write_audit(session, "page.restore", "page", target_id=page_id,
                user_id=system_or_user_id(ctx, session), ip=client_ip(request))
    session.commit()
    return {"restored": True, "id": page_id}


@router.delete("/trash/{page_id}")
async def purge_trash_page(
    page_id: str,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
    request: Request = None,
):
    """Permanently delete a trashed page (vectors + revisions + row)."""
    page = session.get(Page, page_id)
    if page is None:
        raise HTTPException(404, "Page not found")
    require_space(session, ctx, page.space_id, write=True)
    if page.deleted_at is None:
        raise HTTPException(400, "页面不在回收站")
    purge_page_fully(session, page)
    write_audit(session, "page.purge", "page", target_id=page_id,
                user_id=system_or_user_id(ctx, session), ip=client_ip(request))
    session.commit()
    return {"purged": True, "id": page_id}


# ── Attachments (Phase 3, P3-T3) ──────────────────────────────────

@router.post("/pages/{page_id}/attachments", response_model=AttachmentOut)
async def upload_attachment(
    page_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
    request: Request = None,
):
    """Upload an attachment for a page (local storage v1; object storage in
    Phase 4)."""
    page = _get_page(session, page_id)
    require_space(session, ctx, page.space_id, write=True)
    content = await file.read()
    if not content:
        raise HTTPException(400, "空文件")
    cap = app_config.app.max_file_size_mb * 1024 * 1024
    if len(content) > cap:
        raise HTTPException(413, f"附件超过 {app_config.app.max_file_size_mb}MB 限制")
    att_id = new_id()
    att = Attachment(
        id=att_id,
        page_id=page_id,
        original_name=file.filename or "attachment",
        stored_path=att_id,  # storage key (local: data/attachments/<id>; s3: bucket key)
        size=len(content),
        mime_type=file.content_type,
        created_by=system_or_user_id(ctx, session),
    )
    session.add(att)
    get_storage().save(att_id, content)
    write_audit(session, "attachment.upload", "attachment", target_id=att.id,
                user_id=att.created_by,
                detail={"page_id": page_id, "name": att.original_name}, ip=client_ip(request))
    session.commit()
    return att


@router.get("/pages/{page_id}/attachments", response_model=list[AttachmentOut])
async def list_attachments(
    page_id: str,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    page = _get_page(session, page_id)
    require_space(session, ctx, page.space_id)
    return session.execute(
        select(Attachment).where(Attachment.page_id == page_id).order_by(Attachment.created_at)
    ).scalars().all()


@router.get("/attachments/{attachment_id}/download")
async def download_attachment(
    attachment_id: str,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    att = session.get(Attachment, attachment_id)
    if att is None:
        raise HTTPException(404, "Attachment not found")
    page = _get_page(session, att.page_id)  # trashed page → 404 (isolated)
    require_space(session, ctx, page.space_id)
    try:
        data = get_storage().read(att.stored_path or att.id)
    except Exception:
        raise HTTPException(404, "Attachment file missing")
    fname = urllib.parse.quote(att.original_name or "attachment")
    return Response(
        content=data,
        media_type=att.mime_type or "application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"},
    )


@router.delete("/attachments/{attachment_id}")
async def delete_attachment(
    attachment_id: str,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
    request: Request = None,
):
    att = session.get(Attachment, attachment_id)
    if att is None:
        raise HTTPException(404, "Attachment not found")
    page = _get_page(session, att.page_id)  # trashed page → 404 (isolated)
    require_space(session, ctx, page.space_id, write=True)
    try:
        get_storage().delete(att.stored_path or att.id)
    except Exception:
        _log.warning("Failed to delete attachment %s", attachment_id, exc_info=True)
    session.delete(att)
    write_audit(session, "attachment.delete", "attachment", target_id=attachment_id,
                user_id=system_or_user_id(ctx, session), ip=client_ip(request))
    session.commit()
    return {"deleted": True, "id": attachment_id}


# ── Audit (Phase 2, P2-T3) ────────────────────────────────────────

@router.get("/audit", response_model=list[AuditLogOut])
async def list_audit(
    limit: int = Query(50, ge=1, le=500),
    action: str = Query(""),
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    """Audit trail, admin-only (superuser / dev mode)."""
    if app_config.security.api_key and not ctx.is_superuser:
        raise HTTPException(403, "仅管理员可查看审计")
    q = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if action:
        q = q.where(AuditLog.action == action)
    return session.execute(q).scalars().all()


# ── Scoped API keys (Phase 4, P4-T7) ──────────────────────────────

def _require_key_admin(session: Session, ctx: AuthContext, space_id: str) -> None:
    """Manage keys for a space: space owner, superuser, or dev mode."""
    if not app_config.security.api_key:
        return
    if ctx.is_superuser:
        return
    if ctx.user is None:
        raise HTTPException(401, "Not authenticated")
    from src.wiki.acl import space_role

    if space_role(session, ctx.user.id, space_id) != "owner":
        raise HTTPException(403, "需要空间 owner 或超级用户")


@router.post("/api-keys", response_model=ApiKeyCreated)
async def create_api_key(
    body: ApiKeyCreate,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
    request: Request = None,
):
    """Issue a scoped API key (role within one space). Plaintext is returned
    exactly once; only its SHA-256 hash is stored."""
    _require_key_admin(session, ctx, body.space_id)
    if session.get(Space, body.space_id) is None:
        raise HTTPException(404, "空间不存在")
    plaintext = "sk_" + secrets.token_urlsafe(32)
    key = ApiKey(
        name=body.name,
        key_hash=hashlib.sha256(plaintext.encode("utf-8")).hexdigest(),
        space_id=body.space_id,
        role=body.role,
        created_by=system_or_user_id(ctx, session),
    )
    session.add(key)
    write_audit(session, "apikey.create", "apikey", target_id=key.id, user_id=key.created_by,
                detail={"space_id": body.space_id, "role": body.role}, ip=client_ip(request))
    session.commit()
    return ApiKeyCreated(id=key.id, name=key.name, key=plaintext, space_id=key.space_id,
                         role=key.role, is_active=True, last_used_at=None, created_at=key.created_at)


@router.get("/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(session: Session = Depends(get_db), ctx: AuthContext = Depends(get_auth_context)):
    if not app_config.security.api_key or ctx.is_superuser:
        return session.execute(select(ApiKey).order_by(ApiKey.created_at.desc())).scalars().all()
    if ctx.user is not None:
        # owner of one or more spaces can list those spaces' keys (P4-T2 修复)
        owned = session.execute(select(Space.id).where(Space.owner_user_id == ctx.user.id)).scalars().all()
        if owned:
            return session.execute(
                select(ApiKey).where(ApiKey.space_id.in_(owned)).order_by(ApiKey.created_at.desc())
            ).scalars().all()
    raise HTTPException(403, "仅超级用户或空间 owner 可查看 API Key")


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    key_id: str,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
    request: Request = None,
):
    key = session.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(404, "API Key not found")
    _require_key_admin(session, ctx, key.space_id)
    key.is_active = False
    write_audit(session, "apikey.revoke", "apikey", target_id=key_id,
                user_id=system_or_user_id(ctx, session), ip=client_ip(request))
    session.commit()
    return {"revoked": True, "id": key_id}


# ── Revisions ──────────────────────────────────────────────────────

@router.get("/pages/{page_id}/revisions", response_model=list[RevisionOut])
async def page_revisions(
    page_id: str,
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    page = _get_page(session, page_id)
    require_space(session, ctx, page.space_id)
    return (
        session.execute(
            select(Revision)
            .where(Revision.page_id == page_id)
            .order_by(Revision.revision_id.desc())
            .limit(limit)
        ).scalars().all()
    )


@router.get("/pages/{page_id}/revisions/diff")
async def revision_diff(
    page_id: str,
    from_rev: int = Query(..., ge=1, description="base revision_id"),
    to_rev: int = Query(..., ge=1, description="target revision_id"),
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    """Line-level diff between two revisions (ROADMAP P3-T1).

    Defined BEFORE `/revisions/{revision_id}` so the literal `diff` segment is
    not parsed as an integer revision id.
    """
    page = _get_page(session, page_id)
    require_space(session, ctx, page.space_id)
    revs = {
        r.revision_id: r
        for r in session.execute(select(Revision).where(Revision.page_id == page_id)).scalars()
    }
    if from_rev not in revs or to_rev not in revs:
        raise HTTPException(404, "Revision not found")
    return {
        "page_id": page_id,
        "from_revision": from_rev,
        "to_revision": to_rev,
        "lines": _compute_diff(revs[from_rev].content_md, revs[to_rev].content_md),
    }


@router.get("/pages/{page_id}/revisions/{revision_id}", response_model=RevisionOut)
async def get_revision(
    page_id: str,
    revision_id: int,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    page = _get_page(session, page_id)
    require_space(session, ctx, page.space_id)
    revision = session.execute(
        select(Revision).where(
            Revision.page_id == page_id,
            Revision.revision_id == revision_id,
        )
    ).scalar_one_or_none()
    if revision is None:
        raise HTTPException(404, "Revision not found")
    return revision


# ── Links / backlinks (Phase 3, P3-T4) ────────────────────────────

@router.get("/pages/{page_id}/links", response_model=list[LinkOut])
async def page_links(
    page_id: str,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    """Outgoing links of a page (`[[target]]` refs resolved on save)."""
    page = _get_page(session, page_id)
    require_space(session, ctx, page.space_id)
    rows = session.execute(
        select(Link, Page.title)
        .outerjoin(Page, Page.id == Link.target_page_id)
        .where(Link.source_page_id == page_id)
        .order_by(Link.label)
    ).all()
    return [
        LinkOut(target_page_id=l.target_page_id, target_title=p_title or l.target_slug, label=l.label)
        for l, p_title in rows
    ]


@router.get("/pages/{page_id}/backlinks", response_model=list[BacklinkOut])
async def page_backlinks(
    page_id: str,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    """Pages that reference this page (reverse of links)."""
    page = _get_page(session, page_id)
    require_space(session, ctx, page.space_id)
    rows = session.execute(
        select(Page)
        .join(Link, Link.source_page_id == Page.id)
        .where(Link.target_page_id == page_id, Page.deleted_at.is_(None))
        .distinct()
        .order_by(Page.title)
    ).scalars().all()
    return [BacklinkOut(page_id=p.id, title=p.title) for p in rows]


# ── Comments (Phase 3, P3-T5) ─────────────────────────────────────

@router.post("/pages/{page_id}/comments", response_model=CommentOut)
async def create_comment(
    page_id: str,
    body: CommentCreate,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
    request: Request = None,
):
    """Comment on a page (optionally pinned to a revision, or replying to one).
    Anyone who can read the space can comment."""
    page = _get_page(session, page_id)
    require_space(session, ctx, page.space_id)
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "评论内容不能为空")
    if body.parent_comment_id:
        parent = session.get(Comment, body.parent_comment_id)
        if parent is None or parent.page_id != page_id:
            raise HTTPException(400, "回复的评论无效")
    uid = system_or_user_id(ctx, session)
    comment = Comment(
        page_id=page_id,
        revision_id=body.revision_id,
        user_id=uid,
        parent_comment_id=body.parent_comment_id,
        content=content,
    )
    session.add(comment)
    write_audit(session, "comment.create", "comment", target_id=comment.id, user_id=uid,
                detail={"page_id": page_id}, ip=client_ip(request))
    session.commit()
    username = session.execute(select(User.username).where(User.id == uid)).scalar()
    return CommentOut(id=comment.id, page_id=page_id, revision_id=comment.revision_id,
                      user_id=uid, username=username, parent_comment_id=comment.parent_comment_id,
                      content=content, created_at=comment.created_at)


@router.get("/pages/{page_id}/comments", response_model=list[CommentOut])
async def list_comments(
    page_id: str,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    page = _get_page(session, page_id)
    require_space(session, ctx, page.space_id)
    rows = session.execute(
        select(Comment, User.username)
        .outerjoin(User, User.id == Comment.user_id)
        .where(Comment.page_id == page_id)
        .order_by(Comment.created_at)
    ).all()
    return [
        CommentOut(id=c.id, page_id=c.page_id, revision_id=c.revision_id, user_id=c.user_id,
                   username=u or "system", parent_comment_id=c.parent_comment_id,
                   content=c.content, created_at=c.created_at)
        for c, u in rows
    ]


@router.delete("/comments/{comment_id}")
async def delete_comment(
    comment_id: str,
    session: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
    request: Request = None,
):
    """Delete a comment (author, space writer, or superuser). Replies cascade."""
    comment = session.get(Comment, comment_id)
    if comment is None:
        raise HTTPException(404, "Comment not found")
    page = session.get(Page, comment.page_id)
    if page is None:
        raise HTTPException(404, "Page not found")
    # Author deletes their own comment; otherwise require space write access.
    if not app_config.security.api_key:
        pass  # dev mode
    elif ctx.is_superuser:
        pass
    elif ctx.user is None:
        raise HTTPException(401, "Not authenticated")
    elif ctx.user.id == comment.user_id:
        pass
    else:
        from src.wiki.acl import space_role

        if space_role(session, ctx.user.id, page.space_id) not in ("owner", "editor"):
            raise HTTPException(403, "无权删除该评论")
    delete_comment_tree(session, comment)  # recursive: clears replies at any depth
    write_audit(session, "comment.delete", "comment", target_id=comment_id,
                user_id=system_or_user_id(ctx, session), ip=client_ip(request))
    session.commit()
    return {"deleted": True, "id": comment_id}
