from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    return _replace_managed_block(existing, body, BEGIN_MARKER, END_MARKER, force=force)


def replace_toml_managed_block(existing: str, body: str, *, force: bool = False) -> tuple[str, bool]:
    return _replace_managed_block(existing, body, TOML_BEGIN_MARKER, TOML_END_MARKER, force=force)


def build_claude_block() -> str:
    return f"""# Clodex Native Collaboration

Claude Code is the default strategist and orchestrator for Clodex work.
Codex is the default engineer for implementation, fixes, and focused audit work.

When a user asks for non-trivial design, implementation, debugging, refactoring, or review work:

1. Prefer the MCP tool `clodex_handoff_create` to create or resume a durable handoff.
2. Use `clodex_handoff_get` before acting on an existing handoff.
3. Produce the plan, design, acceptance criteria, risks, and test commands before implementation.
4. Delegate implementation-oriented work to Codex through Clodex instead of manually telling the user to run Codex.
5. Use `clodex_handoff_update` to record plan artifacts, phase changes, review notes, changed files, tests, unresolved issues, and diff hashes.
6. Use `clodex_handoff_decide` before claiming completion when Codex has implemented or audited changes.
7. Stop and summarize for the user when Clodex reports `blocked`.

Default role contract:

- Claude Code is the default strategist.
- Codex is the default engineer.
- Owner starts as `claude`.
- Handoff budget is {DEFAULT_HANDOFF_BUDGET}.
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
    return f"""# Clodex Native Collaboration

Codex is the default engineer for Clodex work, while Claude Code is the default strategist.

When Clodex context is present:

1. Treat incoming Clodex handoffs as implementation, audit, or fix work according to the current phase.
2. Use `clodex_handoff_get` before acting on an existing run.
3. Implement from the accepted Claude plan and avoid expanding scope without recording the reason.
4. Use `clodex_handoff_update` to record changed files, tests run, unresolved issues, audit verdicts, and diff hash.
5. Use `clodex_handoff_create` only when the user asks Codex to start a native collaboration directly.
6. Use `clodex_handoff_decide` before claiming completion after implementation or audit work.
7. Ask for clarification through Clodex when product intent or acceptance criteria are unclear.
8. Respect owner, phase, and handoff budget state. When the budget is exhausted, summarize the disagreement for the user.

Default role contract:

- Claude Code is the default strategist.
- Codex is the default engineer.
- Owner starts as `claude`.
- Handoff budget is {DEFAULT_HANDOFF_BUDGET}.
- Phase order is `planning`, `implementation`, `audit`, `fix`, `decision`.

Prefer MCP tools. If MCP is unavailable, use these CLI fallbacks:

```bash
clodex task start "<task>"
clodex task get <run-id>
clodex task cancel <run-id>
clodex audit --diff
clodex status
```
"""


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


def build_clodex_policy_block() -> str:
    return """# Native Mode

Clodex native mode is enabled for this repository.

- Use MCP handoff tools as the primary coordination channel.
- Keep Claude Code as strategist by default and Codex as engineer by default.
- Require agreement before reporting non-trivial implementation work as complete.
- Use CLI fallbacks only when MCP is unavailable.
"""


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
    plan = plan_native_install(
        repo_root,
        dry_run=False,
        global_mode=global_mode,
        no_mcp_config=no_mcp_config,
        force=force,
    )
    for item in plan["files"]:
        path = Path(item["path"])
        if item["action"] == "unchanged":
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item["preview"], encoding="utf-8", newline="\n")
    return plan


def _replace_managed_block(
    existing: str,
    body: str,
    begin_marker: str,
    end_marker: str,
    *,
    force: bool,
) -> tuple[str, bool]:
    newline = _detect_newline(existing)
    block_body = _normalize_block_body(body, newline)
    replacement = f"{begin_marker}{newline}{block_body}{end_marker}{newline}"
    begin_count = existing.count(begin_marker)
    end_count = existing.count(end_marker)

    if begin_count == 0 and end_count == 0:
        updated = existing + _append_separator(existing, newline) + replacement
        return updated, updated != existing

    begin = existing.find(begin_marker)
    end = existing.find(end_marker)
    if begin_count != 1 or end_count != 1 or begin == -1 or end == -1 or end < begin:
        if not force:
            raise ManagedBlockError(f"Malformed Clodex managed block for {begin_marker!r}")
        marker_positions = [pos for pos in (begin, end) if pos != -1]
        prefix = existing[: min(marker_positions)] if marker_positions else existing
        updated = prefix + _prefix_separator(prefix, newline) + replacement
        return updated, updated != existing

    end_after = _after_marker_line(existing, end + len(end_marker))
    updated = existing[:begin] + replacement + existing[end_after:]
    return updated, updated != existing


def _normalize_block_body(body: str, newline: str) -> str:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    return normalized.replace("\n", newline)


def _detect_newline(existing: str) -> str:
    for index, char in enumerate(existing):
        if char == "\r":
            if existing[index : index + 2] == "\r\n":
                return "\r\n"
            return "\r"
        if char == "\n":
            return "\n"
    return "\n"


def _append_separator(existing: str, newline: str) -> str:
    if not existing or existing.endswith(newline * 2):
        return ""
    if _ends_with_newline(existing):
        return newline
    return newline * 2


def _prefix_separator(prefix: str, newline: str) -> str:
    if not prefix or _ends_with_newline(prefix):
        return ""
    return newline


def _ends_with_newline(value: str) -> bool:
    return value.endswith(("\r\n", "\n", "\r"))


def _after_marker_line(existing: str, offset: int) -> int:
    if existing.startswith("\r\n", offset):
        return offset + 2
    if existing.startswith("\n", offset):
        return offset + 1
    if existing.startswith("\r", offset):
        return offset + 1
    return offset
