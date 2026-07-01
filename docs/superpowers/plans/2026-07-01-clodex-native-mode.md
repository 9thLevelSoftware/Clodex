# Clodex Native Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Clodex installable with npm and usable as a native collaboration layer where Claude Code and Codex learn to hand work to each other through repo instructions and MCP, without the user manually driving `clodex build`.

**Architecture:** Add a focused `clodex/native.py` module for managed instruction blocks, repo-scoped MCP config generation, native status, and native doctor checks. Extend the existing SQLite `runs` table and MCP server instead of creating a parallel coordination system, then wire new `clodex init` and `clodex native ...` commands through the existing argparse CLI.

**Tech Stack:** Python 3.12 standard library, SQLite, argparse, line-delimited JSON-RPC MCP server, existing Clodex fake-CLI unittest harness, npm Node launchers already present in `npm/`.

---

## File Structure

- Create `clodex/native.py`
  - Owns managed block replacement, native instruction templates, repo/global target selection, dry-run planning, repo MCP config rendering, native status, and native doctor composition.
  - Does not invoke Claude or Codex directly.
- Modify `clodex/cli.py`
  - Adds `clodex init [--global] [--dry-run] [--no-mcp-config] [--force]`.
  - Adds `clodex native status` and `clodex native doctor`.
  - Delegates all native-specific work to `clodex.native`.
- Modify `clodex/state.py`
  - Adds migration-safe run columns: `owner`, `phase`, `handoff_count`, `handoff_budget`, `last_actor`, `blocked_reason`.
  - Adds small handoff-oriented methods over the existing `runs`, `run_events`, and `artifacts` tables.
- Modify `clodex/mcp_server.py`
  - Adds semantic MCP tools: `clodex_handoff_create`, `clodex_handoff_update`, `clodex_handoff_get`, `clodex_handoff_decide`.
  - Keeps existing lower-level tools unchanged.
- Modify `tests/test_clodex.py`
  - Adds native-mode tests beside existing CLI/MCP/state tests using `TempRepo`.
  - Keeps standard-library `unittest`; no new test framework.
- Modify `README.md`
  - Repositions Clodex as native Claude Code/Codex collaboration first.
  - Moves the harness command catalog lower as reference.
- Modify `package.json`
  - Updates npm description/keywords so npm install matches the native collaboration story.
- Optionally modify `skills/clodex-workflow/SKILL.md`
  - Update only if current wording still tells agents to manually run the old harness-first flow after README/package updates.

## Task 1: Managed Blocks and Native Instruction Templates

**Files:**
- Create: `clodex/native.py`
- Modify: `tests/test_clodex.py`

- [ ] **Step 1: Add imports for native helpers to the test file**

In `tests/test_clodex.py`, add this import block beside the existing Clodex imports:

```python
from clodex.native import (
    BEGIN_MARKER,
    END_MARKER,
    ManagedBlockError,
    build_agents_block,
    build_claude_block,
    replace_managed_block,
)
```

- [ ] **Step 2: Write failing managed-block tests**

Append these tests to `class ClodexTests(unittest.TestCase):` in `tests/test_clodex.py`:

```python
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
```

- [ ] **Step 3: Run the new tests and verify they fail for missing module**

Run:

```powershell
python -m unittest tests.test_clodex.ClodexTests.test_replace_managed_block_preserves_unmanaged_content tests.test_clodex.ClodexTests.test_native_instruction_templates_include_mcp_and_cli_fallbacks -v
```

Expected:

```text
ModuleNotFoundError: No module named 'clodex.native'
```

- [ ] **Step 4: Create `clodex/native.py` with managed block primitives and templates**

Create `clodex/native.py` with these definitions:

