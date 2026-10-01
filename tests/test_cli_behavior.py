"""Tests: CLI behavior (audit targets, exit codes, errors, repo root, human output)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from clodex import cli
from clodex.cli import exit_code_for, format_human
from clodex.workflow import ClodexWorkflow
from tests.support import ROOT, FakeCliPath, TempRepo


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def run_main(repo: Path, *args: str, env: dict | None = None) -> tuple[int, str, str]:
    """Run clodex.cli.main in-process against `repo`; returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, {"CLODEX_REPO_ROOT": str(repo), **(env or {})}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(args))
        except SystemExit as exc:  # argparse usage errors
            code = int(exc.code)
    return code, out.getvalue(), err.getvalue()


def run_cli(repo: Path, *args: str, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "clodex", *args],
        cwd=cwd or repo,
        env={**os.environ, "PYTHONPATH": str(ROOT), **(env or {})},
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


class ExitCodeTests(unittest.TestCase):
    def test_status_to_exit_code(self):
        for status in ("approved", "planned", "applied", "apply-check", "dry-run", "queued", "cleaned", "nothing-to-clean", "nothing-to-audit"):
            self.assertEqual(exit_code_for(status), 0, status)
        for status in ("blocked", "apply-refused", "apply-failed", "apply-check-failed", "clean-refused"):
            self.assertEqual(exit_code_for(status), 1, status)
        self.assertEqual(exit_code_for("failed"), 3)
        self.assertEqual(exit_code_for("cancelled"), 4)

    def test_build_exit_codes_through_main(self):
        with TempRepo() as repo, FakeCliPath():
            code, out, _ = run_main(repo, "--json", "build", "--workspace", "local", "implement fixture")
            self.assertEqual((code, json.loads(out)["status"]), (0, "approved"))
        with TempRepo() as repo, FakeCliPath(reject_reviewers=("codex-architecture",)):
            (repo / "CLODEX.md").write_text("---\nmax_fix_loops: 0\n---\nbody\n", encoding="utf-8")
            code, out, _ = run_main(repo, "--json", "build", "--workspace", "local", "x")
            self.assertEqual((code, json.loads(out)["status"]), (1, "blocked"))
        with TempRepo() as repo, FakeCliPath():
            with mock.patch.object(ClodexWorkflow, "_run_json_with_retry", side_effect=RuntimeError("boom")):
                code, out, _ = run_main(repo, "--json", "plan", "x")
            self.assertEqual((code, json.loads(out)["status"]), (3, "failed"))


