"""Deploy Alembic migrations safely (ROADMAP P4-T4): backup first, then upgrade.

Usage:
    python scripts/migrate.py            # backup + upgrade head
    python scripts/migrate.py --no-backup
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = Path(__file__).resolve().parent.parent


def main() -> None:
    if "--no-backup" not in sys.argv:
        print("=== backup before migration ===")
        backup = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "backup_postgres.py")],
            cwd=str(BASE),
        )
        if backup.returncode != 0:
            # Enterprise default: backup failure ABORTS the migration (P4-T2 修复).
            # Pass --no-backup to override.
            print("backup FAILED — aborting migration (pass --no-backup to override)")
            sys.exit(1)
    print("=== alembic upgrade head ===")
    up = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=str(BASE))
    sys.exit(up.returncode)


if __name__ == "__main__":
    main()