```python
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .doctor import run_doctor
from .state import StateStore

BEGIN_MARKER = "<!-- BEGIN CLODEX -->"
END_MARKER = "<!-- END CLODEX -->"
TOML_BEGIN_MARKER = "# BEGIN CLODEX"
TOML_END_MARKER = "# END CLODEX"
DEFAULT_HANDOFF_BUDGET = 6


class ManagedBlockError(ValueError):
    pass


@dataclass(frozen=True)
class NativeFilePlan:
    path: Path
    action: str
    current_status: str
    content: str
    preview: str


def normalize_block_body(body: str) -> str:
    return body if body.endswith("\n") else body + "\n"


def replace_managed_block(existing: str, body: str, *, force: bool = False) -> tuple[str, bool]:
    block_body = normalize_block_body(body)
    replacement = f"{BEGIN_MARKER}\n{block_body}{END_MARKER}\n"
    begin = existing.find(BEGIN_MARKER)
    end = existing.find(END_MARKER)
    if begin == -1 and end == -1:
        if existing and not existing.endswith("\n"):
            existing += "\n"
        separator = "" if not existing or existing.endswith("\n\n") else "\n"
        updated = existing + separator + replacement
        return updated, updated != existing
    if begin == -1 or end == -1 or end < begin:
        if not force:
            raise ManagedBlockError("Malformed Clodex managed block")
        prefix = existing[: begin if begin != -1 else end]
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        updated = prefix + replacement
        return updated, updated != existing
    end_after = end + len(END_MARKER)
    if end_after < len(existing) and existing[end_after : end_after + 1] == "\n":
        end_after += 1
    updated = existing[:begin] + replacement + existing[end_after:]
    return updated, updated != existing


def replace_toml_managed_block(existing: str, body: str, *, force: bool = False) -> tuple[str, bool]:
    block_body = normalize_block_body(body)
    replacement = f"{TOML_BEGIN_MARKER}\n{block_body}{TOML_END_MARKER}\n"
    begin = existing.find(TOML_BEGIN_MARKER)
    end = existing.find(TOML_END_MARKER)
    if begin == -1 and end == -1:
        if existing and not existing.endswith("\n"):
            existing += "\n"
        separator = "\n" if existing else ""
        updated = existing + separator + replacement
        return updated, updated != existing
    if begin == -1 or end == -1 or end < begin:
        if not force:
            raise ManagedBlockError("Malformed Clodex TOML managed block")
        prefix = existing[: begin if begin != -1 else end]
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        updated = prefix + replacement
        return updated, updated != existing
    end_after = end + len(TOML_END_MARKER)
    if end_after < len(existing) and existing[end_after : end_after + 1] == "\n":
        end_after += 1
    updated = existing[:begin] + replacement + existing[end_after:]
    return updated, updated != existing


def build_claude_block() -> str:
    return """# Clodex Native Collaboration

Claude Code is the default strategist and orchestrator for Clodex work.

When a user asks for non-trivial design, implementation, debugging, refactoring, or review work:

1. Prefer the MCP tool `clodex_handoff_create` to create or resume a durable handoff.
2. Produce the plan, design, acceptance criteria, risks, and test commands before implementation.
3. Delegate implementation-oriented work to Codex through Clodex instead of manually telling the user to run Codex.
4. Use `clodex_handoff_update` to record plan artifacts, phase changes, review notes, changed files, tests, unresolved issues, and diff hashes.
5. Use `clodex_handoff_decide` before claiming completion when Codex has implemented or audited changes.
6. Stop and summarize for the user when Clodex reports `blocked`.

Default role contract:

- Claude Code is the default strategist.
- Codex is the default engineer.
- Owner starts as `claude`.
- Handoff budget is 6.
- Phase order is `planning`, `implementation`, `audit`, `fix`, `decision`.

If MCP tools are unavailable, use these CLI fallbacks:

```bash
clodex task start "<task>"
clodex task get <run-id>
clodex task cancel <run-id>
clodex audit --diff
clodex status
```
"""


def build_agents_block() -> str:
    return """# Clodex Native Collaboration

Codex is the default engineer for Clodex work, while Claude Code is the default strategist.

When Clodex context is present:

1. Treat incoming Clodex handoffs as implementation, audit, or fix work according to the current phase.
2. Use `clodex_handoff_get` before acting on an existing run.
3. Implement from the accepted Claude plan and avoid expanding scope without recording the reason.
4. Use `clodex_handoff_update` to record changed files, tests run, unresolved issues, audit verdicts, and diff hash.
5. Ask for clarification through Clodex when product intent or acceptance criteria are unclear.
6. Respect owner, phase, and handoff budget state. When the budget is exhausted, summarize the disagreement for the user.

Prefer MCP tools. If MCP is unavailable, use `clodex task get <run-id>`, `clodex audit --diff`, and `clodex status` as CLI fallbacks.
"""
```

- [ ] **Step 5: Run managed-block and template tests**

Run:

```powershell
python -m unittest tests.test_clodex.ClodexTests.test_replace_managed_block_preserves_unmanaged_content tests.test_clodex.ClodexTests.test_replace_managed_block_appends_when_missing tests.test_clodex.ClodexTests.test_replace_managed_block_is_idempotent tests.test_clodex.ClodexTests.test_replace_managed_block_rejects_malformed_block_without_force tests.test_clodex.ClodexTests.test_replace_managed_block_force_replaces_malformed_tail tests.test_clodex.ClodexTests.test_native_instruction_templates_include_mcp_and_cli_fallbacks -v
```

Expected:

```text
OK
```

- [ ] **Step 6: Commit Task 1**

Run:

```powershell
git add clodex/native.py tests/test_clodex.py
git commit -m "feat: add native instruction block helpers"
```

Expected:

```text
[codex/npm-install ...] feat: add native instruction block helpers
```

## Task 2: Repo-Scoped Native Install Planning and MCP Config Rendering

**Files:**
- Modify: `clodex/native.py`
- Modify: `tests/test_clodex.py`

- [ ] **Step 1: Add failing tests for native install dry-run and config preservation**

Append these tests to `class ClodexTests(unittest.TestCase):`:

```python
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

    def test_render_mcp_json_force_replaces_invalid_json(self):
        from clodex.native import render_mcp_json

        data = json.loads(render_mcp_json("{not-json", force=True))
        self.assertEqual(data["mcpServers"]["clodex"]["command"], "clodex")

    def test_render_codex_toml_preserves_existing_config(self):
        from clodex.native import render_codex_toml

        rendered = render_codex_toml('model = "gpt-5.5"\n')
        self.assertIn('model = "gpt-5.5"', rendered)
        self.assertIn("[mcp_servers.clodex]", rendered)
        self.assertIn('command = "clodex"', rendered)
        self.assertIn('args = ["mcp-server"]', rendered)
```

