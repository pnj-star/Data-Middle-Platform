"""Pydantic request/response schemas for the wiki API (ROADMAP P1-T2)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PageCreate(BaseModel):
    space_id: str
    title: str = Field(..., min_length=1, max_length=512)
    parent_page_id: str | None = None
    content: str = ""  # optional initial markdown for the first revision


class PageUpdate(BaseModel):
    content: str
    note: str | None = None


class ImportRequest(BaseModel):
    """Import a converted pipeline file as a wiki page draft (P1-T3)."""

    space_id: str
    parent_page_id: str | None = None


class PageOut(BaseModel):
    id: str
    space_id: str
    parent_page_id: str | None
    title: str
    slug: str | None
    content_type: str
    status: str
    # Pointer to the last *published* revision (ROADMAP D10). Only advanced by
    # publish; edits that are not yet published leave this unchanged.
    current_revision_id: str | None
    # Latest revision content — what the editor edits, whether published or not.
    content: str = ""
    source_file_name: str | None
    source_file_extension: str | None
    created_at: datetime
    updated_at: datetime


class PageNode(BaseModel):
    """One node of the page tree, with nested children."""

    id: str
    title: str
    status: str
    parent_page_id: str | None
    has_current_revision: bool
    children: list["PageNode"] = []


class RevisionOut(BaseModel):
    id: str
    page_id: str
    revision_id: int
    content_md: str
    note: str | None
    editor_user_id: str | None
    created_at: datetime


class SpaceOut(BaseModel):
    id: str
    slug: str
    name: str
    description: str | None
    created_at: datetime


# ── Space management (Phase 2, P2-T4) ─────────────────────────────

class SpaceCreate(BaseModel):
    slug: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(..., min_length=1, max_length=128)
    description: str | None = None


class MemberAdd(BaseModel):
    user_id: str
    role: str = Field(..., pattern="^(owner|editor|reader)$")


class MemberOut(BaseModel):
    user_id: str
    username: str
    role: str


class TrashItemOut(BaseModel):
    id: str
    title: str
    space_id: str
    deleted_at: datetime


class AttachmentOut(BaseModel):
    id: str
    page_id: str
    original_name: str
    size: int
    mime_type: str | None
    created_at: datetime


class LinkOut(BaseModel):
    target_page_id: str | None
    target_title: str | None
    label: str


class BacklinkOut(BaseModel):
    page_id: str
    title: str


class ApiKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    space_id: str
    role: str = Field(..., pattern="^(owner|editor|reader)$")


class ApiKeyOut(BaseModel):
    id: str
    name: str
    space_id: str
    role: str
    is_active: bool
    last_used_at: datetime | None
    created_at: datetime


class ApiKeyCreated(ApiKeyOut):
    key: str  # plaintext, shown exactly once


class CommentCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)
    revision_id: int | None = None  # pin a comment to a specific revision (optional)
    parent_comment_id: str | None = None  # reply to a comment


class CommentOut(BaseModel):
    id: str
    page_id: str
    revision_id: int | None
    user_id: str | None
    username: str | None
    parent_comment_id: str | None
    content: str
    created_at: datetime


# ── Auth (Phase 2, P2-T1) ─────────────────────────────────────────

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(..., min_length=6, max_length=128)
    email: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: str
    username: str
    email: str | None
    provider: str
    created_at: datetime


class AuditLogOut(BaseModel):
    id: str
    user_id: str | None
    action: str
    target_type: str
    target_id: str | None
    detail: dict | None
    ip: str | None
    result: str | None
    created_at: datetime


PageNode.model_rebuild()
