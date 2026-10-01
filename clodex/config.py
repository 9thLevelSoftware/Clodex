from __future__ import annotations

import sys
from datetime import date
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # npm installs have no site-packages PyYAML; use the vendored copy
    from ._vendor import yaml


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
        "model": "gpt-6.1-sol",
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


class ConfigError(ValueError):
    """CLODEX.md could not be loaded."""


# Codex models being retired: model -> (retire date, successor). A fuller
# registry replaces this once model validation lands.
RETIRING_CODEX_MODELS: dict[str, tuple[str, str]] = {
    "gpt-5.5": ("2026-10-14", "gpt-6.1-sol"),
}
_warned_models: set[str] = set()


def warn_if_model_retiring(model: str) -> None:
    entry = RETIRING_CODEX_MODELS.get(model)
    if entry is None or model in _warned_models:
        return
    _warned_models.add(model)
    retire_on, successor = entry
    verb = "has retired" if date.today() >= date.fromisoformat(retire_on) else "retires"
    print(
        f"clodex: warning: Codex model '{model}' {verb} on {retire_on}. "
        f"Set `model: {successor}` under `codex:` in the CLODEX.md front matter.",
        file=sys.stderr,
    )


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
    parsed = parse_front_matter(front_matter, str(contract))
    merged = deep_merge(DEFAULT_CONFIG, parsed)
    warn_if_model_retiring(str(merged["codex"].get("model", "")))
    return ClodexConfig(repo_root=root, raw=merged, prompt_body=body.strip())


def split_front_matter(text: str) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    return "", text


def parse_front_matter(text: str, source: str = "CLODEX.md") -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML front matter in {source}: {exc}") from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ConfigError(f"{source} front matter must be a mapping, got {type(parsed).__name__}")
    return _drop_nulls(parsed)


def _drop_nulls(value: dict[str, Any]) -> dict[str, Any]:
    """Treat `key:` with no value as unset so it cannot wipe out a default section."""
    return {key: _drop_nulls(item) if isinstance(item, dict) else item for key, item in value.items() if item is not None}


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