- [ ] **Step 2: Run the new tests and verify missing functions fail**

Run:

```powershell
python -m unittest tests.test_clodex.ClodexTests.test_native_plan_dry_run_previews_instruction_and_mcp_files tests.test_clodex.ClodexTests.test_render_mcp_json_preserves_existing_servers tests.test_clodex.ClodexTests.test_render_codex_toml_preserves_existing_config -v
```

Expected:

```text
ImportError
```

- [ ] **Step 3: Add MCP config rendering and install planning functions**

Append these definitions to `clodex/native.py`:

```python
def clodex_mcp_server_entry() -> dict[str, Any]:
    return {"command": "clodex", "args": ["mcp-server"]}


def render_mcp_json(existing: str, *, force: bool = False) -> str:
    text = existing.strip()
    if not text:
        data: dict[str, Any] = {}
    else:
        try:
            data = json.loads(existing)
        except json.JSONDecodeError as exc:
            if not force:
                raise ManagedBlockError(f"Invalid .mcp.json: {exc}") from exc
            data = {}
    if not isinstance(data, dict):
        if not force:
            raise ManagedBlockError(".mcp.json root must be an object")
        data = {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    servers["clodex"] = clodex_mcp_server_entry()
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def codex_mcp_block() -> str:
    return """[mcp_servers.clodex]
command = "clodex"
args = ["mcp-server"]
"""


def render_codex_toml(existing: str, *, force: bool = False) -> str:
    return replace_toml_managed_block(existing, codex_mcp_block(), force=force)[0]


def repo_native_targets(repo_root: Path, *, no_mcp_config: bool = False) -> list[tuple[Path, str, str]]:
    targets = [
        (repo_root / "CLAUDE.md", "managed", build_claude_block()),
        (repo_root / "AGENTS.md", "managed", build_agents_block()),
        (repo_root / "CLODEX.md", "managed", build_clodex_policy_block()),
    ]
    if not no_mcp_config:
        targets.extend(
            [
                (repo_root / ".mcp.json", "json", ""),
                (repo_root / ".codex" / "config.toml", "toml", ""),
            ]
        )
    return targets


def build_clodex_policy_block() -> str:
    return """# Native Mode

Clodex native mode is enabled for this repository.

- Use MCP handoff tools as the primary coordination channel.
- Keep Claude Code as strategist by default and Codex as engineer by default.
- Require agreement before reporting non-trivial implementation work as complete.
- Use CLI fallbacks only when MCP is unavailable.
"""


def target_status(path: Path, desired: str) -> str:
    if not path.exists():
        return "missing"
    try:
        current = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "invalid"
    return "current" if current == desired else "stale"


def _planned_content(path: Path, kind: str, body: str, *, force: bool) -> str:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if kind == "managed":
        return replace_managed_block(existing, body, force=force)[0]
    if kind == "json":
        return render_mcp_json(existing, force=force)
    if kind == "toml":
        return render_codex_toml(existing, force=force)
    raise ValueError(f"unknown native target kind: {kind}")


def plan_native_install(
    repo_root: Path,
    *,
    dry_run: bool = False,
    global_mode: bool = False,
    no_mcp_config: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    target_root = Path.home() if global_mode else repo_root
    files: list[dict[str, Any]] = []
    for path, kind, body in repo_native_targets(target_root, no_mcp_config=no_mcp_config):
        content = _planned_content(path, kind, body, force=force)
        status = target_status(path, content)
        if status == "missing":
            action = "create"
        elif status == "current":
            action = "unchanged"
        else:
            action = "update"
        files.append(
            {
                "path": str(path),
                "action": action,
                "status": status,
                "preview": content,
            }
        )
    return {
        "mode": "global" if global_mode else "repo",
        "dry_run": dry_run,
        "root": str(target_root),
        "files": files,
    }


def apply_native_install(
    repo_root: Path,
    *,
    global_mode: bool = False,
    no_mcp_config: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    plan = plan_native_install(repo_root, dry_run=False, global_mode=global_mode, no_mcp_config=no_mcp_config, force=force)
    for item in plan["files"]:
        path = Path(item["path"])
        if item["action"] == "unchanged":
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item["preview"], encoding="utf-8", newline="\n")
    return plan
```

- [ ] **Step 4: Run native planning/config tests**

Run:

```powershell
python -m unittest tests.test_clodex.ClodexTests.test_native_plan_dry_run_previews_instruction_and_mcp_files tests.test_clodex.ClodexTests.test_native_plan_no_mcp_config_skips_mcp_files tests.test_clodex.ClodexTests.test_render_mcp_json_preserves_existing_servers tests.test_clodex.ClodexTests.test_render_mcp_json_rejects_invalid_json_without_force tests.test_clodex.ClodexTests.test_render_mcp_json_force_replaces_invalid_json tests.test_clodex.ClodexTests.test_render_codex_toml_preserves_existing_config -v
```

