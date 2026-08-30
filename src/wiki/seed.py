"""Idempotent seed data for the wiki domain (ROADMAP P1-T2).

Creates the system user (v1 is single-user, no login — all writes are
attributed to `system`), the default roles (created now, enforced in Phase 2),
and the default space. Safe to call repeatedly; only missing rows are created.
"""
from __future__ import annotations

from sqlalchemy import select

from src.wiki.models import (
    AUTH_PROVIDER_LOCAL,
    Role,
    Space,
    USER_STATUS_ACTIVE,
    User,
)

SYSTEM_USERNAME = "system"
DEFAULT_SPACE_SLUG = "default"
DEFAULT_SPACE_NAME = "默认空间"
DEFAULT_ROLES = ("owner", "editor", "reader")


def ensure_seed_data(session) -> dict:
    """Create system user / default roles / default space if missing.

    Returns a dict of created counts (idempotent: all zeros on a seeded DB).
    """
    created = {"users": 0, "roles": 0, "spaces": 0}

    system = session.execute(
        select(User).where(User.username == SYSTEM_USERNAME)
    ).scalar_one_or_none()
    if system is None:
        system = User(
            username=SYSTEM_USERNAME,
            provider=AUTH_PROVIDER_LOCAL,
            status=USER_STATUS_ACTIVE,
        )
        session.add(system)
        session.flush()
        created["users"] = 1

    for name in DEFAULT_ROLES:
        if session.execute(select(Role).where(Role.name == name)).scalar_one_or_none() is None:
            session.add(Role(name=name, description=f"{name} role within a space"))
            created["roles"] += 1

    space = session.execute(
        select(Space).where(Space.slug == DEFAULT_SPACE_SLUG)
    ).scalar_one_or_none()
    if space is None:
        space = Space(
            slug=DEFAULT_SPACE_SLUG,
            name=DEFAULT_SPACE_NAME,
            description="默认知识空间",
            owner_user_id=system.id,
        )
        session.add(space)
        session.flush()
        created["spaces"] = 1

    return created
