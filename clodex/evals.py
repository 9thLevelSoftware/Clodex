from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import load_config
from .state import StateStore


def run_local_evals(repo_root: Path | None = None) -> dict[str, Any]:
    config = load_config(repo_root)
    store = StateStore(config.state_path)
    scenarios = [
        {"name": "config-loads", "passed": config.workspace["backend"] in {"git-worktree", "local"}},
        {"name": "state-schema-v2", "passed": store.schema_version() >= 2},
        {"name": "reviewers-configured", "passed": len(config.reviewers) >= 2},
    ]
    return {"passed": all(item["passed"] for item in scenarios), "scenarios": scenarios}