Expected:

```text
OK
```

- [ ] **Step 5: Commit Task 2**

Run:

```powershell
git add clodex/native.py tests/test_clodex.py
git commit -m "feat: plan native clodex initialization"
```

Expected:

```text
[codex/npm-install ...] feat: plan native clodex initialization
```

## Task 3: `clodex init`, `clodex native status`, and `clodex native doctor`

**Files:**
- Modify: `clodex/cli.py`
- Modify: `clodex/native.py`
- Modify: `tests/test_clodex.py`

- [ ] **Step 1: Add failing CLI tests for init/status/doctor**

Append these tests to `class ClodexTests(unittest.TestCase):`:

```python
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

    def test_cli_native_doctor_combines_doctor_and_native_status(self):
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
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "native", "doctor"],
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
            self.assertIn("npm_launcher", data)
```

- [ ] **Step 2: Run one CLI test and verify parser failure**

Run:

```powershell
python -m unittest tests.test_clodex.ClodexTests.test_cli_init_dry_run_does_not_write_files -v
```

Expected:

```text
AssertionError: 2 != 0
```

- [ ] **Step 3: Add native status and doctor helpers**

Append these definitions to `clodex/native.py`:

```python
def native_status(repo_root: Path, *, global_mode: bool = False, no_mcp_config: bool = False, force: bool = False) -> dict[str, Any]:
    target_root = Path.home() if global_mode else repo_root
    files: list[dict[str, Any]] = []
    for path, kind, body in repo_native_targets(target_root, no_mcp_config=no_mcp_config):
        try:
            content = _planned_content(path, kind, body, force=force)
            status = target_status(path, content)
            if status == "missing":
                action = "create"
            elif status == "current":
                action = "unchanged"
            else:
                action = "update"
            files.append({"path": str(path), "status": status, "action": action})
        except ManagedBlockError as exc:
            files.append({"path": str(path), "status": "invalid", "action": "error", "error": str(exc)})
    ok = all(item["status"] == "current" for item in files)
    return {"ok": ok, "mode": "global" if global_mode else "repo", "root": str(target_root), "files": files}


def native_doctor(repo_root: Path, *, global_mode: bool = False, no_mcp_config: bool = False, force: bool = False) -> tuple[int, dict[str, Any]]:
    native = native_status(repo_root, global_mode=global_mode, no_mcp_config=no_mcp_config, force=force)
    doctor_code, doctor = run_doctor()
    npm_launcher = shutil.which("clodex") or shutil.which("clodex.cmd") or shutil.which("clodex.ps1")
    data = {
        "ok": native["ok"] and doctor_code == 0,
        "native": native,
        "doctor": doctor,
        "npm_launcher": {"ok": npm_launcher is not None, "path": npm_launcher},
        "python": {"ok": True, "executable": sys.executable},
    }
    return (0 if data["ok"] else 1), data
```

- [ ] **Step 4: Wire CLI commands**

In `clodex/cli.py`, add imports:

```python
from .native import apply_native_install, native_doctor, native_status, plan_native_install
```

In `main`, after the existing `doctor` parser and before `plan`, add:

```python
    init = sub.add_parser("init", help="Install native Claude Code and Codex collaboration instructions")
    init.add_argument("--global", action="store_true", dest="global_mode")
    init.add_argument("--dry-run", action="store_true")
    init.add_argument("--no-mcp-config", action="store_true")
    init.add_argument("--force", action="store_true")

    native = sub.add_parser("native", help="Inspect native Clodex setup")
    native_sub = native.add_subparsers(dest="native_command", required=True)
    native_status_cmd = native_sub.add_parser("status")
    native_status_cmd.add_argument("--global", action="store_true", dest="global_mode")
    native_status_cmd.add_argument("--no-mcp-config", action="store_true")
    native_status_cmd.add_argument("--force", action="store_true")
    native_doctor_cmd = native_sub.add_parser("doctor")
    native_doctor_cmd.add_argument("--global", action="store_true", dest="global_mode")
    native_doctor_cmd.add_argument("--no-mcp-config", action="store_true")
    native_doctor_cmd.add_argument("--force", action="store_true")
```

In `main`, after the `doctor` command handling and before `workflow = ClodexWorkflow(Path.cwd())`, add:

```python
    if args.command == "init":
        if args.dry_run:
            data = plan_native_install(
                Path.cwd(),
                dry_run=True,
                global_mode=args.global_mode,
                no_mcp_config=args.no_mcp_config,
                force=args.force,
            )
        else:
            data = apply_native_install(
                Path.cwd(),
                global_mode=args.global_mode,
                no_mcp_config=args.no_mcp_config,
                force=args.force,
            )
        print_output(data, args.json)
        return 0
    if args.command == "native":
        if args.native_command == "status":
            data = native_status(
                Path.cwd(),
                global_mode=args.global_mode,
                no_mcp_config=args.no_mcp_config,
                force=args.force,
            )
            print_output(data, args.json)
            return 0 if data["ok"] else 1
        if args.native_command == "doctor":
            exit_code, data = native_doctor(
                Path.cwd(),
                global_mode=args.global_mode,
                no_mcp_config=args.no_mcp_config,
                force=args.force,
            )
            print_output(data, args.json)
            return exit_code
```

