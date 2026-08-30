"""Text embedding + batch insertion into Milvus mushroom_knowledge collection."""
from __future__ import annotations

import threading
import uuid
from typing import Any

from sentence_transformers import SentenceTransformer

from .config import config as app_config
from . import milvus_client as mc
from .logging_config import get_logger

_log = get_logger(__name__)

MODEL_NAME = app_config.app.sentence_transformers_model
BATCH_SIZE = 32

# Milvus VARCHAR max length for content (schema constraint).
_MILVUS_FIELD_MAX = 65535
_TRUNC_MARKER = "…[截断]"

_model: SentenceTransformer | None = None
_model_lock = threading.Lock()


def _get_model() -> SentenceTransformer:
    """Lazy-load the embedding model with thread-safe singleton pattern.

    Uses BAAI/bge-small-zh-v1.5 (512 dims) by default.
    Set EMBEDDER_LOCAL_FILES_ONLY=false in .env to allow automatic download.
    """
    global _model
    if _model is None:
        with _model_lock:
            # Double-check within lock
            if _model is None:
                local_only = app_config.app.embedder_local_files_only
                _log.info("Loading embedding model: %s (local_files_only=%s)", MODEL_NAME, local_only)
                _model = SentenceTransformer(MODEL_NAME, local_files_only=local_only)
    return _model


def get_embedding(text: str) -> list[float]:
    """Generate a 512-dim embedding for a single text."""
    model = _get_model()
    vec = model.encode([text], normalize_embeddings=True, show_progress_bar=False)
    return vec[0].tolist()


def get_embeddings(texts: list[str]) -> list[list[float]]:
    """Batch generate 512-dim embeddings for multiple texts."""
    if not texts:
        return []
    model = _get_model()
    vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [v.tolist() for v in vecs]


def truncate(text: str, max_len: int = _MILVUS_FIELD_MAX, marker: str = _TRUNC_MARKER) -> str:
    """Truncate a field to fit the Milvus VARCHAR limit, appending a marker.

    Prevents oversized parent blocks from failing the whole Milvus insert.
    """
    if len(text) <= max_len:
        return text
    return text[: max_len - len(marker)] + marker


def insert_chunks(
    child_contents: list[str],
    parent_ids: list[str],
    chunk_indexes: list[int],
    metadata: dict[str, str],
    doc_version: int | list[int] = 1,
    chunk_ids: list[str] | None = None,
) -> int:
    """Generate embeddings for child chunks and batch-upsert into Milvus.

    Args:
        child_contents: List of child chunk texts (embedding source).
        parent_ids: Corresponding parent block business IDs (MySQL ref).
        chunk_indexes: Child chunk positions within parent.
        metadata: Dict with keys: source, mushroom_type, product_id, category,
            tenant_id, kb_id.
        doc_version: One version for all children, or one version per child;
            values are checked against the MySQL parent block during retrieval.
        chunk_ids: Optional Milvus entity ids — pass the SQLite chunk ids so
            chunk edits can be synced back to Milvus precisely. Falls back to
            fresh uuids when absent.

    Returns:
        Number of entities inserted.
    """
    if not child_contents:
        return 0

    if isinstance(doc_version, list) and len(doc_version) != len(child_contents):
        raise ValueError("doc_version list length must match child_contents")

    embeddings = get_embeddings(child_contents)

    source = metadata.get("source", "")
    mushroom_type = metadata.get("mushroom_type", "")
    product_id = metadata.get("product_id", "")
    category = metadata.get("category", "")
    tenant_id = metadata.get("tenant_id", "default")
    kb_id = metadata.get("kb_id", "default")

    entities: list[dict[str, Any]] = []
    for i, (child, pid, idx, emb) in enumerate(
        zip(child_contents, parent_ids, chunk_indexes, embeddings)
    ):
        entity_id = str(chunk_ids[i]) if chunk_ids and i < len(chunk_ids) else uuid.uuid4().hex
        version = doc_version[i] if isinstance(doc_version, list) else doc_version
        entities.append({
            "id": entity_id,
            "tenant_id": tenant_id,
            "kb_id": kb_id,
            "content": truncate(child),
            "parent_id": pid,
            "chunk_index": idx,
            "doc_version": version,
            "is_current": True,
            "product_id": product_id,
            "source": source,
            "category": category,
            "mushroom_type": mushroom_type,
            "embedding": emb,
        })

    _log.info("Upserting %d text chunks into Milvus", len(entities))
    count = mc.insert_text_entities(entities)
    _log.info("Upserted %d text chunks", count)
    return count


def delete_by_source(source: str, *, tenant_id: str | None = None,
                     kb_id: str | None = None) -> int:
    """Delete all vectors for a given source file from mushroom_knowledge."""
    from . import milvus_expr as mx

    expr = mx.eq("source", source)
    if tenant_id is not None:
        expr = mx.and_expr(expr, mx.eq("tenant_id", tenant_id))
    if kb_id is not None:
        expr = mx.and_expr(expr, mx.eq("kb_id", kb_id))
    _log.info("Deleting text vectors for source: %s", source)
    count = mc.delete_by_expr(expr, app_config.milvus.text_collection)
    _log.info("Deleted %d text vectors", count)
    return count
