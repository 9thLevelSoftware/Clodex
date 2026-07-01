from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .config import load_config
from .state import StateStore
from .trace import TraceWriter


HOOK_EVENTS = [
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "SubagentStart",
    "SubagentStop",
    "TaskCreated",
    "TaskCompleted",
    "WorktreeCreated",
    "WorktreeRemoved",
    "FileChanged",
    "Stop",
]


def hook_config(repo_root: Path | None = None) -> dict[str, Any]:
    config = load_config(repo_root)
    command = f"{sys.executable} -m clodex hooks ingest"
    return {
        "hooks": {
            event: [{"type": "command", "command": command}]
            for event in HOOK_EVENTS
        },
        "clodex": {
            "repo_root": str(config.repo_root),
            "events": HOOK_EVENTS,
        },
    }


def ingest_hook_event(repo_root: Path | None, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    config = load_config(repo_root)
    state = StateStore(config.state_path)
    run_dir = config.runs_root / run_id
    events_dir = run_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    hook_file = events_dir / "claude-hooks.jsonl"
    with hook_file.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    TraceWriter(run_dir, run_id, state).event("hook.ingest", payload)
    return {"run_id": run_id, "event_file": str(hook_file)}