- [ ] **Step 5: Run CLI native tests**

Run:

```powershell
python -m unittest tests.test_clodex.ClodexTests.test_cli_init_dry_run_does_not_write_files tests.test_clodex.ClodexTests.test_cli_init_writes_native_files_and_preserves_user_content tests.test_clodex.ClodexTests.test_cli_init_no_mcp_config_writes_only_instruction_files tests.test_clodex.ClodexTests.test_cli_native_status_reports_current_and_missing_components tests.test_clodex.ClodexTests.test_cli_native_status_reports_invalid_malformed_block tests.test_clodex.ClodexTests.test_cli_native_doctor_combines_doctor_and_native_status -v
```

Expected:

```text
OK
```

- [ ] **Step 6: Commit Task 3**

Run:

```powershell
git add clodex/cli.py clodex/native.py tests/test_clodex.py
git commit -m "feat: add native clodex init commands"
```

Expected:

```text
[codex/npm-install ...] feat: add native clodex init commands
```

## Task 4: Handoff State, Ownership, and Budget Enforcement

**Files:**
- Modify: `clodex/state.py`
- Modify: `tests/test_clodex.py`

- [ ] **Step 1: Add failing state tests for native handoff fields and budget**

Append these tests to `class ClodexTests(unittest.TestCase):`:

```python
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

    def test_handoff_get_includes_latest_events_and_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            store.create_handoff("run-native", "native task", owner="claude", phase="planning", handoff_budget=2)
            store.add_artifact("run-native", "plan", "/tmp/plan.json", "json")
            store.update_handoff("run-native", phase="implementation", actor="claude", report={"summary": "ready"})
            data = store.get_handoff("run-native")
            self.assertEqual(data["run"]["id"], "run-native")
            self.assertEqual(data["run"]["phase"], "implementation")
            self.assertEqual(data["artifacts"][0]["name"], "plan")
            self.assertGreaterEqual(len(data["events"]), 1)
```

- [ ] **Step 2: Run handoff state tests and verify missing method failure**

Run:

```powershell
python -m unittest tests.test_clodex.ClodexTests.test_state_migrations_add_native_handoff_columns tests.test_clodex.ClodexTests.test_handoff_update_increments_budget_and_blocks_when_exhausted tests.test_clodex.ClodexTests.test_handoff_get_includes_latest_events_and_artifacts -v
```

Expected:

```text
AttributeError: 'StateStore' object has no attribute 'create_handoff'
```

- [ ] **Step 3: Extend run migrations**

In `clodex/state.py`, update `_ensure_run_columns` so the `columns` dictionary includes:

```python
            "owner": "text",
            "phase": "text",
            "handoff_count": "integer not null default 0",
            "handoff_budget": "integer not null default 6",
            "last_actor": "text",
            "blocked_reason": "text",
```

- [ ] **Step 4: Add handoff methods to `StateStore`**

Add these methods to `clodex/state.py` after `create_run`:

```python
    def create_handoff(
        self,
        run_id: str,
        prompt: str,
        *,
        owner: str = "claude",
        phase: str = "planning",
        handoff_budget: int = 6,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        task_id = task_id or run_id
        with self.session() as con:
            con.execute(
                """
                insert into runs(
                    id, task_id, status, prompt, owner, phase, handoff_count,
                    handoff_budget, created_at, updated_at, started_at
                )
                values (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (run_id, task_id, "handoff", prompt, owner, phase, handoff_budget, timestamp, timestamp, timestamp),
            )
        self.add_event(run_id, "handoff.create", {"owner": owner, "phase": phase, "handoff_budget": handoff_budget})
        run = self.get_run(run_id)
        if run is None:
            raise ValueError(f"failed to create handoff: {run_id}")
        return run

    def update_handoff(
        self,
        run_id: str,
        *,
        phase: str | None = None,
        actor: str | None = None,
        owner: str | None = None,
        report: dict[str, Any] | None = None,
        increment_handoff: bool = False,
        status: str | None = None,
        blocked_reason: str | None = None,
        diff_hash: str | None = None,
    ) -> dict[str, Any]:
        current = self.get_run(run_id)
        if current is None:
            raise ValueError(f"unknown run: {run_id}")
        next_count = int(current.get("handoff_count") or 0) + (1 if increment_handoff else 0)
        budget = int(current.get("handoff_budget") or 6)
        next_status = status or current.get("status") or "handoff"
        next_blocked_reason = blocked_reason
        if next_count > budget:
            next_status = "blocked"
            next_blocked_reason = "handoff budget exhausted"
        with self.session() as con:
            con.execute(
                """
                update runs set
                    status=?,
                    owner=coalesce(?, owner),
                    phase=coalesce(?, phase),
                    handoff_count=?,
                    last_actor=coalesce(?, last_actor),
                    blocked_reason=coalesce(?, blocked_reason),
                    diff_hash=coalesce(?, diff_hash),
                    updated_at=?,
                    completed_at=case when ? in ('approved','blocked','failed','cancelled','applied') then coalesce(completed_at, ?) else completed_at end
                where id=?
                """,
                (
                    next_status,
                    owner,
                    phase,
                    next_count,
                    actor,
                    next_blocked_reason,
                    diff_hash,
                    now_iso(),
                    next_status,
                    now_iso(),
                    run_id,
                ),
            )
        event = "handoff.blocked" if next_status == "blocked" else "handoff.update"
        self.add_event(
            run_id,
            event,
            {
                "phase": phase,
                "actor": actor,
                "owner": owner,
                "handoff_count": next_count,
                "handoff_budget": budget,
                "report": report or {},
                "status": next_status,
                "blocked_reason": next_blocked_reason,
            },
        )
        run = self.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run: {run_id}")
        return run
```

