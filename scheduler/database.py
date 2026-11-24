from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine


def make_engine(url: str):
    options = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    if url.startswith("sqlite:///./"):
        Path(url.removeprefix("sqlite:///./")).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url, **options)


class RunStore:
    def __init__(self, url: str):
        if not url.startswith("sqlite:"):
            raise ValueError("Run history currently requires a SQLite database URL")
        path = url.removeprefix("sqlite:///")
        if path == ":memory:":
            self.path = ":memory:"
            self._memory = sqlite3.connect(self.path, check_same_thread=False)
        else:
            self.path = path
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._memory = None
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                exit_code INTEGER,
                output TEXT NOT NULL DEFAULT '',
                definition_revision INTEGER NOT NULL DEFAULT 1
            )""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
            if "workflow_run_id" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN workflow_run_id TEXT")
            if "definition_revision" not in columns:
                db.execute(
                    "ALTER TABLE runs ADD COLUMN definition_revision INTEGER NOT NULL DEFAULT 1"
                )
            db.execute("""CREATE TABLE IF NOT EXISTS job_definitions (
                id TEXT PRIMARY KEY,
                definition TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1
            )""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS ix_runs_job_started_id ON runs(job_id, started_at, id)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS ix_runs_started_id ON runs(started_at, id)")
            job_columns = {row[1] for row in db.execute("PRAGMA table_info(job_definitions)")}
            if "revision" not in job_columns:
                db.execute(
                    "ALTER TABLE job_definitions ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
                )

    def _connect(self):
        return self._memory if self._memory is not None else sqlite3.connect(self.path)

    def check(self) -> None:
        db = self._connect()
        db.execute("SELECT 1").fetchone()

    def recover_interrupted_runs(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        message = (
            "Scheduler service restarted before this task finished. Execution state is unknown; "
            "verify external outputs before rerunning."
        )
        db = self._connect()
        cursor = db.execute(
            "UPDATE runs SET finished_at=?, status='failed', exit_code=-1, "
            "output=CASE WHEN output='' THEN ? ELSE substr(output || char(10) || ?, -20000) END "
            "WHERE status='running'",
            (now, message, message),
        )
        db.commit()
        return cursor.rowcount

    def save_job(self, definition: dict) -> int:
        db = self._connect()
        now = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(definition)
        row = db.execute(
            "SELECT definition, revision FROM job_definitions WHERE id=?", (definition["id"],)
        ).fetchone()
        if row is None:
            revision = 1
            db.execute(
                "INSERT INTO job_definitions(id, definition, created_at, updated_at, revision) "
                "VALUES (?, ?, ?, ?, ?)",
                (definition["id"], encoded, now, now, revision),
            )
        elif row[0] == encoded:
            revision = row[1]
        else:
            revision = row[1] + 1
            db.execute(
                "UPDATE job_definitions SET definition=?, updated_at=?, revision=? WHERE id=?",
                (encoded, now, revision, definition["id"]),
            )
        db.commit()
        return revision

    def get_job(self, job_id: str) -> dict | None:
        db = self._connect()
        row = db.execute("SELECT definition FROM job_definitions WHERE id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_jobs(self) -> list[dict]:
        db = self._connect()
        return [
            json.loads(row[0])
            for row in db.execute(
                "SELECT definition FROM job_definitions ORDER BY created_at, id"
            ).fetchall()
        ]

    def job_metadata(self, job_id: str) -> dict | None:
        db = self._connect()
        row = db.execute(
            "SELECT revision, created_at, updated_at FROM job_definitions WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        return {"revision": row[0], "created_at": row[1], "updated_at": row[2]}

    def delete_job(self, job_id: str) -> None:
        db = self._connect()
        db.execute("DELETE FROM job_definitions WHERE id=?", (job_id,))
        db.commit()

    def start(
        self, job_id: str, workflow_run_id: str | None = None, definition_revision: int = 1
    ) -> int:
        db = self._connect()
        cursor = db.execute(
            "INSERT INTO runs(job_id, started_at, status, workflow_run_id, definition_revision) "
            "VALUES (?, ?, 'running', ?, ?)",
            (job_id, datetime.now(timezone.utc).isoformat(), workflow_run_id, definition_revision),
        )
        db.commit()
        return cursor.lastrowid

    def finish(self, run_id: int, status: str, exit_code: int | None, output: str) -> None:
        db = self._connect()
        db.execute(
            "UPDATE runs SET finished_at=?, status=?, exit_code=?, "
            "output=substr(output || ?, -20000) WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), status, exit_code, output, run_id),
        )
        db.commit()

    def append_output(self, run_id: int, chunk: str) -> None:
        if not chunk:
            return
        db = self._connect()
        db.execute(
            "UPDATE runs SET output=substr(output || ?, -20000) WHERE id=?",
            (chunk, run_id),
        )
        db.commit()

    def list(
        self,
        limit: int = 100,
        job_id: str | None = None,
        include_output: bool = True,
        offset: int = 0,
        started_after: str | None = None,
        started_before: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        self.prune_runs()
        db = self._connect()
        db.row_factory = sqlite3.Row
        columns = (
            "*"
            if include_output
            else (
                "id, job_id, started_at, finished_at, status, exit_code, workflow_run_id, "
                "definition_revision"
            )
        )
        clauses = []
        parameters: list = []
        if job_id:
            clauses.append("job_id=?")
            parameters.append(job_id)
        if started_after:
            clauses.append("started_at>=?")
            parameters.append(started_after)
        if started_before:
            clauses.append("started_at<?")
            parameters.append(started_before)
        if status:
            clauses.append("status=?")
            parameters.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((limit, offset))
        rows = db.execute(
            f"SELECT {columns} FROM runs{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id: int) -> dict | None:
        self.prune_runs()
        db = self._connect()
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def recent_by_job(self, days: int = 5) -> dict[str, list[dict]]:
        self.prune_runs(retention_days=days)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        db = self._connect()
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT id, job_id, started_at, status, exit_code FROM runs "
            "WHERE started_at>=? ORDER BY started_at, id",
            (cutoff,),
        ).fetchall()
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            run = dict(row)
            grouped.setdefault(run["job_id"], []).append(run)
        return grouped

    def count_runs(
        self,
        job_id: str | None = None,
        started_after: str | None = None,
        started_before: str | None = None,
        status: str | None = None,
    ) -> int:
        self.prune_runs()
        clauses = []
        parameters: list = []
        for column, value, operator in (
            ("job_id", job_id, "="),
            ("started_at", started_after, ">="),
            ("started_at", started_before, "<"),
            ("status", status, "="),
        ):
            if value is not None:
                clauses.append(f"{column}{operator}?")
                parameters.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        db = self._connect()
        row = db.execute(f"SELECT COUNT(*) FROM runs{where}", parameters).fetchone()
        return int(row[0])

    def prune_runs(self, retention_days: int = 5) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        db = self._connect()
        cursor = db.execute("DELETE FROM runs WHERE started_at<? AND status!='running'", (cutoff,))
        db.commit()
        return cursor.rowcount

    def latest_status_between(self, job_id: str, start: str, end: str) -> str | None:
        db = self._connect()
        row = db.execute(
            "SELECT status FROM runs WHERE job_id=? AND started_at>=? AND started_at<? "
            "ORDER BY id DESC LIMIT 1",
            (job_id, start, end),
        ).fetchone()
        return row[0] if row else None
