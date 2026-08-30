"""Add is_current BOOLEAN field to the text collection schema.

Milvus cannot ALTER TABLE ADD COLUMN in place. This script:
1. Checks if ``is_current`` already exists.
2. Creates a temporary collection with the new schema.
3. Copies all entities from the old collection (is_current = True).
4. Drops the old collection and renames the temp one.

Run AFTER stopping all ingest workers to avoid concurrent writes.

Example:
    python scripts/migrate_milvus_add_is_current.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE))

from pymilvus import Collection, utility  # noqa: E402

from src.config import config as app_config  # noqa: E402
from src.logging_config import get_logger  # noqa: E402
from src import milvus_client as mc  # noqa: E402

_log = get_logger(__name__)


def _has_field(collection: Collection, field_name: str) -> bool:
    return any(f.name == field_name for f in collection.schema.fields)


def migrate() -> None:
    mc.ensure_connected()
    name = app_config.milvus.text_collection

    if not utility.has_collection(name):
        _log.info("Collection %s does not exist; nothing to migrate.", name)
        return

    old = Collection(name)
    old.load()

    if _has_field(old, "is_current"):
        _log.info("Collection %s already has is_current; skipping.", name)
        return

    dim = old.schema.fields[-2].params.get("dim", 512)  # embedding field
    tmp_name = f"{name}_migrating_{__import__('uuid').uuid4().hex[:8]}"
    new_col = mc._create_text_collection(tmp_name, dim)
    new_col.load()

    output_fields = [
        f.name for f in old.schema.fields if f.name != "sparse"
    ]
    _log.info("Copying entities from %s → %s …", name, tmp_name)

    batch_size = 500
    offset = 0
    total_copied = 0
    while True:
        rows = old.query(expr="", output_fields=output_fields,
                         offset=offset, limit=batch_size)
        if not rows:
            break
        entities = []
        for r in rows:
            e = dict(r)
            e["is_current"] = True
            entities.append(e)
        new_col.insert(entities)
        total_copied += len(entities)
        offset += batch_size

    new_col.flush()
    _log.info("Copied %d entities.", total_copied)

    # Verify count matches before swapping.
    old.load()
    old_count = old.query(expr="", output_fields=["count(*)"])[0]["count(*)"]
    if total_copied < old_count:
        _log.error("Copy incomplete (%d / %d). Aborting without swap.",
                   total_copied, old_count)
        utility.drop_collection(tmp_name)
        return

    utility.drop_collection(name)
    utility.rename_collection(tmp_name, name)
    _log.info("Migration complete: %s now includes is_current field.", name)


if __name__ == "__main__":
    migrate()
