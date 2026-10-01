from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from . import capabilities
from .config import ClodexConfig
from .schemas import compact_schema, schema_path


@dataclass(frozen=True)
class AgentCommand:
    name: str
    argv: list[str]
    # When set, the agent's JSON output is validated against this packaged schema.
    schema_name: str | None = None
    # When set, the runner adds `-o <file>` so the final message is read from a file
    # instead of being scraped out of stdout (codex exec).
    capture_last_message: bool = False

    def display(self) -> str:
        shown: list[str] = []
        for index, arg in enumerate(self.argv):
            shown.append(f"<{self.schema_name} schema>" if index and self.argv[index - 1] == "--json-schema" else arg)
        return " ".join(quote_arg(arg) for arg in shown)


def quote_arg(arg: str) -> str:
    if not arg:
        return "''"
    if any(ch.isspace() or ch in "'\"" for ch in arg):
        return "'" + arg.replace("'", "'\\''") + "'"
    return arg


def inline_schema_supported(setting: object = "auto", resolved: str | None = None) -> bool:
    """Whether to pass `claude --json-schema <inline json>`.

    The flag takes inline JSON only. A Windows .cmd/.bat shim re-parses the command
    line with cmd.exe and mangles the quotes, so `auto` turns the flag off for shims.
    """
    if isinstance(setting, bool):
        return setting
    text = str(setting).strip().lower()
    if text in {"true", "yes", "on"}:
        return True
    if text in {"false", "no", "off"}:
        return False
    resolved = resolved or shutil.which("claude")
    return not (resolved and resolved.lower().endswith((".cmd", ".bat")))


def _claude_command(name: str, config: ClodexConfig, role: str, schema_name: str) -> AgentCommand:
    settings = config.claude_role(role)
    argv = [
        "claude",
        "-p",
        "--model",
        str(settings["model"]),
        "--effort",
        str(settings["effort"]),
        "--permission-mode",
        str(settings["permission_mode"]),
        "--output-format",
        "json",
    ]
    fallback = settings.get("fallback_model")
    if fallback and capabilities.supports(config.repo_root, "claude", "--fallback-model") is not False:
        argv.extend(["--fallback-model", ",".join(fallback) if isinstance(fallback, (list, tuple)) else str(fallback)])
    if settings.get("max_budget_usd") not in (None, "") and capabilities.supports(config.repo_root, "claude", "--max-budget-usd") is not False:
        argv.extend(["--max-budget-usd", str(settings["max_budget_usd"])])
    if inline_schema_supported(settings.get("json_schema", "auto")) and capabilities.supports(config.repo_root, "claude", "--json-schema") is not False:
        argv.extend(["--json-schema", compact_schema(schema_name)])
    return AgentCommand(name=name, argv=argv, schema_name=schema_name)


def claude_plan_command(config: ClodexConfig) -> AgentCommand:
    return _claude_command("claude-plan", config, "plan", "plan")


def claude_audit_command(config: ClodexConfig) -> AgentCommand:
    return _claude_command("claude-audit", config, "audit", "audit_verdict")


def codex_exec_command(config: ClodexConfig, repo_root: Path, approval_profile: str | None = None) -> AgentCommand:
    codex = config.codex
    profile = approval_profile or str(codex.get("approval_profile", "ci"))
    argv = [
        "codex",
        "exec",
        "-m",
        str(codex["model"]),
        "-c",
        f'model_reasoning_effort="{codex["reasoning_effort"]}"',
    ]
    if profile == "auto_review":
        argv.append("--approve-for-me")
    else:
        # `codex exec` is non-interactive and has no --ask-for-approval flag.
        argv.extend(["-c", 'approval_policy="never"'])
    argv.extend(["--sandbox", str(codex["sandbox"]), "-C", str(repo_root), "-"])
    return AgentCommand(name="codex-build", argv=argv)


def codex_review_command(config: ClodexConfig, repo_root: Path) -> AgentCommand:
    # `codex exec review` cannot take a prompt together with --uncommitted and ignores
    # --output-schema, so audits run as a read-only `codex exec`; the audit prompt
    # already embeds the diff.
    codex = config.codex_role("audit")
    argv = [
        "codex",
        "exec",
        "-m",
        str(codex["model"]),
        "-c",
        f'model_reasoning_effort="{codex["reasoning_effort"]}"',
        "-c",
        'approval_policy="never"',
        "--sandbox",
        "read-only",
        "--ephemeral",
        "-C",
        str(repo_root),
    ]
    # An older Codex without these flags still works: the prompt asks for JSON, stdout is
    # parsed, and the reply is validated against the schema either way.
    if capabilities.supports(config.repo_root, "codex", "--output-schema") is not False:
        argv.extend(["--output-schema", str(schema_path("audit_verdict"))])
    argv.append("-")
    return AgentCommand(
        name="codex-audit",
        argv=argv,
        schema_name="audit_verdict",
        capture_last_message=capabilities.supports(config.repo_root, "codex", "--output-last-message") is not False,
    )
