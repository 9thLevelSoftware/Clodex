"""Claude Code hook integration: record session events into the Clodex trace.

Hook semantics that matter here (Claude Code hooks reference): a command hook that exits with
code 2 *blocks* the action it is attached to, and its stdout is added to Claude's context for
some events. So ingestion must never exit 2 and must print nothing unless asked to.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .config import load_config
from .launcher import clodex_argv
from .state import StateStore
from .trace import TraceWriter

# Event names as spelled in the Claude Code hooks reference. Tool events (PreToolUse /
# PostToolUse) are left out on purpose: each hook starts a process, which would slow every
# tool call, and FileChanged requires a matcher.
HOOK_EVENTS = [
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "SubagentStart",
    "SubagentStop",
    "TaskCreated",
    "TaskCompleted",
    "WorktreeCreate",
    "WorktreeRemove",
    "Stop",
]
HOOK_TIMEOUT_SECONDS = 10
DEFAULT_RUN_ID = "manual-hook-event"
SCOPES = ("local", "project", "user")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def safe_run_id(value: str) -> str:
    """A run id that is safe to use as a directory name; raises ValueError otherwise."""
    if not isinstance(value, str) or not _RUN_ID.match(value) or ".." in value:
        raise ValueError(f"Invalid run id: {value!r} (use letters, digits, '.', '_' or '-', at most 128 characters)")
    return value


def derive_run_id(explicit: str | None, payload: dict[str, Any], environ: dict[str, str] | None = None) -> str:
    """--run-id if given (validated), else $CLODEX_RUN_ID or the session id, else a shared default."""
    if explicit:
        return safe_run_id(explicit)
    env = os.environ if environ is None else environ
    for candidate in (env.get("CLODEX_RUN_ID"), payload.get("session_id")):
        if candidate:
            try:
                return safe_run_id(str(candidate))
            except ValueError:
                continue  # untrusted input: fall back rather than fail a hook
    return DEFAULT_RUN_ID


def hook_handler() -> dict[str, Any]:
    argv = clodex_argv("hooks", "ingest")
    return {"type": "command", "command": argv[0], "args": argv[1:], "timeout": HOOK_TIMEOUT_SECONDS}


def hook_config() -> dict[str, Any]:
    """The `hooks` block, ready to paste into a Claude Code settings file."""
    handler = hook_handler()
    return {"hooks": {event: [{"hooks": [dict(handler)]}] for event in HOOK_EVENTS}}


def is_clodex_handler(handler: Any) -> bool:
    return isinstance(handler, dict) and list(handler.get("args") or [])[-2:] == ["hooks", "ingest"]


def merge_hooks(settings: dict[str, Any], *, remove: bool = False) -> dict[str, Any]:
    """Add (or with `remove`, strip) Clodex's hook entries, leaving every other hook untouched."""
    merged = json.loads(json.dumps(settings))
    hooks = merged.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        raise ValueError("settings 'hooks' must be an object")
    handler = hook_handler()
    for event in HOOK_EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"settings hooks.{event} must be an array")
        kept = []
        for group in groups:
            inner = [h for h in (group.get("hooks") or []) if not is_clodex_handler(h)] if isinstance(group, dict) else None
            if inner is None:
                kept.append(group)
            elif inner:
                kept.append({**group, "hooks": inner})
        if not remove:
            kept.append({"hooks": [dict(handler)]})
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if hooks:
        merged["hooks"] = hooks
    else:
        merged.pop("hooks", None)
    return merged


def settings_path(repo_root: Path, scope: str) -> Path:
    if scope == "local":
        return repo_root / ".claude" / "settings.local.json"
    if scope == "project":
        return repo_root / ".claude" / "settings.json"
    if scope == "user":
        home = os.environ.get("CLAUDE_CONFIG_DIR")
        return (Path(home) if home else Path.home() / ".claude") / "settings.json"
    raise ValueError(f"Unknown scope: {scope} (use one of {', '.join(SCOPES)})")


def install_hooks(repo_root: Path, scope: str = "local", *, dry_run: bool = False, remove: bool = False, force: bool = False) -> dict[str, Any]:
    """Merge Clodex's hooks into a Claude Code settings file (or remove them again)."""
    path = settings_path(repo_root, scope)
    existing_text = ""
    newline = "\n"
    if path.exists():
        with path.open(encoding="utf-8", newline="") as handle:
            existing_text = handle.read()
        newline = "\r\n" if "\r\n" in existing_text else "\n"
    try:
        settings = json.loads(existing_text) if existing_text.strip() else {}
        if not isinstance(settings, dict):
            raise ValueError("settings root must be an object")
        merged = merge_hooks(settings, remove=remove)
    except ValueError as exc:
        if not force:
            raise ValueError(f"Cannot update {path}: {exc} (use --force to replace it)") from exc
        merged = merge_hooks({}, remove=remove)
    rendered = (json.dumps(merged, indent=2) + "\n").replace("\n", newline)
    if not path.exists():
        action = "unchanged" if remove else "create"
    else:
        action = "unchanged" if rendered == existing_text else ("remove" if remove else "update")
    if not dry_run and action in {"create", "update", "remove"}:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(rendered)
    note = "project scope is meant to be committed but contains machine-specific paths; prefer --scope local" if scope == "project" and not remove else None
    return {"path": str(path), "scope": scope, "action": action, "dry_run": dry_run, **({"note": note} if note else {}), "preview": merged}


def ingest_hook_event(repo_root: Path | None, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    config = load_config(repo_root)
    run_id = safe_run_id(run_id)
    runs_root = config.runs_root.resolve()
    run_dir = (runs_root / run_id).resolve()
    if run_dir.parent != runs_root:  # belt and braces on top of the id check
        raise ValueError(f"Invalid run id: {run_id!r}")
    state = StateStore(config.state_path)
    events_dir = run_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    hook_file = events_dir / "claude-hooks.jsonl"
    with hook_file.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    TraceWriter(run_dir, run_id, state).event("hook.ingest", payload)
    return {"run_id": run_id, "event_file": str(hook_file)}


def parse_hook_payload(text: str) -> dict[str, Any]:
    """The JSON object Claude Code sends on stdin; anything else is kept (truncated) rather than lost."""
    if not text.strip():
        return {}
    try:
        payload = json.loads(text)
    except ValueError:
        return {"raw": text[:2000]}
    return payload if isinstance(payload, dict) else {"value": payload}
