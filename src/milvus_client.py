"""Milvus connection + collection schema management (write-only for ingest pipeline).

Schema aligns with rag project's milvus_client.py to ensure zero-code-coupling
compatibility. Both projects share the same Milvus instance and collection names.
"""
from __future__ import annotations

import threading
import time
import warnings
from typing import Any

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    exceptions as pymilvus_exceptions,
    FieldSchema,
    Function,
    FunctionType,
    connections,
    utility,
    PyMilvusDeprecationWarning,
)

# pymilvus 2.x ORM-style API (connections/Collection/utility) emits a
# PyMilvusDeprecationWarning on EVERY call ("will be removed in PyMilvus 3.1").
# Cosmetic console noise in the API/worker log — migrating to MilvusClient is
# out of scope mid-project — so silence just this category, not all warnings.
warnings.filterwarnings("ignore", category=PyMilvusDeprecationWarning)

from .config import config as app_config
from . import milvus_expr as mx
from .logging_config import get_logger

_log = get_logger(__name__)

_connected: bool = False
_connect_lock = threading.Lock()
_text_collection: Collection | None = None
_image_collection: Collection | None = None
_collection_lock = threading.Lock()
_last_health_check: float = 0.0


def ensure_connected() -> None:
    """Establish Milvus connection (idempotent, thread-safe)."""
    global _connected
    if _connected:
        return
    with _connect_lock:
        if _connected:
            return
        conn_args: dict[str, Any] = {
            "host": app_config.milvus.host,
            "port": app_config.milvus.port,
        }
        if app_config.milvus.user:
            conn_args["user"] = app_config.milvus.user
            conn_args["password"] = app_config.milvus.password
            conn_args["secure"] = app_config.milvus.secure
        connections.connect("default", **conn_args)
        _connected = True
        _log.info("Connected to Milvus at %s:%d", app_config.milvus.host, app_config.milvus.port)


def _create_text_collection(name: str, dim: int) -> Collection:
    schema = CollectionSchema(
        fields=[
            FieldSchema("id", DataType.VARCHAR, max_length=256, is_primary=True),
            FieldSchema("tenant_id", DataType.VARCHAR, max_length=64),
            FieldSchema("kb_id", DataType.VARCHAR, max_length=64),
            FieldSchema("product_id", DataType.VARCHAR, max_length=128),
            FieldSchema(
                "content", DataType.VARCHAR, max_length=65535,
                enable_analyzer=True, analyzer_params={"type": "chinese"},
            ),
            FieldSchema("source", DataType.VARCHAR, max_length=512),
            FieldSchema("category", DataType.VARCHAR, max_length=128),
            FieldSchema("parent_id", DataType.VARCHAR, max_length=256),
            FieldSchema("chunk_index", DataType.INT32),
            FieldSchema("doc_version", DataType.INT64),
            FieldSchema("is_current", DataType.BOOL, default_value=True),
            FieldSchema("mushroom_type", DataType.VARCHAR, max_length=128),
            FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=dim),
            FieldSchema(
                "sparse", DataType.SPARSE_FLOAT_VECTOR, is_function_output=True,
            ),
        ],
        functions=[
            Function(
                name="bm25",
                function_type=FunctionType.BM25,
                input_field_names=["content"],
                output_field_names=["sparse"],
                params={},
            ),
        ],
        description="Mushroom knowledge base text chunks",
    )
    collection = Collection(name, schema)
    index_params = {
        "metric_type": "COSINE",
        "index_type": "IVF_FLAT",
        "params": {"nlist": 128},
    }
    collection.create_index("embedding", index_params)
    collection.create_index("sparse", {
        "index_type": "SPARSE_INVERTED_INDEX",
        "metric_type": "BM25",
        "params": {"bm25_k1": 1.2, "bm25_b": 0.75},
    })
    _log.info("Created text collection: %s (dim=%d)", name, dim)
    return collection


def _validate_text_schema(collection: Collection) -> None:
    """Refuse an old shared collection before a partial entity insert fails."""
    required = {"id", "tenant_id", "kb_id", "content", "parent_id",
                "doc_version", "is_current"}
    actual = {field.name for field in collection.schema.fields}
    missing = sorted(required - actual)
    if missing:
        raise RuntimeError(
            f"Milvus collection '{collection.name}' is missing fields {missing}. "
            "Run scripts/migrate_milvus_add_is_current.py before ingestion."
        )


