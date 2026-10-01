"""Tests: run failure handling, worktree cleanup and safe apply."""

from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from clodex.artifacts import ArtifactStore, current_diff, hash_text
from clodex.config import load_config
from clodex.workflow import ClodexWorkflow
from tests.support import FakeCliPath, TempRepo


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def worktrees(repo: Path) -> list[str]:
    return [line for line in git(repo, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]


def lock_released(workflow: ClodexWorkflow, run_id: str):
    with workflow.state.session() as con:
        row = con.execute("select released_at from workspace_locks where run_id=?", (run_id,)).fetchone()
        return row["released_at"] if row else "no-lock"


class FailureHandlingTests(unittest.TestCase):
    def test_unexpected_error_marks_build_failed_and_removes_its_worktree(self):
        with TempRepo() as repo, FakeCliPath():
            workflow = ClodexWorkflow(repo)
            with mock.patch.object(ClodexWorkflow, "_run_json_with_retry", side_effect=RuntimeError("boom")):
                result = workflow.build("implement fixture", workspace_backend="git-worktree")
            self.assertEqual(result.status, "failed")
            self.assertIn("boom", result.data["error"])
            self.assertTrue(result.data["workspace_released"])
            run = workflow.state.get_run(result.run_id)
            self.assertEqual(run["status"], "failed")
            self.assertIn("boom", run["error"])
            self.assertFalse(Path(run["workspace_path"]).exists())
            self.assertEqual(len(worktrees(repo)), 1, "only the main checkout should remain")
            self.assertIsNotNone(lock_released(workflow, result.run_id))
            self.assertIn("RuntimeError", (Path(result.artifacts_dir) / "error.txt").read_text(encoding="utf-8"))
            task = next(t for t in workflow.state.list_tasks() if t["id"] == result.task_id)
            self.assertEqual(task["status"], "failed")

    def test_unexpected_error_marks_plan_failed(self):
        with TempRepo() as repo, FakeCliPath():
            workflow = ClodexWorkflow(repo)
            with mock.patch.object(ClodexWorkflow, "_run_json_with_retry", side_effect=RuntimeError("no plan")):
                result = workflow.plan("plan fixture")
            self.assertEqual(result.status, "failed")
            self.assertEqual(workflow.state.get_run(result.run_id)["status"], "failed")

    def test_failed_local_build_never_removes_the_source_checkout(self):
        with TempRepo() as repo, FakeCliPath():
            workflow = ClodexWorkflow(repo)
            with mock.patch.object(ClodexWorkflow, "_run_json_with_retry", side_effect=RuntimeError("boom")):
                result = workflow.build("implement fixture", workspace_backend="local")
            self.assertEqual(result.status, "failed")
            self.assertFalse(result.data["workspace_released"])
            self.assertTrue((repo / "seed.txt").exists())


class CleanTests(unittest.TestCase):
    def test_clean_removes_finished_worktree_but_keeps_artifacts(self):
        with TempRepo() as repo, FakeCliPath():
            workflow = ClodexWorkflow(repo)
            built = workflow.build("implement fixture", workspace_backend="git-worktree")
            self.assertEqual(built.status, "approved")
            workspace = Path(built.data["workspace"]["path"])
            self.assertTrue(workspace.exists())
            cleaned = workflow.clean_run(built.run_id)
            self.assertEqual(cleaned.status, "cleaned")
            self.assertFalse(workspace.exists())
            self.assertEqual(len(worktrees(repo)), 1)
            self.assertTrue((Path(built.artifacts_dir) / "apply.patch").exists())
            self.assertEqual(workflow.clean_run(built.run_id).status, "nothing-to-clean")
            self.assertEqual(workflow.apply_run(built.run_id, check=True).status, "apply-check", "the patch outlives the worktree")

    def test_clean_refuses_runs_that_are_still_active(self):
        with TempRepo() as repo:
            workflow = ClodexWorkflow(repo)
            workflow.state.upsert_task("t", "t", "running")
            workflow.state.create_run("r1", "t", "p", "running")
            self.assertEqual(workflow.clean_run("r1").status, "clean-refused")
            with self.assertRaises(ValueError):
                workflow.clean_run("nope")

    def test_release_only_removes_paths_under_workspace_root(self):
        with TempRepo() as repo:
            from clodex.workspace import WorkspaceManager

            manager = WorkspaceManager(repo, load_config(repo))
            self.assertFalse(manager.release(repo))
            self.assertFalse(manager.release(repo / "seed.txt"))
            self.assertTrue((repo / "seed.txt").exists())


class ApplySafetyTests(unittest.TestCase):
    def test_approved_worktree_run_applies_once(self):
        with TempRepo() as repo, FakeCliPath():
            workflow = ClodexWorkflow(repo)
            built = workflow.build("implement fixture", workspace_backend="git-worktree")
            self.assertEqual(workflow.apply_run(built.run_id, check=True).status, "apply-check")
            self.assertFalse((repo / "implemented.txt").exists(), "--check must not modify the tree")
            applied = workflow.apply_run(built.run_id)
            self.assertEqual(applied.status, "applied")
            self.assertTrue((repo / "implemented.txt").exists())
            again = workflow.apply_run(built.run_id)
            self.assertEqual(again.status, "apply-refused")
            self.assertIn("already applied", again.data["error"])

    def test_blocked_run_is_refused_unless_forced(self):
        with TempRepo() as repo, FakeCliPath(reject_reviewers=("codex-architecture",)):
            (repo / "CLODEX.md").write_text("---\nmax_fix_loops: 0\n---\nbody\n", encoding="utf-8")
            git(repo, "add", "CLODEX.md")
            git(repo, "commit", "-m", "no fix loops")
            workflow = ClodexWorkflow(repo)
            built = workflow.build("implement fixture", workspace_backend="git-worktree")
            self.assertEqual(built.status, "blocked")
            refused = workflow.apply_run(built.run_id)
            self.assertEqual(refused.status, "apply-refused")
            self.assertIn("blocked", refused.data["error"])
            self.assertFalse((repo / "implemented.txt").exists())
            self.assertEqual(workflow.apply_run(built.run_id, force=True).status, "applied")
            self.assertTrue((repo / "implemented.txt").exists())

    def test_patch_that_no_longer_matches_the_approved_hash_is_refused(self):
        with TempRepo() as repo, FakeCliPath():
            workflow = ClodexWorkflow(repo)
            built = workflow.build("implement fixture", workspace_backend="git-worktree")
            patch = Path(built.artifacts_dir) / "apply.patch"
            patch.write_bytes(patch.read_bytes().replace(b"implemented", b"tampered!!!"))
            refused = workflow.apply_run(built.run_id)
            self.assertEqual(refused.status, "apply-refused")
            self.assertIn("approved diff hash", refused.data["error"])
            self.assertFalse((repo / "implemented.txt").exists())

    def test_local_workspace_run_reports_changes_already_in_the_tree(self):
        with TempRepo() as repo, FakeCliPath():
            workflow = ClodexWorkflow(repo)
            built = workflow.build("implement fixture", workspace_backend="local")
            self.assertEqual(built.status, "approved")
            result = workflow.apply_run(built.run_id)
            self.assertEqual(result.status, "applied")
            self.assertIn("already in the working tree", result.data["note"])
            self.assertEqual(workflow.state.get_run(built.run_id)["status"], "applied")


class ByteExactDiffTests(unittest.TestCase):
    def test_crlf_content_round_trips_through_diff_artifact_and_hash(self):
        with TempRepo() as repo:
            git(repo, "config", "core.autocrlf", "false")
            (repo / "crlf.txt").write_bytes(b"a\r\nb\r\n")
            git(repo, "add", "crlf.txt")
            git(repo, "commit", "-m", "crlf")
            (repo / "crlf.txt").write_bytes(b"a\r\nb\r\nc\r\n")
            raw = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=repo, capture_output=True, check=True).stdout
            diff = current_diff(repo)
            self.assertIn("+c\r\n", diff)
            self.assertEqual(hash_text(diff), hashlib.sha256(raw).hexdigest())
            saved = ArtifactStore(load_config(repo), "run-x").write_text("changes.diff", diff, exact=True)
            self.assertEqual(saved.read_bytes(), raw)
            # Reset everything: the fixture's seed files were written under the host's autocrlf setting.
            git(repo, "checkout", "--", ".")
            applied =subprocess.run(["git", "apply", "--check", str(saved)], cwd=repo, capture_output=True, text=True)
            self.assertEqual(applied.returncode, 0, applied.stderr)

    def test_non_utf8_bytes_survive_the_diff_round_trip(self):
        with TempRepo() as repo:
            git(repo, "config", "core.autocrlf", "false")
            (repo / "latin.txt").write_bytes(b"caf\xe9\n")
            git(repo, "add", "latin.txt")
            git(repo, "commit", "-m", "latin1")
            (repo / "latin.txt").write_bytes(b"caf\xe9 au lait\n")
            raw = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=repo, capture_output=True, check=True).stdout
            diff = current_diff(repo)
            self.assertEqual(hash_text(diff), hashlib.sha256(raw).hexdigest())
            saved = ArtifactStore(load_config(repo), "run-y").write_text("changes.diff", diff, exact=True)
            self.assertEqual(saved.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
