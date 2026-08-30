"""SQLAlchemy 2.0 models for the wiki domain (Postgres).

Enterprise knowledge-base data model per ROADMAP §7. Covers spaces, page tree,
revisions, attachments, links, comments, plus identity/ACL (users, roles, space
members) and audit logs.

Enablement timeline (see ROADMAP):
- users / roles / space_members: created in P0, seeded in P1-T2, enforced in Phase 2
- attachments / links / comments: created in P0, enabled in Phase 3
- audit_logs: created in P0, enabled in Phase 2

Conventions:
- Primary keys are `String(32)` hex UUIDs (uuid4().hex), matching the legacy
  SQLite tables' id style so `pages.source_file_id` (a soft cross-store
  reference into the SQLite files table, no FK) lines up across stores.
- Status-ish fields are plain `String` + module-level constants, never PG
  ENUM types, so value evolution never requires a type rebuild.
- `pages.current_revision_id` and `revisions.page_id` form a circular FK pair;
  SQLAlchemy handles it at declaration time, Alembic handles it in P0-T3.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    """Hex UUID primary key, same style as the legacy SQLite id columns."""
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    """Declarative base for all wiki-domain tables."""


# --- Page lifecycle statuses (ROADMAP D10) ---
PAGE_STATUS_DRAFT = "draft"
PAGE_STATUS_PUBLISHING = "publishing"
PAGE_STATUS_PUBLISHED = "published"
PAGE_STATUS_PUBLISH_FAILED = "publish_failed"

# --- Identity / auth provider (ROADMAP D, reserved for Phase 2 SSO/OIDC) ---
AUTH_PROVIDER_LOCAL = "local"
AUTH_PROVIDER_SSO_OIDC = "sso_oidc"

# --- User / role statuses ---
USER_STATUS_ACTIVE = "active"
USER_STATUS_DISABLED = "disabled"


# ── Identity & ACL (enabled Phase 2, seeded Phase 1) ─────────────────

class User(Base):
    """Application user. Local login (password) or SSO/OIDC (provider+external_id)."""
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    # provider + external_id reserve SSO/OIDC: NULL external_id never collides
    # in Postgres unique constraints, so local users are unaffected.
    provider: Mapped[str] = mapped_column(String(32), default=AUTH_PROVIDER_LOCAL)
    external_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default=USER_STATUS_ACTIVE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("provider", "external_id"),)


class Role(Base):
    """Named role (owner/editor/reader/admin...). scope reserves global roles."""
    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    scope: Mapped[str] = mapped_column(String(16), default="space")
    description: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Space(Base):
    """A knowledge space: top-level container owning a page tree."""
    __tablename__ = "spaces"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(String(512))
    owner_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SpaceMember(Base):
    """User → role within a space (space-level ACL, enforced Phase 2)."""
    __tablename__ = "space_members"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("space_id", "user_id"),)


# ── Content domain ─────────────────────────────────────────────────

class Page(Base):
    """A wiki page. `parent_page_id` forms the page tree (NULL = root).

    `current_revision_id` points at the latest published revision; it is only
    advanced after the revision's vectors are written (ROADMAP D10), so
    "published" pages always have vectors in Milvus.

    `source_file_id` is a soft cross-store reference into the legacy SQLite
    files table (no FK, the row may be deleted later); the name/extension
    snapshots keep the "imported from <file>" display alive (ROADMAP D12).
    """
    __tablename__ = "pages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id"), index=True)
    parent_page_id: Mapped[str | None] = mapped_column(ForeignKey("pages.id"))
    title: Mapped[str] = mapped_column(String(512))
    # Per-space unique when set; NULL slugs are exempt from the constraint.
    slug: Mapped[str | None] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(16), default="markdown")
    current_revision_id: Mapped[str | None] = mapped_column(ForeignKey("revisions.id"))
    status: Mapped[str] = mapped_column(String(16), default=PAGE_STATUS_DRAFT)
    # Import provenance (soft cross-store reference, no FK).
    source_file_id: Mapped[str | None] = mapped_column(String(32))
    source_file_name: Mapped[str | None] = mapped_column(String(255))
    source_file_extension: Mapped[str | None] = mapped_column(String(32))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    updated_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    # Soft-delete marker (ROADMAP P3-T2): non-null means the page is in the
    # trash; vectors + revisions are kept so restore is instant. Only permanent
    # deletion (retention expiry or explicit trash purge) removes them.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("space_id", "slug"),
        Index("ix_pages_source_file_id", "source_file_id"),
        Index("ix_pages_status", "status"),
        Index("ix_pages_deleted_at", "deleted_at"),
    )


class Revision(Base):
    """One saved version of a page. `revision_id` increments per page and is the
    same value stored as `revision_id` on Milvus wiki vectors (ROADMAP D9), so
    Postgres and Milvus always agree on which revision a vector belongs to."""
    __tablename__ = "revisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    page_id: Mapped[str] = mapped_column(ForeignKey("pages.id"), index=True)
    revision_id: Mapped[int] = mapped_column(Integer)
    content_md: Mapped[str] = mapped_column(Text)
    editor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    note: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("page_id", "revision_id"),)


# ── Phase 3 features (tables created now, enabled later) ────────────

class Attachment(Base):
    """File attached to a page (stored locally in v1, object storage in Phase 4)."""
    __tablename__ = "attachments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    page_id: Mapped[str] = mapped_column(ForeignKey("pages.id"), index=True)
    original_name: Mapped[str] = mapped_column(String(512))
    stored_path: Mapped[str] = mapped_column(String(1024))
    size: Mapped[int] = mapped_column(Integer)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Link(Base):
    """Internal page-to-page link (backlink graph, enabled Phase 3).

    `target_page_id` links to an existing page; `target_slug` covers links whose
    target page does not exist yet (wiki-style forward references)."""
    __tablename__ = "links"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_page_id: Mapped[str] = mapped_column(ForeignKey("pages.id"), index=True)
    target_page_id: Mapped[str | None] = mapped_column(ForeignKey("pages.id"))
    target_slug: Mapped[str | None] = mapped_column(String(512))
    label: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_links_target", "target_page_id", "target_slug"),)


class Comment(Base):
    """Comment on a page, optionally pinned to a specific revision."""
    __tablename__ = "comments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    page_id: Mapped[str] = mapped_column(ForeignKey("pages.id"), index=True)
    revision_id: Mapped[int | None] = mapped_column(Integer)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    parent_comment_id: Mapped[str | None] = mapped_column(ForeignKey("comments.id"))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ── Audit (enabled Phase 2) ────────────────────────────────────────

class AuditLog(Base):
    """Append-only operation audit: who, when, on what, result (Phase 2)."""
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSON)
    ip: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_audit_target", "target_type", "target_id"),
        Index("ix_audit_user", "user_id"),
        Index("ix_audit_created", "created_at"),
    )


# ── Scoped API keys (ROADMAP P4-T7) ──────────────────────────────

class ApiKey(Base):
    """A scoped API key granting a role within one space (owner/editor/reader).

    Only the SHA-256 hash is stored; the plaintext is shown once at creation.
    The global API_KEY (SecurityConfig) remains the superuser/service key.
    """
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    is_active: Mapped[bool] = mapped_column(default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
