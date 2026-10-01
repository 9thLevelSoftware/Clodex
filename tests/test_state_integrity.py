"""Tests: state-store integrity (terminal states, cancellation, validation, artifact ledger)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from clodex.state import HANDOFF_PHASES, TERMINAL_STATUSES, StateStore
from clodex.workflow import ClodexWorkflow
from tests.support import FakeCliPath, TempRepo


class StateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "state.sqlite3")
        self.store.upsert_task("t", "task", "running")

    def make_run(self, run_id: str, status: str = "running") -> str:
        self.store.create_run(run_id, "t", "prompt", status)
        return run_id

    def status(self, run_id: str) -> str:
        return str(self.store.get_run(run_id)["status"])


class TerminalStateTests(StateCase):
    def test_finished_runs_cannot_move_to_another_status(self):
        for terminal in sorted(TERMINAL_STATUSES - {"applied", "completed"}):
            run = self.make_run(f"r-{terminal}", terminal)
            with self.assertRaises(ValueError, msg=terminal):
                self.store.update_run(run, "running")
            with self.assertRaises(ValueError, msg=terminal):
                self.store.update_run(run, "approved" if terminal != "approved" else "blocked")
            self.assertEqual(self.status(run), terminal)

    def test_applied_and_completed_are_final(self):
        for terminal in ("applied", "completed"):
            run = self.make_run(f"r-{terminal}", terminal)
            with self.assertRaises(ValueError):
                self.store.update_run(run, "approved")

    def test_same_status_update_is_allowed_and_finished_runs_can_be_applied(self):
        run = self.make_run("r1", "blocked")
        self.store.update_run(run, "blocked", error="more detail")
        self.assertEqual(self.store.get_run(run)["error"], "more detail")
        self.store.update_run(run, "applied")
        self.assertEqual(self.status(run), "applied")

    def test_unfinished_runs_move_freely(self):
        run = self.make_run("r1", "queued")
        for status in ("running", "needs-fix", "cancel_requested", "approved"):
            self.store.update_run(run, status)
            self.assertEqual(self.status(run), status)

    def test_late_worker_cannot_overwrite_a_cancellation(self):
        run = self.make_run("r1")
        self.store.request_cancel(run)
        self.store.complete_cancel(run)
        self.assertEqual(self.status(run), "cancelled")
        with self.assertRaises(ValueError):
            self.store.update_run(run, "approved", diff_hash="abc")
        self.assertEqual(self.status(run), "cancelled")
        self.assertIsNone(self.store.get_run(run)["diff_hash"])


class CancelTests(StateCase):
    def test_cancel_of_a_running_run(self):
        run = self.make_run("r1")
        self.store.request_cancel(run)
        self.assertEqual(self.status(run), "cancel_requested")
        self.assertTrue(self.store.cancellation_requested(run))
        self.store.complete_cancel(run)
        self.assertEqual(self.status(run), "cancelled")

    def test_cancel_never_overwrites_any_finished_status(self):
        for terminal in sorted(TERMINAL_STATUSES):
            run = self.make_run(f"r-{terminal}", terminal)
            self.store.request_cancel(run)
            self.store.complete_cancel(run)
            self.assertEqual(self.status(run), terminal, terminal)
            with self.store.session() as con:
                row = con.execute("select completed_at from cancellations where run_id=?", (run,)).fetchone()
            self.assertIsNone(row["completed_at"], f"{terminal}: nothing was cancelled")


class HandoffValidationTests(StateCase):
    def test_unknown_phase_and_status_are_rejected_known_ones_accepted(self):
        self.store.create_handoff("h1", "t", "prompt", "claude")
        for phase in sorted(HANDOFF_PHASES):
            self.assertEqual(self.store.update_handoff("h1", phase=phase)["phase"], phase)
        with self.assertRaisesRegex(ValueError, "unknown handoff phase"):
            self.store.update_handoff("h1", phase="vibing")
        with self.assertRaisesRegex(ValueError, "unknown handoff status"):
            self.store.update_handoff("h1", status="running")
        self.assertEqual(self.store.update_handoff("h1", status="handoff")["status"], "handoff")
        self.assertEqual(self.store.get_run("h1")["phase"], sorted(HANDOFF_PHASES)[-1], "rejected updates must not change the phase")


class WorkspaceLockTests(StateCase):
    def test_release_sets_released_at_once(self):
        self.make_run("r1")
        self.store.add_workspace_lock("r1", "/src", "/ws", "git-worktree")
        self.store.release_workspace_lock("r1")
        with self.store.session() as con:
            first = con.execute("select released_at from workspace_locks where run_id='r1'").fetchone()["released_at"]
        self.assertIsNotNone(first)
        self.store.release_workspace_lock("r1")
        with self.store.session() as con:
            self.assertEqual(con.execute("select released_at from workspace_locks where run_id='r1'").fetchone()["released_at"], first)


class ArtifactLedgerTests(unittest.TestCase):
    def test_build_records_artifact_rows_without_duplicates(self):
        with TempRepo() as repo, FakeCliPath():
            workflow = ClodexWorkflow(repo)
            result = workflow.build("implement fixture", workspace_backend="git-worktree")
            self.assertEqual(result.status, "approved")
            with workflow.state.session() as con:
                rows = [dict(r) for r in con.execute("select name, path, kind from artifacts where run_id=?", (result.run_id,))]
            names = [row["name"] for row in rows]
            for expected in ("01-claude-plan.json", "02-codex-implementation.md", "05-agreement.json", "changes.diff", "apply.patch"):
                self.assertIn(expected, names)
            self.assertEqual(len(names), len(set(names)), "rewritten artifacts must not duplicate rows")
            kinds = {row["name"]: row["kind"] for row in rows}
            self.assertEqual(kinds["01-claude-plan.json"], "json")
            self.assertEqual(kinds["apply.patch"], "patch")
            self.assertTrue(all(Path(row["path"]).exists() for row in rows))


if __name__ == "__main__":
    unittest.main()
