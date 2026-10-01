from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import retirement

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
        "permission_mode": "plan",
        # Planning gets the most reasoning; audits run often, so they default lower.
        "plan": {"model": "opus", "effort": "max"},
        "audit": {"model": "opus", "effort": "high"},
    },
    "codex": {
        "model": "gpt-6.1-sol",
        "reasoning_effort": "xhigh",
        "sandbox": "workspace-write",
        "approval_profile": "ci",
    },
    "audit": {
        "quorum": "unanimous",
        "max_diff_bytes": 200000,
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


ROLES = ("plan", "audit")


class ConfigError(ValueError):
    """CLODEX.md could not be loaded."""


_warned_models: set[str] = set()


def warn_if_model_retiring(model: str) -> None:
    info = retirement(model)
    if info is None or model in _warned_models:
        return
    _warned_models.add(model)
    verb = "has retired" if info.retired else "retires"
    fix = f" Set `model: {info.successor}` under `codex:` in the CLODEX.md front matter (or run `clodex init --migrate`)." if info.successor else ""
    print(f"clodex: warning: Codex model '{model}' {verb} on {info.on.isoformat()}.{fix}", file=sys.stderr)


@dataclass(frozen=True)
class ClodexConfig:
    repo_root: Path
    raw: dict[str, Any] = field(default_factory=dict)
    prompt_body: str = ""
    # Only what CLODEX.md set explicitly (no defaults), so legacy flat keys can
    # outrank the per-role defaults.
    user: dict[str, Any] = field(default_factory=dict)

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

    def claude_role(self, role: str) -> dict[str, Any]:
        """Effective Claude settings for `plan` or `audit`.

        Precedence, lowest to highest: built-in role defaults, flat `claude.*` keys in
        CLODEX.md (the pre-0.2 layout, which drove both roles), then `claude.<role>.*`.
        """
        defaults = DEFAULT_CONFIG["claude"]
        user = self.user.get("claude")
        user = user if isinstance(user, dict) else {}
        flat = {key: value for key, value in user.items() if key not in ROLES}
        explicit = user.get(role)
        explicit = explicit if isinstance(explicit, dict) else {}
        return {"permission_mode": defaults["permission_mode"], **defaults[role], **flat, **explicit}

    @property
    def codex(self) -> dict[str, Any]:
        return dict(DEFAULT_CONFIG["codex"] | self.raw.get("codex", {}))

    def codex_role(self, role: str) -> dict[str, Any]:
        """Codex settings for a role; `codex.audit.*` overrides model/effort for audits."""
        base = {key: value for key, value in self.codex.items() if key not in ROLES}
        override = self.codex.get(role)
        return {**base, **(override if isinstance(override, dict) else {})}

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


def resolve_repo_root() -> Path:
    """The repo Clodex should work on: $CLODEX_REPO_ROOT if set (MCP clients start servers from anywhere), else the git root above the cwd."""
    override = os.environ.get("CLODEX_REPO_ROOT")
    return find_repo_root(Path(override)) if override else find_repo_root()


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
    audit_override = merged["codex"].get("audit")
    if isinstance(audit_override, dict) and audit_override.get("model"):
        warn_if_model_retiring(str(audit_override["model"]))
    return ClodexConfig(repo_root=root, raw=merged, prompt_body=body.strip(), user=parsed)


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
