"""Create the wiki-domain Milvus collection (ROADMAP P0-T4).

Owns `wiki_knowledge` — separate from the rag-shared mushroom_knowledge /
mushroom_images (ROADMAP D1). The schema carries page_id / revision_id /
space_id so search results trace back to a page + revision (ROADMAP D3); row
ids are `{page_id}:{revision_id}:{chunk_index}`.

Idempotent: if the collection already exists it is verified and left untouched
(never dropped). Rebuild explicitly with --force (drops + recreates).

Usage:
    python scripts/create_wiki_collection.py
    python scripts/create_wiki_collection.py --force
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.wiki.milvus import create_wiki_collection, wiki_collection_name


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the wiki Milvus collection")
    parser.add_argument("--force", action="store_true", help="drop & recreate if present")
    args = parser.parse_args()

    col = create_wiki_collection(force=args.force)
    from src.wiki.milvus import verify_wiki_collection

    problems = verify_wiki_collection(col)
    fields = [f.name for f in col.schema.fields]
    print(f"wiki collection '{wiki_collection_name()}' ready")
    print("  fields:", ", ".join(fields))
    print("  num_entities:", col.num_entities)
    if problems:
        print("  WARNING schema mismatch:", "; ".join(problems))
    else:
        print("  schema verified: OK")


if __name__ == "__main__":
    main()
