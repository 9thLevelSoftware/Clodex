from __future__ import annotations

import json
import os
import runpy
import shutil
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
    def __init__(self, reject_once: bool = False, malformed_once: bool = False, sleep_seconds: float = 0):
        self.reject_once = reject_once
        self.malformed_once = malformed_once
        self.sleep_seconds = sleep_seconds

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