class ErrorHandlingTests(unittest.TestCase):
    def test_unknown_run_ids_are_one_line_errors_not_tracebacks(self):
        with TempRepo() as repo:
            for args in (("apply", "nope"), ("clean", "nope"), ("task", "cancel", "nope")):
                result = run_cli(repo, *args)
                self.assertEqual(result.returncode, 2, args)
                self.assertIn("clodex: error: Unknown run: nope", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_debug_env_restores_the_traceback(self):
        with TempRepo() as repo:
            result = run_cli(repo, "apply", "nope", env={"CLODEX_DEBUG": "1"})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Traceback", result.stderr)

    def test_usage_errors_exit_2(self):
        with TempRepo() as repo:
            self.assertEqual(run_cli(repo, "audit", "--base", "x", "--commit", "y").returncode, 2)
            self.assertEqual(run_cli(repo, "nonsense").returncode, 2)


class AuditTargetTests(unittest.TestCase):
    def commit_change(self, repo: Path, name: str = "feature.txt", text: str = "feature\n") -> str:
        (repo / name).write_text(text, encoding="utf-8")
        git(repo, "add", name)
        git(repo, "commit", "-m", f"add {name}")
        return git(repo, "rev-parse", "HEAD")

    def test_nothing_to_audit_creates_no_run(self):
        with TempRepo() as repo, FakeCliPath():
            code, out, _ = run_main(repo, "--json", "audit")
            self.assertEqual((code, json.loads(out)["status"]), (0, "nothing-to-audit"))
            self.assertEqual(ClodexWorkflow(repo).state.list_runs(), [])

    def test_uncommitted_diff_is_the_default_and_diff_flag_is_accepted(self):
        with TempRepo() as repo, FakeCliPath():
            (repo / "seed.txt").write_text("changed\n", encoding="utf-8")
            for flag in ([], ["--diff"]):
                code, out, _ = run_main(repo, "--json", "audit", *flag)
                self.assertEqual((code, json.loads(out)["status"]), (0, "approved"), flag)

    def test_audit_a_single_commit(self):
        with TempRepo() as repo, FakeCliPath():
            sha = self.commit_change(repo)
            result = ClodexWorkflow(repo).audit(commit=sha)
            self.assertEqual(result.status, "approved")
            diff = (Path(result.artifacts_dir) / "changes.diff").read_text(encoding="utf-8")
            self.assertIn("feature.txt", diff)
            self.assertNotIn("seed.txt", diff)

    def test_audit_of_a_commit_never_runs_a_fix_attempt(self):
        with TempRepo() as repo, FakeCliPath(reject_reviewers=("codex-architecture",)):
            (repo / "CLODEX.md").write_text("---\nmax_fix_loops: 2\n---\nbody\n", encoding="utf-8")
            git(repo, "add", "CLODEX.md")
            git(repo, "commit", "-m", "allow fixes")
            sha = self.commit_change(repo)
            result = ClodexWorkflow(repo).audit(commit=sha)
            self.assertEqual(result.status, "blocked")
            self.assertFalse(list(Path(result.artifacts_dir).glob("fix-attempt-*")))

    def test_audit_everything_since_a_base_ref_including_uncommitted_edits(self):
        with TempRepo() as repo, FakeCliPath():
            base = git(repo, "rev-parse", "HEAD")
            self.commit_change(repo, "one.txt")
            (repo / "two.txt").write_text("uncommitted\n", encoding="utf-8")
            git(repo, "add", "two.txt")
            result = ClodexWorkflow(repo).audit(base=base)
            self.assertEqual(result.status, "approved")
            diff = (Path(result.artifacts_dir) / "changes.diff").read_text(encoding="utf-8")
            self.assertIn("one.txt", diff)
            self.assertIn("two.txt", diff)

    def test_bad_refs_and_conflicting_targets_are_value_errors(self):
        with TempRepo() as repo, FakeCliPath():
            workflow = ClodexWorkflow(repo)
            with self.assertRaisesRegex(ValueError, "Unknown git ref: nope"):
                workflow.audit(base="nope")
            with self.assertRaisesRegex(ValueError, "Unknown git ref: nope"):
                workflow.audit(commit="nope")
            with self.assertRaisesRegex(ValueError, "either a base ref or a commit"):
                workflow.audit(base="HEAD", commit="HEAD")
            result = run_cli(repo, "audit", "--commit", "nope")
            self.assertEqual(result.returncode, 2)
            self.assertIn("Unknown git ref", result.stderr)


class RepoRootTests(unittest.TestCase):
    def test_commands_from_a_subdirectory_use_the_repo_root(self):
        with TempRepo() as repo:
            sub = repo / "pkg" / "deep"
            sub.mkdir(parents=True)
            result = run_cli(repo, "--json", "status", cwd=sub)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((repo / ".clodex" / "state.sqlite3").is_file())
            self.assertFalse((sub / ".clodex").exists())

    def test_repo_root_env_overrides_the_working_directory(self):
        with TempRepo() as repo, TempRepo() as elsewhere:
            result = run_cli(repo, "--json", "status", cwd=elsewhere, env={"CLODEX_REPO_ROOT": str(repo)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((repo / ".clodex" / "state.sqlite3").is_file())
            self.assertFalse((elsewhere / ".clodex").exists())


class HumanOutputTests(unittest.TestCase):
    def test_format_human_renders_nested_data_readably(self):
        lines = format_human({"tasks": [{"id": "t1", "status": "done"}], "ok": True, "note": None, "empty": [], "deep": {"a": 1}})
        text = "\n".join(lines)
        self.assertIn("tasks:\n  - t1\n    status: done", text)
        self.assertIn("ok: yes", text)
        self.assertIn("note: -", text)
        self.assertIn("empty: (none)", text)
        self.assertIn("deep:\n  a: 1", text)
        self.assertNotIn("{", text)

    def test_non_json_output_is_text_and_json_flag_still_json(self):
        with TempRepo() as repo, FakeCliPath():
            plain = run_cli(repo, "status")
            self.assertEqual(plain.returncode, 0, plain.stderr)
            with self.assertRaises(ValueError):
                json.loads(plain.stdout)
            self.assertIn("tasks: (none)", plain.stdout)
            self.assertIn("runs: (none)", plain.stdout)
            self.assertIsInstance(json.loads(run_cli(repo, "--json", "status").stdout), dict)

    def test_doctor_human_output_lists_checks_and_fixes(self):
        with TempRepo() as repo, FakeCliPath():
            (repo / "CLODEX.md").write_text("---\ncodex:\n  model: gpt-5.4\n---\nbody\n", encoding="utf-8")
            result = run_cli(repo, "doctor")
            self.assertEqual(result.returncode, 1)
            self.assertIn("[ok]   python", result.stdout)
            self.assertIn("[ok]   claude:", result.stdout)
            self.assertIn("[FAIL] codex.model", result.stdout)
            self.assertIn("fix:", result.stdout)
            self.assertIn("Problems found", result.stdout)
        with TempRepo() as repo, FakeCliPath():
            result = run_cli(repo, "doctor")
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("Everything looks good.", result.stdout)


class McpAuditToolTests(unittest.TestCase):
    def test_audit_tool_takes_base_and_commit_and_reports_bad_refs_as_tool_errors(self):
        from clodex import mcp_server

        with TempRepo() as repo, FakeCliPath(), mock.patch.dict(os.environ, {"CLODEX_REPO_ROOT": str(repo)}):
            properties = mcp_server.TOOL_INDEX["clodex_audit"]["inputSchema"]["properties"]
            self.assertIn("base", properties)
            self.assertIn("commit", properties)
            bad = mcp_server.tool_call("clodex_audit", {"commit": "nope"})
            self.assertTrue(bad["isError"])
            self.assertIn("Unknown git ref", bad["content"][0]["text"])
            (repo / "new.txt").write_text("x\n", encoding="utf-8")
            git(repo, "add", "new.txt")
            git(repo, "commit", "-m", "new")
            good = mcp_server.tool_call("clodex_audit", {"commit": "HEAD"})
            self.assertFalse(good["isError"])
            self.assertIn("approved", good["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