Add this method after `get_run`:

```python
    def get_handoff(self, run_id: str) -> dict[str, Any] | None:
        import json

        with self.session() as con:
            run = con.execute("select * from runs where id=?", (run_id,)).fetchone()
            if run is None:
                return None
            events = [
                {**dict(row), "data": json.loads(row["data_json"])}
                for row in con.execute("select * from run_events where run_id=? order by id", (run_id,))
            ]
            artifacts = [dict(row) for row in con.execute("select * from artifacts where run_id=? order by id", (run_id,))]
        run_dict = dict(run)
        remaining = int(run_dict.get("handoff_budget") or 6) - int(run_dict.get("handoff_count") or 0)
        return {
            "run": run_dict,
            "events": events,
            "artifacts": artifacts,
            "budget_remaining": max(0, remaining),
            "next_expected_actor": "codex" if run_dict.get("last_actor") == "claude" else "claude",
        }
```

- [ ] **Step 5: Run handoff state tests**

Run:

```powershell
python -m unittest tests.test_clodex.ClodexTests.test_state_migrations_add_native_handoff_columns tests.test_clodex.ClodexTests.test_handoff_update_increments_budget_and_blocks_when_exhausted tests.test_clodex.ClodexTests.test_handoff_get_includes_latest_events_and_artifacts -v
```

Expected:

```text
OK
```

- [ ] **Step 6: Run migration regression test**

Run:

```powershell
python -m unittest tests.test_clodex.ClodexTests.test_state_migrations_add_v2_tables -v
```

Expected:

```text
OK
```

- [ ] **Step 7: Commit Task 4**

Run:

```powershell
git add clodex/state.py tests/test_clodex.py
git commit -m "feat: add native handoff state"
```

Expected:

```text
[codex/npm-install ...] feat: add native handoff state
```

## Task 5: Semantic MCP Handoff Tools

**Files:**
- Modify: `clodex/mcp_server.py`
- Modify: `tests/test_clodex.py`

- [ ] **Step 1: Add failing MCP handoff tests**

Append these tests to `class ClodexTests(unittest.TestCase):`:

```python
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

    def test_mcp_handoff_create_update_get_and_decide(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "PYTHONPATH": str(ROOT)}
            create = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "clodex_handoff_create",
                    "arguments": {"run_id": "run-mcp", "task": "native task", "owner": "claude", "handoff_budget": 2},
                },
            }
            update = {
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
            }
            get = {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "clodex_handoff_get", "arguments": {"run_id": "run-mcp"}},
            }
            decide = {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "clodex_handoff_decide", "arguments": {"run_id": "run-mcp"}},
            }
            payload = "\n".join(json.dumps(item) for item in [create, update, get, decide]) + "\n"
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=payload,
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            responses = [json.loads(line) for line in result.stdout.splitlines()]
            get_text = responses[2]["result"]["content"][0]["text"]
            get_data = json.loads(get_text)
            self.assertEqual(get_data["run"]["id"], "run-mcp")
            self.assertEqual(get_data["run"]["phase"], "implementation")
            decide_text = responses[3]["result"]["content"][0]["text"]
            decision = json.loads(decide_text)
            self.assertIn(decision["decision"], {"needs_fix", "blocked"})

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
            response = json.loads(result.stdout.splitlines()[1])
            data = json.loads(response["result"]["content"][0]["text"])
            self.assertEqual(data["status"], "blocked")
            self.assertEqual(data["blocked_reason"], "handoff budget exhausted")
```

- [ ] **Step 2: Run MCP handoff list test and verify missing tool failure**

Run:

```powershell
python -m unittest tests.test_clodex.ClodexTests.test_mcp_tools_list_includes_handoff_tools -v
```

Expected:

```text
AssertionError: 'clodex_handoff_create' not found
```

- [ ] **Step 3: Add MCP tool schemas**

In `clodex/mcp_server.py`, append these dictionaries to the `TOOLS` list after `clodex_task_cancel`:

