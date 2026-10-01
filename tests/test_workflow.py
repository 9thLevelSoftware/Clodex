"""Tests: plan / build / audit workflow, workspaces, agents, tracing."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from clodex.agents import AgentRunner
from clodex.commands import claude_plan_command
from clodex.config import load_config
from clodex.trace import TraceWriter
from clodex.workflow import ClodexWorkflow
from clodex.workspace import DirtyWorkspaceError, WorkspaceManager
from tests.support import TempRepo, FakeCliPath


class WorkflowTests(unittest.TestCase):
    def test_trace_writer_appends_jsonl_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = TraceWriter(Path(tmp), "run-1")
            trace.event("phase.start", {"phase": "planning"})
            lines = (Path(tmp) / "trace.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            event = json.loads(lines[0])
            self.assertEqual(event["run_id"], "run-1")
            self.assertEqual(event["event"], "phase.start")
            self.assertEqual(event["data"]["phase"], "planning")

    def test_workspace_manager_refuses_dirty_source(self):
        with TempRepo() as repo:
            (repo / "seed.txt").write_text("dirty\n", encoding="utf-8")
            manager = WorkspaceManager(repo, load_config(repo))
            with self.assertRaises(DirtyWorkspaceError):
                manager.prepare("run-dirty", backend="git-worktree")

    def test_worktree_build_isolated_until_apply(self):
        with TempRepo() as repo, FakeCliPath():
            result = ClodexWorkflow(repo).build("implement fixture")
            self.assertEqual(result.status, "approved")
            self.assertFalse((repo / "implemented.txt").exists())
            apply_result = ClodexWorkflow(repo).apply_run(result.run_id)
            self.assertEqual(apply_result.status, "applied")
            self.assertTrue((repo / "implemented.txt").exists())
            workspace = Path(result.data["workspace"]["path"])
            self.assertTrue(workspace.exists())

    def test_build_happy_path_creates_agreement(self):
        with TempRepo() as repo, FakeCliPath():
            result = ClodexWorkflow(repo).build("implement fixture", workspace_backend="local")
            self.assertEqual(result.status, "approved")
            agreement = json.loads((Path(result.artifacts_dir) / "05-agreement.json").read_text(encoding="utf-8"))
            self.assertTrue(agreement["approved"])
            self.assertTrue((repo / "implemented.txt").exists())
            self.assertTrue((Path(result.artifacts_dir) / "trace.jsonl").exists())
            self.assertTrue((Path(result.artifacts_dir) / "reviewers" / "claude-plan.json").exists())

    def test_rejection_triggers_fix_loop(self):
        with TempRepo() as repo, FakeCliPath(reject_once=True):
            result = ClodexWorkflow(repo).build("implement fixture", workspace_backend="local")
            self.assertEqual(result.status, "approved")
            self.assertIn("fixed", (repo / "implemented.txt").read_text(encoding="utf-8"))

    def test_required_reviewer_rejection_blocks(self):
        with TempRepo() as repo:
            config = repo / "CLODEX.md"
            config.write_text(
                config.read_text(encoding="utf-8").replace("max_fix_loops: 2", "max_fix_loops: 0"),
                encoding="utf-8",
            )
            with FakeCliPath(reject_once=True):
                result = ClodexWorkflow(repo).build("implement fixture", workspace_backend="local")
            self.assertEqual(result.status, "blocked")
            self.assertFalse(result.data["approved"])

    def test_malformed_plan_retries_once(self):
        with TempRepo() as repo, FakeCliPath(malformed_once=True):
            result = ClodexWorkflow(repo).plan("plan fixture")
            self.assertEqual(result.status, "planned")

    def test_envelope_error_retries_once_then_succeeds(self):
        with TempRepo() as repo, FakeCliPath(envelope_error_once=True):
            result = ClodexWorkflow(repo).plan("plan fixture")
            self.assertEqual(result.status, "planned")
            self.assertEqual(result.data["goal"], "test goal")

    def test_plan_artifact_is_the_plan_not_the_envelope(self):
        with TempRepo() as repo, FakeCliPath():
            result = ClodexWorkflow(repo).plan("plan fixture")
            plan = json.loads((Path(result.artifacts_dir) / "01-claude-plan.json").read_text(encoding="utf-8"))
            self.assertNotIn("type", plan)
            self.assertEqual(plan["goal"], "test goal")

    def test_agreement_without_required_reviewers_is_not_approved(self):
        self.assertFalse(ClodexWorkflow._agreement([], "h", 0)["approved"])
        optional_only = [{"reviewer_id": "x", "approved": True, "diff_hash": "h", "_required": False}]
        self.assertFalse(ClodexWorkflow._agreement(optional_only, "h", 0)["approved"])

    def test_agent_timeout_returns_result_instead_of_raising(self):
        with TempRepo() as repo, FakeCliPath(sleep_seconds=2):
            config = load_config(repo)
            result = AgentRunner(repo).run(claude_plan_command(config), "prompt", timeout=0.3)
            self.assertTrue(result.timed_out)
            self.assertEqual(result.returncode, 124)
            self.assertFalse(result.ok)
