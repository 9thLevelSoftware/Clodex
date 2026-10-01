"""Tests: CLI entry points: doctor, init, native status/doctor."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from clodex.native import BEGIN_MARKER
from tests.support import ROOT, cli_test_python, TempRepo, FakeCliPath


class CliTests(unittest.TestCase):
    def test_doctor_reports_fake_clis(self):
        with TempRepo() as repo, FakeCliPath():
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "doctor"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            self.assertTrue(data["claude"]["ok"])
            self.assertTrue(data["codex"]["ok"])

    def test_cli_init_dry_run_does_not_write_files(self):
        with TempRepo() as repo:
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "init", "--dry-run"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            self.assertTrue(data["dry_run"])
            self.assertFalse((repo / "CLAUDE.md").exists())
            self.assertTrue(any(Path(item["path"]).name == "CLAUDE.md" for item in data["files"]))

    def test_cli_init_dry_run_reports_blocked_codex_parent_without_partial_writes(self):
        with TempRepo() as repo:
            clodex_before = (repo / "CLODEX.md").read_bytes()
            (repo / ".codex").write_text("not a directory\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "init", "--dry-run"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            item = next(
                item
                for item in data["files"]
                if Path(item["path"]).name == "config.toml" and Path(item["path"]).parent.name == ".codex"
            )
            self.assertEqual(item["action"], "error")
            self.assertEqual(item["status"], "invalid")
            self.assertIn(".codex", item["error"])
            self.assertFalse((repo / "CLAUDE.md").exists())
            self.assertFalse((repo / "AGENTS.md").exists())
            self.assertFalse((repo / ".mcp.json").exists())
            self.assertTrue((repo / ".codex").is_file())
            self.assertEqual((repo / "CLODEX.md").read_bytes(), clodex_before)

    def test_cli_init_writes_native_files_and_preserves_user_content(self):
        with TempRepo() as repo:
            (repo / "CLAUDE.md").write_text("user header\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "init"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            claude = (repo / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertTrue(claude.startswith("user header\n"))
            self.assertIn(BEGIN_MARKER, claude)
            self.assertTrue((repo / "AGENTS.md").exists())
            self.assertTrue((repo / ".mcp.json").exists())
            self.assertTrue((repo / ".codex" / "config.toml").exists())

    def test_cli_init_no_mcp_config_writes_only_instruction_files(self):
        with TempRepo() as repo:
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "init", "--no-mcp-config"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((repo / "CLAUDE.md").exists())
            self.assertFalse((repo / ".mcp.json").exists())
            self.assertFalse((repo / ".codex" / "config.toml").exists())

    def test_cli_init_rejects_blocked_codex_parent_without_partial_writes(self):
        with TempRepo() as repo:
            clodex_before = (repo / "CLODEX.md").read_bytes()
            (repo / ".codex").write_text("not a directory\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "init"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            self.assertFalse(data["ok"])
            self.assertIn(".codex", data["error"])
            self.assertFalse((repo / "CLAUDE.md").exists())
            self.assertFalse((repo / "AGENTS.md").exists())
            self.assertFalse((repo / ".mcp.json").exists())
            self.assertTrue((repo / ".codex").is_file())
            self.assertEqual((repo / "CLODEX.md").read_bytes(), clodex_before)

    def test_cli_native_status_reports_current_and_missing_components(self):
        with TempRepo() as repo:
            subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "init"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            status = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "native", "status"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            data = json.loads(status.stdout)
            self.assertTrue(data["ok"])
            statuses = {Path(item["path"]).name: item["status"] for item in data["files"]}
            self.assertEqual(statuses["CLAUDE.md"], "current")
            (repo / "AGENTS.md").unlink()
            missing_status = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "native", "status"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(missing_status.returncode, 1, missing_status.stdout + missing_status.stderr)
            missing_data = json.loads(missing_status.stdout)
            self.assertFalse(missing_data["ok"])
            files = {Path(item["path"]).name: item for item in missing_data["files"]}
            self.assertEqual(files["CLAUDE.md"]["status"], "current")
            self.assertEqual(files["AGENTS.md"]["status"], "missing")
            self.assertEqual(files["AGENTS.md"]["action"], "create")

    def test_cli_native_status_reports_invalid_malformed_block(self):
        with TempRepo() as repo:
            (repo / "CLAUDE.md").write_text("header\n<!-- BEGIN CLODEX -->\nmissing end\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "native", "status", "--no-mcp-config"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            self.assertFalse(data["ok"])
            statuses = {Path(item["path"]).name: item["status"] for item in data["files"]}
            self.assertEqual(statuses["CLAUDE.md"], "invalid")

    def test_cli_native_status_reports_blocked_config_parent(self):
        with TempRepo() as repo:
            (repo / ".codex").write_text("not a directory\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "native", "status"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            self.assertFalse(data["ok"])
            item = next(item for item in data["files"] if Path(item["path"]).name == "config.toml")
            self.assertEqual(item["status"], "invalid")
            self.assertEqual(item["action"], "error")
            self.assertIn(".codex", item["error"])

    def test_cli_native_doctor_combines_doctor_and_native_status(self):
        python = cli_test_python()
        if python is None:
            self.skipTest("native doctor CLI test requires Python 3.12+")
        with TempRepo() as repo, FakeCliPath(include_clodex=True):
            subprocess.run(
                [python, "-m", "clodex", "--json", "init"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            result = subprocess.run(
                [python, "-m", "clodex", "--json", "native", "doctor"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            self.assertTrue(data["ok"])
            self.assertTrue(data["doctor"]["claude"]["ok"])
            self.assertTrue(data["doctor"]["codex"]["ok"])
            self.assertTrue(data["npm_launcher"]["ok"])

    def test_node_launcher_native_doctor_sets_launcher_env(self):
        python = cli_test_python()
        if python is None:
            self.skipTest("node launcher native doctor test requires Python 3.12+")
        with TempRepo() as repo, FakeCliPath():
            subprocess.run(
                [python, "-m", "clodex", "--json", "init"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            result = subprocess.run(
                ["node", str(ROOT / "npm" / "clodex.js"), "--json", "native", "doctor"],
                cwd=repo,
                env={**os.environ, "CLODEX_PYTHON": python},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            self.assertTrue(data["ok"])
            self.assertTrue(data["npm_launcher"]["ok"])
            self.assertEqual(Path(data["npm_launcher"]["path"]).resolve(), (ROOT / "npm" / "clodex.js").resolve())

    def test_native_doctor_fails_when_npm_launcher_missing(self):
        from clodex.native import native_doctor

        with TempRepo() as repo, FakeCliPath():
            subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "init"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )

            original_which = shutil.which

            def without_clodex(command, *args, **kwargs):
                if command in {"clodex", "clodex.cmd", "clodex.ps1"}:
                    return None
                return original_which(command, *args, **kwargs)

            doctor = {
                "ok": True,
                "repo_root": str(repo),
                "claude": {"ok": True},
                "codex": {"ok": True},
            }
            with (
                mock.patch("clodex.native.run_doctor", return_value=(0, doctor)) as run_doctor,
                mock.patch("clodex.native.shutil.which", side_effect=without_clodex),
                mock.patch.dict(os.environ, {"CLODEX_NPM_LAUNCHER": ""}),
            ):
                exit_code, data = native_doctor(repo)

            self.assertNotEqual(exit_code, 0)
            self.assertFalse(data["npm_launcher"]["ok"])
            self.assertFalse(data["ok"])
            self.assertTrue(data["native"]["ok"])
            self.assertTrue(data["doctor"]["claude"]["ok"])
            self.assertTrue(data["doctor"]["codex"]["ok"])
            self.assertIn("reason", data["npm_launcher"])
            run_doctor.assert_called_once_with(repo)

    def test_native_doctor_global_mode_uses_home_for_doctor_root(self):
        from clodex.native import native_doctor

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            launcher = home / "clodex.js"
            launcher.write_text("// fake launcher\n", encoding="utf-8")
            doctor = {
                "ok": True,
                "repo_root": str(home),
                "claude": {"ok": True},
                "codex": {"ok": True},
            }
            with (
                mock.patch("clodex.native.Path.home", return_value=home),
                mock.patch("clodex.native.run_doctor", return_value=(0, doctor)) as run_doctor,
                mock.patch("clodex.native._npm_launcher_status", return_value={"ok": True, "path": str(launcher)}),
            ):
                _exit_code, data = native_doctor(Path("repo"), global_mode=True)

            self.assertEqual(data["native"]["root"], str(home))
            self.assertEqual(data["doctor"]["repo_root"], str(home))
            run_doctor.assert_called_once_with(home)
