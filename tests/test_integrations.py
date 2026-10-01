"""Tests: how Clodex plugs into Claude Code and Codex (launcher, hooks, native init, manifests, installers)."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from clodex import native
from clodex.config import resolve_repo_root
from clodex.hooks import (
    DEFAULT_RUN_ID,
    HOOK_EVENTS,
    derive_run_id,
    hook_config,
    hook_handler,
    ingest_hook_event,
    install_hooks,
    is_clodex_handler,
    merge_hooks,
    parse_hook_payload,
    safe_run_id,
    settings_path,
)
from clodex.launcher import clodex_argv, mcp_server_entry
from tests.support import ROOT, TempRepo

# Event names as spelled in the Claude Code hooks reference (verified against the docs).
DOCUMENTED_EVENTS = {
    "SessionStart", "Setup", "UserPromptSubmit", "UserPromptExpansion", "PreToolUse", "PermissionRequest",
    "PermissionDenied", "PostToolUse", "PostToolUseFailure", "PostToolBatch", "Stop", "StopFailure",
    "SubagentStart", "SubagentStop", "TaskCreated", "TaskCompleted", "TeammateIdle", "PreCompact",
    "PostCompact", "PreModelSwitch", "PostModelSwitch", "Notification", "MessageDisplay",
    "InstructionsLoaded", "ConfigChange", "CwdChanged", "DirectoryAdded", "FileChanged",
    "WorktreeCreate", "WorktreeRemove", "Elicitation", "ElicitationResult", "SessionEnd",
}


@contextlib.contextmanager
def env(**changes):
    """Set (or with None, remove) environment variables for the duration."""
    saved = {key: os.environ.get(key) for key in changes}
    try:
        for key, value in changes.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_command(argv: list[str], cwd: Path, stdin: str = "", extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a command the way a host program would: no PYTHONPATH from us, stdin piped."""
    clean = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    return subprocess.run(argv, cwd=cwd, input=stdin, env={**clean, **(extra_env or {})}, capture_output=True, text=True, encoding="utf-8")


