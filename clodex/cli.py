from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .config import ConfigError, resolve_repo_root
from .doctor import run_doctor
from .evals import run_local_evals
from .hooks import hook_config, ingest_hook_event
from .mcp_server import main as mcp_main
from .migrate import migrate_contract
from .models import ModelRetiredError
from .native import (
    ManagedBlockError,
    apply_native_install,
    native_doctor,
    native_status,
    plan_native_install,
)
from .tasks import Heartbeat, TaskManager
from .workflow import ClodexWorkflow


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except (ConfigError, ModelRetiredError, ValueError) as exc:
        if os.environ.get("CLODEX_DEBUG"):
            raise
        # Deliberate user-facing errors (unknown run id, bad ref, bad config): one line, no traceback.
        print(f"clodex: error: {exc}", file=sys.stderr)
        return EXIT_USAGE


def handle_migrate(args: argparse.Namespace) -> int:
    contract = resolve_repo_root() / "CLODEX.md"
    if not contract.is_file():
        print_output({"ok": False, "error": f"{contract} not found"}, args.json)
        return 1
    with contract.open(encoding="utf-8", newline="") as handle:  # keep CRLF endings as they are
        text = handle.read()
    migrated, changes = migrate_contract(text, split_claude=args.split_claude)
    written = bool(changes) and not args.dry_run
    if written:
        with contract.open("w", encoding="utf-8", newline="") as handle:
            handle.write(migrated)
    print_output({"ok": True, "file": str(contract), "changes": changes, "dry_run": args.dry_run, "written": written}, args.json)
    return 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clodex", description="Claude Code + Codex CLI workflow orchestrator")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON where supported")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Check local Clodex, Claude Code, Codex, and git readiness")
    doctor.add_argument("--strict", action="store_true", help="Treat warnings (e.g. a model retiring soon) as failures")

    init = sub.add_parser("init", help="Install native Claude Code and Codex collaboration instructions")
    init.add_argument("--global", action="store_true", dest="global_mode")
    init.add_argument("--dry-run", action="store_true")
    init.add_argument("--no-mcp-config", action="store_true")
    init.add_argument("--force", action="store_true")
    init.add_argument("--migrate", action="store_true", help="Update CLODEX.md settings that no longer work (retired models, unsupported efforts)")
    init.add_argument("--split-claude", action="store_true", help="With --migrate: split flat claude.model/effort into plan and audit roles")

    native = sub.add_parser("native", help="Inspect native Clodex setup")
    native_sub = native.add_subparsers(dest="native_command", required=True)
    native_status_cmd = native_sub.add_parser("status")
    native_status_cmd.add_argument("--global", action="store_true", dest="global_mode")
    native_status_cmd.add_argument("--no-mcp-config", action="store_true")
    native_status_cmd.add_argument("--force", action="store_true")
    native_doctor_cmd = native_sub.add_parser("doctor")
    native_doctor_cmd.add_argument("--global", action="store_true", dest="global_mode")
    native_doctor_cmd.add_argument("--no-mcp-config", action="store_true")
    native_doctor_cmd.add_argument("--force", action="store_true")

    plan = sub.add_parser("plan", help="Run the Claude planning wave")
    plan.add_argument("task", nargs="+")
    plan.add_argument("--dry-run", action="store_true")

    build = sub.add_parser("build", help="Run plan, implementation, and dual audit")
    add_build_args(build)

    audit = sub.add_parser("audit", help="Audit current uncommitted changes, everything since a base ref, or one commit")
    audit.add_argument("--dry-run", action="store_true")
    audit_target = audit.add_mutually_exclusive_group()
    audit_target.add_argument("--diff", action="store_true", help="Audit the current uncommitted diff (the default)")
    audit_target.add_argument("--base", metavar="REF", help="Audit everything since this ref diverged from HEAD, including uncommitted edits")
    audit_target.add_argument("--commit", metavar="SHA", help="Audit the changes introduced by one commit")

    run = sub.add_parser("run", help="Alias for build")
    add_build_args(run)

    task = sub.add_parser("task", help="Manage durable Clodex runs")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_start = task_sub.add_parser("start")
    task_start.add_argument("task", nargs="+")
    task_start.add_argument("--workspace", choices=["git-worktree", "local"])
    task_start.add_argument("--approval-profile", choices=["ci", "local", "auto_review"])
    task_start.add_argument("--dry-run", action="store_true")
    task_get = task_sub.add_parser("get")
    task_get.add_argument("run_id")
    task_cancel = task_sub.add_parser("cancel")
    task_cancel.add_argument("run_id")
    task_sub.add_parser("list")
    task_worker = task_sub.add_parser("worker")
    task_worker.add_argument("run_id")
    task_worker.add_argument("--workspace", choices=["git-worktree", "local"])
    task_worker.add_argument("--approval-profile", choices=["ci", "local", "auto_review"])

    queue = sub.add_parser("queue", help="Manage the local task ledger")
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    queue_sub.add_parser("list")
    queue_add = queue_sub.add_parser("add")
    queue_add.add_argument("id")
    queue_add.add_argument("title", nargs="+")
    queue_update = queue_sub.add_parser("update")
    queue_update.add_argument("id")
    queue_update.add_argument("status")

    apply_cmd = sub.add_parser("apply", help="Apply an approved worktree run patch")
    apply_cmd.add_argument("run_id")
    apply_cmd.add_argument("--check", action="store_true")
    apply_cmd.add_argument("--force", action="store_true", help="Apply even if the run is not approved or its patch changed")

    clean_cmd = sub.add_parser("clean", help="Remove the kept git worktree of a finished run")
    clean_cmd.add_argument("run_id")

    trace = sub.add_parser("trace", help="Inspect run traces")
    trace_sub = trace.add_subparsers(dest="trace_command", required=True)
    trace_export = trace_sub.add_parser("export")
    trace_export.add_argument("run_id")
    trace_export.add_argument("--format", choices=["jsonl"], default="jsonl")

    eval_cmd = sub.add_parser("eval", help="Run local Clodex harness evals")
    eval_sub = eval_cmd.add_subparsers(dest="eval_command", required=True)
    eval_sub.add_parser("run")

    hooks = sub.add_parser("hooks", help="Print or ingest Claude Code hook events")
    hooks_sub = hooks.add_subparsers(dest="hooks_command", required=True)
    hooks_sub.add_parser("print")
    hooks_install = hooks_sub.add_parser("install")
    hooks_install.add_argument("--dry-run", action="store_true", required=True)
    hooks_ingest = hooks_sub.add_parser("ingest")
    hooks_ingest.add_argument("--run-id", default="manual-hook-event")

    sub.add_parser("status", help="Show recent tasks and runs")
    sub.add_parser("mcp-server", help="Run the Clodex MCP stdio server")

    args = parser.parse_args(argv)

    if args.command == "mcp-server":
        return mcp_main()
    if args.command == "doctor":
        exit_code, data = run_doctor(resolve_repo_root(), strict=args.strict)
        print_output(data, args.json)
        return exit_code
    if args.command == "init" and args.migrate:
        return handle_migrate(args)
    if args.command == "init":
        try:
            exit_code = 0
            if args.dry_run:
                data = plan_native_install(
                    Path.cwd(),
                    dry_run=True,
                    global_mode=args.global_mode,
                    no_mcp_config=args.no_mcp_config,
                    force=args.force,
                )
                has_errors = any(item["action"] == "error" for item in data["files"])
                exit_code = 1 if has_errors else 0
            else:
                data = apply_native_install(
                    Path.cwd(),
                    global_mode=args.global_mode,
                    no_mcp_config=args.no_mcp_config,
                    force=args.force,
                )
        except ManagedBlockError as exc:
            print_output({"ok": False, "error": str(exc)}, args.json)
            return 1
        print_output(data, args.json)
        return exit_code
    if args.command == "native":
        if args.native_command == "status":
            data = native_status(
                Path.cwd(),
                global_mode=args.global_mode,
                no_mcp_config=args.no_mcp_config,
                force=args.force,
            )
            print_output(data, args.json)
            return 0 if data["ok"] else 1
        if args.native_command == "doctor":
            exit_code, data = native_doctor(
                Path.cwd(),
                global_mode=args.global_mode,
                no_mcp_config=args.no_mcp_config,
                force=args.force,
            )
            print_output(data, args.json)
            return exit_code

    repo = resolve_repo_root()
    workflow = ClodexWorkflow(repo)

    if args.command == "plan":
        result = workflow.plan(" ".join(args.task), dry_run=args.dry_run)
        print_result(result.__dict__, args.json)
        return exit_code_for(result.status)
    if args.command in {"build", "run"}:
        result = workflow.build(
            " ".join(args.task),
            dry_run=args.dry_run,
            workspace_backend=args.workspace,
            approval_profile=args.approval_profile,
            apply_changes=args.apply_changes,
        )
        print_result(result.__dict__, args.json)
        return exit_code_for(result.status)
    if args.command == "audit":
        result = workflow.audit(dry_run=args.dry_run, base=args.base, commit=args.commit)
        print_result(result.__dict__, args.json)
        return exit_code_for(result.status)
    if args.command == "task":
        return handle_task(args, workflow, args.json)
    if args.command == "apply":
        result = workflow.apply_run(args.run_id, check=args.check, force=args.force)
        print_result(result.__dict__, args.json)
        return exit_code_for(result.status)
    if args.command == "clean":
        result = workflow.clean_run(args.run_id)
        print_result(result.__dict__, args.json)
        return exit_code_for(result.status)
    if args.command == "trace":
        return handle_trace(args, workflow, args.json)
    if args.command == "eval":
        data = run_local_evals(repo)
        print_output(data, args.json)
        return 0 if data["passed"] else 1
    if args.command == "hooks":
        return handle_hooks(args, args.json)
    if args.command == "status":
        print_output({"tasks": workflow.state.list_tasks(), "runs": workflow.state.list_runs()}, args.json)
        return 0
    if args.command == "queue":
        if args.queue_command == "list":
            print_output({"tasks": workflow.state.list_tasks()}, args.json)
            return 0
        if args.queue_command == "add":
            workflow.state.upsert_task(args.id, " ".join(args.title), "todo")
            print_output({"created": args.id}, args.json)
            return 0
        if args.queue_command == "update":
            workflow.state.update_task(args.id, args.status)
            print_output({"updated": args.id, "status": args.status}, args.json)
            return 0
    parser.error("unknown command")
    return 2


