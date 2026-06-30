from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import ClodexConfig


@dataclass(frozen=True)
class AgentCommand:
    name: str
    argv: list[str]

    def display(self) -> str:
        return " ".join(quote_arg(arg) for arg in self.argv)


def quote_arg(arg: str) -> str:
    if not arg:
        return "''"
    if any(ch.isspace() or ch in "'\"" for ch in arg):
        return "'" + arg.replace("'", "'\\''") + "'"
    return arg


def claude_plan_command(config: ClodexConfig) -> AgentCommand:
    claude = config.claude
    return AgentCommand(
        name="claude-plan",
        argv=[
            "claude",
            "-p",
            "--model",
            str(claude["model"]),
            "--effort",
            str(claude["effort"]),
            "--permission-mode",
            str(claude["permission_mode"]),
            "--output-format",
            "json",
        ],
    )


def claude_audit_command(config: ClodexConfig) -> AgentCommand:
    return claude_plan_command(config)


def codex_exec_command(config: ClodexConfig, repo_root: Path) -> AgentCommand:
    codex = config.codex
    return AgentCommand(
        name="codex-build",
        argv=[
            "codex",
            "exec",
            "-m",
            str(codex["model"]),
            "-c",
            f'model_reasoning_effort="{codex["reasoning_effort"]}"',
            "--sandbox",
            str(codex["sandbox"]),
            "--ask-for-approval",
            "never",
            "-C",
            str(repo_root),
            "-",
        ],
    )


def codex_review_command(config: ClodexConfig) -> AgentCommand:
    codex = config.codex
    return AgentCommand(
        name="codex-audit",
        argv=[
            "codex",
            "review",
            "--uncommitted",
            "-c",
            f'model="{codex["model"]}"',
            "-c",
            f'model_reasoning_effort="{codex["reasoning_effort"]}"',
            "-",
        ],
    )
