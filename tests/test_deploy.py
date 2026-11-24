import sqlite3

import pytest

from deploy.windows.migrate_database import migrate


def test_service_database_migration_preserves_scheduler_state(tmp_path):
    source = tmp_path / "scheduler.db"
    destination = tmp_path / "service" / "scheduler.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE job_definitions (id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO job_definitions VALUES ('daily-root')")
        db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")
        db.executemany("INSERT INTO runs VALUES (?)", [(1,), (2,)])

    migrate(source, destination)

    with sqlite3.connect(destination) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT id FROM job_definitions").fetchall() == [("daily-root",)]
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
    assert source.exists()


def test_service_database_migration_never_overwrites_state(tmp_path):
    source = tmp_path / "scheduler.db"
    destination = tmp_path / "service.db"
    source.write_bytes(b"source")
    destination.write_bytes(b"keep")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        migrate(source, destination)

    assert destination.read_bytes() == b"keep"
