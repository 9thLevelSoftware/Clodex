from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    @contextmanager
    def session(self):
        con = self.connect()
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _init(self) -> None:
        with self.session() as con:
            con.executescript(
                """
                create table if not exists schema_version (
                    version integer not null
                );
                create table if not exists tasks (
                    id text primary key,
                    title text not null,
                    status text not null,
                    created_at text not null,
                    updated_at text not null
                );
                create table if not exists runs (
                    id text primary key,
                    task_id text,
                    status text not null,
                    prompt text not null,
                    diff_hash text,
                    created_at text not null,
                    updated_at text not null
                );
                create table if not exists audits (
                    id integer primary key autoincrement,
                    run_id text not null,
                    agent text not null,
                    approved integer not null,
                    diff_hash text,
                    verdict_json text not null,
                    created_at text not null
                );
                create table if not exists messages (
                    id integer primary key autoincrement,
                    task_id text,
                    topic text not null,
                    body text not null,
                    created_at text not null
                );
                create table if not exists run_events (
                    id integer primary key autoincrement,
                    run_id text not null,
                    event text not null,
                    data_json text not null,
                    created_at text not null
                );
                create table if not exists artifacts (
                    id integer primary key autoincrement,
                    run_id text not null,
                    name text not null,
                    path text not null,
                    kind text not null,
                    created_at text not null
                );
                create table if not exists workspace_locks (
                    run_id text primary key,
                    source_path text not null,
                    workspace_path text not null,
                    backend text not null,
                    created_at text not null,
                    released_at text
                );
                create table if not exists cancellations (
                    run_id text primary key,
                    requested integer not null,
                    requested_at text not null,
                    completed_at text
                );
                """
            )
            self._ensure_run_columns(con)
            current = con.execute("select version from schema_version order by version desc limit 1").fetchone()
            if current is None:
                con.execute("insert into schema_version(version) values (2)")
            elif int(current["version"]) < 2:
                con.execute("delete from schema_version")
                con.execute("insert into schema_version(version) values (2)")

    def _ensure_run_columns(self, con: sqlite3.Connection) -> None:
        existing = {row["name"] for row in con.execute("pragma table_info(runs)")}
        columns = {
            "workspace_path": "text",
            "artifacts_dir": "text",
            "pid": "integer",
            "error": "text",
            "started_at": "text",
            "completed_at": "text",
        }
        for name, kind in columns.items():
            if name not in existing:
                con.execute(f"alter table runs add column {name} {kind}")

    def table_names(self) -> set[str]:
        with self.session() as con:
            return {str(row["name"]) for row in con.execute("select name from sqlite_master where type='table'")}

    def schema_version(self) -> int:
        with self.session() as con:
            row = con.execute("select version from schema_version order by version desc limit 1").fetchone()
            return int(row["version"]) if row else 0

    def upsert_task(self, task_id: str, title: str, status: str = "todo") -> None:
        timestamp = now_iso()
        with self.session() as con:
            con.execute(
                """
                insert into tasks(id, title, status, created_at, updated_at)
                values (?, ?, ?, ?, ?)
                on conflict(id) do update set
                    title=excluded.title,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (task_id, title, status, timestamp, timestamp),
            )

    def update_task(self, task_id: str, status: str) -> None:
        with self.session() as con:
            con.execute(
                "update tasks set status=?, updated_at=? where id=?",
                (status, now_iso(), task_id),
            )

    def create_run(
        self,
        run_id: str,
        task_id: str,
        prompt: str,
        status: str,
        workspace_path: str | None = None,
        artifacts_dir: str | None = None,
        pid: int | None = None,
    ) -> None:
        timestamp = now_iso()
        with self.session() as con:
            con.execute(
                """
                insert into runs(id, task_id, status, prompt, workspace_path, artifacts_dir, pid, created_at, updated_at, started_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, task_id, status, prompt, workspace_path, artifacts_dir, pid, timestamp, timestamp, timestamp),
            )

    def update_run(
        self,
        run_id: str,
        status: str,
        diff_hash: str | None = None,
        workspace_path: str | None = None,
        artifacts_dir: str | None = None,
        pid: int | None = None,
        error: str | None = None,
    ) -> None:
        completed = now_iso() if status in {"approved", "blocked", "failed", "cancelled", "applied"} else None
        with self.session() as con:
            con.execute(
                """
                update runs set
                    status=?,
                    diff_hash=coalesce(?, diff_hash),
                    workspace_path=coalesce(?, workspace_path),
                    artifacts_dir=coalesce(?, artifacts_dir),
                    pid=coalesce(?, pid),
                    error=coalesce(?, error),
                    completed_at=coalesce(?, completed_at),
                    updated_at=?
                where id=?
                """,
                (status, diff_hash, workspace_path, artifacts_dir, pid, error, completed, now_iso(), run_id),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.session() as con:
            row = con.execute("select * from runs where id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def add_audit(self, run_id: str, agent: str, approved: bool, diff_hash: str | None, verdict_json: str) -> None:
        with self.session() as con:
            con.execute(
                """
                insert into audits(run_id, agent, approved, diff_hash, verdict_json, created_at)
                values (?, ?, ?, ?, ?, ?)
                """,
                (run_id, agent, 1 if approved else 0, diff_hash, verdict_json, now_iso()),
            )

    def add_message(self, task_id: str | None, topic: str, body: str) -> None:
        with self.session() as con:
            con.execute(
                "insert into messages(task_id, topic, body, created_at) values (?, ?, ?, ?)",
                (task_id, topic, body, now_iso()),
            )

    def add_event(self, run_id: str, event: str, data: dict[str, Any]) -> None:
        import json

        with self.session() as con:
            con.execute(
                "insert into run_events(run_id, event, data_json, created_at) values (?, ?, ?, ?)",
                (run_id, event, json.dumps(data, sort_keys=True), now_iso()),
            )

    def add_artifact(self, run_id: str, name: str, path: str, kind: str) -> None:
        with self.session() as con:
            con.execute(
                "insert into artifacts(run_id, name, path, kind, created_at) values (?, ?, ?, ?, ?)",
                (run_id, name, path, kind, now_iso()),
            )

    def add_workspace_lock(self, run_id: str, source_path: str, workspace_path: str, backend: str) -> None:
        with self.session() as con:
            con.execute(
                """
                insert into workspace_locks(run_id, source_path, workspace_path, backend, created_at)
                values (?, ?, ?, ?, ?)
                on conflict(run_id) do update set
                    source_path=excluded.source_path,
                    workspace_path=excluded.workspace_path,
                    backend=excluded.backend
                """,
                (run_id, source_path, workspace_path, backend, now_iso()),
            )

    def request_cancel(self, run_id: str) -> None:
        with self.session() as con:
            con.execute(
                """
                insert into cancellations(run_id, requested, requested_at)
                values (?, 1, ?)
                on conflict(run_id) do update set requested=1, requested_at=excluded.requested_at
                """,
                (run_id, now_iso()),
            )
            con.execute("update runs set status=?, updated_at=? where id=? and status not in ('approved','blocked','failed','cancelled')", ("cancel_requested", now_iso(), run_id))

    def cancellation_requested(self, run_id: str) -> bool:
        with self.session() as con:
            row = con.execute("select requested from cancellations where run_id=?", (run_id,)).fetchone()
            return bool(row and row["requested"])

    def complete_cancel(self, run_id: str) -> None:
        with self.session() as con:
            con.execute("update cancellations set completed_at=? where run_id=?", (now_iso(), run_id))
            con.execute("update runs set status=?, completed_at=?, updated_at=? where id=?", ("cancelled", now_iso(), now_iso(), run_id))

    def list_tasks(self) -> list[dict[str, Any]]:
        with self.session() as con:
            return [dict(row) for row in con.execute("select * from tasks order by updated_at desc")]

    def list_runs(self) -> list[dict[str, Any]]:
        with self.session() as con:
            return [dict(row) for row in con.execute("select * from runs order by updated_at desc limit 20")]
