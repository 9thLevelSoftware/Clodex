from __future__ import annotations

import json
import os
import runpy
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from clodex.commands import claude_plan_command, codex_exec_command, codex_review_command
from clodex.config import load_config
from clodex.jsonutil import extract_json_object
from clodex.native import (
    BEGIN_MARKER,
    END_MARKER,
    ManagedBlockError,
    TOML_BEGIN_MARKER,
    TOML_END_MARKER,
    build_agents_block,
    build_claude_block,
    replace_managed_block,
    replace_toml_managed_block,
)
from clodex.npm_bridge import main as npm_bridge_main
from clodex.state import StateStore
from clodex.trace import TraceWriter
from clodex.workflow import ClodexWorkflow
from clodex.workspace import DirtyWorkspaceError, WorkspaceManager


ROOT = Path(__file__).resolve().parents[1]
BUNDLED_PYTHON = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "python" / "python.exe"


def cli_test_python() -> str | None:
    if BUNDLED_PYTHON.exists():
        return str(BUNDLED_PYTHON)
    if sys.version_info >= (3, 12):
        return sys.executable
    return None


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
    def __init__(
        self,
        reject_once: bool = False,
        malformed_once: bool = False,
        sleep_seconds: float = 0,
        include_clodex: bool = False,
    ):
        self.reject_once = reject_once
        self.malformed_once = malformed_once
        self.sleep_seconds = sleep_seconds
        self.include_clodex = include_clodex

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

if {self.sleep_seconds!r}:
    import time
    time.sleep({self.sleep_seconds!r})

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
        reviewer = 'claude-plan'
        persona = 'plan-adherence'
        reviewer_match = re.search(r'Reviewer ID: ([^\\n]+)', stdin)
        persona_match = re.search(r'Persona: ([^\\n]+)', stdin)
        if reviewer_match:
            reviewer = reviewer_match.group(1).strip()
        if persona_match:
            persona = persona_match.group(1).strip()
        reject_marker = Path('.fake-claude-reject')
        if {str(self.reject_once)!r} == 'True' and not reject_marker.exists():
            reject_marker.write_text('seen')
            print(json.dumps({{'approved': False, 'diff_hash': h, 'reviewer_id': reviewer, 'persona': persona, 'summary': 'reject once', 'findings': [], 'required_fixes': ['append fixed line']}}))
        else:
            print(json.dumps({{'approved': True, 'diff_hash': h, 'reviewer_id': reviewer, 'persona': persona, 'summary': 'ok', 'findings': [], 'required_fixes': []}}))
    else:
        print(json.dumps({{'goal': 'test goal', 'scope': ['repo'], 'out_of_scope': [], 'implementation_spec': ['write implemented.txt'], 'acceptance_criteria': ['diff exists'], 'risks': [], 'test_commands': ['python -m unittest']}}))
    raise SystemExit(0)