class LauncherTests(unittest.TestCase):
    def test_command_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = Path(tmp) / "clodex.js"
            launcher.write_text("//", encoding="utf-8")
            npm = clodex_argv("hooks", "ingest", npm_launcher=str(launcher))
            self.assertEqual(npm[1:], [str(launcher), "hooks", "ingest"])
        self.assertEqual(clodex_argv("x", npm_launcher="", python="py", installed=True), ["py", "-m", "clodex", "x"])
        source = clodex_argv("x", npm_launcher="", python="py", installed=False)
        self.assertEqual((source[0], source[1], source[3]), ("py", "-c", "x"))
        self.assertIn(repr(str(ROOT)), source[2])  # embedded as a Python literal

    def test_source_checkout_command_runs_without_pythonpath(self):
        argv = clodex_argv("--json", "status", npm_launcher="", installed=False)
        with TempRepo() as repo:
            result = run_command(argv, repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("runs", json.loads(result.stdout))

    def test_mcp_entry_portable_and_absolute(self):
        self.assertEqual(mcp_server_entry(portable=True, windows=False, which=lambda _: None), {"command": "clodex", "args": ["mcp-server"]})
        self.assertEqual(mcp_server_entry(portable=True, windows=False, which=lambda _: "/usr/bin/clodex"), {"command": "clodex", "args": ["mcp-server"]})
        cmd = mcp_server_entry(portable=True, windows=True, which=lambda _: "C:\\npm\\clodex.CMD")
        self.assertEqual(cmd, {"command": "cmd", "args": ["/c", "clodex", "mcp-server"]})
        self.assertEqual(mcp_server_entry(portable=True, windows=True, which=lambda _: "C:\\py\\clodex.exe")["command"], "clodex")
        self.assertEqual(mcp_server_entry(portable=True, windows=True, which=lambda _: None)["command"], "clodex")
        absolute = mcp_server_entry(portable=False)
        self.assertTrue(Path(absolute["command"]).is_absolute() or absolute["command"] == "node")
        self.assertEqual(absolute["args"][-1], "mcp-server")


class RepoRootTests(unittest.TestCase):
    def test_precedence_clodex_then_claude_project_dir_then_cwd(self):
        with TempRepo() as one, TempRepo() as two, TempRepo() as three:
            with env(CLODEX_REPO_ROOT=str(one), CLAUDE_PROJECT_DIR=str(two)):
                self.assertEqual(resolve_repo_root(), one.resolve())
            with env(CLODEX_REPO_ROOT=None, CLAUDE_PROJECT_DIR=str(two)):
                self.assertEqual(resolve_repo_root(), two.resolve())
            with env(CLODEX_REPO_ROOT=None, CLAUDE_PROJECT_DIR=str(three / "nope")), mock.patch("clodex.config.Path.cwd", return_value=one):
                self.assertEqual(resolve_repo_root(), one.resolve(), "a path that is not a directory is ignored")

    def test_claude_project_dir_finds_the_repo_even_from_the_claude_config_dir(self):
        # User-scope MCP servers start with ~/.claude as their cwd; CLAUDE_PROJECT_DIR points at the project.
        with TempRepo() as repo, tempfile.TemporaryDirectory() as claude_home:
            sub = repo / "pkg"
            sub.mkdir()
            result = run_command([sys.executable, "-m", "clodex", "--json", "status"], Path(claude_home), extra_env={"PYTHONPATH": str(ROOT), "CLAUDE_PROJECT_DIR": str(sub), "CLODEX_REPO_ROOT": ""})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((repo / ".clodex" / "state.sqlite3").is_file())


class HookConfigTests(unittest.TestCase):
    def test_shape_and_event_names_match_the_claude_code_reference(self):
        config = hook_config()
        self.assertEqual(list(config), ["hooks"], "must be pasteable into a settings file as-is")
        self.assertTrue(set(HOOK_EVENTS) <= DOCUMENTED_EVENTS, sorted(set(HOOK_EVENTS) - DOCUMENTED_EVENTS))
        for forbidden in ("WorktreeCreated", "WorktreeRemoved", "PreToolUse", "PostToolUse", "FileChanged"):
            self.assertNotIn(forbidden, config["hooks"])
        for event, groups in config["hooks"].items():
            self.assertEqual(len(groups), 1)
            (handler,) = groups[0]["hooks"]
            self.assertEqual(handler["type"], "command")
            self.assertTrue(handler["command"] and isinstance(handler["args"], list))
            self.assertEqual(handler["args"][-2:], ["hooks", "ingest"])
            self.assertIsInstance(handler["timeout"], int)
            self.assertTrue(is_clodex_handler(handler))

    def test_merge_is_idempotent_preserves_other_hooks_and_replaces_stale_entries(self):
        user = {
            "theme": "dark",
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "notify-send", "args": ["done"]}]}],
                "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "lint"}]}],
            },
        }
        once = merge_hooks(user)
        self.assertEqual(once["theme"], "dark")
        self.assertEqual(once["hooks"]["PreToolUse"], user["hooks"]["PreToolUse"])
        stop = once["hooks"]["Stop"]
        self.assertEqual(len(stop), 2)
        self.assertEqual(stop[0], user["hooks"]["Stop"][0])
        self.assertEqual(merge_hooks(once), once, "idempotent")
        stale = json.loads(json.dumps(once))
        stale["hooks"]["Stop"][1]["hooks"][0]["command"] = "/old/python"
        refreshed = merge_hooks(stale)
        self.assertEqual(len(refreshed["hooks"]["Stop"]), 2)
        self.assertEqual(refreshed["hooks"]["Stop"][1]["hooks"][0]["command"], hook_handler()["command"])

    def test_remove_strips_only_clodex_and_drops_emptied_events(self):
        user = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}}
        removed = merge_hooks(merge_hooks(user), remove=True)
        self.assertEqual(removed, user)
        self.assertEqual(merge_hooks(merge_hooks({}), remove=True), {})

    def test_malformed_settings_are_rejected(self):
        with self.assertRaises(ValueError):
            merge_hooks({"hooks": []})
        with self.assertRaises(ValueError):
            merge_hooks({"hooks": {"Stop": {}}})