def _create_image_collection(name: str, dim: int) -> Collection:
    schema = CollectionSchema(
        fields=[
            FieldSchema("id", DataType.VARCHAR, max_length=256, is_primary=True),
            FieldSchema("product_id", DataType.VARCHAR, max_length=128),
            FieldSchema("image_url", DataType.VARCHAR, max_length=1024),
            FieldSchema("description", DataType.VARCHAR, max_length=2048),
            FieldSchema("mushroom_type", DataType.VARCHAR, max_length=128),
            FieldSchema("source", DataType.VARCHAR, max_length=512),
            FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=dim),
        ],
        description="Mushroom product images with CLIP embeddings",
    )
    collection = Collection(name, schema)
    index_params = {
        "metric_type": "COSINE",
        "index_type": "IVF_FLAT",
        "params": {"nlist": 128},
    }
    collection.create_index("embedding", index_params)
    _log.info("Created image collection: %s (dim=%d)", name, dim)
    return collection


def _reload_collection(name: str) -> None:
    """Force reload a collection to pick up external changes (e.g., drop/recreate)."""
    global _text_collection, _image_collection
    with _collection_lock:
        if name == app_config.milvus.text_collection and _text_collection is not None:
            try:
                # The cached ORM object can outlive an external rename/recreate.
                # Validate against a fresh description so a legacy-schema object
                # cannot keep inserting into the replacement collection.
                _validate_text_schema(Collection(name))
                _text_collection.load()
                _log.debug("Reloaded text collection: %s", name)
            except Exception as e:
                _log.warning("Failed to reload text collection: %s", e)
                _text_collection = None  # force re-init
        elif name == app_config.milvus.image_collection and _image_collection is not None:
            try:
                _image_collection.load()
                _log.debug("Reloaded image collection: %s", name)
            except Exception as e:
                _log.warning("Failed to reload image collection: %s", e)
                _image_collection = None


def health_check() -> bool:
    """Periodic health check — reloads collections if they've been stale too long.

    Returns True if the Milvus connection is healthy.
    """
    global _last_health_check
    now = time.time()
    interval = app_config.milvus.health_check_interval
    if now - _last_health_check < interval:
        return True

    try:
        ensure_connected()
        # Verify the connection is still alive
        collections = utility.list_collections()
        _last_health_check = now

        # Reload collections to refresh cached state
        tc = app_config.milvus.text_collection
        ic = app_config.milvus.image_collection
        if tc in collections:
            _reload_collection(tc)
        if ic in collections:
            _reload_collection(ic)

        return True
    except Exception as e:
        _log.error("Milvus health check failed: %s", e)
        return False


def _get_collection(collection_name: str) -> Collection | None:
    """Return the cached, loaded collection for the given name.

    All operations (read + write) go through the same cached instance so
    that delete + flush + load makes deletions immediately visible to
    every subsequent query.
    """
    global _text_collection, _image_collection
    ensure_connected()
    health_check()  # periodically verify collection health

    with _collection_lock:
        if collection_name == app_config.milvus.text_collection:
            if _text_collection is None:
                collection: Collection
                if not utility.has_collection(collection_name):
                    collection = _create_text_collection(collection_name, app_config.milvus.dim)
                else:
                    collection = Collection(collection_name)
                    _validate_text_schema(collection)
                collection.load()
                # Cache only after validation/load succeed. Otherwise a failed
                # schema check would leave the invalid collection installed and
                # later calls would skip validation entirely.
                _text_collection = collection
            return _text_collection
        elif collection_name == app_config.milvus.image_collection:
            if _image_collection is None:
                if not utility.has_collection(collection_name):
                    _image_collection = _create_image_collection(collection_name, app_config.milvus.dim)
                else:
                    _image_collection = Collection(collection_name)
                _image_collection.load()
            return _image_collection

    # Management/read APIs may inspect any existing collection. Unlike the two
    # pipeline collections above, never create it implicitly.
    if utility.has_collection(collection_name):
        collection = Collection(collection_name)
        collection.load()
        return collection
    return None


# Backward-compatible helpers
def get_text_collection() -> Collection:
    return _get_collection(app_config.milvus.text_collection)  # type: ignore[return-value]


def get_image_collection() -> Collection:
    return _get_collection(app_config.milvus.image_collection)  # type: ignore[return-value]


# ── Write operations ──────────────────────────────────────────────

def insert_text_entities(entities: list[dict[str, Any]]) -> int:
    """Batch upsert text chunk entities into mushroom_knowledge.

    Duplicate chunk ids occur on metadata-only re-ingest (chunks are not
    re-created, so SQLite ids are reused). A plain insert leaves old and new
    vectors with the same primary key, and Milvus deletes by primary key only,
    so old-scope cleanup would also remove the newly inserted rows. Upsert
    replaces the old vector in-place, keeping exactly one authoritative row.
    """
    if not entities:
        return 0
    global _text_collection
    try:
        collection = get_text_collection()
        result = collection.upsert(entities)
        collection.flush()
    except pymilvus_exceptions.DataNotMatchException:
        # A migration can replace the remote collection while this process still
        # holds its pre-migration cached schema. Row validation happens before
        # any data is written, so retrying against the current schema is safe.
        with _collection_lock:
            _text_collection = None
        collection = get_text_collection()
        result = collection.upsert(entities)
        collection.flush()
    return result.upsert_count


def insert_image_entities(entities: list[dict[str, Any]]) -> int:
    """Batch insert image entities into mushroom_images."""
    if not entities:
        return 0
    collection = get_image_collection()
    result = collection.insert(entities)
    collection.flush()
    return result.insert_count


def delete_by_expr(expr: str, collection_name: str) -> int:
    """Delete entities and force the cached collection to pick up the change."""
    col = _get_collection(collection_name)
    if col is None:
        return 0
    result = col.delete(expr)
    # flush + load makes the deletion visible to subsequent queries
    # on the same cached collection instance
    col.flush()
    col.load()
    return result.delete_count if result else 0


def delete_by_ids(
    ids: list[str],
    collection_name: str,
) -> int:
    """Delete entities by primary key.

    Milvus delete expressions that contain the primary-key field act only on
    that key; additional tenant/kb predicates are not honored by the delete
    path. Callers must therefore guarantee ids are globally unique before
    deleting stale ids here. The ingest pipeline guarantees uniqueness by
    upserting chunk ids, which replaces any prior row with the same id.
    """
    if not ids:
        return 0
    expr = mx.in_expr("id", ids)
    return delete_by_expr(expr, collection_name)


# ── Read operations (all share the same cached collection) ─────────

def get_stats(collection_name: str) -> dict[str, Any]:
    """Collection stats — uses num_entities for accurate count."""
    col = _get_collection(collection_name)
    if col is None:
        return {"collection_name": collection_name, "num_entities": 0,
                "index_type": "", "is_loaded": False}
    try:
        # Use num_entities which excludes deleted rows and is more efficient
        num = col.num_entities
    except Exception:
        num = 0
    # A collection may carry multiple indexes (e.g. dense + sparse hybrid),
    # so iterate col.indexes instead of has_index()/index(), which raise
    # AmbiguousIndexName when more than one index exists.
    indexes = []
    try:
        for idx in col.indexes:
            params = idx.params or {}
            indexes.append({
                "name": idx.index_name,
                "field": idx.field_name,
                "type": params.get("index_type", ""),
                "metric_type": params.get("metric_type", ""),
            })
    except Exception as e:
        _log.warning("Failed to read indexes for %s: %s", collection_name, e)
    index_type = ",".join(i["type"] for i in indexes if i["type"])
    return {
        "collection_name": collection_name,
        "num_entities": num,
        "indexes": indexes,
        "index_type": index_type or "NONE",
        "is_loaded": True,
    }


def list_collections() -> list[str]:
    """Return all collection names that already exist in Milvus."""
    ensure_connected()
    return sorted(utility.list_collections())


def collection_exists(collection_name: str) -> bool:
    ensure_connected()
    return bool(collection_name) and utility.has_collection(collection_name)


def get_collection_schema(collection_name: str) -> dict[str, Any]:
    """Return scalar/vector field metadata for an existing collection."""
    if not collection_exists(collection_name):
        raise pymilvus_exceptions.CollectionNotExistException(
            code=1, message=f"Collection '{collection_name}' does not exist"
        )
    col = Collection(collection_name)
    return {
        "name": col.name,
        "description": col.description or "",
        "fields": [
            {
                "name": field.name,
                "type": str(field.dtype),
                "is_primary": bool(getattr(field, "is_primary", False)),
            }
            for field in col.schema.fields
        ],
    }


_VECTOR_DATA_TYPES = {
    DataType.FLOAT_VECTOR,
    DataType.BINARY_VECTOR,
    DataType.FLOAT16_VECTOR,
    DataType.BFLOAT16_VECTOR,
    DataType.SPARSE_FLOAT_VECTOR,
}


def _collection_fields(collection: Collection) -> list[Any]:
    return list(collection.schema.fields)


def _scalar_output_fields(collection: Collection) -> list[str]:
    return [
        field.name for field in _collection_fields(collection)
        if field.dtype not in _VECTOR_DATA_TYPES
    ]


def _primary_key_field(collection: Collection) -> Any:
    for field in _collection_fields(collection):
        if getattr(field, "is_primary", False):
            return field
    raise ValueError(f"Collection '{collection.name}' has no primary key field")


