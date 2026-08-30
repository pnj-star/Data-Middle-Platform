"""Pydantic models for request/response schemas."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field

from .config import config as _cfg


# --- Request models ---

class ConvertRequest(BaseModel):
    mushroom_type: str = ""
    product_id: str = ""


class ChunkRequest(BaseModel):
    chunk_size: int = Field(default=_cfg.app.chunk_size_default, ge=100, le=2000)
    overlap: int = Field(default=_cfg.app.chunk_overlap_default, ge=0, le=200)
    max_parent_size: int = Field(default=_cfg.app.max_parent_size_default, ge=500, le=8000)
    parent_lookback_tokens: int = Field(default=_cfg.app.parent_lookback_default, ge=0, le=500)
    child_lookback_tokens: int | None = Field(default=None, ge=0, le=500)


class PreviewUpdate(BaseModel):
    markdown: str
    target: Literal["raw", "cleaned"] = "cleaned"


class ChunkUpdateRequest(BaseModel):
    action: Literal["delete", "merge"]
    chunk_ids: list[str]


class IngestFullRequest(BaseModel):
    chunk_size: int = Field(default=_cfg.app.chunk_size_default, ge=100, le=2000)
    overlap: int = Field(default=_cfg.app.chunk_overlap_default, ge=0, le=200)
    max_parent_size: int = Field(default=_cfg.app.max_parent_size_default, ge=500, le=8000)
    parent_lookback_tokens: int = Field(default=_cfg.app.parent_lookback_default, ge=0, le=500)
    child_lookback_tokens: int | None = Field(default=None, ge=0, le=500)


class FileMetaUpdate(BaseModel):
    """Partial update for file metadata (all fields optional)."""
    mushroom_type: str | None = None
    product_id: str | None = None
    tenant_id: str | None = None
    kb_id: str | None = None


# --- Response models ---

class FileInfo(BaseModel):
    id: str
    name: str
    size: int
    type: str
    extension: str
    mushroom_type: str = ""
    product_id: str = ""
    tenant_id: str = "default"
    kb_id: str = "default"
    status: str = "uploaded"
    error: str | None = None
    clean_state: str = "raw"
    has_clean: bool = False
    chunk_count: int = 0
    created_at: str


class FileListResponse(BaseModel):
    files: list[FileInfo]
    total: int


class UploadResponse(BaseModel):
    files: list[FileInfo]


class TaskResponse(BaseModel):
    task_id: str


class TaskStatus(BaseModel):
    task_id: str
    state: str
    step: str = ""
    pct: int = 0
    result: dict | None = None
    error: str | None = None


class ConvertPreview(BaseModel):
    file_id: str
    markdown: str
    raw_markdown: str | None = None
    clean_state: str = "raw"
    clean_report: dict = {}
    cleaner_version: str | None = None
    cleaned_at: str | None = None
    metadata: dict


class ChildChunkResponse(BaseModel):
    id: str
    content: str
    size: int
    index: int


class ParentChunkResponse(BaseModel):
    title: str
    content: str
    size: int
    children: list[ChildChunkResponse]
    parent_id: str = ""


class ChunkTreeResponse(BaseModel):
    file_id: str
    file_name: str
    parents: list[ParentChunkResponse]
    parent_count: int
    child_count: int
    avg_child_size: float


class MilvusDoc(BaseModel):
    id: str
    pk: str
    content: str = ""
    image_url: str = ""
    description: str = ""
    source: str = ""
    source_name: str = ""
    mushroom_type: str = ""
    product_id: str = ""
    tenant_id: str = ""
    kb_id: str = ""
    parent_id: str = ""
    chunk_index: int | None = None
    doc_version: int | None = None
    data: dict[str, Any] = {}


class MilvusStats(BaseModel):
    collection_name: str
    num_entities: int
    index_type: str = ""
    is_loaded: bool = False


class MilvusDocListResponse(BaseModel):
    collection: str
    documents: list[MilvusDoc]
    total: int
    page: int
    limit: int
    schema_fields: list[str]
    pk_field: str = "id"
    file_name_truncated: bool = False


class DeleteResponse(BaseModel):
    deleted: bool = True
    count: int = 0
    dependents: list[str] = []


class SaveResponse(BaseModel):
    saved: bool = True
    count: int = 0
    merged_id: str | None = None
    task_id: str | None = None
    reingest: bool = False
