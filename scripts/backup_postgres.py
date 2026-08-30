"""Postgres backup for the wiki (ROADMAP P4-T4).

Uses `pg_dump` (structure + data, custom format). Requires the PostgreSQL
client tools on PATH — install postgresql-client, or run from a machine that
has them. Keeps the newest BACKUP_KEEP (default 7) dumps.

Usage:
    python scripts/backup_postgres.py
    BACKUP_KEEP=14 python scripts/backup_postgres.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import config as app_config  # noqa: E402


def main() -> None:
    cfg = app_config.postgres
    out_dir = Path(app_config.data_dir_abs) / "backups" / "postgres"
    out_dir.mkdir(parents=True, exist_ok=True)

    pg_dump = shutil.which("pg_dump")
    if pg_dump is None:
        sys.exit(
            "pg_dump not found on PATH. Install PostgreSQL client tools "
            "(e.g. apt install postgresql-client), or restore schema via Alembic "
            "and data via a logical export. See docs/backup.md."
        )

    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"wiki-{stamp}.dump"
    env = {**os.environ, "PGPASSWORD": cfg.password}
    cmd = [
        pg_dump,
        "-h", cfg.host, "-p", str(cfg.port), "-U", cfg.user, "-d", cfg.db,
        "-Fc",  # custom format: schema + data, restorable
        "-f", str(target),
    ]
    subprocess.run(cmd, env=env, check=True)

    keep = int(os.environ.get("BACKUP_KEEP", "7"))
    dumps = sorted(out_dir.glob("wiki-*.dump"))
    for old in dumps[:-keep]:
        old.unlink()
        print(f"removed old backup {old.name}")
    print(f"backup written: {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
