"""`clodex eval run`: an offline self-test of the harness itself.

Everything runs in a throwaway directory and never calls Claude or Codex, so it is fast, free and
safe to run anywhere (CI included). It checks that the pieces Clodex relies on still fit together:
your CLODEX.md, the command lines it builds, the JSON schemas, the state store, workspaces, native
setup and hooks.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .commands import claude_audit_command, claude_plan_command, codex_exec_command, codex_review_command
from .config import ClodexConfig, load_config
from .hooks import HOOK_EVENTS, hook_config, merge_hooks
from .jsonutil import AgentEnvelopeError, extract_json_object
from .models import validate
from .native import plan_native_install
from .quorum import evaluate, quorum_met
from .schemas import SCHEMA_NAMES, load_schema, schema_path
from .state import StateStore
from .workspace import WorkspaceManager


class EvalFailure(Exception):
    pass


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise EvalFailure(message)


def _config_valid(config: ClodexConfig, _tmp: Path) -> str:
    errors = [d for d in validate(config) if d.level == "error"]
    _check(not errors, "; ".join(f"{d.where}: {d.message}" for d in errors))
    return "CLODEX.md settings are valid"


def _reviewers_and_quorum(config: ClodexConfig, _tmp: Path) -> str:
    required = [r for r in config.reviewers if r.get("required", True)]
    _check(bool(required), "no required reviewers configured")
    _check(not quorum_met(0, len(required), config.audit.get("quorum", "unanimous")), "an empty approval set must never satisfy the quorum")
    _check(quorum_met(len(required), len(required), config.audit.get("quorum", "unanimous")), "all required reviewers approving must satisfy the quorum")
    verdicts = [{"reviewer_id": str(r["id"]), "approved": True, "diff_hash": "h", "_required": bool(r.get("required", True))} for r in config.reviewers]
    _check(evaluate(verdicts, "h", 0, config.audit.get("quorum", "unanimous"))["approved"], "unanimous approval was not accepted")
    return f"{len(required)} required reviewer(s), quorum {config.audit.get('quorum', 'unanimous')}"


def _commands(config: ClodexConfig, tmp: Path) -> str:
    plan, audit = claude_plan_command(config), claude_audit_command(config)
    build, review = codex_exec_command(config, tmp), codex_review_command(config, tmp)
    for command in (plan, audit):
        _check(command.argv[:2] == ["claude", "-p"] and "--effort" in command.argv and "--output-format" in command.argv, f"{command.name}: unexpected argv")
    _check("--ask-for-approval" not in build.argv, "codex exec no longer accepts --ask-for-approval")
    _check(build.argv[:2] == ["codex", "exec"] and build.argv[-1] == "-", "codex-build: unexpected argv")
    _check(review.argv[:2] == ["codex", "exec"] and "read-only" in review.argv, "codex-audit must run read-only")
    if "--output-schema" in review.argv:
        _check(Path(review.argv[review.argv.index("--output-schema") + 1]).is_file(), "the audit schema file is missing")
    return "claude plan/audit and codex build/audit command lines are well-formed"


def _strict(schema: dict[str, Any], path: str = "$") -> None:
    if schema.get("type") == "object":
        _check(schema.get("additionalProperties") is False, f"{path}: additionalProperties must be false")
        _check(set(schema.get("required", [])) == set(schema.get("properties", {})), f"{path}: every property must be required")
        for name, sub in schema.get("properties", {}).items():
            _strict(sub, f"{path}.{name}")
    if schema.get("type") == "array":
        _strict(schema["items"], f"{path}[]")


def _schemas(_config: ClodexConfig, _tmp: Path) -> str:
    for name in SCHEMA_NAMES:
        _check(schema_path(name).is_file(), f"{name}: schema file missing")
        schema = load_schema(name)
        _check("$schema" not in schema, f"{name}: Claude rejects the draft 2020-12 $schema URI")
        _strict(schema, name)
    return f"{len(SCHEMA_NAMES)} schemas are strict-mode compatible"


def _output_parsing(_config: ClodexConfig, _tmp: Path) -> str:
    envelope = {"type": "result", "is_error": False, "result": '{"ok": true}'}
    _check(extract_json_object(json.dumps(envelope)) == {"ok": True}, "a Claude result envelope was not unwrapped")
    _check(extract_json_object(json.dumps({**envelope, "structured_output": {"ok": 1}})) == {"ok": 1}, "structured_output is not preferred")
    try:
        extract_json_object(json.dumps({"type": "result", "is_error": True, "result": "Not logged in"}))
    except AgentEnvelopeError:
        pass
    else:
        raise EvalFailure("an error envelope was accepted")
    return "Claude result envelopes and fenced JSON are handled"


def _state_roundtrip(_config: ClodexConfig, tmp: Path) -> str:
    store = StateStore(tmp / "state.sqlite3")
    store.create_handoff("h", "task")
    question = store.add_clarification("h", "codex", "Which one?")
    store.answer_clarification("h", question["id"], "This one", "claude")
    store.update_handoff("h", actor="claude", diff_hash="d", report={"approved": True})
    delegation = store.start_delegation("h", "implement", "go")
    store.update_delegation(delegation["id"], status="completed")
    data = store.get_handoff("h")
    _check(store.schema_version() >= 4, "state schema is out of date")
    _check(bool(data and data["delegations"] and not data["open_clarifications"]), "the handoff ledger did not round-trip")
    return f"schema v{store.schema_version()}: handoff, clarification and delegation round-trip"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)


def _workspaces(config: ClodexConfig, tmp: Path) -> str:
    repo = tmp / "ws-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "eval@example.com")
    _git(repo, "config", "user.name", "eval")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "seed")
    scratch = ClodexConfig(repo_root=repo, raw={"workspace_root": ".clodex/workspaces"})
    manager = WorkspaceManager(repo, scratch)
    workspace = manager.prepare("eval-run", "git-worktree")
    _check(workspace.path.is_dir() and workspace.is_worktree, "git worktree was not created")
    _check(manager.release(workspace.path) and not workspace.path.exists(), "git worktree was not removed")
    return "git worktrees can be created and removed"


def _native_setup(_config: ClodexConfig, tmp: Path) -> str:
    repo = tmp / "native-repo"
    repo.mkdir()
    plan = plan_native_install(repo, dry_run=True)
    errors = [item for item in plan["files"] if item["action"] == "error"]
    _check(not errors, "; ".join(item.get("error", "") for item in errors))
    _check(len(plan["files"]) >= 5, "expected CLAUDE.md, AGENTS.md, CLODEX.md, .mcp.json and .codex/config.toml")
    return f"`clodex init` would manage {len(plan['files'])} files"


def _hooks(_config: ClodexConfig, _tmp: Path) -> str:
    config = hook_config()
    _check(set(config["hooks"]) == set(HOOK_EVENTS), "hook config does not cover the hook events")
    once = merge_hooks({"theme": "dark"})
    _check(merge_hooks(once) == once and once["theme"] == "dark", "merging hooks is not idempotent or lost settings")
    _check(merge_hooks(once, remove=True) == {"theme": "dark"}, "removing hooks left something behind")
    return f"{len(HOOK_EVENTS)} hook events, merge/remove are idempotent"


SCENARIOS: list[tuple[str, Callable[[ClodexConfig, Path], str]]] = [
    ("config-valid", _config_valid),
    ("reviewers-and-quorum", _reviewers_and_quorum),
    ("command-lines", _commands),
    ("json-schemas", _schemas),
    ("agent-output-parsing", _output_parsing),
    ("state-roundtrip", _state_roundtrip),
    ("git-worktrees", _workspaces),
    ("native-setup", _native_setup),
    ("hooks", _hooks),
]


def run_local_evals(repo_root: Path | None = None) -> dict[str, Any]:
    """Run every scenario; one failing scenario never stops the others."""
    config = load_config(repo_root)
    scenarios: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="clodex-eval-") as tmp:
        for name, check in SCENARIOS:
            scratch = Path(tmp) / name
            scratch.mkdir()
            try:
                scenarios.append({"name": name, "passed": True, "detail": check(config, scratch)})
            except EvalFailure as exc:
                scenarios.append({"name": name, "passed": False, "detail": str(exc)})
            except Exception as exc:  # noqa: BLE001 - report the scenario as failed, with why
                scenarios.append({"name": name, "passed": False, "detail": f"{type(exc).__name__}: {exc}"})
    return {"passed": all(item["passed"] for item in scenarios), "scenarios": scenarios}