def add_build_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("task", nargs="+")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workspace", choices=["git-worktree", "local"])
    parser.add_argument("--approval-profile", choices=["ci", "local", "auto_review"])
    parser.add_argument("--apply", action="store_true", dest="apply_changes")


def handle_task(args: argparse.Namespace, workflow: ClodexWorkflow, as_json: bool) -> int:
    manager = TaskManager(resolve_repo_root())
    if args.task_command == "start":
        result = manager.start(" ".join(args.task), workspace_backend=args.workspace, approval_profile=args.approval_profile, dry_run=args.dry_run)
        print_result(result.__dict__, as_json)
        return 0
    if args.task_command == "get":
        data = manager.get(args.run_id)
        if data is None:
            print_output({"error": f"unknown run: {args.run_id}"}, as_json)
            return 1
        print_output(data, as_json)
        return 0
    if args.task_command == "cancel":
        result = manager.cancel(args.run_id)
        print_result(result.__dict__, as_json)
        return 0
    if args.task_command == "list":
        print_output(manager.list(), as_json)
        return 0
    if args.task_command == "worker":
        with Heartbeat(workflow.state, args.run_id):
            result = workflow.run_existing(args.run_id, workspace_backend=args.workspace, approval_profile=args.approval_profile)
        print_result(result.__dict__, as_json)
        return exit_code_for(result.status)
    return 2