if name == 'codex':
    if args and args[0] == 'review':
        h = requested_hash()
        reviewer = 'codex-architecture'
        persona = 'architecture'
        reviewer_match = re.search(r'Reviewer ID: ([^\\n]+)', stdin)
        persona_match = re.search(r'Persona: ([^\\n]+)', stdin)
        if reviewer_match:
            reviewer = reviewer_match.group(1).strip()
        if persona_match:
            persona = persona_match.group(1).strip()
        print(json.dumps({{'approved': True, 'diff_hash': h, 'reviewer_id': reviewer, 'persona': persona, 'summary': 'ok', 'findings': [], 'required_fixes': []}}))
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
        names = ["claude", "codex"]
        if self.include_clodex:
            names.append("clodex")
        if os.name == "nt":
            for name in names:
                (self.bin / f"{name}.cmd").write_text(
                    f"@echo off\r\n{sys.executable} \"%~dp0fake_cli.py\" {name} %*\r\n",
                    encoding="utf-8",
                )
        else:
            for name in names:
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
            self.assertEqual(config.workspace["backend"], "git-worktree")
            self.assertEqual(config.workspace["apply_mode"], "manual")
            self.assertEqual(config.codex["approval_profile"], "ci")
            self.assertTrue(config.mcp["async_tasks"])
            self.assertTrue(config.tracing["enabled"])
            self.assertGreaterEqual(len(config.reviewers), 2)
            self.assertIn("--permission-mode", claude_plan_command(config).argv)
            self.assertIn("model_reasoning_effort=\"xhigh\"", codex_exec_command(config, repo).argv)
            self.assertIn("--uncommitted", codex_review_command(config).argv)

    def test_approval_profiles_change_codex_command(self):
        with TempRepo() as repo:
            config = load_config(repo)
            ci = codex_exec_command(config, repo).argv
            local = codex_exec_command(config, repo, approval_profile="local").argv
            auto = codex_exec_command(config, repo, approval_profile="auto_review").argv
            self.assertIn("never", ci)
            self.assertIn("on-request", local)
            self.assertIn("approvals_reviewer=\"auto_review\"", auto)

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
            self.assertEqual(store.schema_version(), 2)

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
        self.assertIn("clodex_task_start", names)
        self.assertIn("clodex_task_get", names)
        self.assertIn("clodex_task_cancel", names)

    def test_mcp_tools_list_includes_handoff_tools(self):
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
        self.assertIn("clodex_handoff_create", names)
        self.assertIn("clodex_handoff_update", names)
        self.assertIn("clodex_handoff_get", names)
        self.assertIn("clodex_handoff_decide", names)

    def test_mcp_tasks_get_unknown_run(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "missing"}}
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
        self.assertEqual(response["error"]["code"], -32004)

    def test_mcp_handoff_create_update_get_and_decide(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_create",
                            "arguments": {"run_id": "run-mcp", "task": "native task", "owner": "claude", "handoff_budget": 2},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-mcp",
                                "phase": "implementation",
                                "actor": "claude",
                                "increment_handoff": True,
                                "report": {"summary": "plan accepted"},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_get", "arguments": {"run_id": "run-mcp"}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_decide", "arguments": {"run_id": "run-mcp"}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_get", "arguments": {"run_id": "run-mcp"}},
                    },
                ]
            ) + "\n"
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=payload,
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertFalse(responses[0]["result"]["isError"])
        created = json.loads(responses[0]["result"]["content"][0]["text"])
        self.assertEqual(created["id"], "run-mcp")
        self.assertEqual(created["handoff_budget"], 2)

        self.assertFalse(responses[1]["result"]["isError"])
        updated = json.loads(responses[1]["result"]["content"][0]["text"])
        self.assertEqual(updated["phase"], "implementation")
        self.assertEqual(updated["handoff_count"], 1)

        get_data = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertEqual(get_data["run"]["id"], "run-mcp")
        self.assertEqual(get_data["run"]["phase"], "implementation")
        self.assertEqual(get_data["budget_remaining"], 1)
        self.assertEqual(get_data["next_expected_actor"], "codex")

        self.assertFalse(responses[3]["result"]["isError"])
        decision = json.loads(responses[3]["result"]["content"][0]["text"])
        self.assertEqual(decision["decision"], "needs_fix")
        self.assertEqual(decision["phase"], "implementation")
        self.assertEqual(decision["budget_remaining"], 1)
        self.assertEqual(decision["next_expected_actor"], "codex")

        final_data = json.loads(responses[4]["result"]["content"][0]["text"])
        self.assertTrue(any(event["event"] == "handoff.decide" for event in final_data["events"]))

    def test_mcp_handoff_decide_approves_matching_claude_and_codex_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_create",
                            "arguments": {"run_id": "run-approve", "task": "native task", "handoff_budget": 4},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-approve",
                                "phase": "audit",
                                "actor": "claude",
                                "diff_hash": "abc123",
                                "report": {"approved": True, "summary": "matches plan"},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-approve",
                                "phase": "audit",
                                "actor": "codex",
                                "diff_hash": "abc123",
                                "report": {"approved": True, "summary": "implementation sound"},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_decide", "arguments": {"run_id": "run-approve"}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_get", "arguments": {"run_id": "run-approve"}},
                    },
                ]
            ) + "\n"
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=payload,
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        for response in responses[:4]:
            self.assertFalse(response["result"]["isError"])
        decision = json.loads(responses[3]["result"]["content"][0]["text"])
        self.assertEqual(decision["decision"], "approved")
        self.assertEqual(decision["diff_hash"], "abc123")
        self.assertEqual(decision["approved_by"], ["claude", "codex"])

        data = json.loads(responses[4]["result"]["content"][0]["text"])
        self.assertEqual(data["run"]["status"], "approved")
        self.assertEqual(data["run"]["phase"], "decision")
        self.assertEqual(data["run"]["diff_hash"], "abc123")
        self.assertTrue(any(event["event"] == "handoff.approved" for event in data["events"]))
        self.assertTrue(any(event["event"] == "handoff.decide" for event in data["events"]))

    def test_mcp_handoff_decide_requires_approvals_for_latest_diff_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_create",
                            "arguments": {"run_id": "run-stale-approval", "task": "native task", "handoff_budget": 6},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-stale-approval",
                                "actor": "claude",
                                "diff_hash": "abc123",
                                "report": {"approved": True},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-stale-approval",
                                "actor": "codex",
                                "diff_hash": "abc123",
                                "report": {"approved": True},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-stale-approval",
                                "actor": "codex",
                                "diff_hash": "def456",
                                "report": {"approved": False, "summary": "new diff needs review"},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_decide", "arguments": {"run_id": "run-stale-approval"}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 6,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-stale-approval",
                                "actor": "claude",
                                "diff_hash": "def456",
                                "report": {"approved": True},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-stale-approval",
                                "actor": "codex",
                                "diff_hash": "def456",
                                "report": {"approved": True},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 8,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_decide", "arguments": {"run_id": "run-stale-approval"}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_get", "arguments": {"run_id": "run-stale-approval"}},
                    },
                ]
            ) + "\n"
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=payload,
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        first_decision = json.loads(responses[4]["result"]["content"][0]["text"])
        self.assertFalse(responses[4]["result"]["isError"])
        self.assertEqual(first_decision["decision"], "needs_fix")

        final_decision = json.loads(responses[7]["result"]["content"][0]["text"])
        self.assertFalse(responses[7]["result"]["isError"])
        self.assertEqual(final_decision["decision"], "approved")
        self.assertEqual(final_decision["diff_hash"], "def456")

        data = json.loads(responses[8]["result"]["content"][0]["text"])
        self.assertEqual(data["run"]["status"], "approved")
        self.assertEqual(data["run"]["diff_hash"], "def456")

    def test_mcp_handoff_decide_treats_hashless_rejection_as_withdrawal(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_create",
                            "arguments": {"run_id": "run-withdraw", "task": "native task", "handoff_budget": 6},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-withdraw",
                                "actor": "claude",
                                "diff_hash": "abc123",
                                "report": {"approved": True},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-withdraw",
                                "actor": "codex",
                                "diff_hash": "abc123",
                                "report": {"approved": True},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-withdraw",
                                "actor": "codex",
                                "report": {"approved": False, "summary": "withdrawing approval"},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_decide", "arguments": {"run_id": "run-withdraw"}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 6,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-withdraw",
                                "actor": "codex",
                                "diff_hash": "abc123",
                                "report": {"approved": True},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_decide", "arguments": {"run_id": "run-withdraw"}},
                    },
                ]
            ) + "\n"
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=payload,
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        withdrawn = json.loads(responses[4]["result"]["content"][0]["text"])
        self.assertFalse(responses[4]["result"]["isError"])
        self.assertEqual(withdrawn["decision"], "needs_fix")

        approved = json.loads(responses[6]["result"]["content"][0]["text"])
        self.assertFalse(responses[6]["result"]["isError"])
        self.assertEqual(approved["decision"], "approved")
        self.assertEqual(approved["diff_hash"], "abc123")

    def test_mcp_handoff_budget_exhaustion_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_create",
                            "arguments": {"run_id": "run-budget", "task": "native task", "handoff_budget": 0},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {"run_id": "run-budget", "actor": "claude", "increment_handoff": True},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_decide", "arguments": {"run_id": "run-budget"}},
                    },
                ]
            ) + "\n"
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=payload,
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        created = json.loads(responses[0]["result"]["content"][0]["text"])
        self.assertEqual(created["handoff_budget"], 0)

        self.assertTrue(responses[1]["result"]["isError"])
        update_data = json.loads(responses[1]["result"]["content"][0]["text"])
        self.assertEqual(update_data["status"], "blocked")
        self.assertEqual(update_data["handoff_count"], 1)
        self.assertEqual(update_data["blocked_reason"], "handoff budget exhausted")

        self.assertTrue(responses[2]["result"]["isError"])
        decision = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertEqual(decision["decision"], "blocked")
        self.assertEqual(decision["blocked_reason"], "handoff budget exhausted")

    def test_mcp_handoff_update_rejects_direct_approved_without_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_create",
                            "arguments": {"run_id": "run-invalid", "task": "native task"},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {"run_id": "run-invalid", "status": "approved", "actor": "claude"},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_get", "arguments": {"run_id": "run-invalid"}},
                    },
                ]
            ) + "\n"
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=payload,
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertFalse(responses[0]["result"]["isError"])
        self.assertTrue(responses[1]["result"]["isError"])
        self.assertIn("handoff status cannot be set", responses[1]["result"]["content"][0]["text"])
        data = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertEqual(data["run"]["status"], "handoff")
        self.assertEqual(data["run"]["handoff_count"], 0)
        self.assertIsNone(data["run"]["last_actor"])

    def test_mcp_handoff_create_invalid_budget_returns_tool_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = {
                "jsonrpc": "2.0",
                "id": 41,
                "method": "tools/call",
                "params": {
                    "name": "clodex_handoff_create",
                    "arguments": {"run_id": "run-invalid-budget", "task": "native task", "handoff_budget": "not-an-int"},
                },
            }
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=json.dumps(request) + "\n",
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(response["id"], 41)
        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(response["result"]["content"][0]["text"], "handoff_budget must be an integer")

    def test_mcp_handoff_create_negative_budget_returns_tool_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = {
                "jsonrpc": "2.0",
                "id": 42,
                "method": "tools/call",
                "params": {
                    "name": "clodex_handoff_create",
                    "arguments": {"run_id": "run-negative-budget", "task": "native task", "handoff_budget": -1},
                },
            }
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=json.dumps(request) + "\n",
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(response["id"], 42)
        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])
        self.assertIn("handoff_budget must be non-negative", response["result"]["content"][0]["text"])

    def test_mcp_handoff_create_rejects_bool_and_float_budgets(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "jsonrpc": "2.0",
                        "id": 49,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_create",
                            "arguments": {"run_id": "run-bool-budget", "task": "native task", "handoff_budget": True},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 50,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_create",
                            "arguments": {"run_id": "run-float-budget", "task": "native task", "handoff_budget": 1.5},
                        },
                    },
                ]
            ) + "\n"
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=payload,
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual({response["id"] for response in responses}, {49, 50})
        for response in responses:
            self.assertNotIn("error", response)
            self.assertTrue(response["result"]["isError"])
            self.assertEqual(response["result"]["content"][0]["text"], "handoff_budget must be an integer")

    def test_mcp_handoff_create_duplicate_run_id_returns_tool_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            create = {
                "jsonrpc": "2.0",
                "id": 43,
                "method": "tools/call",
                "params": {
                    "name": "clodex_handoff_create",
                    "arguments": {"run_id": "run-duplicate", "task": "native task"},
                },
            }
            duplicate = {
                "jsonrpc": "2.0",
                "id": 44,
                "method": "tools/call",
                "params": {
                    "name": "clodex_handoff_create",
                    "arguments": {"run_id": "run-duplicate", "task": "native task"},
                },
            }
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=json.dumps(create) + "\n" + json.dumps(duplicate) + "\n",
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(responses[0]["id"], 43)
        self.assertFalse(responses[0]["result"]["isError"])
        self.assertEqual(responses[1]["id"], 44)
        self.assertNotIn("error", responses[1])
        self.assertTrue(responses[1]["result"]["isError"])
        self.assertIn("UNIQUE constraint failed", responses[1]["result"]["content"][0]["text"])

    def test_mcp_handoff_missing_required_arguments_return_tool_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "jsonrpc": "2.0",
                        "id": 45,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_create", "arguments": {"run_id": "run-missing-task"}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 46,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_update", "arguments": {"actor": "claude"}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 47,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_get", "arguments": {}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 48,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_decide", "arguments": {}},
                    },
                ]
            ) + "\n"
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=payload,
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        expected = {
            45: "Missing required argument: task",
            46: "Missing required argument: run_id",
            47: "Missing required argument: run_id",
            48: "Missing required argument: run_id",
        }
        self.assertEqual({response["id"] for response in responses}, set(expected))
        for response in responses:
            self.assertNotIn("error", response)
            self.assertTrue(response["result"]["isError"])
            self.assertEqual(response["result"]["content"][0]["text"], expected[response["id"]])

    def test_task_start_get_cancel_lifecycle(self):
        with TempRepo() as repo, FakeCliPath(sleep_seconds=1):
            start = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "task", "start", "--workspace", "local", "slow fixture"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(start.returncode, 0, start.stdout + start.stderr)
            run_id = json.loads(start.stdout)["run_id"]
            cancel = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "task", "cancel", run_id],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(cancel.returncode, 0, cancel.stdout + cancel.stderr)
            for _ in range(20):
                status = subprocess.run(
                    [sys.executable, "-m", "clodex", "--json", "task", "get", run_id],
                    cwd=repo,
                    env={**os.environ, "PYTHONPATH": str(ROOT)},
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
                data = json.loads(status.stdout)
                if data["run"]["status"] in {"cancelled", "approved", "blocked"}:
                    break
                time.sleep(0.1)
            self.assertIn(data["run"]["status"], {"cancel_requested", "cancelled"})

    def test_hooks_print_and_ingest(self):
        with TempRepo() as repo:
            printed = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "hooks", "print"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(printed.returncode, 0, printed.stderr)
            config = json.loads(printed.stdout)
            self.assertIn("hooks", config)
            ingested = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "hooks", "ingest", "--run-id", "run-hooks"],
                cwd=repo,
                input=json.dumps({"event": "SessionStart"}),
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(ingested.returncode, 0, ingested.stdout + ingested.stderr)

    def test_trace_export_and_eval_run(self):
        with TempRepo() as repo, FakeCliPath():
            result = ClodexWorkflow(repo).build("implement fixture", workspace_backend="local")
            exported = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "trace", "export", result.run_id, "--format", "jsonl"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(exported.returncode, 0, exported.stderr)
            self.assertIn("run.start", exported.stdout)
            evaluated = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "eval", "run"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(evaluated.returncode, 0, evaluated.stdout + evaluated.stderr)

    def test_npm_package_exposes_bins_and_files(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["name"], "clodex")
        self.assertEqual(package["bin"]["clodex"], "npm/clodex.js")
        self.assertEqual(package["bin"]["clodex-mcp-server"], "npm/clodex-mcp-server.js")
        self.assertIn("clodex/*.py", package["files"])
        self.assertIn("npm/*.js", package["files"])

    def test_npm_bridge_invokes_python_cli(self):
        with mock.patch("runpy.run_module") as run_module, mock.patch.object(sys, "argv", ["bridge", "--json", "doctor"]):
            with self.assertRaises(SystemExit) as exit_context:
                npm_bridge_main()
        self.assertEqual(exit_context.exception.code, 0)
        run_module.assert_called_once_with("clodex", run_name="__main__", alter_sys=True)

    def test_node_launcher_dry_run_executes_python_module(self):
        result = subprocess.run(
            ["node", str(ROOT / "npm" / "clodex.js"), "--json", "build", "--dry-run", "npm smoke"],
            cwd=ROOT,
            env={**os.environ, "CLODEX_PYTHON": sys.executable},
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["status"], "dry-run")
        self.assertEqual(data["data"]["task"], "npm smoke")

    def test_replace_managed_block_preserves_unmanaged_content(self):
        existing = "header\n<!-- BEGIN CLODEX -->\nold\n<!-- END CLODEX -->\nfooter\n"
        updated, changed = replace_managed_block(existing, "new\n")
        self.assertTrue(changed)
        self.assertEqual(updated, "header\n<!-- BEGIN CLODEX -->\nnew\n<!-- END CLODEX -->\nfooter\n")

    def test_replace_managed_block_appends_when_missing(self):
        updated, changed = replace_managed_block("header\n", "new\n")
        self.assertTrue(changed)
        self.assertEqual(updated, "header\n\n<!-- BEGIN CLODEX -->\nnew\n<!-- END CLODEX -->\n")

    def test_replace_managed_block_is_idempotent(self):
        existing = f"{BEGIN_MARKER}\nnew\n{END_MARKER}\n"
        updated, changed = replace_managed_block(existing, "new\n")
        self.assertFalse(changed)
        self.assertEqual(updated, existing)

    def test_replace_managed_block_rejects_malformed_block_without_force(self):
        with self.assertRaises(ManagedBlockError):
            replace_managed_block("header\n<!-- BEGIN CLODEX -->\nmissing end\n", "new\n")

    def test_replace_managed_block_force_replaces_malformed_tail(self):
        updated, changed = replace_managed_block("header\n<!-- BEGIN CLODEX -->\nmissing end\n", "new\n", force=True)
        self.assertTrue(changed)
        self.assertEqual(updated, "header\n<!-- BEGIN CLODEX -->\nnew\n<!-- END CLODEX -->\n")

    def test_replace_managed_block_preserves_crlf_style(self):
        existing = "header\r\n<!-- BEGIN CLODEX -->\r\nold\r\n<!-- END CLODEX -->\r\nfooter\r\n"
        updated, changed = replace_managed_block(existing, "new\n")
        self.assertTrue(changed)
        self.assertEqual(updated, "header\r\n<!-- BEGIN CLODEX -->\r\nnew\r\n<!-- END CLODEX -->\r\nfooter\r\n")

    def test_replace_toml_managed_block_appends_when_missing(self):
        updated, changed = replace_toml_managed_block("header\n", "new\n")
        self.assertTrue(changed)
        self.assertEqual(updated, "header\n\n# BEGIN CLODEX\nnew\n# END CLODEX\n")

    def test_replace_toml_managed_block_is_idempotent(self):
        existing = f"{TOML_BEGIN_MARKER}\nnew\n{TOML_END_MARKER}\n"
        updated, changed = replace_toml_managed_block(existing, "new\n")
        self.assertFalse(changed)
        self.assertEqual(updated, existing)

    def test_replace_toml_managed_block_rejects_malformed_block_without_force(self):
        with self.assertRaises(ManagedBlockError):
            replace_toml_managed_block("header\n# BEGIN CLODEX\nmissing end\n", "new\n")

    def test_replace_toml_managed_block_force_replaces_malformed_tail(self):
        updated, changed = replace_toml_managed_block("header\n# BEGIN CLODEX\nmissing end\n", "new\n", force=True)
        self.assertTrue(changed)
        self.assertEqual(updated, "header\n# BEGIN CLODEX\nnew\n# END CLODEX\n")

    def test_replace_toml_managed_block_preserves_crlf_style(self):
        existing = "header\r\n# BEGIN CLODEX\r\nold\r\n# END CLODEX\r\nfooter\r\n"
        updated, changed = replace_toml_managed_block(existing, "new\n")
        self.assertTrue(changed)
        self.assertEqual(updated, "header\r\n# BEGIN CLODEX\r\nnew\r\n# END CLODEX\r\nfooter\r\n")

    def test_native_instruction_templates_include_mcp_and_cli_fallbacks(self):
        claude = build_claude_block()
        agents = build_agents_block()
        self.assertIn("clodex_handoff_create", claude)
        self.assertIn("clodex_handoff_decide", claude)
        self.assertIn("clodex task start", claude)
        self.assertIn("Codex is the default engineer", claude)
        self.assertIn("clodex_handoff_update", agents)
        self.assertIn("Claude Code is the default strategist", agents)
        self.assertIn("handoff budget", agents)

    def test_native_plan_dry_run_previews_instruction_and_mcp_files(self):
        from clodex.native import plan_native_install

        with TempRepo() as repo:
            plan = plan_native_install(repo, dry_run=True)
        paths = {Path(item["path"]).name for item in plan["files"]}
        self.assertIn("CLAUDE.md", paths)
        self.assertIn("AGENTS.md", paths)
        self.assertIn("CLODEX.md", paths)
        self.assertIn(".mcp.json", paths)
        self.assertIn("config.toml", paths)
        self.assertTrue(plan["dry_run"])
        self.assertTrue(any("clodex_handoff_create" in item["preview"] for item in plan["files"]))

    def test_native_plan_no_mcp_config_skips_mcp_files(self):
        from clodex.native import plan_native_install

        with TempRepo() as repo:
            plan = plan_native_install(repo, dry_run=True, no_mcp_config=True)
        paths = {str(Path(item["path"]).as_posix()) for item in plan["files"]}
        self.assertFalse(any(path.endswith(".mcp.json") for path in paths))
        self.assertFalse(any(path.endswith(".codex/config.toml") for path in paths))

    def test_native_plan_reports_invalid_utf8_target(self):
        from clodex.native import plan_native_install

        with TempRepo() as repo:
            (repo / "CLAUDE.md").write_bytes(b"\xff\xfe\xff")
            plan = plan_native_install(repo, no_mcp_config=True)
        item = next(item for item in plan["files"] if Path(item["path"]).name == "CLAUDE.md")
        self.assertEqual(item["status"], "invalid")
        self.assertEqual(item["action"], "error")
        self.assertIn("utf-8", item["error"].lower())

    def test_native_plan_reports_directory_target_as_invalid(self):
        from clodex.native import plan_native_install

        with TempRepo() as repo:
            (repo / "CLAUDE.md").mkdir()
            plan = plan_native_install(repo, no_mcp_config=True)
        item = next(item for item in plan["files"] if Path(item["path"]).name == "CLAUDE.md")
        self.assertEqual(item["status"], "invalid")
        self.assertEqual(item["action"], "error")
        self.assertEqual(item["preview"], "")
        self.assertIn("CLAUDE.md", item["error"])

    def test_native_plan_reports_invalid_unrelated_toml_config(self):
        from clodex.native import plan_native_install

        with TempRepo() as repo:
            config = repo / ".codex" / "config.toml"
            config.parent.mkdir()
            config.write_text(
                "[mcp_servers.other]\n"
                'command = "one"\n'
                "\n"
                "[mcp_servers.other]\n"
                'command = "two"\n',
                encoding="utf-8",
            )
            plan = plan_native_install(repo)
        item = next(item for item in plan["files"] if item["path"].endswith(".codex\\config.toml") or item["path"].endswith(".codex/config.toml"))
        self.assertEqual(item["status"], "invalid")
        self.assertEqual(item["action"], "error")
        self.assertIn("Invalid .codex/config.toml", item["error"])

    def test_apply_native_install_rejects_invalid_target_without_partial_writes(self):
        from clodex.native import apply_native_install

        with TempRepo() as repo:
            (repo / "CLAUDE.md").write_bytes(b"\xff\xfe\xff")
            clodex_before = (repo / "CLODEX.md").read_bytes()
            with self.assertRaisesRegex(ManagedBlockError, "CLAUDE.md"):
                apply_native_install(repo, no_mcp_config=True)
            self.assertFalse((repo / "AGENTS.md").exists())
            self.assertEqual((repo / "CLAUDE.md").read_bytes(), b"\xff\xfe\xff")
            self.assertEqual((repo / "CLODEX.md").read_bytes(), clodex_before)

    def test_apply_native_install_rejects_directory_target_without_partial_writes(self):
        from clodex.native import apply_native_install

        with TempRepo() as repo:
            (repo / "CLAUDE.md").mkdir()
            clodex_before = (repo / "CLODEX.md").read_bytes()
            with self.assertRaisesRegex(ManagedBlockError, "CLAUDE.md"):
                apply_native_install(repo, no_mcp_config=True)
            self.assertTrue((repo / "CLAUDE.md").is_dir())
            self.assertFalse((repo / "AGENTS.md").exists())
            self.assertEqual((repo / "CLODEX.md").read_bytes(), clodex_before)

    def test_apply_native_install_rejects_parent_file_conflict_without_partial_writes(self):
        from clodex.native import apply_native_install

        with TempRepo() as repo:
            clodex_before = (repo / "CLODEX.md").read_bytes()
            (repo / ".codex").write_text("not a directory\n", encoding="utf-8")
            with self.assertRaisesRegex(ManagedBlockError, ".codex"):
                apply_native_install(repo)
            self.assertFalse((repo / "CLAUDE.md").exists())
            self.assertFalse((repo / "AGENTS.md").exists())
            self.assertFalse((repo / ".mcp.json").exists())
            self.assertTrue((repo / ".codex").is_file())
            self.assertEqual((repo / "CLODEX.md").read_bytes(), clodex_before)

    def test_apply_native_install_preserves_crlf_codex_config(self):
        from clodex.native import apply_native_install

        with TempRepo() as repo:
            config = repo / ".codex" / "config.toml"
            config.parent.mkdir()
            config.write_bytes(b'model = "gpt-5.5"\r\n')
            apply_native_install(repo)
            content = config.read_bytes()
        self.assertIn(b"# BEGIN CLODEX\r\n", content)
        self.assertIn(b"[mcp_servers.clodex]\r\n", content)
        self.assertIn(b'args = ["mcp-server"]\r\n', content)

    def test_apply_native_install_force_adopts_crlf_unmanaged_codex_config(self):
        from clodex.native import apply_native_install

        with TempRepo() as repo:
            config = repo / ".codex" / "config.toml"
            config.parent.mkdir()
            config.write_bytes(
                b'model = "gpt-5.5"\r\n'
                b"\r\n"
                b"[mcp_servers.clodex]\r\n"
                b'command = "old-clodex"\r\n'
                b'args = ["old"]\r\n'
                b"\r\n"
                b"[mcp_servers.other]\r\n"
                b'command = "other"\r\n'
            )
            apply_native_install(repo, force=True)
            content = config.read_bytes()
        self.assertEqual(content.count(b"[mcp_servers.clodex]"), 1)
        self.assertIn(b"# BEGIN CLODEX\r\n", content)
        self.assertIn(b'command = "clodex"\r\n', content)
        self.assertIn(b"[mcp_servers.other]\r\n", content)
        self.assertIn(b'command = "other"\r\n', content)

    def test_render_mcp_json_preserves_existing_servers(self):
        from clodex.native import render_mcp_json

        rendered = render_mcp_json('{"mcpServers":{"existing":{"command":"node","args":["server.js"]}}}')
        data = json.loads(rendered)
        self.assertEqual(data["mcpServers"]["existing"]["command"], "node")
        self.assertEqual(data["mcpServers"]["clodex"]["command"], "clodex")
        self.assertEqual(data["mcpServers"]["clodex"]["args"], ["mcp-server"])

    def test_render_mcp_json_rejects_invalid_json_without_force(self):
        from clodex.native import render_mcp_json

        with self.assertRaises(ManagedBlockError):
            render_mcp_json("{not-json")

    def test_render_mcp_json_rejects_non_object_servers_without_force(self):
        from clodex.native import render_mcp_json

        with self.assertRaises(ManagedBlockError):
            render_mcp_json('{"mcpServers":[]}')

    def test_render_mcp_json_rejects_null_servers_without_force(self):
        from clodex.native import render_mcp_json

        with self.assertRaises(ManagedBlockError):
            render_mcp_json('{"mcpServers":null}')

    def test_render_mcp_json_force_replaces_invalid_json(self):
        from clodex.native import render_mcp_json

        data = json.loads(render_mcp_json("{not-json", force=True))
        self.assertEqual(data["mcpServers"]["clodex"]["command"], "clodex")

    def test_render_mcp_json_force_replaces_non_object_servers(self):
        from clodex.native import render_mcp_json

        data = json.loads(render_mcp_json('{"mcpServers":[]}', force=True))
        self.assertEqual(data["mcpServers"]["clodex"]["command"], "clodex")

    def test_render_mcp_json_preserves_crlf_style(self):
        from clodex.native import render_mcp_json

        rendered = render_mcp_json('{\r\n  "mcpServers": {}\r\n}\r\n')
        self.assertIn("\r\n", rendered)
        self.assertNotIn("\n", rendered.replace("\r\n", ""))
        self.assertTrue(rendered.endswith("\r\n"))

    def test_render_codex_toml_preserves_existing_config(self):
        from clodex.native import render_codex_toml

        rendered = render_codex_toml('model = "gpt-5.5"\n')
        self.assertIn('model = "gpt-5.5"', rendered)
        self.assertIn("[mcp_servers.clodex]", rendered)
        self.assertIn('command = "clodex"', rendered)
        self.assertIn('args = ["mcp-server"]', rendered)

    def test_render_codex_toml_rejects_unmanaged_clodex_table_without_force(self):
        from clodex.native import render_codex_toml

        existing = 'model = "gpt-5.5"\n\n[mcp_servers.clodex]\ncommand = "old-clodex"\n'
        with self.assertRaisesRegex(ManagedBlockError, "mcp_servers.clodex"):
            render_codex_toml(existing)

    def test_render_codex_toml_rejects_quoted_unmanaged_clodex_tables_without_force(self):
        from clodex.native import render_codex_toml

        for header in ('[mcp_servers."clodex"]', '["mcp_servers".clodex]', '["mcp_servers"."clodex"]'):
            with self.subTest(header=header):
                existing = f'model = "gpt-5.5"\n\n{header}\ncommand = "old-clodex"\n'
                with self.assertRaisesRegex(ManagedBlockError, "mcp_servers.clodex"):
                    render_codex_toml(existing)

    def test_render_codex_toml_rejects_escaped_unmanaged_clodex_table_without_force(self):
        from clodex.native import render_codex_toml

        existing = '["mcp_servers"."clo\\u0064ex"]\ncommand = "old"\n'
        with self.assertRaisesRegex(ManagedBlockError, "mcp_servers.clodex"):
            render_codex_toml(existing)

    def test_render_codex_toml_rejects_non_header_clodex_shapes_with_and_without_force(self):
        from clodex.native import render_codex_toml

        for existing in (
            'mcp_servers.clodex.command = "old"\n',
            '[mcp_servers]\nclodex = { command = "old" }\n',
            'mcp_servers = { clodex = { command = "old" } }\n',
        ):
            with self.subTest(existing=existing):
                with self.assertRaisesRegex(ManagedBlockError, "mcp_servers.clodex"):
                    render_codex_toml(existing)
                with self.assertRaisesRegex(ManagedBlockError, "mcp_servers.clodex"):
                    render_codex_toml(existing, force=True)

    def test_render_codex_toml_rejects_descendant_unmanaged_clodex_table_without_force(self):
        from clodex.native import render_codex_toml

        existing = '[mcp_servers.clodex.env]\nFOO = "old"\n'
        with self.assertRaisesRegex(ManagedBlockError, "mcp_servers.clodex"):
            render_codex_toml(existing)

    def test_render_codex_toml_ignores_table_text_inside_multiline_basic_string(self):
        from clodex.native import render_codex_toml

        existing = 'notes = """\n[mcp_servers.clodex]\ncommand = "text only"\n"""\n'
        rendered = render_codex_toml(existing)
        data = tomllib.loads(rendered)
        self.assertEqual(data["notes"], '[mcp_servers.clodex]\ncommand = "text only"\n')
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
        self.assertIn('[mcp_servers.clodex]\ncommand = "text only"\n', rendered)

    def test_render_codex_toml_force_ignores_table_text_inside_multiline_basic_string(self):
        from clodex.native import render_codex_toml

        existing = 'notes = """\n[mcp_servers.clodex]\ncommand = "text only"\n"""\n'
        rendered = render_codex_toml(existing, force=True)
        data = tomllib.loads(rendered)
        self.assertEqual(data["notes"], '[mcp_servers.clodex]\ncommand = "text only"\n')
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
        self.assertIn('[mcp_servers.clodex]\ncommand = "text only"\n', rendered)

    def test_render_codex_toml_ignores_table_text_inside_multiline_literal_string(self):
        from clodex.native import render_codex_toml

        existing = "notes = '''\n[mcp_servers.clodex]\ncommand = \"text only\"\n'''\n"
        rendered = render_codex_toml(existing)
        data = tomllib.loads(rendered)
        self.assertEqual(data["notes"], '[mcp_servers.clodex]\ncommand = "text only"\n')
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
        self.assertIn('[mcp_servers.clodex]\ncommand = "text only"\n', rendered)

    def test_render_codex_toml_preserves_multiline_basic_string_managed_markers(self):
        from clodex.native import render_codex_toml

        existing = 'notes = """\n# BEGIN CLODEX\nnot managed\n# END CLODEX\n"""\n'
        rendered = render_codex_toml(existing)
        data = tomllib.loads(rendered)
        self.assertEqual(data["notes"], "# BEGIN CLODEX\nnot managed\n# END CLODEX\n")
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
        self.assertEqual(rendered.count("# BEGIN CLODEX"), 2)

    def test_render_codex_toml_force_preserves_multiline_basic_string_managed_markers(self):
        from clodex.native import render_codex_toml

        existing = 'notes = """\n# BEGIN CLODEX\nnot managed\n# END CLODEX\n"""\n'
        rendered = render_codex_toml(existing, force=True)
        data = tomllib.loads(rendered)
        self.assertEqual(data["notes"], "# BEGIN CLODEX\nnot managed\n# END CLODEX\n")
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
        self.assertEqual(rendered.count("# BEGIN CLODEX"), 2)

    def test_render_codex_toml_preserves_multiline_literal_string_managed_markers(self):
        from clodex.native import render_codex_toml

        existing = "notes = '''\n# BEGIN CLODEX\nnot managed\n# END CLODEX\n'''\n"
        rendered = render_codex_toml(existing)
        data = tomllib.loads(rendered)
        self.assertEqual(data["notes"], "# BEGIN CLODEX\nnot managed\n# END CLODEX\n")
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
        self.assertEqual(rendered.count("# BEGIN CLODEX"), 2)

    def test_render_codex_toml_force_preserves_one_sided_marker_inside_multiline_string(self):
        from clodex.native import render_codex_toml

        existing = 'model = "gpt-5.5"\nnotes = """\n# BEGIN CLODEX\nnot managed\n"""\n'
        rendered = render_codex_toml(existing, force=True)
        data = tomllib.loads(rendered)
        self.assertEqual(data["model"], "gpt-5.5")
        self.assertEqual(data["notes"], "# BEGIN CLODEX\nnot managed\n")
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")

    def test_render_codex_toml_real_malformed_managed_block_outside_string_rejects_and_force_repairs(self):
        from clodex.native import render_codex_toml

        existing = 'model = "gpt-5.5"\n# BEGIN CLODEX\nold = "value"\n'
        with self.assertRaises(ManagedBlockError):
            render_codex_toml(existing)
        rendered = render_codex_toml(existing, force=True)
        data = tomllib.loads(rendered)
        self.assertEqual(data["model"], "gpt-5.5")
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
        self.assertNotIn('old = "value"', rendered)

    def test_render_codex_toml_force_adopts_unmanaged_clodex_table(self):
        from clodex.native import render_codex_toml

        existing = (
            'model = "gpt-5.5"\n'
            "\n"
            "[mcp_servers.clodex]\n"
            'command = "old-clodex"\n'
            'args = ["old"]\n'
            "\n"
            "[mcp_servers.other]\n"
            'command = "other"\n'
        )
        rendered = render_codex_toml(existing, force=True)
        self.assertEqual(rendered.count("[mcp_servers.clodex]"), 1)
        self.assertIn('model = "gpt-5.5"', rendered)
        self.assertIn("[mcp_servers.other]", rendered)
        self.assertIn('command = "other"', rendered)
        self.assertEqual(tomllib.loads(rendered)["mcp_servers"]["clodex"]["command"], "clodex")

    def test_render_codex_toml_force_adopts_descendant_unmanaged_clodex_tables(self):
        from clodex.native import render_codex_toml

        existing = (
            'model = "gpt-5.5"\n'
            "\n"
            "[mcp_servers.clodex]\n"
            'command = "old-clodex"\n'
            "\n"
            "[mcp_servers.clodex.env]\n"
            'FOO = "old"\n'
            "\n"
            "[mcp_servers.clodex.env.nested]\n"
            'BAR = "old"\n'
            "\n"
            "[mcp_servers.other]\n"
            'command = "other"\n'
        )
        rendered = render_codex_toml(existing, force=True)
        data = tomllib.loads(rendered)
        self.assertEqual(rendered.count("[mcp_servers.clodex]"), 1)
        self.assertNotIn("[mcp_servers.clodex.env]", rendered)
        self.assertNotIn("[mcp_servers.clodex.env.nested]", rendered)
        self.assertIn('model = "gpt-5.5"', rendered)
        self.assertIn("[mcp_servers.other]", rendered)
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
        self.assertNotIn("env", data["mcp_servers"]["clodex"])
        self.assertEqual(data["mcp_servers"]["other"]["command"], "other")

    def test_render_codex_toml_force_adopts_quoted_unmanaged_clodex_tables(self):
        from clodex.native import render_codex_toml

        for header in ('[mcp_servers."clodex"]', '["mcp_servers".clodex]', '["mcp_servers"."clodex"]'):
            with self.subTest(header=header):
                existing = (
                    'model = "gpt-5.5"\r\n'
                    "\r\n"
                    f"{header}\r\n"
                    'command = "old-clodex"\r\n'
                    'args = ["old"]\r\n'
                    "\r\n"
                    "[mcp_servers.other]\r\n"
                    'command = "other"\r\n'
                )
                rendered = render_codex_toml(existing, force=True)
                data = tomllib.loads(rendered)
                self.assertEqual(rendered.count("[mcp_servers.clodex]"), 1)
                self.assertNotIn(header, rendered)
                self.assertIn('model = "gpt-5.5"', rendered)
                self.assertIn("[mcp_servers.other]", rendered)
                self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
                self.assertEqual(data["mcp_servers"]["other"]["command"], "other")
                self.assertIn("# BEGIN CLODEX\r\n", rendered)

    def test_render_codex_toml_force_adopts_escaped_unmanaged_clodex_table(self):
        from clodex.native import render_codex_toml

        existing = (
            'model = "gpt-5.5"\r\n'
            "\r\n"
            '["mcp_servers"."clo\\u0064ex"]\r\n'
            'command = "old"\r\n'
            "\r\n"
            "[mcp_servers.other]\r\n"
            'command = "other"\r\n'
        )
        rendered = render_codex_toml(existing, force=True)
        data = tomllib.loads(rendered)
        self.assertEqual(rendered.count("[mcp_servers.clodex]"), 1)
        self.assertNotIn('["mcp_servers"."clo\\u0064ex"]', rendered)
        self.assertIn('model = "gpt-5.5"', rendered)
        self.assertIn("[mcp_servers.other]", rendered)
        self.assertEqual(data["mcp_servers"]["clodex"]["command"], "clodex")
        self.assertEqual(data["mcp_servers"]["other"]["command"], "other")
        self.assertIn("# BEGIN CLODEX\r\n", rendered)

    def test_render_codex_toml_rejects_invalid_unrelated_toml_without_force(self):
        from clodex.native import render_codex_toml

        existing = (
            "[mcp_servers.other]\n"
            'command = "one"\n'
            "\n"
            "[mcp_servers.other]\n"
            'command = "two"\n'
        )
        with self.assertRaisesRegex(ManagedBlockError, "Invalid .codex/config.toml"):
            render_codex_toml(existing)

    def test_render_codex_toml_rejects_invalid_unrelated_toml_with_force(self):
        from clodex.native import render_codex_toml

        existing = (
            "[mcp_servers.other]\n"
            'command = "one"\n'
            "\n"
            "[mcp_servers.other]\n"
            'command = "two"\n'
        )
        with self.assertRaisesRegex(ManagedBlockError, "Invalid .codex/config.toml"):
            render_codex_toml(existing, force=True)


if __name__ == "__main__":
    unittest.main()
