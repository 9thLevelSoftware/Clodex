from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "name": "Clodex Dual CLI Workflow",
    "max_fix_loops": 2,
    "workspace_root": ".clodex/workspaces",
    "runs_root": ".clodex/runs",
    "state_path": ".clodex/state.sqlite3",
    "workspace": {
        "backend": "git-worktree",
        "apply_mode": "manual",
    },
    "claude": {
        "model": "opus",
        "effort": "max",
        "permission_mode": "plan",
    },
    "codex": {
        "model": "gpt-5.5",
        "reasoning_effort": "xhigh",
        "sandbox": "workspace-write",
        "approval_profile": "ci",
    },
    "audit": {
        "quorum": "unanimous",
        "personas": ["security", "performance", "portability", "test-gap"],
        "reviewers": [
            {"id": "claude-plan", "backend": "claude", "persona": "plan-adherence", "required": True, "timeout": 600},
            {"id": "codex-architecture", "backend": "codex", "persona": "architecture", "required": True, "timeout": 600},
            {"id": "security", "backend": "codex", "persona": "security", "required": False, "timeout": 600},
            {"id": "performance", "backend": "codex", "persona": "performance", "required": False, "timeout": 600},
            {"id": "portability", "backend": "codex", "persona": "portability", "required": False, "timeout": 600},
            {"id": "test-gap", "backend": "claude", "persona": "test-gap", "required": False, "timeout": 600},
        ],
    },
    "mcp": {
        "async_tasks": True,
    },
    "tracing": {
        "enabled": True,
    },
}


@dataclass(frozen=True)
class ClodexConfig:
    repo_root: Path
    raw: dict[str, Any] = field(default_factory=dict)
    prompt_body: str = ""

    @property
    def max_fix_loops(self) -> int:
        return int(self.raw.get("max_fix_loops", DEFAULT_CONFIG["max_fix_loops"]))

    @property
    def workspace_root(self) -> Path:
        return self.repo_root / str(self.raw.get("workspace_root", DEFAULT_CONFIG["workspace_root"]))

    @property
    def runs_root(self) -> Path:
        return self.repo_root / str(self.raw.get("runs_root", DEFAULT_CONFIG["runs_root"]))

    @property
    def state_path(self) -> Path:
        return self.repo_root / str(self.raw.get("state_path", DEFAULT_CONFIG["state_path"]))

    @property
    def workspace(self) -> dict[str, Any]:
        return dict(DEFAULT_CONFIG["workspace"] | self.raw.get("workspace", {}))

    @property
    def claude(self) -> dict[str, Any]:
        return dict(DEFAULT_CONFIG["claude"] | self.raw.get("claude", {}))

    @property
    def codex(self) -> dict[str, Any]:
        return dict(DEFAULT_CONFIG["codex"] | self.raw.get("codex", {}))

    @property
    def audit(self) -> dict[str, Any]:
        return dict(DEFAULT_CONFIG["audit"] | self.raw.get("audit", {}))

    @property
    def reviewers(self) -> list[dict[str, Any]]:
        reviewers = self.audit.get("reviewers", DEFAULT_CONFIG["audit"]["reviewers"])
        return [dict(item) for item in reviewers]

    @property
    def mcp(self) -> dict[str, Any]:
        return dict(DEFAULT_CONFIG["mcp"] | self.raw.get("mcp", {}))

    @property
    def tracing(self) -> dict[str, Any]:
        return dict(DEFAULT_CONFIG["tracing"] | self.raw.get("tracing", {}))


def find_repo_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return current


def load_config(repo_root: Path | None = None) -> ClodexConfig:
    root = (repo_root or find_repo_root()).resolve()
    contract = root / "CLODEX.md"
    if not contract.exists():
        return ClodexConfig(repo_root=root, raw=dict(DEFAULT_CONFIG), prompt_body="")

    text = contract.read_text(encoding="utf-8")
    front_matter, body = split_front_matter(text)
    parsed = parse_minimal_yaml(front_matter)
    merged = deep_merge(DEFAULT_CONFIG, parsed)
    return ClodexConfig(repo_root=root, raw=merged, prompt_body=body.strip())


def split_front_matter(text: str) -> tuple[str, str]:
    if not text.startswith("---"):
        return "", text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return "", text
    return parts[1], parts[2]


def parse_minimal_yaml(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_section: str | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if indent == 0 and value == "":
            result[key] = {}
            current_section = key
            continue
        target = result
        if indent > 0 and current_section:
            section = result.setdefault(current_section, {})
            if isinstance(section, dict):
                target = section
        target[key] = parse_scalar(value)
    return result


def parse_scalar(value: str) -> Any:
    if value == "":
        return ""
    if value.startswith(("[", "{")) and value.endswith(("]", "}")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if value.isdigit():
        return int(value)
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("\"'") for item in inner.split(",")]
    return value.strip("\"'")


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key, value in base.items():
        if isinstance(value, dict):
            merged[key] = deep_merge(value, {})
        else:
            merged[key] = value
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged
