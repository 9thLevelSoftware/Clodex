"""Tests: SQLite state store and handoff ledger."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from clodex.state import StateStore


class StateTests(unittest.TestCase):
    def test_state_migrations_add_v2_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            store = StateStore(db)
            tables = store.table_names()
            self.assertIn("schema_version", tables)
            self.assertIn("run_events", tables)
            self.assertIn("artifacts", tables)
            self.assertIn("workspace_locks", tables)
            self.assertIn("cancellations", tables)
            self.assertEqual(store.schema_version(), 3)

    def test_state_migrations_add_native_handoff_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            store.create_handoff("run-native", "native task", owner="claude", phase="planning", handoff_budget=2)
            run = store.get_run("run-native")
            self.assertEqual(run["owner"], "claude")
            self.assertEqual(run["phase"], "planning")
            self.assertEqual(run["handoff_count"], 0)
            self.assertEqual(run["handoff_budget"], 2)
            self.assertIsNone(run["blocked_reason"])

    def test_state_migrations_backfill_existing_old_schema_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            con = sqlite3.connect(db)
            try:
                con.executescript(
                    """
                    create table schema_version (
                        version integer not null
                    );
                    insert into schema_version(version) values (2);
                    create table runs (
                        id text primary key,
                        task_id text,
                        status text not null,
                        prompt text not null,
                        diff_hash text,
                        created_at text not null,
                        updated_at text not null
                    );
                    insert into runs(id, task_id, status, prompt, diff_hash, created_at, updated_at)
                    values ('run-old', 'task-old', 'handoff', 'old task', null, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
                    """
                )
                con.commit()
            finally:
                con.close()

            store = StateStore(db)
            run = store.get_run("run-old")
            self.assertEqual(run["handoff_count"], 0)
            self.assertEqual(run["handoff_budget"], 6)
            self.assertIsNone(run["owner"])
            self.assertIsNone(run["phase"])
            self.assertIsNone(run["last_actor"])
            self.assertIsNone(run["blocked_reason"])

    def test_create_handoff_rejects_negative_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            with self.assertRaises(ValueError):
                store.create_handoff("run-native", "native task", handoff_budget=-1)
            self.assertIsNone(store.get_run("run-native"))

    def test_handoff_update_increments_budget_and_blocks_when_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            store.create_handoff("run-native", "native task", owner="claude", phase="planning", handoff_budget=1)
            first = store.update_handoff("run-native", phase="implementation", actor="claude", increment_handoff=True)
            self.assertEqual(first["status"], "handoff")
            self.assertEqual(first["handoff_count"], 1)
            second = store.update_handoff("run-native", phase="audit", actor="codex", increment_handoff=True)
            self.assertEqual(second["status"], "blocked")
            self.assertEqual(second["blocked_reason"], "handoff budget exhausted")

    def test_handoff_update_blocks_immediately_with_zero_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            store.create_handoff("run-native", "native task", owner="claude", phase="planning", handoff_budget=0)
            result = store.update_handoff("run-native", phase="implementation", actor="claude", increment_handoff=True)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["handoff_count"], 1)
            self.assertEqual(result["blocked_reason"], "handoff budget exhausted")

    def test_handoff_update_rejects_direct_approved_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            store.create_handoff("run-native", "native task", owner="claude", phase="planning", handoff_budget=2)
            with self.assertRaises(ValueError):
                store.update_handoff("run-native", status="approved", phase="done", actor="claude", increment_handoff=True, diff_hash="abc123")
            run = store.get_run("run-native")
            self.assertEqual(run["status"], "handoff")
            self.assertEqual(run["phase"], "planning")
            self.assertEqual(run["handoff_count"], 0)
            self.assertIsNone(run["last_actor"])
            self.assertIsNone(run["diff_hash"])

    def test_approve_handoff_persists_terminal_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            store.create_handoff("run-native", "native task", owner="claude", phase="audit", handoff_budget=2)
            with self.assertRaises(ValueError):
                store.approve_handoff("run-native", "")
            approved = store.approve_handoff("run-native", "abc123", approved_by=["codex", "claude"])
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(approved["phase"], "decision")
            self.assertEqual(approved["diff_hash"], "abc123")
            self.assertIsNotNone(approved["completed_at"])
            data = store.get_handoff("run-native")
            event = next(event for event in data["events"] if event["event"] == "handoff.approved")
            self.assertEqual(event["data"]["diff_hash"], "abc123")
            self.assertEqual(event["data"]["approved_by"], ["claude", "codex"])
            with self.assertRaises(ValueError):
                store.update_handoff("run-native", phase="fix", actor="codex")

    def test_handoff_update_blocked_and_failed_require_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            for status in ("blocked", "failed"):
                run_id = f"run-{status}"
                with self.subTest(status=status):
                    store.create_handoff(run_id, "native task", owner="claude", phase="planning", handoff_budget=2)
                    with self.assertRaises(ValueError):
                        store.update_handoff(run_id, status=status, phase="audit", actor="claude", diff_hash="abc123")
                    run = store.get_run(run_id)
                    self.assertEqual(run["status"], "handoff")
                    self.assertEqual(run["phase"], "planning")
                    self.assertEqual(run["handoff_count"], 0)
                    self.assertIsNone(run["last_actor"])
                    self.assertIsNone(run["blocked_reason"])
                    self.assertIsNone(run["diff_hash"])

    def test_handoff_update_blocked_with_reason_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            store.create_handoff("run-native", "native task", owner="claude", phase="planning", handoff_budget=2)
            result = store.update_handoff("run-native", status="blocked", phase="audit", actor="claude", blocked_reason="manual review")
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["phase"], "audit")
            self.assertEqual(result["last_actor"], "claude")
            self.assertEqual(result["blocked_reason"], "manual review")
            self.assertIsNotNone(result["completed_at"])

    def test_handoff_update_failed_accepts_report_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            store.create_handoff("run-native", "native task", owner="claude", phase="planning", handoff_budget=2)
            result = store.update_handoff("run-native", status="failed", actor="claude", report={"reason": "tool crashed"})
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["blocked_reason"], "tool crashed")
            self.assertIsNotNone(result["completed_at"])

    def test_handoff_update_uses_latest_persisted_count_across_stores(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            StateStore(db).create_handoff("run-native", "native task", owner="claude", phase="planning", handoff_budget=1)
            first = StateStore(db).update_handoff("run-native", actor="claude", increment_handoff=True)
            self.assertEqual(first["status"], "handoff")
            self.assertEqual(first["handoff_count"], 1)
            second = StateStore(db).update_handoff("run-native", actor="codex", increment_handoff=True)
            self.assertEqual(second["status"], "blocked")
            self.assertEqual(second["handoff_count"], 2)
            self.assertEqual(second["blocked_reason"], "handoff budget exhausted")

    def test_blocked_handoff_cannot_be_updated_back_to_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            store.create_handoff("run-native", "native task", owner="claude", phase="planning", handoff_budget=0)
            store.update_handoff("run-native", phase="implementation", actor="claude", increment_handoff=True)
            with self.assertRaises(ValueError):
                store.update_handoff("run-native", status="handoff", phase="audit", actor="codex", diff_hash="abc123")
            run = store.get_run("run-native")
            self.assertEqual(run["status"], "blocked")
            self.assertEqual(run["phase"], "implementation")
            self.assertEqual(run["handoff_count"], 1)
            self.assertEqual(run["blocked_reason"], "handoff budget exhausted")
            self.assertIsNone(run["diff_hash"])

    def test_approved_handoff_cannot_be_updated_or_incremented(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            store.create_handoff("run-native", "native task", owner="claude", phase="planning", handoff_budget=2)
            active = store.update_handoff("run-native", phase="done", actor="claude", increment_handoff=True)
            self.assertEqual(active["status"], "handoff")
            self.assertEqual(active["handoff_count"], 1)
            store.update_run("run-native", "approved")
            with self.assertRaises(ValueError):
                store.update_handoff("run-native", phase="audit", actor="codex", increment_handoff=True, diff_hash="abc123")
            run = store.get_run("run-native")
            self.assertEqual(run["status"], "approved")
            self.assertEqual(run["phase"], "done")
            self.assertEqual(run["handoff_count"], 1)
            self.assertIsNone(run["diff_hash"])

    def test_handoff_get_includes_latest_events_and_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            store.create_handoff("run-native", "native task", owner="claude", phase="planning", handoff_budget=2)
            initial = store.get_handoff("run-native")
            self.assertEqual(initial["next_expected_actor"], "claude")
            store.add_artifact("run-native", "plan", "/tmp/plan.json", "json")
            store.update_handoff("run-native", phase="implementation", actor="claude", report={"summary": "ready"})
            data = store.get_handoff("run-native")
            self.assertEqual(data["run"]["id"], "run-native")
            self.assertEqual(data["run"]["phase"], "implementation")
            self.assertEqual(data["artifacts"][0]["name"], "plan")
            self.assertGreaterEqual(len(data["events"]), 1)
            self.assertEqual(data["next_expected_actor"], "codex")

    def test_handoff_get_uses_owner_before_any_actor_has_acted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            store.create_handoff("run-native", "native task", owner="codex", phase="planning", handoff_budget=2)
            data = store.get_handoff("run-native")
            self.assertEqual(data["run"]["owner"], "codex")
            self.assertIsNone(data["run"]["last_actor"])
            self.assertEqual(data["next_expected_actor"], "codex")

    def test_handoff_get_returns_raw_event_data_for_corrupt_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            store = StateStore(db)
            store.create_handoff("run-native", "native task", owner="claude", phase="planning", handoff_budget=2)
            con = sqlite3.connect(db)
            try:
                con.execute(
                    "insert into run_events(run_id, event, data_json, created_at) values (?, ?, ?, ?)",
                    ("run-native", "handoff.bad-json", "{not-json", "2026-01-01T00:00:00Z"),
                )
                con.commit()
            finally:
                con.close()
            data = store.get_handoff("run-native")
            event = next(event for event in data["events"] if event["event"] == "handoff.bad-json")
            self.assertEqual(event["data"], {"raw": "{not-json"})
