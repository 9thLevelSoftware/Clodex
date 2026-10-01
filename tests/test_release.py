"""Tests: release-level behavior (apply_mode, mcp.async_tasks, eval self-test, changelog)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from clodex import __version__, evals
from clodex.config import DEFAULT_CONFIG, load_config
from clodex.evals import SCENARIOS, run_local_evals
from clodex.models import validate
from clodex.workflow import ClodexWorkflow
from tests.support import ROOT, FakeCliPath, TempRepo
from tests.test_mcp_conformance import ServerCase


def write_contract(repo: Path, front_matter: str, commit: bool = False) -> None:
    (repo / "CLODEX.md").write_text(f"---\n{front_matter}---\nbody\n", encoding="utf-8")
    if commit:  # the worktree backend refuses a checkout with uncommitted tracked changes
        subprocess.run(["git", "add", "CLODEX.md"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "contract"], cwd=repo, check=True, capture_output=True)


class ApplyModeTests(unittest.TestCase):
    def test_auto_applies_an_approved_worktree_build_and_manual_does_not(self):
        with TempRepo() as repo, FakeCliPath():
            write_contract(repo, "workspace:\n  apply_mode: auto\n", commit=True)
            result = ClodexWorkflow(repo).build("implement fixture", workspace_backend="git-worktree")
            self.assertEqual(result.status, "applied")
            self.assertTrue((repo / "implemented.txt").is_file())
        with TempRepo() as repo, FakeCliPath():
            write_contract(repo, "workspace:\n  apply_mode: manual\n", commit=True)
            result = ClodexWorkflow(repo).build("implement fixture", workspace_backend="git-worktree")
            self.assertEqual(result.status, "approved")
            self.assertFalse((repo / "implemented.txt").exists())

    def test_auto_never_applies_a_blocked_build(self):
        with TempRepo() as repo, FakeCliPath(reject_reviewers=("codex-architecture",)):
            write_contract(repo, "max_fix_loops: 0\nworkspace:\n  apply_mode: auto\n", commit=True)
            result = ClodexWorkflow(repo).build("implement fixture", workspace_backend="git-worktree")
            self.assertEqual(result.status, "blocked")
            self.assertFalse((repo / "implemented.txt").exists())

    def test_unknown_apply_mode_is_a_doctor_error(self):
        with TempRepo() as repo:
            write_contract(repo, "workspace:\n  apply_mode: sometimes\n")
            found = [d for d in validate(load_config(repo)) if d.level == "error"]
            self.assertEqual([d.where for d in found], ["workspace.apply_mode"])


class AsyncTasksSwitchTests(ServerCase):
    def test_disabled_removes_the_tasks_capability_and_the_task_tool(self):
        write_contract(self.repo, "mcp:\n  async_tasks: false\n")
        live = self.start(initialize=False)
        result = live.call("initialize", {"protocolVersion": "2025-11-25"})["result"]
        self.assertNotIn("tasks", result["capabilities"])
        self.assertEqual(live.call("tasks/list")["error"]["code"], -32601)
        ignored = live.call("tools/call", {"name": "clodex_build", "arguments": {"task": "x", "dry_run": True}, "task": {"ttl": 1}})
        self.assertFalse(ignored["result"]["isError"], "task metadata is ignored, the call runs normally")
        started = live.tool("clodex_task_start", {"task": "x"})["result"]
        self.assertTrue(started["isError"])
        self.assertIn("mcp.async_tasks", started["content"][0]["text"])

    def test_enabled_by_default_and_a_broken_config_does_not_take_the_server_down(self):
        live = self.start(initialize=False)
        self.assertIn("tasks", live.call("initialize", {"protocolVersion": "2025-11-25"})["result"]["capabilities"])
        (self.repo / "CLODEX.md").write_text("---\nclaude: [unterminated\n---\n", encoding="utf-8")
        broken = self.start(initialize=False).call("initialize", {"protocolVersion": "2025-11-25"})
        self.assertIn("tasks", broken["result"]["capabilities"], "falls back to the default instead of failing initialize")
        write_contract(self.repo, "")  # leave a valid file: the test cleanup loads the config too


class EvalTests(unittest.TestCase):
    def test_every_scenario_passes_on_a_clean_repo_and_nothing_is_written_to_it(self):
        with TempRepo() as repo:
            before = {p.name for p in repo.iterdir()}
            data = run_local_evals(repo)
            failing = [s for s in data["scenarios"] if not s["passed"]]
            self.assertTrue(data["passed"], failing)
            self.assertEqual([s["name"] for s in data["scenarios"]], [name for name, _ in SCENARIOS])
            self.assertTrue(all(s["detail"] for s in data["scenarios"]))
            self.assertEqual({p.name for p in repo.iterdir()}, before, "the self-test must not touch the repo (no .clodex state)")

    def test_a_bad_config_fails_its_scenario_but_the_rest_still_run(self):
        with TempRepo() as repo:
            write_contract(repo, 'audit:\n  reviewers: [{"id": "a", "backend": "claude", "required": false}]\n')
            data = run_local_evals(repo)
            by_name = {s["name"]: s for s in data["scenarios"]}
            self.assertFalse(data["passed"])
            self.assertFalse(by_name["config-valid"]["passed"])
            self.assertIn("no required reviewers", by_name["config-valid"]["detail"])
            self.assertFalse(by_name["reviewers-and-quorum"]["passed"])
            self.assertTrue(by_name["json-schemas"]["passed"] and by_name["hooks"]["passed"])

    def test_a_scenario_that_crashes_is_reported_not_raised(self):
        def explode(config, tmp):
            raise KeyError("boom")

        patched = [("explodes", explode), *SCENARIOS[:1]]
        with TempRepo() as repo, mock.patch.object(evals, "SCENARIOS", patched):
            data = run_local_evals(repo)
        self.assertEqual([s["passed"] for s in data["scenarios"]], [False, True])
        self.assertIn("KeyError", data["scenarios"][0]["detail"])

    def test_cli_output_and_exit_code(self):
        with TempRepo() as repo:
            result = subprocess.run([sys.executable, "-m", "clodex", "eval", "run"], cwd=repo, env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("[ok]   config-valid", result.stdout)
            self.assertIn("All checks passed.", result.stdout)
            write_contract(repo, "audit:\n  quorum: most\n")
            failed = subprocess.run([sys.executable, "-m", "clodex", "eval", "run"], cwd=repo, env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(failed.returncode, 1)
            self.assertIn("[FAIL]", failed.stdout)
            self.assertRegex(failed.stdout, r"\d+ check\(s\) failed")


class ReleaseHygieneTests(unittest.TestCase):
    def test_unused_config_keys_are_gone(self):
        self.assertNotIn("personas", DEFAULT_CONFIG["audit"])
        self.assertNotIn("personas", (ROOT / "CLODEX.md").read_text(encoding="utf-8"))

    def test_changelog_leads_with_the_current_version_and_covers_the_upgrade(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        headings = re.findall(r"(?m)^## (\S+)", text)
        self.assertEqual(headings[0], __version__, "newest release first")
        self.assertEqual(len(headings), len(set(headings)))
        for must in ("clodex doctor", "init --migrate", "init --force", "gpt-6.1-sol"):
            self.assertIn(must, text)
        self.assertIn("CHANGELOG.md", (ROOT / "package.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