class InstallHooksTests(unittest.TestCase):
    def test_scopes_map_to_the_documented_settings_files(self):
        with TempRepo() as repo, tempfile.TemporaryDirectory() as home, env(CLAUDE_CONFIG_DIR=None):
            with mock.patch.object(Path, "home", return_value=Path(home)):
                self.assertEqual(settings_path(repo, "local"), repo / ".claude" / "settings.local.json")
                self.assertEqual(settings_path(repo, "project"), repo / ".claude" / "settings.json")
                self.assertEqual(settings_path(repo, "user"), Path(home) / ".claude" / "settings.json")
            with env(CLAUDE_CONFIG_DIR=home):
                self.assertEqual(settings_path(repo, "user"), Path(home) / "settings.json")
            with self.assertRaises(ValueError):
                settings_path(repo, "global")

    def test_install_dry_run_create_update_unchanged_and_uninstall(self):
        with TempRepo() as repo:
            path = repo / ".claude" / "settings.local.json"
            dry = install_hooks(repo, dry_run=True)
            self.assertEqual((dry["action"], path.exists()), ("create", False))
            created = install_hooks(repo)
            self.assertEqual(created["action"], "create")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(sorted(data["hooks"]), sorted(HOOK_EVENTS))
            self.assertEqual(install_hooks(repo)["action"], "unchanged")
            path.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}), encoding="utf-8")
            self.assertEqual(install_hooks(repo)["action"], "update")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["permissions"], {"allow": ["Bash(ls)"]})
            self.assertEqual(install_hooks(repo, remove=True)["action"], "remove")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"permissions": {"allow": ["Bash(ls)"]}})
            self.assertEqual(install_hooks(repo, remove=True)["action"], "unchanged")

    def test_invalid_json_is_refused_unless_forced_and_crlf_is_kept(self):
        with TempRepo() as repo:
            path = repo / ".claude" / "settings.local.json"
            path.parent.mkdir()
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "--force"):
                install_hooks(repo)
            self.assertEqual(path.read_text(encoding="utf-8"), "{not json")
            install_hooks(repo, force=True)
            self.assertIn("hooks", json.loads(path.read_text(encoding="utf-8")))
            path.write_bytes(b'{\r\n  "theme": "dark"\r\n}\r\n')
            install_hooks(repo)
            raw = path.read_bytes()
            self.assertIn(b"\r\n", raw)
            self.assertEqual(raw.replace(b"\r\n", b"").count(b"\n"), 0, "no bare LF mixed into a CRLF file")

    def test_project_scope_warns_about_machine_specific_paths(self):
        with TempRepo() as repo:
            self.assertIn("machine-specific", install_hooks(repo, "project", dry_run=True)["note"])
            self.assertNotIn("note", install_hooks(repo, "local", dry_run=True))


