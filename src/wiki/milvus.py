"""Wiki-domain Milvus collection (ROADMAP P0-T4, access layer lands in P1-T1).

The wiki project owns its own collection (`wiki_knowledge`) fully separate from
the rag-shared `mushroom_knowledge` / `mushroom_images` (ROADMAP D1) — those
schemas are frozen for rag compatibility and must not be touched.

Schema carries page/revision/space scope so search results trace back to a page
+ revision (ROADMAP D3), and uses the deterministic row id
`{page_id}:{revision_id}:{chunk_index}` so any (page, revision, chunk) is
addressable and deletable by prefix.

This module currently holds the schema + create logic. Read/write helpers
(query / search / delete) are added in P1-T1.
"""
from __future__ import annotations

from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, utility

from src import milvus_expr as mx
from src.config import config as app_config
from src.milvus_client import ensure_connected
from src.logging_config import get_logger

_log = get_logger(__name__)

# Row id pattern {page_id}:{revision_id}:{chunk_index} — deterministic so a
# (page, revision, chunk) is addressable and deletable by prefix.
ID_PREFIX_SEP = ":"
# 32(page) + 1 + 8(rev) + 1 + 8(idx) worst case, with headroom.
_ID_MAX_LEN = 256


def wiki_collection_name() -> str:
    """Resolved wiki collection name (config, default `wiki_knowledge`)."""
    return app_config.milvus.wiki_collection


def make_row_id(page_id: str, revision_id: int, chunk_index: int) -> str:
    """Deterministic Milvus primary key for a (page, revision, chunk)."""
    return f"{page_id}{ID_PREFIX_SEP}{revision_id}{ID_PREFIX_SEP}{chunk_index}"


def _schema(dim: int) -> CollectionSchema:
    """Schema for the wiki collection (fields per ROADMAP §7)."""
    return CollectionSchema(
        fields=[
            FieldSchema("id", DataType.VARCHAR, max_length=_ID_MAX_LEN, is_primary=True),
            FieldSchema("page_id", DataType.VARCHAR, max_length=32),
            FieldSchema("revision_id", DataType.INT32),
            FieldSchema("space_id", DataType.VARCHAR, max_length=32),
            FieldSchema("content", DataType.VARCHAR, max_length=65535),
            FieldSchema("chunk_index", DataType.INT32),
            FieldSchema("parent_title", DataType.VARCHAR, max_length=512),
            FieldSchema("parent_content", DataType.VARCHAR, max_length=65535),
            # Empty string (not NULL) for rows imported without a source file.
            FieldSchema("source_file_id", DataType.VARCHAR, max_length=32),
            FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=dim),
        ],
        description="Wiki page chunks (page/revision-scoped, owned by this project)",
    )


def verify_wiki_collection(col: Collection) -> list[str]:
    """Compare an existing collection against the expected schema/index.

    Returns a list of problems (empty = fully matches). Field names and the
    embedding index are checked; dim/index metric are reported if mismatched.
    """
    problems: list[str] = []
    expected = _schema(app_config.milvus.dim)
    expected_fields = {f.name: f for f in expected.fields}
    actual_fields = {f.name: f for f in col.schema.fields}

    missing = set(expected_fields) - set(actual_fields)
    extra = set(actual_fields) - set(expected_fields)
    if missing:
        problems.append(f"missing fields: {sorted(missing)}")
    if extra:
        problems.append(f"extra fields: {sorted(extra)}")

    dim = app_config.milvus.dim
    emb = actual_fields.get("embedding")
    if emb is not None and emb.params.get("dim") != dim:
        problems.append(f"embedding dim {emb.params.get('dim')} != expected {dim}")

    idx_fields = {i.field_name for i in col.indexes}
    if "embedding" not in idx_fields:
        problems.append("missing embedding index")

    return problems


