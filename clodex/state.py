from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _int_or_default(value: Any, default: int) -> int:
    return default if value is None else int(value)


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
            "owner": "text",
            "phase": "text",
            "handoff_count": "integer not null default 0",
            "handoff_budget": "integer not null default 6",
            "last_actor": "text",
            "blocked_reason": "text",
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
        timestamp = now_iso()
        completed = timestamp if status in {"approved", "blocked", "failed", "cancelled", "applied"} else None
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
                (status, diff_hash, workspace_path, artifacts_dir, pid, error, completed, timestamp, run_id),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.session() as con:
            row = con.execute("select * from runs where id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def create_handoff(
        self,
        run_id: str,
        prompt: str,
        owner: str = "claude",
        phase: str = "planning",
        handoff_budget: int = 6,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        with self.session() as con:
            con.execute(
                """
                insert into runs(
                    id, task_id, status, prompt, owner, phase, handoff_count,
                    handoff_budget, created_at, updated_at, started_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, task_id, "handoff", prompt, owner, phase, 0, handoff_budget, timestamp, timestamp, timestamp),
            )
            self._insert_event(
                con,
                run_id,
                "handoff.create",
                {"owner": owner, "phase": phase, "handoff_budget": handoff_budget, "task_id": task_id},
                timestamp,
            )
            row = con.execute("select * from runs where id=?", (run_id,)).fetchone()
            return dict(row)

    def update_handoff(
        self,
        run_id: str,
        phase: str | None = None,
        actor: str | None = None,
        owner: str | None = None,
        report: dict[str, Any] | None = None,
        increment_handoff: bool = False,
        status: str | None = None,
        blocked_reason: str | None = None,
        diff_hash: str | None = None,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        with self.session() as con:
            row = con.execute("select * from runs where id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown run: {run_id}")

            current = dict(row)
            handoff_count = _int_or_default(current.get("handoff_count"), 0)
            handoff_budget = _int_or_default(current.get("handoff_budget"), 6)
            next_count = handoff_count + (1 if increment_handoff else 0)
            next_status = status or str(current["status"])
            next_blocked_reason = blocked_reason if blocked_reason is not None else current.get("blocked_reason")
            completed = None

            if next_count > handoff_budget:
                next_status = "blocked"
                next_blocked_reason = "handoff budget exhausted"
                completed = timestamp
            elif next_status in {"approved", "blocked", "failed", "cancelled", "applied"}:
                completed = timestamp

            con.execute(
                """
                update runs set
                    status=?,
                    owner=coalesce(?, owner),
                    phase=coalesce(?, phase),
                    handoff_count=?,
                    last_actor=coalesce(?, last_actor),
                    diff_hash=coalesce(?, diff_hash),
                    blocked_reason=?,
                    completed_at=coalesce(?, completed_at),
                    updated_at=?
                where id=?
                """,
                (
                    next_status,
                    owner,
                    phase,
                    next_count,
                    actor,
                    diff_hash,
                    next_blocked_reason,
                    completed,
                    timestamp,
                    run_id,
                ),
            )

            updated = con.execute("select * from runs where id=?", (run_id,)).fetchone()
            updated_run = dict(updated)
            updated_count = _int_or_default(updated_run.get("handoff_count"), 0)
            updated_budget = _int_or_default(updated_run.get("handoff_budget"), 6)
            event_data: dict[str, Any] = {
                "actor": actor,
                "owner": updated_run.get("owner"),
                "phase": updated_run.get("phase"),
                "status": updated_run.get("status"),
                "handoff_count": updated_run.get("handoff_count"),
                "handoff_budget": updated_run.get("handoff_budget"),
                "budget_remaining": max(updated_budget - updated_count, 0),
            }
            if report is not None:
                event_data["report"] = report
            if diff_hash is not None:
                event_data["diff_hash"] = diff_hash
            if updated_run.get("blocked_reason"):
                event_data["blocked_reason"] = updated_run["blocked_reason"]

            event = "handoff.blocked" if updated_run.get("status") == "blocked" else "handoff.update"
            self._insert_event(con, run_id, event, event_data, timestamp)
            return updated_run

    def get_handoff(self, run_id: str) -> dict[str, Any] | None:
        with self.session() as con:
            row = con.execute("select * from runs where id=?", (run_id,)).fetchone()
            if row is None:
                return None

            run = dict(row)
            event_rows = con.execute("select * from run_events where run_id=? order by id", (run_id,)).fetchall()
            events = []
            for event_row in event_rows:
                event = dict(event_row)
                data_json = event.pop("data_json", "")
                try:
                    event["data"] = json.loads(data_json) if data_json else {}
                except (TypeError, json.JSONDecodeError):
                    event["data"] = {"raw": data_json}
                events.append(event)

            artifacts = [dict(artifact) for artifact in con.execute("select * from artifacts where run_id=? order by id", (run_id,))]
            handoff_count = _int_or_default(run.get("handoff_count"), 0)
            handoff_budget = _int_or_default(run.get("handoff_budget"), 6)
            next_expected_actor = "codex" if run.get("last_actor") == "claude" else "claude"
            return {
                "run": run,
                "events": events,
                "artifacts": artifacts,
                "budget_remaining": max(handoff_budget - handoff_count, 0),
                "next_expected_actor": next_expected_actor,
            }

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
        with self.session() as con:
            self._insert_event(con, run_id, event, data, now_iso())

    def _insert_event(self, con: sqlite3.Connection, run_id: str, event: str, data: dict[str, Any], timestamp: str) -> None:
        con.execute(
            "insert into run_events(run_id, event, data_json, created_at) values (?, ?, ?, ?)",
            (run_id, event, json.dumps(data, sort_keys=True), timestamp),
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
        timestamp = now_iso()
        with self.session() as con:
            con.execute("update cancellations set completed_at=? where run_id=?", (timestamp, run_id))
            con.execute("update runs set status=?, completed_at=?, updated_at=? where id=?", ("cancelled", timestamp, timestamp, run_id))

    def list_tasks(self) -> list[dict[str, Any]]:
        with self.session() as con:
            return [dict(row) for row in con.execute("select * from tasks order by updated_at desc")]

    def list_runs(self) -> list[dict[str, Any]]:
        with self.session() as con:
            return [dict(row) for row in con.execute("select * from runs order by updated_at desc limit 20")]
