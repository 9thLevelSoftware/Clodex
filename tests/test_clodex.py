from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from clodex.commands import claude_plan_command, codex_exec_command, codex_review_command
from clodex.config import load_config
from clodex.jsonutil import extract_json_object
from clodex.workflow import ClodexWorkflow


ROOT = Path(__file__).resolve().parents[1]


class TempRepo:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        shutil.copy(ROOT / "CLODEX.md", self.path / "CLODEX.md")
        subprocess.run(["git", "init"], cwd=self.path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.path, check=True)
        (self.path / "seed.txt").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "add", "CLODEX.md", "seed.txt"], cwd=self.path, check=True)
        subprocess.run(["git", "commit", "-m", "seed"], cwd=self.path, check=True, capture_output=True)
        return self.path

    def __exit__(self, exc_type, exc, tb):
        self.tmp.cleanup()


class FakeCliPath:
    def __init__(self, reject_once: bool = False, malformed_once: bool = False):
        self.reject_once = reject_once
        self.malformed_once = malformed_once

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name)
        self._write_fake()
        self.old_path = os.environ.get("PATH", "")
        self.old_pathext = os.environ.get("PATHEXT", "")
        os.environ["PATH"] = str(self.bin) + os.pathsep + self.old_path
        if os.name == "nt":
            os.environ["PATHEXT"] = ".CMD;.BAT;.EXE;" + self.old_pathext
        return self.bin

    def __exit__(self, exc_type, exc, tb):
        os.environ["PATH"] = self.old_path
        if os.name == "nt":
            os.environ["PATHEXT"] = self.old_pathext
        self.tmp.cleanup()

    def _write_fake(self):
        fake = self.bin / "fake_cli.py"
        fake.write_text(
            f"""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

name = sys.argv[1]
args = sys.argv[2:]
stdin = sys.stdin.read()

if '--version' in args:
    print(name + ' fake 1.0.0')
    raise SystemExit(0)

def diff_hash():
    out = subprocess.run(['git', 'diff', '--binary', 'HEAD'], capture_output=True, text=True, encoding='utf-8').stdout
    return hashlib.sha256(out.encode('utf-8')).hexdigest()

def requested_hash():
    match = re.search(r'Diff hash: ([a-f0-9]{{64}})', stdin)
    return match.group(1) if match else diff_hash()

if name == 'claude':
    marker = Path('.fake-claude-malformed')
    if {str(self.malformed_once)!r} == 'True' and not marker.exists():
        marker.write_text('seen')
        print('not json')
        raise SystemExit(0)
    if 'adversarial auditor' in stdin:
        h = requested_hash()
        reject_marker = Path('.fake-claude-reject')
        if {str(self.reject_once)!r} == 'True' and not reject_marker.exists():
            reject_marker.write_text('seen')
            print(json.dumps({{'approved': False, 'diff_hash': h, 'summary': 'reject once', 'findings': [], 'required_fixes': ['append fixed line']}}))
        else:
            print(json.dumps({{'approved': True, 'diff_hash': h, 'summary': 'ok', 'findings': [], 'required_fixes': []}}))
    else:
        print(json.dumps({{'goal': 'test goal', 'scope': ['repo'], 'out_of_scope': [], 'implementation_spec': ['write implemented.txt'], 'acceptance_criteria': ['diff exists'], 'risks': [], 'test_commands': ['python -m unittest']}}))
    raise SystemExit(0)

if name == 'codex':
    if args and args[0] == 'review':
        h = requested_hash()
        print(json.dumps({{'approved': True, 'diff_hash': h, 'summary': 'ok', 'findings': [], 'required_fixes': []}}))
        raise SystemExit(0)
    if 'Required fixes' in stdin:
        Path('implemented.txt').write_text('implemented\\nfixed\\n', encoding='utf-8')
        print('fixed implementation')
    else:
        Path('implemented.txt').write_text('implemented\\n', encoding='utf-8')
        print('implemented')
    raise SystemExit(0)

raise SystemExit(2)
""",
            encoding="utf-8",
        )
        if os.name == "nt":
            for name in ("claude", "codex"):
                (self.bin / f"{name}.cmd").write_text(
                    f"@echo off\r\n{sys.executable} \"%~dp0fake_cli.py\" {name} %*\r\n",
                    encoding="utf-8",
                )
        else:
            for name in ("claude", "codex"):
                path = self.bin / name
                path.write_text(f"#!/usr/bin/env bash\nexec {sys.executable} \"$(dirname \"$0\")/fake_cli.py\" {name} \"$@\"\n", encoding="utf-8")
                path.chmod(path.stat().st_mode | stat.S_IXUSR)


class ClodexTests(unittest.TestCase):
    def test_config_and_commands_use_requested_defaults(self):
        with TempRepo() as repo:
            config = load_config(repo)
            self.assertEqual(config.claude["model"], "opus")
            self.assertEqual(config.claude["effort"], "max")
            self.assertEqual(config.codex["model"], "gpt-5.5")
            self.assertEqual(config.codex["reasoning_effort"], "xhigh")
            self.assertIn("--permission-mode", claude_plan_command(config).argv)
            self.assertIn("model_reasoning_effort=\"xhigh\"", codex_exec_command(config, repo).argv)
            self.assertIn("--uncommitted", codex_review_command(config).argv)

    def test_json_extraction_handles_fenced_json(self):
        value = extract_json_object("```json\n{\"approved\": true}\n```")
        self.assertTrue(value["approved"])

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

    def test_build_happy_path_creates_agreement(self):
        with TempRepo() as repo, FakeCliPath():
            result = ClodexWorkflow(repo).build("implement fixture")
            self.assertEqual(result.status, "approved")
            agreement = json.loads((Path(result.artifacts_dir) / "05-agreement.json").read_text(encoding="utf-8"))
            self.assertTrue(agreement["approved"])
            self.assertTrue((repo / "implemented.txt").exists())

    def test_rejection_triggers_fix_loop(self):
        with TempRepo() as repo, FakeCliPath(reject_once=True):
            result = ClodexWorkflow(repo).build("implement fixture")
            self.assertEqual(result.status, "approved")
            self.assertIn("fixed", (repo / "implemented.txt").read_text(encoding="utf-8"))

    def test_malformed_plan_retries_once(self):
        with TempRepo() as repo, FakeCliPath(malformed_once=True):
            result = ClodexWorkflow(repo).plan("plan fixture")
            self.assertEqual(result.status, "planned")

    def test_mcp_tools_list(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        result = subprocess.run(
            [sys.executable, "-m", "clodex", "mcp-server"],
            input=json.dumps(request) + "\n",
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout.splitlines()[0])
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertIn("clodex_build", names)
        self.assertIn("clodex_task_update", names)


if __name__ == "__main__":
    unittest.main()
