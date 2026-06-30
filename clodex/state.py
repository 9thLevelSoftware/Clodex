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
                """
            )

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

    def create_run(self, run_id: str, task_id: str, prompt: str, status: str) -> None:
        timestamp = now_iso()
        with self.session() as con:
            con.execute(
                """
                insert into runs(id, task_id, status, prompt, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?)
                """,
                (run_id, task_id, status, prompt, timestamp, timestamp),
            )

    def update_run(self, run_id: str, status: str, diff_hash: str | None = None) -> None:
        with self.session() as con:
            con.execute(
                "update runs set status=?, diff_hash=coalesce(?, diff_hash), updated_at=? where id=?",
                (status, diff_hash, now_iso(), run_id),
            )

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

    def list_tasks(self) -> list[dict[str, Any]]:
        with self.session() as con:
            return [dict(row) for row in con.execute("select * from tasks order by updated_at desc")]

    def list_runs(self) -> list[dict[str, Any]]:
        with self.session() as con:
            return [dict(row) for row in con.execute("select * from runs order by updated_at desc limit 20")]