```python
    {
        "name": "clodex_handoff_create",
        "title": "Create Clodex handoff",
        "description": "Create a native Claude/Codex handoff run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "task": {"type": "string"},
                "owner": {"type": "string"},
                "phase": {"type": "string"},
                "handoff_budget": {"type": "integer"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "clodex_handoff_update",
        "title": "Update Clodex handoff",
        "description": "Record native handoff phase, actor, reports, artifacts, and budget usage.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "phase": {"type": "string"},
                "actor": {"type": "string"},
                "owner": {"type": "string"},
                "increment_handoff": {"type": "boolean"},
                "report": {"type": "object"},
                "status": {"type": "string"},
                "blocked_reason": {"type": "string"},
                "diff_hash": {"type": "string"},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "clodex_handoff_get",
        "title": "Get Clodex handoff",
        "description": "Read native handoff state, events, artifacts, budget, and next actor.",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
    },
    {
        "name": "clodex_handoff_decide",
        "title": "Decide Clodex handoff",
        "description": "Evaluate whether a native handoff is approved, needs fixes, or blocked.",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
    },
```

- [ ] **Step 4: Add MCP handoff dispatch**

In `clodex/mcp_server.py`, add `import uuid` near the top:

```python
import uuid
```

In `tool_call`, add these branches before the final `else`:

```python
    elif name == "clodex_handoff_create":
        run_id = str(arguments.get("run_id") or f"native-{uuid.uuid4().hex[:12]}")
        run = workflow.state.create_handoff(
            run_id,
            str(arguments["task"]),
            owner=str(arguments.get("owner") or "claude"),
            phase=str(arguments.get("phase") or "planning"),
            handoff_budget=int(arguments.get("handoff_budget") or 6),
        )
        return {"content": [{"type": "text", "text": json.dumps(run, indent=2)}], "isError": False}
    elif name == "clodex_handoff_update":
        run = workflow.state.update_handoff(
            str(arguments["run_id"]),
            phase=arguments.get("phase"),
            actor=arguments.get("actor"),
            owner=arguments.get("owner"),
            report=arguments.get("report") if isinstance(arguments.get("report"), dict) else None,
            increment_handoff=bool(arguments.get("increment_handoff", False)),
            status=arguments.get("status"),
            blocked_reason=arguments.get("blocked_reason"),
            diff_hash=arguments.get("diff_hash"),
        )
        return {"content": [{"type": "text", "text": json.dumps(run, indent=2)}], "isError": run.get("status") == "blocked"}
    elif name == "clodex_handoff_get":
        data = workflow.state.get_handoff(str(arguments["run_id"]))
        if data is None:
            return {"content": [{"type": "text", "text": f"Unknown run: {arguments['run_id']}"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(data, indent=2)}], "isError": False}
    elif name == "clodex_handoff_decide":
        data = workflow.state.get_handoff(str(arguments["run_id"]))
        if data is None:
            return {"content": [{"type": "text", "text": f"Unknown run: {arguments['run_id']}"}], "isError": True}
        run = data["run"]
        if run.get("status") == "approved":
            decision = {"decision": "approved", "run_id": run["id"], "diff_hash": run.get("diff_hash")}
        elif run.get("status") == "blocked":
            decision = {"decision": "blocked", "run_id": run["id"], "blocked_reason": run.get("blocked_reason") or run.get("error")}
        else:
            decision = {
                "decision": "needs_fix",
                "run_id": run["id"],
                "phase": run.get("phase"),
                "budget_remaining": data["budget_remaining"],
            }
        workflow.state.add_event(run["id"], "handoff.decide", decision)
        return {"content": [{"type": "text", "text": json.dumps(decision, indent=2)}], "isError": decision["decision"] == "blocked"}
```

- [ ] **Step 5: Run MCP handoff tests**

Run:

```powershell
python -m unittest tests.test_clodex.ClodexTests.test_mcp_tools_list_includes_handoff_tools tests.test_clodex.ClodexTests.test_mcp_handoff_create_update_get_and_decide tests.test_clodex.ClodexTests.test_mcp_handoff_budget_exhaustion_blocks -v
```

Expected:

```text
OK
```

- [ ] **Step 6: Run existing MCP regression tests**

Run:

```powershell
python -m unittest tests.test_clodex.ClodexTests.test_mcp_tools_list tests.test_clodex.ClodexTests.test_mcp_tasks_get_unknown_run -v
```

Expected:

```text
OK
```

- [ ] **Step 7: Commit Task 5**

Run:

```powershell
git add clodex/mcp_server.py tests/test_clodex.py
git commit -m "feat: add native handoff mcp tools"
```

Expected:

```text
[codex/npm-install ...] feat: add native handoff mcp tools
```

## Task 6: README, npm Positioning, and Final Verification

**Files:**
- Modify: `README.md`
- Modify: `package.json`
- Optionally modify: `skills/clodex-workflow/SKILL.md`

- [ ] **Step 1: Inspect current README and package metadata**

Run:

```powershell
Get-Content -Raw README.md
Get-Content -Raw package.json
Get-Content -Raw skills\clodex-workflow\SKILL.md
```

Expected:

```text
The README and package metadata are printed; identify wording that still presents Clodex primarily as a manually driven harness.
```

- [ ] **Step 2: Update README headline workflow**

Edit `README.md` so the first usage section leads with:

```markdown
## Native Claude/Codex Collaboration

```bash
npm install -g clodex
clodex init
```

Use Claude Code or Codex as usual. Clodex adds repo instructions and MCP tools that teach each agent how to coordinate with the other: Claude plans, Codex implements, both audit, and Clodex enforces durable handoff state and bounded agreement.
```

