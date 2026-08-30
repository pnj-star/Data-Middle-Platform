"""Space-level access control for the wiki (ROADMAP Phase 2, P2-T2).

Auth model:
- Global API_KEY (when configured) acts as a superuser / service account — it
  bypasses ACL. Keeps the legacy console and backend scripts working.
- JWT users resolve to a `User`; their space access comes from `space_members`:
      owner / editor → read + write
      reader         → read only
- Dev/test mode (API_KEY unset): anonymous requests are allowed, so the
  existing unauth'd tests and local dev keep working. Production must set
  API_KEY (and JWT_SECRET) — then authentication is enforced.

Roles are seeded in src/wiki/seed.py (owner/editor/reader); membership rows are
added via space_members (managed by the API / future admin UI).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import config as app_config
from src.wiki.auth import decode_token
from src.wiki.database import get_db
from src.wiki.models import ApiKey, Role, SpaceMember, User

WRITE_ROLES = {"owner", "editor"}
READ_ROLES = {"owner", "editor", "reader"}


@dataclass
class AuthContext:
    """Resolved identity of a request."""

    user: User | None = None
    is_superuser: bool = False
    # space_id -> role, granted by a scoped API key (P4-T7)
    role_grants: dict[str, str] = field(default_factory=dict)


def get_auth_context(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_db),
) -> AuthContext:
    """Resolve the request identity: global API key (superuser), scoped API key
    (role within one space), or JWT user."""
    api_key = app_config.security.api_key
    if api_key and x_api_key == api_key:
        return AuthContext(is_superuser=True)
    if x_api_key:
        # scoped API key (P4-T7): only the hash is stored; grants one space role
        kh = hashlib.sha256(x_api_key.encode("utf-8")).hexdigest()
        row = session.execute(
            select(ApiKey).where(ApiKey.key_hash == kh, ApiKey.is_active.is_(True))
        ).scalar_one_or_none()
        if row is not None:
            # Persist last_used_at in its own transaction — the request's own
            # session (get_db) may be read-only and never commit (P4-T2 修复).
            from sqlalchemy import update as sa_update

            from src.wiki.database import session_scope

            with session_scope() as s:
                s.execute(
                    sa_update(ApiKey).where(ApiKey.id == row.id)
                    .values(last_used_at=datetime.now(timezone.utc))
                )
            return AuthContext(role_grants={row.space_id: row.role})
    if authorization and app_config.security.jwt_secret and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        if token:
            try:
                payload = decode_token(token)
            except Exception:
                # A presented-but-invalid token is an explicit 401, not anonymous.
                raise HTTPException(401, "Invalid or expired token")
            user = session.get(User, payload.get("sub"))
            if user is not None and user.status == "active":
                return AuthContext(user=user)
    return AuthContext()


def space_role(session: Session, user_id: str, space_id: str) -> str | None:
    """Role name of `user_id` in `space_id`, or None if not a member."""
    return session.execute(
        select(Role.name)
        .join(SpaceMember, SpaceMember.role_id == Role.id)
        .where(SpaceMember.user_id == user_id, SpaceMember.space_id == space_id)
    ).scalar_one_or_none()


def require_space(session: Session, ctx: AuthContext, space_id: str, write: bool = False) -> None:
    """Enforce access to a space; raises 401/403 on failure.

    In dev mode (API_KEY unset) anonymous access is allowed. Once API_KEY is
    configured, a request must be either superuser or a JWT user with the role
    required by `write`.
    """
    if not app_config.security.api_key:
        return
    if ctx.is_superuser:
        return
    if ctx.user is not None:
        role = space_role(session, ctx.user.id, space_id)
    else:
        role = ctx.role_grants.get(space_id)  # scoped API key (P4-T7)
    if role is None:
        if ctx.user is None and not ctx.role_grants:
            raise HTTPException(401, "Not authenticated")
        # authenticated identity, but no access to this space
        raise HTTPException(403, "无权访问该空间")
    allowed = WRITE_ROLES if write else READ_ROLES
    if role not in allowed:
        detail = "需要该空间的写权限" if write else "无权访问该空间"
        raise HTTPException(403, detail)


def require_space_owner(session: Session, ctx: AuthContext, space_id: str) -> None:
    """Space owner (or superuser / dev mode) may manage members & space."""
    if not app_config.security.api_key:
        return
    if ctx.is_superuser:
        return
    if ctx.user is None:
        raise HTTPException(401, "Not authenticated")
    if space_role(session, ctx.user.id, space_id) != "owner":
        raise HTTPException(403, "需要该空间的管理员（owner）权限")


def accessible_space_ids(session: Session, ctx: AuthContext) -> list[str] | None:
    """Space ids the identity may read. None = all spaces (superuser / dev mode)."""
    if not app_config.security.api_key or ctx.is_superuser:
        return None
    if ctx.user is not None:
        rows = session.execute(
            select(SpaceMember.space_id).where(SpaceMember.user_id == ctx.user.id)
        ).scalars().all()
        return list(rows)
    if ctx.role_grants:
        return list(ctx.role_grants.keys())  # scoped API key's space (P4-T7)
    raise HTTPException(401, "Not authenticated")


def system_or_user_id(ctx: AuthContext, session: Session) -> str:
    """Actor id for writes: the JWT user, or the system user for superusers."""
    if ctx.user is not None:
        return ctx.user.id
    from src.wiki.seed import SYSTEM_USERNAME

    return session.execute(select(User.id).where(User.username == SYSTEM_USERNAME)).scalar_one()
