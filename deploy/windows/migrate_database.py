"""Create a consistent SQLite backup for the Windows service state directory."""

from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def migrate(source: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing service database: {destination}")
    if not source.exists():
        print(f"No existing database at {source}; the service will initialize a new one.")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f"{destination.name}.", suffix=".initializing", dir=destination.parent, delete=False
    ) as temporary_file:
        temporary = Path(temporary_file.name)
    try:
        with closing(
            sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
        ) as origin:
            with closing(sqlite3.connect(temporary)) as target, target:
                origin.backup(target, pages=256, sleep=0.05)
                result = target.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise RuntimeError(f"source database integrity check failed: {result}")
                tables = {
                    row[0]
                    for row in target.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if "job_definitions" not in tables or "runs" not in tables:
                    raise RuntimeError("source database is missing scheduler tables")
                definitions = target.execute("SELECT COUNT(*) FROM job_definitions").fetchone()[0]
                runs = target.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        os.link(temporary, destination)
        temporary.unlink()
        print(f"Copied {definitions} task definitions and {runs} execution records.")
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    migrate(args.source, args.destination)


if __name__ == "__main__":
    main()
