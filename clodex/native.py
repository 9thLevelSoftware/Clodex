from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .doctor import run_doctor


BEGIN_MARKER = "<!-- BEGIN CLODEX -->"
END_MARKER = "<!-- END CLODEX -->"
TOML_BEGIN_MARKER = "# BEGIN CLODEX"
TOML_END_MARKER = "# END CLODEX"
DEFAULT_HANDOFF_BUDGET = 6
CODEX_MCP_TABLE = "mcp_servers.clodex"
TOML_HEADER_PROBE_KEY = "__clodex_header_probe__"


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
    return _replace_toml_managed_block(existing, body, force=force)


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
    newline = _detect_newline(existing)
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
    if "mcpServers" not in data:
        servers = {}
        data["mcpServers"] = servers
    else:
        servers = data["mcpServers"]
        if not isinstance(servers, dict):
            if not force:
                raise ManagedBlockError(".mcp.json mcpServers must be an object")
            servers = {}
            data["mcpServers"] = servers
    servers["clodex"] = clodex_mcp_server_entry()
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").replace("\n", newline)


def codex_mcp_block() -> str:
    return """[mcp_servers.clodex]
command = "clodex"
args = ["mcp-server"]
"""


def render_codex_toml(existing: str, *, force: bool = False) -> str:
    has_unmanaged_clodex_table = _has_unmanaged_codex_mcp_table(existing)
    if existing.strip():
        try:
            tomllib.loads(existing)
        except tomllib.TOMLDecodeError as exc:
            if not force or not has_unmanaged_clodex_table:
                raise ManagedBlockError(f"Invalid .codex/config.toml: {exc}") from exc
            repaired = _remove_unmanaged_codex_mcp_tables(existing)
            if repaired.strip():
                try:
                    tomllib.loads(repaired)
                except tomllib.TOMLDecodeError as repaired_exc:
                    raise ManagedBlockError(f"Invalid .codex/config.toml after removing unmanaged [{CODEX_MCP_TABLE}]: {repaired_exc}") from repaired_exc
            existing = repaired
            has_unmanaged_clodex_table = False
            _reject_unsupported_unmanaged_codex_mcp_config(existing)
        else:
            _reject_unsupported_unmanaged_codex_mcp_config(existing)
    if has_unmanaged_clodex_table:
        if not force:
            raise ManagedBlockError(f".codex/config.toml already contains unmanaged [{CODEX_MCP_TABLE}]")
        existing = _remove_unmanaged_codex_mcp_tables(existing)
    rendered = replace_toml_managed_block(existing, codex_mcp_block(), force=force)[0]
    _validate_rendered_codex_toml(rendered)
    return rendered


def _validate_rendered_codex_toml(rendered: str) -> None:
    try:
        data = tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as exc:
        raise ManagedBlockError(f"Rendered .codex/config.toml is invalid: {exc}") from exc
    if not _has_clodex_mcp_command(data):
        raise ManagedBlockError(f"Rendered .codex/config.toml is missing [{CODEX_MCP_TABLE}] command")