Move the existing command catalog below this native workflow under a heading like:

```markdown
## Harness Commands
```

Do not remove existing CLI reference material for `doctor`, `plan`, `build`, `audit`, `task`, `apply`, `trace`, `eval`, `hooks`, or `mcp-server`.

- [ ] **Step 3: Update npm metadata**

In `package.json`, set or update:

```json
{
  "description": "Native Claude Code and Codex collaboration for local-first agent workflows",
  "keywords": [
    "claude-code",
    "codex",
    "mcp",
    "agent-harness",
    "developer-tools",
    "ai"
  ]
}
```

Preserve existing `name`, `version`, `bin`, `files`, `scripts`, `repository`, `license`, and any package metadata already present.

- [ ] **Step 4: Update Clodex skill wording if stale**

Open `skills/clodex-workflow/SKILL.md`. If it still frames the primary workflow as manually invoking `clodex build`, adjust the opening guidance to say:

```markdown
Prefer native Clodex coordination when the repository has been initialized with `clodex init`: use the available MCP handoff tools first, and use `clodex task ...`, `clodex audit --diff`, and `clodex status` as fallbacks. The older harness commands remain valid for scripted or non-MCP workflows.
```

If the skill already says this, leave the file unchanged.

- [ ] **Step 5: Run native CLI smoke commands**

Run:

```powershell
python -m unittest tests.test_clodex.ClodexTests.test_cli_init_dry_run_does_not_write_files tests.test_clodex.ClodexTests.test_cli_native_status_reports_current_and_missing_components tests.test_clodex.ClodexTests.test_mcp_handoff_create_update_get_and_decide -v
```

Expected:

```text
OK
```

- [ ] **Step 6: Run the full Python test suite**

Run:

```powershell
python -m unittest discover -s tests -v
```

Expected:

```text
OK
```

- [ ] **Step 7: Run compile verification**

Run:

```powershell
python -m compileall clodex tests
```

Expected:

```text
Listing 'clodex'...
Listing 'tests'...
```

No `SyntaxError` appears.

- [ ] **Step 8: Run npm package smoke checks**

Run:

```powershell
npm pack --dry-run
node npm/clodex.js --json init --dry-run
node npm/clodex.js --json native doctor
```

Expected:

```text
npm pack --dry-run prints a tarball summary that includes package.json, npm launchers, clodex Python files, README.md, CLODEX.md, install.sh, and uninstall.sh.
node npm/clodex.js --json init --dry-run prints JSON with "dry_run": true.
node npm/clodex.js --json native doctor exits 0 when fake or real Claude/Codex CLIs are available; if it exits 1 because local Claude/Codex auth is unavailable, record that exact reason in the final summary.
```

- [ ] **Step 9: Run installer dry-run checks**

Run:

```powershell
bash install.sh --dry-run
bash uninstall.sh --dry-run
```

Expected:

```text
Both commands exit 0 and print the actions they would take.
```

- [ ] **Step 10: Run git whitespace check**

Run:

```powershell
git diff --check
```

Expected:

```text
No output and exit code 0.
```

- [ ] **Step 11: Review final diff**

Run:

```powershell
git diff -- clodex/native.py clodex/cli.py clodex/state.py clodex/mcp_server.py tests/test_clodex.py README.md package.json skills/clodex-workflow/SKILL.md
```

Expected:

```text
Diff shows only native-mode implementation, tests, and documentation/metadata positioning. It does not remove npm launcher files or unrelated package work.
```

- [ ] **Step 12: Commit Task 6**

Run:

```powershell
git add README.md package.json skills/clodex-workflow/SKILL.md
git commit -m "docs: position clodex as native agent collaboration"
```

Expected:

```text
[codex/npm-install ...] docs: position clodex as native agent collaboration
```

If `skills/clodex-workflow/SKILL.md` was unchanged, omit it from `git add`.

## Final Acceptance Checklist

- [ ] `clodex init --dry-run` previews `CLAUDE.md`, `AGENTS.md`, `CLODEX.md`, `.mcp.json`, and `.codex/config.toml` without writing files.
- [ ] `clodex init` creates or updates native files while preserving unmanaged user content outside Clodex managed blocks.
- [ ] `clodex init --no-mcp-config` skips `.mcp.json` and `.codex/config.toml`.
- [ ] `clodex native status` reports `current`, `missing`, `stale`, or `invalid` per native component.
- [ ] `clodex native doctor` includes native status, Claude CLI readiness, Codex CLI readiness, npm launcher visibility, and Python executable details.
- [ ] Existing blocking MCP tools still list and work.
- [ ] New semantic MCP handoff tools create, update, read, and decide a durable run.
- [ ] Handoff budget exhaustion marks the run `blocked` through SQLite state, not prompt text alone.
- [ ] README starts with native Claude/Codex collaboration and keeps harness commands as reference.
- [ ] `python -m unittest discover -s tests -v` passes.
- [ ] `python -m compileall clodex tests` passes.
- [ ] `npm pack --dry-run` passes.
- [ ] `git diff --check` passes.