def handle_trace(args: argparse.Namespace, workflow: ClodexWorkflow, as_json: bool) -> int:
    if args.trace_command == "export":
        run = workflow.state.get_run(args.run_id)
        artifacts_dir = Path(str((run or {}).get("artifacts_dir") or workflow.config.runs_root / args.run_id))
        trace_path = artifacts_dir / "trace.jsonl"
        if not trace_path.exists():
            print_output({"error": f"trace not found: {trace_path}"}, as_json)
            return 1
        print(trace_path.read_text(encoding="utf-8"), end="")
        return 0
    return 2


def handle_hooks(args: argparse.Namespace, as_json: bool) -> int:
    if args.hooks_command == "print":
        print_output(hook_config(resolve_repo_root()), as_json)
        return 0
    if args.hooks_command == "install":
        print_output({"dry_run": True, "config": hook_config(resolve_repo_root())}, as_json)
        return 0
    if args.hooks_command == "ingest":
        payload = json.loads(sys.stdin.read() or "{}")
        print_output(ingest_hook_event(resolve_repo_root(), args.run_id, payload), as_json)
        return 0
    return 2


# Exit codes: 0 success, 1 the work was refused or rejected (blocked, nothing applied),
# 2 usage or configuration error, 3 failed unexpectedly, 4 cancelled.
EXIT_BLOCKED = 1
EXIT_USAGE = 2
EXIT_FAILED = 3
EXIT_CANCELLED = 4
_BLOCKED_STATUSES = {"blocked", "apply-refused", "apply-failed", "apply-check-failed", "clean-refused"}