def _has_clodex_mcp_command(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return False
    clodex = servers.get("clodex")
    return isinstance(clodex, dict) and clodex.get("command") == "clodex"


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
        current = _read_text_preserve_newlines(path)
    except (OSError, UnicodeDecodeError):
        return "invalid"
    return "current" if current == desired else "stale"


def _planned_content(path: Path, kind: str, body: str, *, force: bool) -> str:
    existing = _read_text_preserve_newlines(path) if path.exists() else ""
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
        try:
            content = _planned_content(path, kind, body, force=force)
        except UnicodeDecodeError as exc:
            files.append(
                {
                    "path": str(path),
                    "action": "error",
                    "status": "invalid",
                    "preview": "",
                    "error": f"Invalid UTF-8 in {path}: {exc}",
                }
            )
            continue
        except OSError as exc:
            files.append(
                {
                    "path": str(path),
                    "action": "error",
                    "status": "invalid",
                    "preview": "",
                    "error": f"Cannot read {path}: {exc}",
                }
            )
            continue
        except ManagedBlockError as exc:
            files.append(
                {
                    "path": str(path),
                    "action": "error",
                    "status": "invalid",
                    "preview": "",
                    "error": str(exc),
                }
            )
            continue
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
    _mark_preflight_conflicts(files, files)
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
    errors = [item for item in plan["files"] if item["action"] == "error"]
    if errors:
        paths = ", ".join(item["path"] for item in errors)
        raise ManagedBlockError(f"Cannot apply native install while target files are invalid: {paths}")
    _preflight_native_writes(plan["files"])
    for item in plan["files"]:
        path = Path(item["path"])
        if item["action"] == "unchanged":
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(item["preview"])
    return plan


def native_status(
    repo_root: Path,
    *,
    global_mode: bool = False,
    no_mcp_config: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    plan = plan_native_install(
        repo_root,
        dry_run=True,
        global_mode=global_mode,
        no_mcp_config=no_mcp_config,
        force=force,
    )
    files = [_status_entry(item) for item in plan["files"]]
    ok = all(item["status"] == "current" and item["action"] == "unchanged" for item in files)
    return {
        "ok": ok,
        "mode": plan["mode"],
        "root": plan["root"],
        "files": files,
    }


def native_doctor(
    repo_root: Path,
    *,
    global_mode: bool = False,
    no_mcp_config: bool = False,
    force: bool = False,
) -> tuple[int, dict[str, Any]]:
    native = native_status(
        repo_root,
        global_mode=global_mode,
        no_mcp_config=no_mcp_config,
        force=force,
    )
    doctor_code, doctor = run_doctor(Path(native["root"]))
    npm_launcher_status = _npm_launcher_status()
    data = {
        "ok": native["ok"] and doctor_code == 0 and npm_launcher_status["ok"],
        "native": native,
        "doctor": doctor,
        "npm_launcher": npm_launcher_status,
        "python": {
            "ok": sys.version_info >= (3, 12),
            "executable": sys.executable,
            "version": sys.version.split()[0],
        },
    }
    return (0 if data["ok"] else 1), data


def _npm_launcher_status() -> dict[str, Any]:
    env_launcher = os.environ.get("CLODEX_NPM_LAUNCHER")
    env_error: str | None = None
    if env_launcher:
        env_path = Path(env_launcher)
        if env_path.exists():
            return {"ok": True, "path": str(env_path)}
        env_error = f"CLODEX_NPM_LAUNCHER does not exist: {env_launcher}"

    launcher = shutil.which("clodex") or shutil.which("clodex.cmd") or shutil.which("clodex.ps1")
    if launcher is not None:
        return {"ok": True, "path": launcher}

    return {
        "ok": False,
        "path": None,
        "reason": env_error or "No clodex launcher found on PATH and CLODEX_NPM_LAUNCHER is not set",
    }


def _status_entry(item: dict[str, Any]) -> dict[str, Any]:
    entry = {
        "path": item["path"],
        "action": item["action"],
        "status": item["status"],
    }
    if "error" in item:
        entry["error"] = item["error"]
    return entry


def _mark_preflight_conflicts(status_files: list[dict[str, Any]], plan_files: list[dict[str, Any]]) -> None:
    for conflict_path, message in _native_write_conflicts(plan_files):
        for item in status_files:
            path = Path(item["path"])
            if path == conflict_path or conflict_path in path.parents:
                item["action"] = "error"
                item["status"] = "invalid"
                item["error"] = message
                if "preview" in item:
                    item["preview"] = ""


def _read_text_preserve_newlines(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _preflight_native_writes(files: list[dict[str, Any]]) -> None:
    conflicts = [message for _path, message in _native_write_conflicts(files)]
    if conflicts:
        raise ManagedBlockError("Cannot apply native install because target paths are blocked: " + "; ".join(conflicts))


def _native_write_conflicts(files: list[dict[str, Any]]) -> list[tuple[Path, str]]:
    conflicts: list[tuple[Path, str]] = []
    for item in files:
        if item["action"] in {"unchanged", "error"}:
            continue
        path = Path(item["path"])
        if path.exists() and path.is_dir():
            conflicts.append((path, f"{path} is a directory"))
            continue
        for ancestor in path.parents:
            if ancestor.exists() and not ancestor.is_dir():
                conflicts.append((ancestor, f"{ancestor} is not a directory"))
                break
    return conflicts


def _has_unmanaged_codex_mcp_table(existing: str) -> bool:
    return bool(_unmanaged_codex_mcp_table_paths(existing))


def _unmanaged_codex_mcp_table_paths(existing: str) -> list[list[str]]:
    paths: list[list[str]] = []
    in_managed_block = False
    for line, outside_multiline_string in _toml_scanned_lines(existing):
        if not outside_multiline_string:
            continue
        stripped = line.strip()
        if stripped == TOML_BEGIN_MARKER:
            in_managed_block = True
        elif stripped == TOML_END_MARKER:
            in_managed_block = False
        elif not in_managed_block:
            parts = _toml_table_parts(line)
            if parts is not None and _is_codex_mcp_table_path(parts):
                paths.append(parts)
    return paths


def _remove_unmanaged_codex_mcp_tables(existing: str) -> str:
    lines = _toml_scanned_lines(existing)
    kept: list[str] = []
    index = 0
    in_managed_block = False
    while index < len(lines):
        line, outside_multiline_string = lines[index]
        if not outside_multiline_string:
            kept.append(line)
            index += 1
            continue
        stripped = line.strip()
        if stripped == TOML_BEGIN_MARKER:
            in_managed_block = True
            kept.append(line)
            index += 1
            continue
        if stripped == TOML_END_MARKER:
            in_managed_block = False
            kept.append(line)
            index += 1
            continue
        if not in_managed_block and _is_codex_mcp_table_header(line):
            index += 1
            while index < len(lines):
                next_line, next_outside_multiline_string = lines[index]
                if next_outside_multiline_string:
                    next_parts = _toml_table_parts(next_line)
                    if next_parts is not None:
                        if _is_codex_mcp_table_path(next_parts):
                            index += 1
                            continue
                        break
                index += 1
            continue
        kept.append(line)
        index += 1
    return "".join(kept)


def _remove_toml_managed_blocks(existing: str) -> str:
    kept: list[str] = []
    in_managed_block = False
    for line, outside_multiline_string in _toml_scanned_lines(existing):
        stripped = line.strip()
        if outside_multiline_string and stripped == TOML_BEGIN_MARKER:
            in_managed_block = True
            continue
        if outside_multiline_string and stripped == TOML_END_MARKER and in_managed_block:
            in_managed_block = False
            continue
        if not in_managed_block:
            kept.append(line)
    return "".join(kept)


def _reject_unsupported_unmanaged_codex_mcp_config(existing: str) -> None:
    unmanaged_text = _remove_unmanaged_codex_mcp_tables(_remove_toml_managed_blocks(existing))
    if not unmanaged_text.strip():
        return
    try:
        data = tomllib.loads(unmanaged_text)
    except tomllib.TOMLDecodeError as exc:
        raise ManagedBlockError(f"Invalid .codex/config.toml after removing unmanaged [{CODEX_MCP_TABLE}]: {exc}") from exc
    if _has_codex_mcp_server_semantics(data):
        raise ManagedBlockError(f".codex/config.toml contains unsupported unmanaged [{CODEX_MCP_TABLE}] config")


def _has_codex_mcp_server_semantics(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    servers = data.get("mcp_servers")
    return isinstance(servers, dict) and "clodex" in servers


def _toml_scanned_lines(existing: str) -> list[tuple[str, bool]]:
    scanned: list[tuple[str, bool]] = []
    multiline_kind: str | None = None
    for line in existing.splitlines(keepends=True):
        scanned.append((line, multiline_kind is None))
        multiline_kind = _update_toml_multiline_kind(line, multiline_kind)
    return scanned


def _update_toml_multiline_kind(line: str, multiline_kind: str | None) -> str | None:
    index = 0
    while index < len(line):
        if multiline_kind == "basic":
            end = line.find('"""', index)
            if end == -1:
                return multiline_kind
            multiline_kind = None
            index = end + 3
            continue
        if multiline_kind == "literal":
            end = line.find("'''", index)
            if end == -1:
                return multiline_kind
            multiline_kind = None
            index = end + 3
            continue
        if line.startswith('"""', index):
            multiline_kind = "basic"
            index += 3
            continue
        if line.startswith("'''", index):
            multiline_kind = "literal"
            index += 3
            continue
        char = line[index]
        if char == "#":
            return multiline_kind
        if char == '"':
            index = _skip_toml_basic_string(line, index + 1)
            continue
        if char == "'":
            end = line.find("'", index + 1)
            index = len(line) if end == -1 else end + 1
            continue
        index += 1
    return multiline_kind


def _skip_toml_basic_string(line: str, index: int) -> int:
    escaped = False
    while index < len(line):
        char = line[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return index + 1
        index += 1
    return index


def _is_codex_mcp_table_header(line: str) -> bool:
    parts = _toml_table_parts(line)
    return parts is not None and _is_codex_mcp_table_path(parts)


def _is_codex_mcp_table_path(parts: list[str]) -> bool:
    return parts[:2] == ["mcp_servers", "clodex"]


def _is_toml_table_header(line: str) -> bool:
    return _toml_table_parts(line) is not None


def _toml_table_parts(line: str) -> list[str] | None:
    value = line.strip()
    if not value.startswith("["):
        return None
    try:
        parsed = tomllib.loads(f"{value}\n{TOML_HEADER_PROBE_KEY} = true\n")
    except tomllib.TOMLDecodeError:
        return None
    return _find_toml_header_probe_path(parsed)


def _find_toml_header_probe_path(value: Any, path: list[str] | None = None) -> list[str] | None:
    current_path = [] if path is None else path
    if isinstance(value, dict):
        if value.get(TOML_HEADER_PROBE_KEY) is True:
            return current_path
        for key, child in value.items():
            if key == TOML_HEADER_PROBE_KEY:
                continue
            found = _find_toml_header_probe_path(child, current_path + [str(key)])
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_toml_header_probe_path(child, current_path)
            if found is not None:
                return found
    return None


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


def _replace_toml_managed_block(existing: str, body: str, *, force: bool) -> tuple[str, bool]:
    newline = _detect_newline(existing)
    block_body = _normalize_block_body(body, newline)
    replacement = f"{TOML_BEGIN_MARKER}{newline}{block_body}{TOML_END_MARKER}{newline}"
    begin_spans = _toml_marker_line_spans(existing, TOML_BEGIN_MARKER)
    end_spans = _toml_marker_line_spans(existing, TOML_END_MARKER)

    if not begin_spans and not end_spans:
        updated = existing + _append_separator(existing, newline) + replacement
        return updated, updated != existing

    if len(begin_spans) != 1 or len(end_spans) != 1 or end_spans[0][0] < begin_spans[0][0]:
        if not force:
            raise ManagedBlockError(f"Malformed Clodex managed block for {TOML_BEGIN_MARKER!r}")
        marker_positions = [start for start, _end in begin_spans + end_spans]
        prefix = existing[: min(marker_positions)] if marker_positions else existing
        updated = prefix + _prefix_separator(prefix, newline) + replacement
        return updated, updated != existing

    begin_start = begin_spans[0][0]
    end_after = end_spans[0][1]
    updated = existing[:begin_start] + replacement + existing[end_after:]
    return updated, updated != existing


def _toml_marker_line_spans(existing: str, marker: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    offset = 0
    for line, outside_multiline_string in _toml_scanned_lines(existing):
        line_end = offset + len(line)
        if outside_multiline_string and line.strip() == marker:
            spans.append((offset, line_end))
        offset = line_end
    return spans


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