def _cast_milvus_pk(value: Any) -> Any:
    """Convert string PKs used by the admin UI to the collection's PK type."""
    return int(value) if isinstance(value, str) and value.lstrip("-").isdigit() else value


def collection_primary_key(collection_name: str) -> tuple[str, Any]:
    """Return (pk_field_name, value_cast_fn) for an existing collection."""
    col = Collection(collection_name)
    pk = _primary_key_field(col)
    return pk.name, _cast_milvus_pk


def has_scalar_field(collection_name: str, field_name: str) -> bool:
    col = Collection(collection_name)
    return any(field.name == field_name for field in _collection_fields(col))


def _primary_key_predicate(field: Any) -> str:
    if field.dtype == DataType.VARCHAR:
        return f'{field.name} != ""'
    if field.dtype in {
        DataType.INT8, DataType.INT16, DataType.INT32,
        DataType.INT64, DataType.BOOL,
    }:
        return f"{field.name} != -1"
    return f'{field.name} != ""'


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def query_collection_documents(
    collection_name: str, *,
    q: str = "",
    source: str = "",
    source_ids: list[str] | None = None,
    tenant_id: str = "",
    kb_id: str = "",
    page: int = 1,
    limit: int = 20,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    """Query any existing collection using fields present in its schema.

    This is an admin/browse query, not the retrieval search path. Vector fields
    are intentionally omitted from output; all scalar fields are returned so the
    UI can render different collection schemas without hard-coded names.
    """
    if not collection_exists(collection_name):
        raise pymilvus_exceptions.CollectionNotExistException(
            code=1, message=f"Collection '{collection_name}' does not exist"
        )

    col = Collection(collection_name)
    col.load()
    fields = _collection_fields(col)
    field_names = {field.name for field in fields}
    pk = _primary_key_field(col)
    clauses = [_primary_key_predicate(pk)]

    source_field = next((name for name in ("source", "source_file_id", "file_id") if name in field_names), None)
    if source:
        if not source_field:
            raise ValueError("该 Collection 没有 source/source_file_id 字段，无法按源文件 ID 过滤")
        clauses.append(mx.eq(source_field, source))
    if source_ids:
        if not source_field:
            raise ValueError("该 Collection 没有 source/source_file_id 字段，无法按文件名过滤")
        # Milvus boolean expressions and URL lengths are finite; filename search
        # deliberately works on the latest matching metadata rows.
        clauses.append(mx.in_expr(source_field, source_ids[:500]))
    if tenant_id:
        if "tenant_id" not in field_names:
            raise ValueError("该 Collection 没有 tenant_id 字段")
        clauses.append(mx.eq("tenant_id", tenant_id))
    if kb_id:
        if "kb_id" not in field_names:
            raise ValueError("该 Collection 没有 kb_id 字段")
        clauses.append(mx.eq("kb_id", kb_id))

    if q:
        content_field = next((name for name in ("content", "description", "text")
                              if name in field_names), None)
        if not content_field:
            raise ValueError("该 Collection 没有可搜索的文本字段")
        clauses.append(mx.like(content_field, q))

    expr = mx.and_expr(*clauses)
    total = int(col.query(expr=expr, output_fields=["count(*)"])[0]["count(*)"])
    offset = (page - 1) * limit
    output_fields = _scalar_output_fields(col)
    rows = col.query(expr=expr, output_fields=output_fields, offset=offset, limit=limit)
    documents = []
    for raw in rows:
        item = dict(raw)
        item["data"] = _json_safe(item)
        documents.append(item)
    return documents, total, [field.name for field in fields]


def _tenant_scope(collection_name: str, tenant_id: str | None,
                  kb_id: str | None) -> list[str]:
    if collection_name != app_config.milvus.text_collection:
        return []
    clauses = []
    if tenant_id is not None:
        clauses.append(mx.eq("tenant_id", tenant_id))
    if kb_id is not None:
        clauses.append(mx.eq("kb_id", kb_id))
    return clauses


def query_by_source(source: str, collection_name: str, limit: int = 100,
                    page: int = 1, *, tenant_id: str | None = None,
                    kb_id: str | None = None) -> tuple[list[dict[str, Any]], int]:
    col = _get_collection(collection_name)
    if col is None:
        return [], 0
    expr = mx.and_expr(mx.eq("source", source),
                       *_tenant_scope(collection_name, tenant_id, kb_id))
    try:
        total = col.query(expr=expr, output_fields=["count(*)"])[0]["count(*)"]
    except Exception:
        total = 0
    offset = (page - 1) * limit
    fields = _get_output_fields(collection_name)
    try:
        results = col.query(expr=expr, output_fields=fields, offset=offset, limit=limit)
    except Exception:
        results = []
    return [dict(r) for r in results], total


def query_by_expr(expr: str, collection_name: str, limit: int = 16384) -> list[dict[str, Any]]:
    """Query Milvus with a raw boolean expression (for reconciliation).

    Returns all matching rows with id/parent_id/doc_version metadata.
    """
    col = _get_collection(collection_name)
    if col is None:
        return []
    fields = ["id", "parent_id", "doc_version", "source"]
    try:
        results = col.query(expr=expr, output_fields=fields, limit=limit)
    except Exception:
        _log.warning("query_by_expr failed on %s", collection_name, exc_info=True)
        return []
    return [dict(r) for r in results]


def current_filter_expr() -> str:
    """Build a Milvus boolean expr for is_current filtering.

    External RAG retrieval systems can append this to their search filter
    to exclude stale-version vectors from top-k ranking:

        expr = f"{user_filter} and {mc.current_filter_expr()}"
    """
    return "is_current == true"


def search_by_keyword(keyword: str, collection_name: str, limit: int = 100,
                      page: int = 1, *, tenant_id: str | None = None,
                      kb_id: str | None = None) -> tuple[list[dict[str, Any]], int]:
    col = _get_collection(collection_name)
    if col is None:
        return [], 0
    content_field = "content" if collection_name == app_config.milvus.text_collection else "description"
    expr = mx.and_expr(mx.like(content_field, keyword),
                       *_tenant_scope(collection_name, tenant_id, kb_id))
    try:
        total = col.query(expr=expr, output_fields=["count(*)"])[0]["count(*)"]
    except Exception:
        total = 0
    offset = (page - 1) * limit
    fields = _get_output_fields(collection_name)
    try:
        results = col.query(expr=expr, output_fields=fields, offset=offset, limit=limit)
    except Exception:
        results = []
    return [dict(r) for r in results], total


def list_all_documents(collection_name: str, limit: int = 100,
                       page: int = 1, *, tenant_id: str | None = None,
                       kb_id: str | None = None) -> tuple[list[dict[str, Any]], int]:
    col = _get_collection(collection_name)
    if col is None:
        return [], 0
    try:
        scope = _tenant_scope(collection_name, tenant_id, kb_id)
        if scope:
            total = col.query(expr=mx.and_expr(*scope),
                              output_fields=["count(*)"])[0]["count(*)"]
        else:
            total = col.num_entities
    except Exception:
        total = 0
    offset = (page - 1) * limit
    fields = _get_output_fields(collection_name)
    try:
        expr = mx.and_expr("id != ''",
                           *_tenant_scope(collection_name, tenant_id, kb_id))
        results = col.query(expr=expr, output_fields=fields, offset=offset, limit=limit)
    except Exception:
        results = []
    return [dict(r) for r in results], total


def _get_output_fields(collection_name: str) -> list[str]:
    if collection_name == app_config.milvus.text_collection:
        return ["id", "tenant_id", "kb_id", "content", "source",
                "mushroom_type", "product_id", "category", "parent_id",
                "chunk_index", "doc_version", "is_current"]
    else:
        return ["id", "image_url", "description", "mushroom_type", "product_id", "source"]


def purge_file_vectors(file_id: str, file_type: str, name: str = "",
                       name_unique: bool = False, *,
                       tenant_id: str | None = None,
                       kb_id: str | None = None) -> int:
    """Delete all vectors for a file from the appropriate Milvus collection.

    Shared by main.py (sync fallback) and celery purge task (async path).
    ``name_unique`` guards against wiping a same-named sibling's legacy rows.
    """
    coll = (app_config.milvus.text_collection if file_type == "text"
            else app_config.milvus.image_collection)
    # The legacy image collection has no tenant columns yet; scoping is applied
    # only where the schema actually carries tenant_id/kb_id.
    scoped = coll == app_config.milvus.text_collection
    scope = [
        mx.eq("source", file_id),
    ]
    if scoped and tenant_id is not None:
        scope.append(mx.eq("tenant_id", tenant_id))
    if scoped and kb_id is not None:
        scope.append(mx.eq("kb_id", kb_id))
    total = delete_by_expr(mx.and_expr(*scope), coll)
    if name and name_unique:
        legacy_scope = [mx.eq("source", name)]
        if scoped and tenant_id is not None:
            legacy_scope.append(mx.eq("tenant_id", tenant_id))
        if scoped and kb_id is not None:
            legacy_scope.append(mx.eq("kb_id", kb_id))
        total += delete_by_expr(mx.and_expr(*legacy_scope), coll)
    return total
