"""Migrate the legacy shared text collection to the parent-child schema.

The current Milvus server cannot add required ``parent_id`` / ``doc_version``
fields in place. This script copies entities to a temporary collection, maps
old rows to SQLite parent IDs where possible, upserts orphan parent content
into MySQL, and finally swaps collection names. The legacy collection is kept
as a backup rather than dropped.

Example:
    python scripts/migrate_milvus_text_schema.py
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import uuid
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE))

from pymilvus import Collection, connections, utility  # noqa: E402

from src import db  # noqa: E402
from src import milvus_client as mc  # noqa: E402
from src import mysql_client  # noqa: E402
from src.config import config as app_config  # noqa: E402


_PARENT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "rag-parent-block-v1")


def _chunk(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    return value[:limit]


def _load_sqlite_parent_map() -> dict[str, str]:
    with db._db() as conn:
        rows = conn.execute("SELECT id, parent_id FROM chunks").fetchall()
    return {r["id"]: r["parent_id"] for r in rows if r["parent_id"]}


def _fallback_parent_id(tenant_id: str, kb_id: str, title: str,
                        content: str) -> str:
    normalized = " ".join((content or "").split()).casefold()
    normalized_title = " ".join((title or "").split()).casefold()
    identity = (
        f"{tenant_id}|{kb_id}|{normalized_title}|"
        f"{hashlib.sha256(normalized.encode()).hexdigest()}"
    )
    return uuid.uuid5(_PARENT_NAMESPACE, identity).hex


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--temp-suffix", default="_v2_migration")
    args = parser.parse_args()

    old_name = app_config.milvus.text_collection
    temp_name = old_name + args.temp_suffix
    backup_name = old_name + "_legacy_schema"

    mc.ensure_connected()
    if utility.has_collection(temp_name):
        raise SystemExit(f"Temporary collection already exists: {temp_name}")
    if utility.has_collection(backup_name):
        raise SystemExit(f"Legacy backup collection already exists: {backup_name}")

    print(f"This will copy {old_name} to {temp_name}, then swap the names.")
    print(f"The legacy collection remains as {old_name}_legacy_schema.")
    reply = input("Type 'MIGRATE' to continue: ")
    if reply.strip() != "MIGRATE":
        print("Aborted.")
        return 1

    old = Collection(old_name)
    fields = {field.name for field in old.schema.fields}
    required_old = {"id", "tenant_id", "kb_id", "content", "embedding"}
    if not required_old <= fields:
        raise SystemExit(f"Source collection lacks expected fields: {sorted(required_old - fields)}")

    print(f"Creating new schema collection: {temp_name}")
    new = mc._create_text_collection(temp_name, app_config.milvus.dim)
    sqlite_parents = _load_sqlite_parent_map()
    ensured_mysql_parents: set[tuple[str, str, str]] = set()
    copied = 0

    output_fields = [
        name for name in (
            "id", "tenant_id", "kb_id", "product_id", "content", "source",
            "category", "parent_title", "parent_content", "mushroom_type",
            "chunk_index",
        ) if name in fields
    ]
    iterator = old.query_iterator(
        expr="id != ''", output_fields=output_fields, batch_size=args.batch_size,
    )
    try:
        while True:
            rows = iterator.next()
            if not rows:
                break
            entities = []
            for row in rows:
                item = dict(row)
                tenant_id = _chunk(item.get("tenant_id") or "default", 64) or "default"
                kb_id = _chunk(item.get("kb_id") or "default", 64) or "default"
                parent_id = sqlite_parents.get(item["id"])
                if not parent_id:
                    parent_content = item.get("parent_content") or ""
                    parent_id = _fallback_parent_id(
                        tenant_id, kb_id,
                        item.get("parent_title") or "", parent_content,
                    )

                mysql_key = (tenant_id, kb_id, parent_id)
                if item.get("parent_content") and mysql_key not in ensured_mysql_parents:
                    source = item.get("source") or ""
                    is_uuid = len(source) == 32 and all(c in "0123456789abcdef" for c in source)
                    mysql_client.insert_parent_block(
                        parent_id,
                        item.get("parent_title") or "",
                        parent_content,
                        tenant_id=tenant_id,
                        kb_id=kb_id,
                        summary=parent_content[:800],
                        source_type="document",
                        source_id=_chunk(source, 255) if is_uuid else None,
                        category=_chunk(item.get("category"), 128),
                    )
                    ensured_mysql_parents.add(mysql_key)

                entities.append({
                    "id": item["id"],
                    "tenant_id": tenant_id,
                    "kb_id": kb_id,
                    "product_id": _chunk(item.get("product_id"), 128),
                    "content": item["content"],
                    "source": _chunk(item.get("source"), 512),
                    "category": _chunk(item.get("category"), 128),
                    "parent_id": parent_id,
                    "chunk_index": int(item.get("chunk_index") or 0),
                    "doc_version": 1,
                    "mushroom_type": _chunk(item.get("mushroom_type"), 128),
                    "embedding": item["embedding"],
                })

            if entities:
                new.insert(entities)
                new.flush()
                copied += len(entities)
                print(f"  copied {copied} entities")
    finally:
        iterator.close()

    new.load()
    if new.num_entities != copied:
        raise SystemExit(f"Copy verification failed: reported={copied}, visible={new.num_entities}")

    print(f"Swapping names: {old_name} -> {backup_name}; {temp_name} -> {old_name}")
    utility.rename_collection(old_name, backup_name)
    try:
        utility.rename_collection(temp_name, old_name)
    except Exception:
        utility.rename_collection(backup_name, old_name)
        raise

    print(f"Migrated {copied} entities. Keep the backup until search has been verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
