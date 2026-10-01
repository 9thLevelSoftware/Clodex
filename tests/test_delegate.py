"""Tests: delegating a native handoff's work to Codex (implement / fix / audit)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from clodex import mcp_server
from clodex.artifacts import current_diff, hash_text
from clodex.config import DEFAULT_CONFIG
from clodex.delegate import DelegationManager, WaitAborted, cancel_active_delegation, open_fixes, reconcile_delegation
from clodex.procs import pid_alive
from clodex.state import StateStore
from clodex.tasks import TaskManager
from tests.support import ROOT, FakeCliPath, TempRepo
from tests.test_handoff_quorum import HandoffCase, update_event

REVIEWERS = DEFAULT_CONFIG["audit"]["reviewers"]


def wait_for(predicate, timeout: float = 30.0, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class DelegateCase(HandoffCase):
    def setUp(self):
        super().setUp()
        warning_ctx = mock.patch("warnings.filterwarnings")  # keep Popen ResourceWarnings out of the output
        warning_ctx.start()
        self.addCleanup(warning_ctx.stop)
        self.addCleanup(self.stop_workers)

    def stop_workers(self):
        state = self.state
        with state.session() as con:
            run_ids = [row["run_id"] for row in con.execute("select distinct run_id from delegations")]
        for run_id in run_ids:
            cancel_active_delegation(state, run_id)

    def create(self, run_id: str = "d", workspace: str = "git-worktree", **extra):
        is_error, run = self.call("clodex_handoff_create", run_id=run_id, task="Add a feature", workspace=workspace, **extra)
        self.assertFalse(is_error, run)
        return run

    def delegate(self, run_id: str = "d", wait: bool = True, **arguments):
        return self.call("clodex_delegate", run_id=run_id, wait=wait, **arguments)

    def handoff(self, run_id: str = "d") -> dict:
        return self.call("clodex_handoff_get", run_id=run_id)[1]

    def workspace(self, run_id: str = "d") -> Path:
        return Path(self.state.get_run(run_id)["workspace_path"])


class ImplementTests(DelegateCase):
    def test_implement_runs_in_the_isolated_worktree_and_records_the_result(self):
        with FakeCliPath():
            self.create()
            is_error, result = self.delegate(instructions="Create implemented.txt")
            self.assertFalse(is_error, result)
            delegation = result["delegation"]
            self.assertEqual((delegation["status"], delegation["mode"]), ("completed", "implement"))
            self.assertTrue(delegation["pid"])
            self.assertEqual((self.workspace() / "implemented.txt").read_text(encoding="utf-8").strip(), "implemented")
            self.assertFalse((self.repo / "implemented.txt").exists(), "the source checkout must stay untouched")
            data = self.handoff()
            run = data["run"]
            self.assertEqual((run["phase"], run["last_actor"], run["handoff_count"]), ("implementation", "codex", 1))
            self.assertEqual(data["budget_remaining"], 5)
            self.assertEqual(data["next_expected_actor"], "claude")
            self.assertEqual(run["diff_hash"], delegation["diff_hash"])
            self.assertEqual(delegation["diff_hash"], hash_text(current_diff(self.workspace())), "the recorded hash is the workspace's real diff")
            report = next(e for e in reversed(data["events"]) if e["event"] == "handoff.update")["data"]["report"]
            self.assertEqual((report["mode"], report["changed"], report["summary"]), ("implement", True, "implemented"))
            names = {a["name"] for a in data["artifacts"]}
            self.assertTrue({f"delegation-{delegation['id']}-codex.md", "changes.diff"} <= names)
            self.assertEqual(data["delegations"][0]["id"], delegation["id"])
            self.assertEqual(result["handoff"]["next_expected_actor"], "claude")

    def test_default_returns_at_once_and_progress_is_visible(self):
        with FakeCliPath(sleep_seconds=3):
            self.create()
            started = time.monotonic()
            is_error, result = self.delegate(wait=False, instructions="slow job")
            self.assertFalse(is_error, result)
            self.assertLess(time.monotonic() - started, 20, "must not wait for Codex")
            self.assertIn(result["delegation"]["status"], {"queued", "running"})
            self.assertTrue(result["delegation"]["pid"])
            self.assertEqual(self.handoff()["delegations"][0]["id"], result["delegation"]["id"])
            manager = DelegationManager(self.repo)
            finished = manager.wait(int(result["delegation"]["id"]), timeout=120)
            self.assertEqual(finished["status"], "completed")
            self.assertEqual(self.handoff()["run"]["handoff_count"], 1)

    def test_a_worktree_is_created_on_demand_when_the_handoff_has_none(self):
        with FakeCliPath():
            self.create(workspace="none")
            self.assertIsNone(self.state.get_run("d")["workspace_path"])
            is_error, result = self.delegate(instructions="go")
            self.assertFalse(is_error, result)
            path = self.workspace()
            self.assertTrue(path.is_dir())
            self.assertEqual(path.parent.resolve(), (self.repo / ".clodex" / "workspaces").resolve())
            self.assertTrue((path / "implemented.txt").is_file())
            with self.state.session() as con:
                self.assertIsNotNone(con.execute("select 1 from workspace_locks where run_id='d'").fetchone())

    def test_a_local_workspace_edits_the_repo_itself(self):
        with FakeCliPath():
            self.create(workspace="local")
            is_error, result = self.delegate(instructions="go")
            self.assertFalse(is_error, result)
            self.assertTrue((self.repo / "implemented.txt").is_file())

    def test_codex_failure_fails_the_delegation_without_spending_budget(self):
        with FakeCliPath(codex_fails=True):
            self.create()
            is_error, result = self.delegate(instructions="go")
            self.assertTrue(is_error)
            self.assertEqual(result["delegation"]["status"], "failed")
            self.assertIn("codex exited with code 5", result["delegation"]["error"])
            data = self.handoff()
            self.assertEqual(data["run"]["handoff_count"], 0, "a failed job is not a handoff")
            self.assertEqual(data["run"]["status"], "handoff", "Claude decides what to do next")
            failure = next(e for e in reversed(data["events"]) if e["event"] == "handoff.update")["data"]["report"]
            self.assertIn("simulated codex crash", failure["error"])
            self.assertEqual(data["next_expected_actor"], "claude")
            self.assertTrue(any(a["name"].endswith("-codex.md") for a in data["artifacts"]), "the transcript is kept")


class FixTests(DelegateCase):
    def test_open_fixes_collects_unapproved_findings_for_the_latest_diff(self):
        data = {"events": [
            update_event("claude", "h1", approved=False, required_fixes=["old problem"]),
            update_event("claude", "h2", approved=False, required_fixes=["add tests", "add tests"], findings=[{"severity": "high", "file": "a.py", "line": 3, "message": "crash"}]),
            update_event("codex", "h2", approved=True),
            update_event("codex", "h2", approved=False, reviewer_id="security", required_fixes=["escape input"]),
            {"event": "handoff.create", "data": {}},
        ]}
        reviewers = REVIEWERS
        self.assertEqual(open_fixes(data, reviewers), ["add tests", "[high] a.py:3: crash"], "security is not configured as required here, so only claude-plan counts")
        approved = {"events": [update_event("claude", "h", approved=True), update_event("codex", "h", approved=True)]}
        self.assertEqual(open_fixes(approved, reviewers), [])
        self.assertEqual(open_fixes({"events": []}, reviewers), [])

    def test_fix_mode_applies_open_findings(self):
        with FakeCliPath():
            self.create()
            self.delegate(instructions="Create implemented.txt")
            run = self.state.get_run("d")
            self.call("clodex_handoff_update", run_id="d", actor="claude", diff_hash=run["diff_hash"], report={"approved": False, "required_fixes": ["append a fixed line"]})
            is_error, result = self.delegate(mode="fix")
            self.assertFalse(is_error, result)
            self.assertEqual(result["delegation"]["mode"], "fix")
            self.assertEqual((self.workspace() / "implemented.txt").read_text(encoding="utf-8").replace("\r\n", "\n"), "implemented\nfixed\n", "the fix prompt carried the required fixes")
            data = self.handoff()
            self.assertEqual(data["run"]["phase"], "fix")
            self.assertEqual(data["run"]["handoff_count"], 2)
            self.assertNotEqual(data["run"]["diff_hash"], run["diff_hash"], "the diff changed, so earlier approvals no longer count")


class AuditTests(DelegateCase):
    def test_full_native_loop_implement_codex_audit_claude_verdict_decide(self):
        with FakeCliPath():
            self.create()
            self.delegate(instructions="Create implemented.txt")
            diff_hash = self.state.get_run("d")["diff_hash"]
            is_error, result = self.delegate(mode="audit")
            self.assertFalse(is_error, result)
            self.assertEqual(result["delegation"]["mode"], "audit")
            update = next(e for e in reversed(self.handoff()["events"]) if e["event"] == "handoff.update")["data"]
            self.assertEqual((update["actor"], update["diff_hash"]), ("codex", diff_hash))
            self.assertTrue(update["report"]["approved"])
            self.assertEqual(update["report"]["reviewer_id"], "codex-architecture")
            self.assertEqual(self.call("clodex_handoff_decide", run_id="d")[1]["decision"], "needs_fix", "claude has not given its verdict yet")
            self.call("clodex_handoff_update", run_id="d", actor="claude", diff_hash=diff_hash, report={"approved": True, "summary": "plan adherence ok"})
            is_error, decision = self.call("clodex_handoff_decide", run_id="d")
            self.assertEqual(decision["decision"], "approved")
            self.assertEqual(decision["approved_by"], ["claude", "codex"])
            self.assertEqual(self.state.get_run("d")["status"], "approved")
            with self.state.session() as con:
                audits = [dict(r) for r in con.execute("select agent, approved from audits where run_id='d' order by id")]
            self.assertEqual(audits, [{"agent": "codex-architecture", "approved": 1}, {"agent": "claude", "approved": 1}])

    def test_a_rejecting_codex_audit_blocks_approval_and_feeds_the_next_fix(self):
        with FakeCliPath(reject_reviewers=("codex-architecture",)):
            self.create()
            self.delegate(instructions="Create implemented.txt")
            diff_hash = self.state.get_run("d")["diff_hash"]
            self.delegate(mode="audit")
            self.call("clodex_handoff_update", run_id="d", actor="claude", diff_hash=diff_hash, report={"approved": True})
            decision = self.call("clodex_handoff_decide", run_id="d")[1]
            self.assertEqual(decision["decision"], "needs_fix")
            self.assertEqual(decision["required_pending"], ["codex-architecture"])
            self.assertEqual(open_fixes(self.handoff(), self.repo_reviewers()), ["fix from codex-architecture"])

    def repo_reviewers(self):
        from clodex.config import load_config

        return load_config(self.repo).reviewers

    def test_auditing_a_workspace_with_no_changes_fails_cleanly(self):
        with FakeCliPath():
            self.create()
            is_error, result = self.delegate(mode="audit")
            self.assertTrue(is_error)
            self.assertIn("nothing to audit", result["delegation"]["error"])
            self.assertEqual(self.handoff()["run"]["handoff_count"], 0)


class RefusalTests(DelegateCase):
    def test_bad_requests_are_tool_errors(self):
        with FakeCliPath():
            is_error, message = self.delegate(run_id="nope", instructions="x")
            self.assertTrue(is_error)
            self.assertIn("Unknown run: nope", message)
            self.create()
            is_error, message = self.delegate()
            self.assertTrue(is_error)
            self.assertIn("instructions are required", message)
            is_error, message = self.delegate(mode="vibe", instructions="x")
            self.assertTrue(is_error)
            self.assertIn("unknown delegation mode", message)

    def test_finished_and_exhausted_handoffs_cannot_delegate(self):
        with FakeCliPath():
            self.create(run_id="done", workspace="none")
            self.call("clodex_handoff_update", run_id="done", actor="claude", status="blocked", blocked_reason="stop")
            is_error, message = self.delegate(run_id="done", instructions="x")
            self.assertTrue(is_error)
            self.assertIn("the handoff is blocked", message)
            self.create(run_id="tiny", handoff_budget=1)
            self.assertFalse(self.delegate(run_id="tiny", instructions="first")[0])
            is_error, message = self.delegate(run_id="tiny", instructions="second")
            self.assertTrue(is_error)
            self.assertTrue("budget exhausted" in message or "is blocked" in message, message)

    def test_a_retired_codex_model_is_refused_up_front(self):
        (self.repo / "CLODEX.md").write_text("---\ncodex:\n  model: gpt-5.4\n---\nbody\n", encoding="utf-8")
        with FakeCliPath():
            self.create(workspace="none")
            is_error, message = self.delegate(instructions="x")
            self.assertTrue(is_error)
            self.assertIn("retired on", message)
            self.assertEqual(self.handoff()["delegations"], [])

    def test_only_one_delegation_at_a_time_per_handoff(self):
        with FakeCliPath(sleep_seconds=4):
            self.create()
            first = self.delegate(wait=False, instructions="slow")[1]["delegation"]
            is_error, message = self.delegate(wait=False, instructions="second")
            self.assertTrue(is_error)
            self.assertIn("already running", message)
            DelegationManager(self.repo).wait(int(first["id"]), timeout=120)
            is_error, result = self.delegate(wait=False, instructions="third")
            self.assertFalse(is_error, result)
            DelegationManager(self.repo).wait(int(result["delegation"]["id"]), timeout=120)


class SupervisionTests(DelegateCase):
    def test_a_dead_worker_is_noticed_and_does_not_block_the_handoff(self):
        with FakeCliPath():
            self.create(workspace="none")
            run = self.state.get_run("d")
            gone = subprocess.Popen([sys.executable, "-c", "pass"])
            gone.wait()
            delegation = self.state.start_delegation("d", "implement", "x")
            self.state.update_delegation(delegation["id"], status="running", pid=gone.pid)
            self.call("clodex_handoff_get", run_id="d")  # reconciles
            fixed = self.state.get_delegation(delegation["id"])
            self.assertEqual(fixed["status"], "failed")
            self.assertIn("worker process exited", fixed["error"])
            is_error, result = self.delegate(instructions="try again")
            self.assertFalse(is_error, result)
            self.assertEqual(run["status"], "handoff")

    def test_a_delegation_that_never_got_a_worker_fails_after_the_grace_period(self):
        with TempRepo() as repo:
            state = StateStore(repo / "s.sqlite3")
            delegation = state.start_delegation("r", "implement", "x")
            self.assertEqual(reconcile_delegation(state, delegation)["status"], "queued", "just created: still starting")
            with state.session() as con:
                con.execute("update delegations set created_at='2020-01-01T00:00:00Z' where id=?", (delegation["id"],))
            fresh = state.get_delegation(delegation["id"])
            self.assertEqual(reconcile_delegation(state, fresh)["status"], "failed")

    def test_cancelling_the_handoff_stops_the_worker_and_releases_the_worktree(self):
        with FakeCliPath(sleep_seconds=60):
            self.create()
            started = self.delegate(wait=False, instructions="slow")[1]["delegation"]
            workspace = self.workspace()
            self.assertTrue(wait_for(lambda: (workspace / ".fake-pid-codex").exists(), 60), "codex never started")
            worker_pid = started["pid"]
            codex_pid = int((workspace / ".fake-pid-codex").read_text(encoding="utf-8"))
            self.assertTrue(pid_alive(worker_pid) and pid_alive(codex_pid))
            result = TaskManager(self.repo).cancel("d")
            self.assertEqual(result.status, "cancelled")
            self.assertTrue(result.data["worker_stopped"])
            self.assertTrue(wait_for(lambda: not pid_alive(worker_pid), 15), "worker survived")
            self.assertTrue(wait_for(lambda: not pid_alive(codex_pid), 15), "codex survived")
            self.assertEqual(self.state.get_delegation(started["id"])["status"], "cancelled")
            self.assertEqual(self.state.get_run("d")["status"], "cancelled")
            self.assertTrue(result.data["workspace_released"])
            self.assertFalse(workspace.exists())

    def test_cancel_only_signals_a_worker_with_a_recent_heartbeat(self):
        with TempRepo() as repo:
            state = StateStore(repo / "s.sqlite3")
            delegation = state.start_delegation("r", "implement", "x")
            # This pid is the test process itself, with a stale heartbeat: it must never be signalled.
            state.update_delegation(delegation["id"], status="running", pid=os.getpid(), started_at="2020-01-01T00:00:00Z")
            self.assertFalse(cancel_active_delegation(state, "r"))
            self.assertEqual(state.get_delegation(delegation["id"])["status"], "cancelled")
            self.assertTrue(pid_alive(os.getpid()))
            self.assertFalse(cancel_active_delegation(state, "r"), "nothing left to cancel")

    def test_wait_can_be_aborted(self):
        with TempRepo() as repo:
            state = StateStore(repo / "s.sqlite3")
            state.start_delegation("r", "implement", "x")
            manager = DelegationManager(repo)
            manager._state = state
            calls = []
            with self.assertRaises(WaitAborted):
                manager.wait(1, should_stop=lambda: calls.append(1) or len(calls) > 1)
            self.assertEqual(manager.wait(1, timeout=0.2)["status"], "queued", "a timeout returns the current state")


class DelegationStateTests(unittest.TestCase):
    def test_v2_database_upgrades_and_gains_the_table(self):
        import sqlite3
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.sqlite3"
            con = sqlite3.connect(db)
            con.executescript("create table schema_version(version integer not null); insert into schema_version values (2);")
            con.commit()
            con.close()
            store = StateStore(db)
            self.assertEqual(store.schema_version(), 3)
            self.assertIn("delegations", store.table_names())

    def test_start_is_atomic_updates_are_sticky_and_listing_is_newest_first(self):
        with TempRepo() as repo:
            state = StateStore(repo / "s.sqlite3")
            first = state.start_delegation("r", "audit")
            with self.assertRaisesRegex(ValueError, "already running"):
                state.start_delegation("r", "fix")
            self.assertEqual(state.active_delegation("r")["id"], first["id"])
            state.update_delegation(first["id"], status="completed", summary="ok")
            self.assertIsNone(state.active_delegation("r"))
            self.assertEqual(state.update_delegation(first["id"], status="running")["status"], "completed", "finished stays finished")
            second = state.start_delegation("r", "fix")
            self.assertEqual([d["id"] for d in state.list_delegations("r")], [second["id"], first["id"]])
            with self.assertRaises(ValueError):
                state.start_delegation("r", "dance")
            with self.assertRaises(ValueError):
                state.update_delegation(second["id"], colour="red")
            with self.assertRaises(ValueError):
                state.update_delegation(999, status="failed")


class McpSurfaceTests(unittest.TestCase):
    def test_tool_definition_and_blocking_rules(self):
        tool = mcp_server.TOOL_INDEX["clodex_delegate"]
        self.assertEqual(tool["inputSchema"]["required"], ["run_id"])
        self.assertEqual(tool["inputSchema"]["properties"]["mode"]["enum"], ["implement", "fix", "audit"])
        server = mcp_server.McpServer(out=mock.Mock())

        def blocking(**arguments):
            return server.is_blocking("tools/call", {"name": "clodex_delegate", "arguments": arguments})

        self.assertTrue(blocking(run_id="x", wait=True))
        self.assertFalse(blocking(run_id="x", wait=False))
        self.assertFalse(blocking(run_id="x"))
        self.assertFalse(blocking(run_id="x", wait="yes"))

    def test_argument_problems_never_reach_the_manager(self):
        with TempRepo() as repo, mock.patch.dict(os.environ, {"CLODEX_REPO_ROOT": str(repo)}):
            server = mcp_server.McpServer(out=mock.Mock())
            for arguments, expected in (({}, "Missing required argument: run_id"), ({"run_id": "x", "mode": "vibe"}, "Invalid arguments"), ({"run_id": "x", "wait": "yes"}, "Invalid arguments")):
                result = server.tools_call({"name": "clodex_delegate", "arguments": arguments})
                self.assertTrue(result["isError"])
                self.assertIn(expected, result["content"][0]["text"])

    def test_the_worker_command_rejects_unknown_delegations(self):
        with TempRepo() as repo:
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "task", "delegate-worker", "no-run", "999"],
                cwd=repo, env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True, stdin=subprocess.DEVNULL,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("unknown delegation 999", result.stderr)


if __name__ == "__main__":
    unittest.main()