def exit_code_for(status: str) -> int:
    if status in _BLOCKED_STATUSES:
        return EXIT_BLOCKED
    if status == "failed":
        return EXIT_FAILED
    if status == "cancelled":
        return EXIT_CANCELLED
    return 0


def print_result(data: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return
    print(f"status: {data['status']}")
    if data.get("run_id"):
        print(f"run: {data['run_id']}")
    if data.get("task_id"):
        print(f"task: {data['task_id']}")
    if data.get("artifacts_dir"):
        print(f"artifacts: {data['artifacts_dir']}")
    if data.get("data"):
        print(json.dumps(data["data"], indent=2, sort_keys=True))


def print_output(data: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
    elif "diagnostics" in data and "python" in data:
        print("\n".join(format_doctor(data)))
    else:
        print("\n".join(format_human(data)) or "(nothing to show)")


def _scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def format_human(data: Any, indent: int = 0) -> list[str]:
    """Plain-text rendering of nested dicts/lists for people (machine consumers use --json)."""
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{pad}{key}:")
                lines.extend(format_human(value, indent + 1))
            else:
                lines.append(f"{pad}{key}: {_scalar(value) if not isinstance(value, (dict, list)) else '(none)'}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                head = next((str(item[key]) for key in ("id", "name", "run_id", "taskId") if key in item), "")
                lines.append(f"{pad}- {head}".rstrip())
                lines.extend(format_human({k: v for k, v in item.items() if str(v) != head}, indent + 1))
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    else:
        lines.append(f"{pad}{_scalar(data)}")
    return lines


def format_doctor(data: dict[str, Any]) -> list[str]:
    def mark(ok: bool) -> str:
        return "[ok]  " if ok else "[FAIL]"

    lines = [f"{mark(data['python']['ok'])} python {data['python']['version']}", f"{mark(data['contract']['ok'])} CLODEX.md ({data['contract']['path']})"]
    for name in ("git", "claude", "codex"):
        info = data[name]
        lines.append(f"{mark(info['ok'])} {name}: {info.get('version') or 'not found'}")
    for name, auth in (data.get("auth") or {}).items():
        lines.append(f"{mark(auth['status'] != 'logged-out')} {name} login: {auth['status']}")
    for item in data["diagnostics"]:
        tag = "[FAIL]" if item["level"] == "error" else "[warn]"
        lines.append(f"{tag} {item['where']}: {item['message']}")
        if item.get("fix"):
            lines.append(f"       fix: {item['fix']}")
    lines.append("")
    lines.append("Everything looks good." if data["ok"] else "Problems found; see above.")
    return lines