def create_wiki_collection(force: bool = False) -> Collection:
    """Create the wiki collection if missing; verify if present.

    Idempotent by default — a live collection is never dropped, keeping the same
    no-drop discipline the rag-shared collections follow. ``force=True`` drops
    and recreates; that is the caller's responsibility.
    """
    name = wiki_collection_name()
    ensure_connected()
    if utility.has_collection(name) and not force:
        col = Collection(name)
        problems = verify_wiki_collection(col)
        if problems:
            _log.warning(
                "Wiki collection '%s' exists but schema mismatch: %s", name, "; ".join(problems)
            )
        else:
            _log.info("Wiki collection '%s' exists and matches expected schema", name)
        return col
    if force and utility.has_collection(name):
        _log.warning("Dropping and recreating wiki collection '%s' (force)", name)
        utility.drop_collection(name)
    col = Collection(name, _schema(app_config.milvus.dim))
    index_params = {
        "metric_type": "COSINE",
        "index_type": "IVF_FLAT",
        "params": {"nlist": 128},
    }
    col.create_index("embedding", index_params)
    col.load()
    _log.info("Created wiki collection '%s' (dim=%d)", name, app_config.milvus.dim)
    return col


# ── Access layer (ROADMAP P1-T1) ──────────────────────────────────

# Output fields for all reads. `id` is `{page_id}:{revision_id}:{chunk_index}`;
# the rest support search-then-trace back to a page + revision (ROADMAP D3).
_WIKI_FIELDS = [
    "id", "page_id", "revision_id", "space_id", "content",
    "chunk_index", "parent_title", "parent_content", "source_file_id",
]


def _get_collection() -> Collection:
    """Cached, loaded wiki collection (auto-creates it if missing)."""
    name = wiki_collection_name()
    ensure_connected()
    if not utility.has_collection(name):
        create_wiki_collection()
    col = Collection(name)
    col.load()
    return col


def insert_vectors(entities: list[dict]) -> int:
    """Batch insert entities into the wiki collection; flush for visibility."""
    if not entities:
        return 0
    col = _get_collection()
    result = col.insert(entities)
    col.flush()
    return result.insert_count


def delete_by_expr(expr: str) -> int:
    """Delete entities by expr; flush + load makes deletions visible (same
    discipline as src/milvus_client.delete_by_expr)."""
    col = _get_collection()
    result = col.delete(expr)
    col.flush()
    col.load()
    return result.delete_count if result else 0


def delete_by_ids(ids: list[str]) -> int:
    """Delete entities by primary key."""
    if not ids:
        return 0
    return delete_by_expr(mx.in_expr("id", ids))


def search(
    query_vector: list[float],
    filter_expr: str = "",
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """Vector search with an optional scalar pre-filter expr.

    ``filter_expr`` (e.g. ``revision_id in [...] AND space_id == "..."``) is
    applied BEFORE ranking — the search-time grouping pitfall from ROADMAP D9
    is avoided by construction: callers pass the published-revision filter
    built from Postgres (P1-T6).
    """
    col = _get_collection()
    params: dict = {"metric_type": "COSINE"}
    if offset:
        params["offset"] = offset
    results = col.search(
        data=[query_vector],
        anns_field="embedding",
        param=params,
        limit=limit,
        expr=filter_expr or None,
        output_fields=_WIKI_FIELDS,
    )
    hits = results[0] if results else []
    # pymilvus 2.5: `h.entity` is the Hit itself (`{id, distance, entity:{fields}}`);
    # the output fields live on `h.fields`. Read them directly.
    return [dict(h.fields) for h in hits]


def query(expr: str, limit: int = 100, offset: int = 0) -> list[dict]:
    """Scalar query (keyword LIKE, by-page, etc.) with paging."""
    col = _get_collection()
    return [
        dict(r)
        for r in col.query(expr=expr, output_fields=_WIKI_FIELDS, offset=offset, limit=limit)
    ]


def count(expr: str = "") -> int:
    """Row count matching ``expr`` (all rows when empty)."""
    col = _get_collection()
    q = expr or "id != ''"
    try:
        return col.query(expr=q, output_fields=["count(*)"])[0]["count(*)"]
    except Exception:
        return 0


def stats() -> dict:
    """Collection stats for the wiki collection."""
    col = _get_collection()
    return {"collection": wiki_collection_name(), "num_entities": col.num_entities}
