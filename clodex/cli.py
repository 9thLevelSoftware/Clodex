from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .doctor import run_doctor
from .mcp_server import main as mcp_main
from .workflow import ClodexWorkflow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clodex", description="Claude Code + Codex CLI workflow orchestrator")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON where supported")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Check local Clodex, Claude Code, Codex, and git readiness")

    plan = sub.add_parser("plan", help="Run the Claude planning wave")
    plan.add_argument("task", nargs="+")
    plan.add_argument("--dry-run", action="store_true")

    build = sub.add_parser("build", help="Run plan, implementation, and dual audit")
    build.add_argument("task", nargs="+")
    build.add_argument("--dry-run", action="store_true")

    audit = sub.add_parser("audit", help="Audit current uncommitted changes")
    audit.add_argument("--dry-run", action="store_true")
    audit.add_argument("--diff", action="store_true", help="Accepted for compatibility; audit always uses git diff")

    run = sub.add_parser("run", help="Alias for build")
    run.add_argument("task", nargs="+")
    run.add_argument("--dry-run", action="store_true")

    queue = sub.add_parser("queue", help="Manage the local task ledger")
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    queue_sub.add_parser("list")
    queue_add = queue_sub.add_parser("add")
    queue_add.add_argument("id")
    queue_add.add_argument("title", nargs="+")
    queue_update = queue_sub.add_parser("update")
    queue_update.add_argument("id")
    queue_update.add_argument("status")

    sub.add_parser("status", help="Show recent tasks and runs")
    sub.add_parser("mcp-server", help="Run the Clodex MCP stdio server")

    args = parser.parse_args(argv)

    if args.command == "mcp-server":
        return mcp_main()
    if args.command == "doctor":
        exit_code, data = run_doctor()
        print_output(data, args.json)
        return exit_code

    workflow = ClodexWorkflow(Path.cwd())

    if args.command == "plan":
        result = workflow.plan(" ".join(args.task), dry_run=args.dry_run)
        print_result(result.__dict__, args.json)
        return 0
    if args.command in {"build", "run"}:
        result = workflow.build(" ".join(args.task), dry_run=args.dry_run)
        print_result(result.__dict__, args.json)
        return 0 if result.status not in {"blocked"} else 1
    if args.command == "audit":
        result = workflow.audit(dry_run=args.dry_run)
        print_result(result.__dict__, args.json)
        return 0 if result.status not in {"blocked"} else 1
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
    else:
        print(json.dumps(data, indent=2, sort_keys=True))
