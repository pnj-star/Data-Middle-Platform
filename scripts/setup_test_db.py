"""Bootstrap the wiki test database for local dev / CI (ROADMAP P0-T6).

Ensures the dev Postgres container is up, drops + recreates the dedicated test
database, and runs Alembic migrations against it. Run this before wiki tests
when the Postgres container is not already running (the pytest fixture also
does this, but only per session).

Usage:
    python scripts/setup_test_db.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.wiki.testing import reset_test_database, test_database_name, test_dsn  # noqa: E402


def main() -> None:
    print("ensuring postgres container is up ...")
    proc = subprocess.run(["docker", "compose", "up", "-d", "postgres"], check=False)
    if proc.returncode != 0:
        print("WARNING: docker compose up returned non-zero; assuming postgres is already reachable")

    reset_test_database()
    print(f"test database '{test_database_name()}' ready")
    print(f"  dsn: {test_dsn()}")


if __name__ == "__main__":
    main()
