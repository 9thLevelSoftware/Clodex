"""Tests: configurable-quorum handoff decisions, ledger rows and handoff workspaces."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from clodex import mcp_server
from clodex.config import DEFAULT_CONFIG
from clodex.quorum import evaluate_handoff, resolve_reviewer
from clodex.state import StateStore
from clodex.workflow import ClodexWorkflow
from tests.support import TempRepo

DEFAULT_REVIEWERS = DEFAULT_CONFIG["audit"]["reviewers"]


def update_event(actor=None, diff_hash=None, **report):
    return {"event": "handoff.update", "data": {"actor": actor, "diff_hash": diff_hash, "report": report}}


def handoff(*events):
    return {"events": list(events)}


def reviewer(reviewer_id, backend, required=True):
    return {"id": reviewer_id, "backend": backend, "persona": reviewer_id, "required": required}


class ResolveReviewerTests(unittest.TestCase):
    def test_id_beats_backend_and_actor_maps_to_the_first_required_reviewer_of_its_backend(self):
        reviewers = [reviewer("claude-extra", "claude", required=False), reviewer("claude-plan", "claude"), reviewer("codex-arch", "codex")]
        self.assertEqual(resolve_reviewer("claude-extra", reviewers)["id"], "claude-extra")
        self.assertEqual(resolve_reviewer("claude", reviewers)["id"], "claude-plan", "required ones win over earlier optional ones")
        self.assertEqual(resolve_reviewer(" Codex ", reviewers)["id"], "codex-arch")
        only_optional = [reviewer("a", "claude", required=False)]
        self.assertEqual(resolve_reviewer("claude", only_optional)["id"], "a")
        self.assertIsNone(resolve_reviewer("gemini", reviewers))
        self.assertIsNone(resolve_reviewer(None, reviewers))


class EvaluateHandoffTests(unittest.TestCase):
    def test_default_reviewers_need_claude_and_codex_on_the_same_hash(self):
        both = handoff(update_event("claude", "h1", approved=True), update_event("codex", "h1", approved=True))
        result = evaluate_handoff(both, DEFAULT_REVIEWERS)
        self.assertTrue(result["approved"])
        self.assertEqual(result["diff_hash"], "h1")
        self.assertEqual(result["approved_by"], ["claude", "codex"])
        self.assertEqual(result["approved_reviewers"], ["claude-plan", "codex-architecture"])
        one = evaluate_handoff(handoff(update_event("claude", "h1", approved=True)), DEFAULT_REVIEWERS)
        self.assertFalse(one["approved"])
        self.assertEqual(one["required_pending"], ["codex-architecture"])

    def test_a_new_diff_hash_resets_approvals_and_hashless_rejection_withdraws(self):
        moved = handoff(update_event("claude", "h1", approved=True), update_event("codex", "h2", approved=True))
        self.assertFalse(evaluate_handoff(moved, DEFAULT_REVIEWERS)["approved"])
        withdrawn = handoff(update_event("claude", "h1", approved=True), update_event("codex", "h1", approved=True), update_event("claude", None, approved=False))
        result = evaluate_handoff(withdrawn, DEFAULT_REVIEWERS)
        self.assertFalse(result["approved"])
        self.assertEqual(result["required_pending"], ["claude-plan"])

    def test_explicit_reviewer_ids_and_optional_reviewers(self):
        reviewers = [reviewer("claude-plan", "claude"), reviewer("codex-arch", "codex"), reviewer("security", "codex", required=False)]
        data = handoff(
            update_event("claude", "h", approved=True),
            update_event("codex", "h", approved=True, reviewer_id="security"),  # for the optional reviewer
        )
        result = evaluate_handoff(data, reviewers)
        self.assertFalse(result["approved"], "an optional reviewer cannot stand in for a required one")
        self.assertEqual(result["required_pending"], ["codex-arch"])
        data.get("events").append(update_event("codex", "h", approved=True))
        self.assertTrue(evaluate_handoff(data, reviewers)["approved"])
        data.get("events").append(update_event("codex", "h", approved=False, reviewer_id="security"))
        self.assertTrue(evaluate_handoff(data, reviewers)["approved"], "an optional rejection never blocks")

    def test_majority_and_numeric_quorums(self):
        reviewers = [reviewer("a", "claude"), reviewer("b", "codex"), reviewer("c", "codex")]
        two = handoff(update_event("claude", "h", approved=True), update_event("codex", "h", approved=True, reviewer_id="b"))
        self.assertTrue(evaluate_handoff(two, reviewers, "majority")["approved"])
        self.assertFalse(evaluate_handoff(two, reviewers, "unanimous")["approved"])
        self.assertTrue(evaluate_handoff(two, reviewers, 2)["approved"])
        self.assertFalse(evaluate_handoff(two, reviewers, 3)["approved"], "needs all three")
        one = handoff(update_event("claude", "h", approved=True))
        self.assertFalse(evaluate_handoff(one, reviewers, 2)["approved"], "one approval is below a quorum of two")

    def test_nothing_recorded_or_no_required_reviewers_is_never_approved(self):
        self.assertFalse(evaluate_handoff(handoff(), DEFAULT_REVIEWERS)["approved"])
        self.assertIsNone(evaluate_handoff(handoff(), DEFAULT_REVIEWERS)["diff_hash"])
        optional_only = [reviewer("a", "claude", required=False)]
        self.assertFalse(evaluate_handoff(handoff(update_event("claude", "h", approved=True)), optional_only)["approved"])

    def test_events_that_are_not_updates_or_have_bad_data_are_ignored(self):
        data = handoff({"event": "handoff.create", "data": {}}, {"event": "handoff.update", "data": "garbage"}, update_event("claude", "h", approved=True))
        self.assertEqual(evaluate_handoff(data, DEFAULT_REVIEWERS)["approved_reviewers"], ["claude-plan"])


class HandoffCase(unittest.TestCase):
    def setUp(self):
        self.repo_cm = TempRepo()
        self.repo = self.repo_cm.__enter__()
        self.addCleanup(self.repo_cm.__exit__, None, None, None)
        patcher = mock.patch.dict(os.environ, {"CLODEX_REPO_ROOT": str(self.repo)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def contract(self, reviewers: list[dict], quorum: str = "unanimous") -> None:
        (self.repo / "CLODEX.md").write_text(f"---\naudit:\n  quorum: {quorum}\n  reviewers: {json.dumps(reviewers)}\n---\nbody\n", encoding="utf-8")

    def call(self, name: str, **arguments):
        result = mcp_server.tool_call(name, arguments)
        text = result["content"][0]["text"]
        try:
            payload = json.loads(text)
        except ValueError:
            payload = text
        return result["isError"], payload

    @property
    def state(self) -> StateStore:
        return ClodexWorkflow(self.repo).state


class DecideTests(HandoffCase):
    def test_decide_uses_the_configured_reviewers_and_quorum(self):
        self.contract([reviewer("alpha", "claude"), reviewer("beta", "codex"), reviewer("gamma", "codex")], quorum="majority")
        self.call("clodex_handoff_create", run_id="h", task="t")
        self.call("clodex_handoff_update", run_id="h", actor="claude", diff_hash="d1", report={"approved": True})
        is_error, pending = self.call("clodex_handoff_decide", run_id="h")
        self.assertFalse(is_error)
        self.assertEqual(pending["decision"], "needs_fix")
        self.assertEqual(sorted(pending["required_pending"]), ["beta", "gamma"])
        self.assertEqual(pending["approved_reviewers"], ["alpha"])
        self.assertEqual(pending["quorum"], "majority")
        self.call("clodex_handoff_update", run_id="h", actor="codex", diff_hash="d1", report={"approved": True, "reviewer_id": "gamma"})
        is_error, decision = self.call("clodex_handoff_decide", run_id="h")
        self.assertEqual(decision["decision"], "approved")
        self.assertEqual(decision["approved_reviewers"], ["alpha", "gamma"])
        self.assertEqual(decision["approved_by"], ["claude", "codex"])
        self.assertEqual(self.state.get_run("h")["status"], "approved")

    def test_unanimous_is_still_the_default(self):
        self.call("clodex_handoff_create", run_id="u", task="t")
        self.call("clodex_handoff_update", run_id="u", actor="claude", diff_hash="d", report={"approved": True})
        self.assertEqual(self.call("clodex_handoff_decide", run_id="u")[1]["decision"], "needs_fix")
        self.call("clodex_handoff_update", run_id="u", actor="codex", diff_hash="d", report={"approved": True})
        self.assertEqual(self.call("clodex_handoff_decide", run_id="u")[1]["decision"], "approved")

    def test_unknown_reviewer_id_is_rejected_without_recording_anything(self):
        self.call("clodex_handoff_create", run_id="r", task="t")
        is_error, message = self.call("clodex_handoff_update", run_id="r", actor="claude", increment_handoff=True, diff_hash="d", report={"approved": True, "reviewer_id": "ghost"})
        self.assertTrue(is_error)
        self.assertIn("unknown reviewer_id: ghost", message)
        self.assertIn("claude-plan", message, "lists the configured reviewers")
        run = self.state.get_run("r")
        self.assertEqual(run["handoff_count"], 0)
        self.assertEqual(self.state.get_handoff("r")["events"][-1]["event"], "handoff.create")


class LedgerTests(HandoffCase):
    def rows(self, table: str, run_id: str):
        with self.state.session() as con:
            return [dict(row) for row in con.execute(f"select * from {table} where run_id=? order by id", (run_id,))]

    def task(self, task_id: str):
        return next((t for t in self.state.list_tasks() if t["id"] == task_id), None)

    def test_a_handoff_appears_in_the_task_ledger_and_follows_its_run(self):
        self.call("clodex_handoff_create", run_id="led", task="Ship the thing\nwith details")
        self.assertEqual((self.task("led")["status"], self.task("led")["title"]), ("handoff", "Ship the thing"))
        self.assertEqual(self.state.get_run("led")["task_id"], "led")
        tasks = self.call("clodex_status")[1]["tasks"]
        self.assertIn("led", [t["id"] for t in tasks])
        self.call("clodex_handoff_update", run_id="led", actor="claude", diff_hash="d", report={"approved": True})
        self.call("clodex_handoff_update", run_id="led", actor="codex", diff_hash="d", report={"approved": True})
        self.call("clodex_handoff_decide", run_id="led")
        self.assertEqual(self.task("led")["status"], "done")

    def test_blocked_and_budget_exhausted_handoffs_block_their_task(self):
        self.call("clodex_handoff_create", run_id="b1", task="t")
        self.call("clodex_handoff_update", run_id="b1", actor="claude", status="blocked", blocked_reason="needs a human")
        self.assertEqual(self.task("b1")["status"], "blocked")
        self.call("clodex_handoff_create", run_id="b2", task="t", handoff_budget=1)
        self.call("clodex_handoff_update", run_id="b2", actor="claude", increment_handoff=True)
        self.call("clodex_handoff_update", run_id="b2", actor="codex", increment_handoff=True)
        self.assertEqual(self.task("b2")["status"], "blocked")

    def test_verdict_reports_become_audit_rows(self):
        self.call("clodex_handoff_create", run_id="aud", task="t")
        self.call("clodex_handoff_update", run_id="aud", actor="claude", diff_hash="d1", report={"approved": False, "summary": "no tests"})
        self.call("clodex_handoff_update", run_id="aud", actor="codex", diff_hash="d1", report={"approved": True, "reviewer_id": "codex-architecture"})
        self.call("clodex_handoff_update", run_id="aud", actor="claude", report={"note": "not a verdict"})
        rows = self.rows("audits", "aud")
        self.assertEqual([(r["agent"], r["approved"], r["diff_hash"]) for r in rows], [("claude", 0, "d1"), ("codex-architecture", 1, "d1")])
        self.assertEqual(json.loads(rows[0]["verdict_json"])["summary"], "no tests")

    def test_report_artifacts_become_artifact_rows_without_duplicates(self):
        self.call("clodex_handoff_create", run_id="art", task="t")
        report = {"artifacts": ["notes/plan.md", {"name": "diff", "path": "out/changes.diff", "kind": "patch"}, {"nope": 1}, 5]}
        self.call("clodex_handoff_update", run_id="art", actor="claude", report=report)
        self.call("clodex_handoff_update", run_id="art", actor="claude", report={"artifacts": ["notes/plan.md"]})
        rows = {r["name"]: r for r in self.rows("artifacts", "art")}
        self.assertEqual(sorted(rows), ["diff", "plan.md"])
        self.assertEqual((rows["plan.md"]["kind"], rows["plan.md"]["path"]), ("md", "notes/plan.md"))
        self.assertEqual(rows["diff"]["kind"], "patch")
        data = self.call("clodex_handoff_get", run_id="art")[1]
        self.assertEqual(sorted(a["name"] for a in data["artifacts"]), ["diff", "plan.md"])


class WorkspaceTests(HandoffCase):
    def worktrees(self) -> int:
        out = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=self.repo, capture_output=True, text=True).stdout
        return out.count("worktree ")

    def lock(self, run_id: str):
        with self.state.session() as con:
            row = con.execute("select * from workspace_locks where run_id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def test_git_worktree_handoff_gets_an_isolated_checkout(self):
        is_error, run = self.call("clodex_handoff_create", run_id="wt", task="t", workspace="git-worktree")
        self.assertFalse(is_error, run)
        path = Path(run["workspace"]["path"])
        self.assertTrue(path.is_dir())
        self.assertEqual(path.parent.resolve(), (self.repo / ".clodex" / "workspaces").resolve())
        self.assertEqual(self.state.get_run("wt")["workspace_path"], str(path))
        self.assertEqual(self.worktrees(), 2)
        self.assertIsNone(self.lock("wt")["released_at"])
        self.assertEqual(self.call("clodex_handoff_get", run_id="wt")[1]["run"]["workspace_path"], str(path))

    def test_local_none_and_default_workspaces(self):
        _, local = self.call("clodex_handoff_create", run_id="loc", task="t", workspace="local")
        self.assertEqual(Path(self.state.get_run("loc")["workspace_path"]).resolve(), self.repo.resolve())
        self.assertEqual(self.worktrees(), 1)
        self.call("clodex_handoff_create", run_id="none", task="t", workspace="none")
        self.call("clodex_handoff_create", run_id="default", task="t")
        self.assertIsNone(self.state.get_run("none")["workspace_path"])
        self.assertIsNone(self.state.get_run("default")["workspace_path"])
        self.assertNotIn("workspace", self.call("clodex_handoff_create", run_id="plain", task="t")[1])

    def test_invalid_backend_and_dirty_tree_are_tool_errors_that_create_nothing(self):
        is_error, message = self.call("clodex_handoff_create", run_id="bad", task="t", workspace="docker")
        self.assertTrue(is_error)
        self.assertIn("workspace must be one of", message)
        (self.repo / "seed.txt").write_text("dirty\n", encoding="utf-8")
        is_error, message = self.call("clodex_handoff_create", run_id="dirty", task="t", workspace="git-worktree")
        self.assertTrue(is_error)
        self.assertIn("Tracked changes are present", message)
        for run_id in ("bad", "dirty"):
            self.assertIsNone(self.state.get_run(run_id))
        self.assertEqual(self.worktrees(), 1)

    def test_duplicate_run_id_creates_no_second_worktree(self):
        self.call("clodex_handoff_create", run_id="dup", task="t", workspace="git-worktree")
        is_error, message = self.call("clodex_handoff_create", run_id="dup", task="t", workspace="git-worktree")
        self.assertTrue(is_error)
        self.assertIn("already exists", message)
        self.assertEqual(self.worktrees(), 2)

    def test_worktree_is_rolled_back_if_the_handoff_cannot_be_recorded(self):
        with mock.patch.object(StateStore, "create_handoff", side_effect=ValueError("simulated failure")):
            is_error, message = self.call("clodex_handoff_create", run_id="rb", task="t", workspace="git-worktree")
        self.assertTrue(is_error)
        self.assertIn("simulated failure", message)
        self.assertEqual(self.worktrees(), 1, "no orphaned worktree")
        self.assertFalse((self.repo / ".clodex" / "workspaces" / "rb").exists())

    def test_clean_removes_the_worktree_after_the_handoff_finishes(self):
        _, run = self.call("clodex_handoff_create", run_id="fin", task="t", workspace="git-worktree")
        path = Path(run["workspace"]["path"])
        workflow = ClodexWorkflow(self.repo)
        self.assertEqual(workflow.clean_run("fin").status, "clean-refused", "still in progress")
        self.call("clodex_handoff_update", run_id="fin", actor="claude", status="blocked", blocked_reason="done for now")
        self.assertEqual(workflow.clean_run("fin").status, "cleaned")
        self.assertFalse(path.exists())
        self.assertEqual(self.worktrees(), 1)


if __name__ == "__main__":
    unittest.main()
