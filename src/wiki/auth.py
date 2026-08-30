"""Wiki user authentication (ROADMAP Phase 2, P2-T1).

JWT-based auth for wiki users, with SSO/OIDC reserved via
`users.provider` / `users.external_id`. Passwords are hashed with bcrypt.

Token endpoints are disabled (HTTP 503) until `JWT_SECRET` is configured — see
SecurityConfig. The legacy pipeline keeps using the global API_KEY; this module
is exclusively for wiki user identity (P2-T2 wires it into page ACLs).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt as pyjwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from src.config import config as app_config
from src.wiki.database import get_db
from src.wiki.models import USER_STATUS_ACTIVE, User

_ALGO = "HS256"


def _secret() -> str:
    return app_config.security.jwt_secret


def require_jwt_configured() -> None:
    """Raise 503 when JWT auth is not enabled (JWT_SECRET unset)."""
    if not _secret():
        raise HTTPException(503, "JWT 认证未启用（未配置 JWT_SECRET）")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_token(user: User) -> str:
    require_jwt_configured()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.id,
        "username": user.username,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=app_config.security.jwt_expire_minutes)).timestamp()),
    }
    return pyjwt.encode(payload, _secret(), algorithm=_ALGO)


def decode_token(token: str) -> dict:
    require_jwt_configured()
    return pyjwt.decode(token, _secret(), algorithms=[_ALGO])


def get_current_user(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_db),
) -> User:
    """FastAPI dependency: resolve the authenticated user from a Bearer token."""
    require_jwt_configured()
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_token(token)
    except pyjwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    user = session.get(User, payload.get("sub"))
    if user is None or user.status != USER_STATUS_ACTIVE:
        raise HTTPException(401, "User not found or disabled", headers={"WWW-Authenticate": "Bearer"})
    return user
