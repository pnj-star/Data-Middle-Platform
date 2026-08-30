"""One-off helper to wipe Milvus vectors + reset SQLite statuses for dedup re-testing.

WARNING: DESTRUCTIVE. Deletes ALL vectors in the text collection (and optionally
the image collection) shared with the rag project. Intended to give the
content-fingerprint dedup a clean starting point: after running this you must
re-ingest the files from the UI so Milvus is rebuilt WITH dedup applied.

The SQLite status reset is REQUIRED, not cosmetic. ``src.dedup.filter_chunks``
trusts files with ``status='done'`` to be present in Milvus (its dedup base is
``chunks WHERE deduplicated=0 AND status='done'``). Wiping Milvus without
resetting those statuses makes new ingestions skip sections against vectors that
no longer exist — silent data loss. So this flips text files ``done -> chunked``,
which also surfaces them in the UI as "needs re-ingest".

Usage:
    python scripts/reset_milvus_for_dedup_test.py           # text collection only
    python scripts/reset_milvus_for_dedup_test.py --images  # also wipe image collection
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE))

from src import db  # noqa: E402
from src import milvus_client as mc  # noqa: E402
from src.config import config as app_config  # noqa: E402


def _wipe(name: str) -> int:
    """Delete every entity in a collection in place (collection stays loaded).

    Uses ``col.delete`` + ``flush`` + ``load`` — the same pattern as
    ``src.milvus_client.delete_by_expr``. The ``load()`` is REQUIRED: without it
    ``num_entities`` stays stale and still reports the pre-delete count. Never
    ``drop_collection`` here — the text collection carries a rag-owned sparse
    field + indexes that the pipeline's create path would not reproduce.
    """
    col = mc.get_text_collection() if name == app_config.milvus.text_collection else mc.get_image_collection()
    if col is None:
        print(f"  [skip] collection not found: {name}")
        return 0
    before = col.num_entities
    result = col.delete(expr="id != ''")
    col.flush()
    col.load()
    after = col.num_entities
    deleted = result.delete_count if result else 0
    if after != 0:
        print(f"  [warn] {name} still has {after} entities after delete (report {deleted})")
    print(f"  wiped {name}: {before} -> {after} entities (deleted {deleted})")
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(description="Wipe Milvus vectors for dedup re-test")
    parser.add_argument("--images", action="store_true",
                        help="also wipe the image collection (shared with rag search)")
    args = parser.parse_args()

    names = [app_config.milvus.text_collection]
    if args.images:
        names.append(app_config.milvus.image_collection)

    print("This will DELETE ALL vectors in: " + ", ".join(names))
    print("and flip text files done -> chunked in SQLite (dedup base reset).")
    reply = input("Type 'yes' to continue: ")
    if reply.strip().lower() != "yes":
        print("Aborted.")
        return 1

    mc.ensure_connected()
    total = sum(_wipe(n) for n in names)

    with db._db() as conn:
        cur = conn.execute(
            "UPDATE files SET status = 'chunked' WHERE type = 'text' AND status = 'done'"
        )
        conn.commit()
        n_reset = cur.rowcount

    print(f"Deleted {total} vector(s). Reset {n_reset} text file(s) done -> chunked.")
    print("Next: re-ingest the files from the UI — dedup applies at ingest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