class IngestSafetyTests(unittest.TestCase):
    def test_run_ids(self):
        for good in ("run-1", "0f8fad5b-d9cb-469f-a165-70867728950e", "a.b_c-d", "x" * 128):
            self.assertEqual(safe_run_id(good), good)
        for bad in ("", ".", "..", "../x", "a/b", "a\\b", "-leading", "x" * 129, "a..b", "a b", None, 5):
            with self.assertRaises(ValueError, msg=repr(bad)):
                safe_run_id(bad)

    def test_derivation_order_and_untrusted_fallback(self):
        self.assertEqual(derive_run_id("explicit", {"session_id": "s"}, {"CLODEX_RUN_ID": "e"}), "explicit")
        self.assertEqual(derive_run_id(None, {"session_id": "s"}, {"CLODEX_RUN_ID": "e"}), "e")
        self.assertEqual(derive_run_id(None, {"session_id": "s1"}, {}), "s1")
        self.assertEqual(derive_run_id(None, {"session_id": "../../etc"}, {}), DEFAULT_RUN_ID, "a hostile payload must not fail or escape")
        self.assertEqual(derive_run_id(None, {}, {}), DEFAULT_RUN_ID)
        with self.assertRaises(ValueError):
            derive_run_id("../x", {}, {})

    def test_ingest_refuses_traversal_and_writes_under_runs_root(self):
        with TempRepo() as repo:
            with self.assertRaises(ValueError):
                ingest_hook_event(repo, "../escape", {"a": 1})
            self.assertFalse((repo / ".clodex" / "escape").exists())
            result = ingest_hook_event(repo, "run-ok", {"hook_event_name": "Stop"})
            written = Path(result["event_file"])
            self.assertEqual(written.parent.parent, (repo / ".clodex" / "runs" / "run-ok").resolve())
            self.assertEqual(json.loads(written.read_text(encoding="utf-8").strip())["hook_event_name"], "Stop")

    def test_payload_parsing_keeps_anything_it_cannot_parse(self):
        self.assertEqual(parse_hook_payload(""), {})
        self.assertEqual(parse_hook_payload('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_hook_payload("not json")["raw"], "not json")
        self.assertEqual(parse_hook_payload("[1, 2]"), {"value": [1, 2]})
        self.assertEqual(len(parse_hook_payload("x" * 5000)["raw"]), 2000)


class HookCommandTests(unittest.TestCase):
    """The commands Claude Code will actually run."""

    def run_ingest(self, repo: Path, stdin: str, *extra: str) -> subprocess.CompletedProcess:
        return run_command([sys.executable, "-m", "clodex", "hooks", "ingest", *extra], repo, stdin, extra_env={"PYTHONPATH": str(ROOT)})

    def test_success_is_silent_and_uses_the_session_id(self):
        with TempRepo() as repo:
            result = self.run_ingest(repo, json.dumps({"session_id": "sess-1", "hook_event_name": "SessionStart"}))
            self.assertEqual((result.returncode, result.stdout), (0, ""), "stdout can reach Claude's context")
            event_file = repo / ".clodex" / "runs" / "sess-1" / "events" / "claude-hooks.jsonl"
            self.assertTrue(event_file.is_file())

    def test_failures_exit_1_never_2_because_2_blocks_the_hooked_action(self):
        with TempRepo() as repo:
            bad_id = self.run_ingest(repo, "{}", "--run-id", "../x")
            self.assertEqual(bad_id.returncode, 1)
            self.assertIn("Invalid run id", bad_id.stderr)
            self.assertEqual(bad_id.stdout, "")
            garbage = self.run_ingest(repo, "}{ not json")
            self.assertEqual((garbage.returncode, garbage.stdout), (0, ""))
            recorded = (repo / ".clodex" / "runs" / DEFAULT_RUN_ID / "events" / "claude-hooks.jsonl").read_text(encoding="utf-8")
            self.assertIn("not json", recorded)

    def test_verbose_prints_the_result(self):
        with TempRepo() as repo:
            result = self.run_ingest(repo, "{}", "--verbose", "--run-id", "v1")
            self.assertEqual(result.returncode, 0)
            self.assertIn("claude-hooks.jsonl", result.stdout)

    def test_configured_hook_command_works_from_a_bare_environment(self):
        handler = hook_handler()
        with TempRepo() as repo:
            result = run_command([handler["command"], *handler["args"]], repo, json.dumps({"session_id": "bare-env", "hook_event_name": "Stop"}))
            self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
            self.assertTrue((repo / ".clodex" / "runs" / "bare-env" / "events" / "claude-hooks.jsonl").is_file())

    def test_install_and_print_through_the_cli(self):
        with TempRepo() as repo:
            printed = json.loads(run_command([sys.executable, "-m", "clodex", "--json", "hooks", "print"], repo, extra_env={"PYTHONPATH": str(ROOT)}).stdout)
            self.assertEqual(list(printed), ["hooks"])
            result = run_command([sys.executable, "-m", "clodex", "--json", "hooks", "install"], repo, extra_env={"PYTHONPATH": str(ROOT)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["action"], "create")
            self.assertTrue((repo / ".claude" / "settings.local.json").is_file())
            removed = run_command([sys.executable, "-m", "clodex", "--json", "hooks", "uninstall"], repo, extra_env={"PYTHONPATH": str(ROOT)})
            self.assertEqual(json.loads(removed.stdout)["action"], "remove")


class NativeInitTests(unittest.TestCase):
    def setUp(self):
        self.home_cm = tempfile.TemporaryDirectory()
        self.home = Path(self.home_cm.name)
        self.addCleanup(self.home_cm.cleanup)
        patcher = mock.patch.object(Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        env_ctx = env(CLAUDE_CONFIG_DIR=None, CODEX_HOME=None)
        env_ctx.__enter__()
        self.addCleanup(env_ctx.__exit__, None, None, None)

    def paths(self, **kwargs) -> set[Path]:
        return {path for path, _kind, _body in native.native_targets(self.home, global_mode=True, **kwargs)}

    def test_global_mode_writes_where_the_agents_actually_look(self):
        self.assertEqual(
            self.paths(),
            {self.home / ".claude" / "CLAUDE.md", self.home / ".codex" / "AGENTS.md", self.home / ".codex" / "config.toml"},
        )
        self.assertEqual(self.paths(no_mcp_config=True), {self.home / ".claude" / "CLAUDE.md", self.home / ".codex" / "AGENTS.md"})
        for stale in ("CLAUDE.md", "AGENTS.md", "CLODEX.md", ".mcp.json"):
            self.assertNotIn(self.home / stale, self.paths())

    def test_global_mode_honors_config_dir_overrides(self):
        with env(CLAUDE_CONFIG_DIR=str(self.home / "cc"), CODEX_HOME=str(self.home / "cx")):
            self.assertEqual(self.paths(), {self.home / "cc" / "CLAUDE.md", self.home / "cx" / "AGENTS.md", self.home / "cx" / "config.toml"})

    def test_repo_mode_keeps_the_project_files(self):
        with TempRepo() as repo:
            targets = {path.relative_to(repo).as_posix() for path, _k, _b in native.native_targets(repo)}
            self.assertEqual(targets, {"CLAUDE.md", "AGENTS.md", "CLODEX.md", ".mcp.json", ".codex/config.toml"})

    def test_shared_repo_files_are_portable_and_user_level_files_are_absolute(self):
        with TempRepo() as repo:
            plan = native.plan_native_install(repo, dry_run=True)
            toml = next(item for item in plan["files"] if item["path"].endswith("config.toml"))["preview"]
            self.assertEqual(tomllib.loads(toml)["mcp_servers"]["clodex"], {"command": "clodex", "args": ["mcp-server"]})
            mcp = json.loads(next(item for item in plan["files"] if item["path"].endswith(".mcp.json"))["preview"])
            self.assertEqual(mcp["mcpServers"]["clodex"], {"command": "clodex", "args": ["mcp-server"]})
        glob = native.plan_native_install(self.home, dry_run=True, global_mode=True)
        server = tomllib.loads(next(item for item in glob["files"] if item["path"].endswith("config.toml"))["preview"])["mcp_servers"]["clodex"]
        self.assertNotEqual(server["command"], "clodex")
        self.assertEqual(server["args"][-1], "mcp-server")

    def test_toml_block_escapes_windows_paths_and_validates(self):
        entry = {"command": "C:\\Program Files\\nodejs\\node.exe", "args": ["C:\\clodex\\npm\\clodex.js", "mcp-server"]}
        rendered = native.render_codex_toml('model = "x"\n', entry=entry)
        self.assertEqual(tomllib.loads(rendered)["mcp_servers"]["clodex"], entry)
        self.assertEqual(tomllib.loads(rendered)["model"], "x")

    def test_plan_lists_the_claude_registration_command_only_in_global_mode(self):
        with TempRepo() as repo:
            self.assertEqual(native.plan_native_install(repo, dry_run=True)["commands"], [])
        plan = native.plan_native_install(self.home, dry_run=True, global_mode=True)
        self.assertEqual(len(plan["commands"]), 2)
        self.assertIn("claude mcp remove clodex", plan["commands"][0])
        self.assertIn("claude mcp add-json clodex", plan["commands"][1])
        self.assertEqual(native.plan_native_install(self.home, dry_run=True, global_mode=True, no_mcp_config=True)["commands"], [])

    def test_registration_removes_then_adds_and_degrades_without_the_cli(self):
        calls: list[list[str]] = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        results = native.register_claude_user_mcp(runner=runner, which=lambda name: "/bin/claude")
        self.assertEqual([c[:4] for c in calls], [["claude", "mcp", "remove", "clodex"], ["claude", "mcp", "add-json", "clodex"]])
        self.assertTrue(all(call[-2:] == ["--scope", "user"] for call in calls))
        payload = json.loads(calls[1][4])
        self.assertEqual(payload["type"], "stdio")
        self.assertEqual(payload["args"][-1], "mcp-server")
        self.assertEqual([r["returncode"] for r in results], [0, 0])
        skipped = native.register_claude_user_mcp(runner=runner, which=lambda name: None)
        self.assertTrue(all("skipped" in r for r in skipped))

    def test_apply_global_writes_files_and_registers(self):
        with mock.patch.object(native, "register_claude_user_mcp", return_value=[{"command": "claude ...", "returncode": 0}]) as register:
            result = native.apply_native_install(self.home, global_mode=True)
        register.assert_called_once()
        self.assertEqual(result["commands_run"], [{"command": "claude ...", "returncode": 0}])
        self.assertTrue((self.home / ".claude" / "CLAUDE.md").is_file())
        self.assertTrue((self.home / ".codex" / "AGENTS.md").is_file())
        self.assertTrue((self.home / ".codex" / "config.toml").is_file())
        self.assertFalse((self.home / "CLAUDE.md").exists())

    def test_user_scope_status_reads_the_claude_json(self):
        self.assertEqual(native.claude_user_mcp_status()["registered"], False)
        expected = native.clodex_mcp_server_entry(portable=False)
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"clodex": {"type": "stdio", **expected}}}), encoding="utf-8")
        status = native.claude_user_mcp_status()
        self.assertEqual((status["registered"], status["current"]), (True, True))
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"clodex": {"command": "old"}}}), encoding="utf-8")
        status = native.claude_user_mcp_status()
        self.assertEqual((status["registered"], status["current"]), (True, False))
        (self.home / ".claude.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(native.claude_user_mcp_status()["registered"], False)

    def test_dead_helpers_are_gone(self):
        self.assertFalse(hasattr(native, "normalize_block_body"))
        self.assertFalse(hasattr(native, "_is_toml_table_header"))

    @unittest.skipUnless(shutil.which("claude"), "needs the real claude CLI")
    def test_real_claude_cli_registration_is_idempotent_and_updates_in_place(self):
        # Isolated: CLAUDE_CONFIG_DIR keeps this away from the developer's real ~/.claude.json.
        with env(CLAUDE_CONFIG_DIR=str(self.home)):
            first = native.register_claude_user_mcp()
            self.assertEqual([r["returncode"] for r in first][1], 0, first)
            self.assertEqual(native.claude_user_mcp_status()["current"], True)
            self.assertEqual(native.register_claude_user_mcp()[1]["returncode"], 0, "second run must succeed too")
            changed = {"command": "node", "args": ["different.js"]}
            with mock.patch.object(native, "clodex_mcp_server_entry", return_value=changed):
                native.register_claude_user_mcp()
                servers = json.loads((self.home / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]
            self.assertEqual(servers["clodex"]["args"], ["different.js"], "add-json alone would have kept the old value")


class ManifestTests(unittest.TestCase):
    def test_claude_plugin_manifest_follows_the_documented_schema(self):
        manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        documented = {"$schema", "name", "displayName", "version", "description", "author", "homepage", "repository", "license", "keywords", "metadata", "defaultEnabled", "dependencies", "settings", "userConfig", "channels", "skills", "commands", "agents", "hooks", "mcpServers", "lspServers", "outputStyles", "workflows", "experimental"}
        self.assertTrue(set(manifest) <= documented, set(manifest) - documented)
        self.assertRegex(manifest["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
        for command_path in manifest["commands"]:
            self.assertTrue(command_path.startswith("./"), command_path)
            self.assertTrue((ROOT / command_path).exists(), command_path)
        self.assertNotIsInstance(manifest.get("skills"), dict, "skills takes paths, not an object map")
        self.assertTrue((ROOT / "skills" / "clodex-workflow" / "SKILL.md").is_file(), "the default skills/ directory is scanned")
        server = manifest["mcpServers"]["clodex"]
        script = server["args"][0].replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT))
        self.assertTrue(Path(script).is_file(), script)

    def test_codex_manifest_has_only_documented_keys(self):
        manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        for undocumented in ("mcp_servers", "skills"):
            self.assertNotIn(undocumented, manifest)
        self.assertEqual(manifest["name"], "clodex")

    def test_slash_commands_have_descriptions(self):
        for path in sorted((ROOT / "dist" / ".claude" / "commands" / "clodex").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\n"), path.name)
            self.assertIn("\ndescription: ", text.split("---", 2)[1], path.name)

    @unittest.skipUnless(shutil.which("claude"), "needs the real claude CLI")
    def test_real_claude_plugin_validate_passes_strictly(self):
        result = subprocess.run(["claude", "plugin", "validate", str(ROOT), "--strict"], capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Validation passed", result.stdout)


@unittest.skipIf(os.name == "nt", "the installers are bash scripts for macOS/Linux")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.target, self.bin = base / "target", base / "bin"
        fake = base / "fakebin"
        fake.mkdir()
        for name in ("claude", "codex"):
            (fake / name).write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
            (fake / name).chmod(0o755)
        self.env = {**os.environ, "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"}

    def run_script(self, name: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(ROOT / name), *args], env=self.env, capture_output=True, text=True, stdin=subprocess.DEVNULL, cwd=self.tmp.name)

    def tree(self) -> list[str]:
        plugin = self.target / "plugin"
        return sorted(str(p.relative_to(plugin)) for p in plugin.rglob("*"))

    def install(self, *extra: str) -> subprocess.CompletedProcess:
        return self.run_script("install.sh", "--target", str(self.target), "--bin", str(self.bin), *extra)

    def test_install_is_self_contained_idempotent_and_never_nests(self):
        first = self.install()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        before = self.tree()
        plugin = self.target / "plugin"
        for expected in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json", "dist/.claude/commands/clodex/clodex-plan.md", "skills/clodex-workflow/SKILL.md", "npm/clodex-mcp-server.js", "clodex/cli.py", "clodex/_vendor/yaml/__init__.py"):
            self.assertTrue((plugin / expected).is_file(), expected)
        self.assertFalse(any("__pycache__" in item for item in before))
        self.assertFalse((plugin / "dist" / "dist").exists())
        refused = self.install()
        self.assertEqual(refused.returncode, 1)
        self.assertIn("--force", refused.stdout)
        self.assertEqual(self.tree(), before, "a refusal must leave the install untouched")
        forced = self.install("--force")
        self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
        self.assertEqual(self.tree(), before, "re-installing replaces rather than nests")
        self.assertEqual([p.name for p in self.target.iterdir()], ["plugin"], "no staging directory left behind")

    def test_installed_launcher_runs_without_the_source_checkout(self):
        self.assertEqual(self.install().returncode, 0)
        launcher = (self.bin / "clodex").read_text(encoding="utf-8")
        self.assertIn(str(self.target / "plugin"), launcher)
        self.assertNotIn(str(ROOT), launcher)
        with TempRepo() as repo:
            result = subprocess.run([str(self.bin / "clodex"), "--json", "status"], cwd=repo, env={k: v for k, v in self.env.items() if k != "PYTHONPATH"}, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("runs", json.loads(result.stdout))

    def test_uninstall_removes_the_payload_and_legacy_layout(self):
        self.assertEqual(self.install().returncode, 0)
        for legacy in ("claude-plugin", "codex-plugin", "dist", "clodex-workflow-skill"):
            (self.target / legacy).mkdir()
        result = self.run_script("uninstall.sh", "--yes", "--target", str(self.target), "--bin", str(self.bin))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.target.exists(), "the emptied target directory is removed too")
        self.assertFalse((self.bin / "clodex").exists())


if __name__ == "__main__":
    unittest.main()
