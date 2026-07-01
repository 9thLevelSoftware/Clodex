from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from typing import Any

from .tasks import TaskManager
from .workflow import ClodexWorkflow


TOOLS = [
    {
        "name": "clodex_plan",
        "title": "Plan with Clodex",
        "description": "Run the Claude planning wave for a task.",
        "inputSchema": {"type": "object", "properties": {"task": {"type": "string"}, "dry_run": {"type": "boolean"}}, "required": ["task"]},
    },
    {
        "name": "clodex_build",
        "title": "Build with Clodex",
        "description": "Run Claude planning, Codex implementation, and dual audit.",
        "inputSchema": {"type": "object", "properties": {"task": {"type": "string"}, "dry_run": {"type": "boolean"}}, "required": ["task"]},
    },
    {
        "name": "clodex_audit",
        "title": "Audit with Clodex",
        "description": "Run Claude and Codex adversarial audit over current changes.",
        "inputSchema": {"type": "object", "properties": {"dry_run": {"type": "boolean"}}},
    },
    {
        "name": "clodex_status",
        "title": "Clodex status",
        "description": "Show recent tasks and runs.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "clodex_task_create",
        "title": "Create Clodex task",
        "description": "Create or update a task in the local Clodex ledger.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "title": {"type": "string"}}, "required": ["id", "title"]},
    },
    {
        "name": "clodex_task_update",
        "title": "Update Clodex task",
        "description": "Update a local Clodex task status.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}, "required": ["id", "status"]},
    },
    {
        "name": "clodex_task_start",
        "title": "Start async Clodex task",
        "description": "Start a durable Clodex build and return a task handle.",
        "inputSchema": {
            "type": "object",
            "properties": {"task": {"type": "string"}, "workspace": {"type": "string"}, "approval_profile": {"type": "string"}, "dry_run": {"type": "boolean"}},
            "required": ["task"],
        },
    },
    {
        "name": "clodex_task_get",
        "title": "Get async Clodex task",
        "description": "Get a durable Clodex run by run id.",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
    },
    {
        "name": "clodex_task_cancel",
        "title": "Cancel async Clodex task",
        "description": "Request cancellation of a durable Clodex run.",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
    },
    {
        "name": "clodex_handoff_create",
        "title": "Create Clodex handoff",
        "description": "Create a native Claude/Codex handoff run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "task": {"type": "string"},
                "owner": {"type": "string"},
                "phase": {"type": "string"},
                "handoff_budget": {"type": "integer"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "clodex_handoff_update",
        "title": "Update Clodex handoff",
        "description": "Record native handoff phase, actor, report, status, and budget usage.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "phase": {"type": "string"},
                "actor": {"type": "string"},
                "owner": {"type": "string"},
                "increment_handoff": {"type": "boolean"},
                "report": {"type": "object"},
                "status": {"type": "string"},
                "blocked_reason": {"type": "string"},
                "diff_hash": {"type": "string"},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "clodex_handoff_get",
        "title": "Get Clodex handoff",
        "description": "Read native handoff state, events, artifacts, budget, and next actor.",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
    },
    {
        "name": "clodex_handoff_decide",
        "title": "Decide Clodex handoff",
        "description": "Evaluate whether a native handoff is approved, needs fixes, or blocked.",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
    },
]

HANDOFF_TOOL_NAMES = {
    "clodex_handoff_create",
    "clodex_handoff_update",
    "clodex_handoff_get",
    "clodex_handoff_decide",
}


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            response = handle_request(request)
        except Exception as exc:  # MCP servers must not crash on bad client input.
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(exc)}}
        if response is not None:
            print(json.dumps(response), flush=True)
    return 0


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {"listChanged": False}, "tasks": {}},
                "serverInfo": {"name": "clodex-mcp-server", "version": "0.1.0"},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        return {"jsonrpc": "2.0", "id": request_id, "result": tool_call(name, arguments)}
    if method == "tasks/get":
        params = request.get("params") or {}
        run_id = str(params.get("id") or params.get("taskId") or "")
        data = TaskManager().get(run_id)
        if data is None:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32004, "message": f"Unknown task: {run_id}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": task_result(data)}
    if method == "tasks/cancel":
        params = request.get("params") or {}
        run_id = str(params.get("id") or params.get("taskId") or "")
        try:
            result = TaskManager().cancel(run_id)
        except ValueError as exc:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32004, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": request_id, "result": task_result({"run": result.__dict__})}
    if method == "tasks/update":
        params = request.get("params") or {}
        run_id = str(params.get("id") or params.get("taskId") or "")
        data = TaskManager().get(run_id)
        if data is None:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32004, "message": f"Unknown task: {run_id}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": task_result(data)}
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name in HANDOFF_TOOL_NAMES and not isinstance(arguments, dict):
        return call_text("Arguments must be an object", is_error=True)

    workflow = ClodexWorkflow()
    if name == "clodex_plan":
        result = workflow.plan(str(arguments["task"]), dry_run=bool(arguments.get("dry_run", False)))
    elif name == "clodex_build":
        result = workflow.build(str(arguments["task"]), dry_run=bool(arguments.get("dry_run", False)))
    elif name == "clodex_audit":
        result = workflow.audit(dry_run=bool(arguments.get("dry_run", False)))
    elif name == "clodex_status":
        data = {"tasks": workflow.state.list_tasks(), "runs": workflow.state.list_runs()}
        return {"content": [{"type": "text", "text": json.dumps(data, indent=2)}], "isError": False}
    elif name == "clodex_task_create":
        workflow.state.upsert_task(str(arguments["id"]), str(arguments["title"]), "todo")
        return {"content": [{"type": "text", "text": "task created"}], "isError": False}
    elif name == "clodex_task_update":
        workflow.state.update_task(str(arguments["id"]), str(arguments["status"]))
        return {"content": [{"type": "text", "text": "task updated"}], "isError": False}
    elif name == "clodex_task_start":
        result = TaskManager().start(
            str(arguments["task"]),
            workspace_backend=arguments.get("workspace"),
            approval_profile=arguments.get("approval_profile"),
            dry_run=bool(arguments.get("dry_run", False)),
        )
        return {"content": [{"type": "text", "text": json.dumps(task_result({"run": result.__dict__}), indent=2)}], "isError": False}
    elif name == "clodex_task_get":
        data = TaskManager().get(str(arguments["run_id"]))
        if data is None:
            return {"content": [{"type": "text", "text": f"Unknown run: {arguments['run_id']}"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(data, indent=2)}], "isError": False}
    elif name == "clodex_task_cancel":
        result = TaskManager().cancel(str(arguments["run_id"]))
        return {"content": [{"type": "text", "text": json.dumps(result.__dict__, indent=2)}], "isError": False}
    elif name == "clodex_handoff_create":
        try:
            handoff_budget = handoff_budget_argument(arguments)
            run = workflow.state.create_handoff(
                str(arguments.get("run_id") or f"native-{uuid.uuid4().hex[:12]}"),
                str(required_argument(arguments, "task")),
                owner=str(arguments.get("owner") or "claude"),
                phase=str(arguments.get("phase") or "planning"),
                handoff_budget=handoff_budget,
            )
        except (KeyError, TypeError, ValueError, sqlite3.IntegrityError) as exc:
            return call_text(expected_handoff_error(exc), is_error=True)
        return call_json(run)
    elif name == "clodex_handoff_update":
        try:
            run = workflow.state.update_handoff(
                str(required_argument(arguments, "run_id")),
                phase=arguments.get("phase"),
                actor=arguments.get("actor"),
                owner=arguments.get("owner"),
                increment_handoff=bool(arguments.get("increment_handoff", False)),
                report=arguments.get("report") if isinstance(arguments.get("report"), dict) else None,
                status=arguments.get("status"),
                blocked_reason=arguments.get("blocked_reason"),
                diff_hash=arguments.get("diff_hash"),
            )
        except (KeyError, TypeError, ValueError, sqlite3.IntegrityError) as exc:
            return call_text(expected_handoff_error(exc), is_error=True)
        return call_json(run, is_error=run.get("status") == "blocked")
    elif name == "clodex_handoff_get":
        try:
            run_id = str(required_argument(arguments, "run_id"))
            data = workflow.state.get_handoff(run_id)
        except (KeyError, TypeError, ValueError, sqlite3.IntegrityError) as exc:
            return call_text(expected_handoff_error(exc), is_error=True)
        if data is None:
            return call_text(f"Unknown run: {run_id}", is_error=True)
        return call_json(data)
    elif name == "clodex_handoff_decide":
        try:
            run_id = str(required_argument(arguments, "run_id"))
            data = workflow.state.get_handoff(run_id)
        except (KeyError, TypeError, ValueError, sqlite3.IntegrityError) as exc:
            return call_text(expected_handoff_error(exc), is_error=True)
        if data is None:
            return call_text(f"Unknown run: {run_id}", is_error=True)

        run = data["run"]
        status = run.get("status")
        if status == "approved":
            decision = {"decision": "approved", "run_id": run["id"], "diff_hash": run.get("diff_hash")}
            is_error = False
        elif status in {"blocked", "failed", "cancelled", "completed", "applied"}:
            decision = {
                "decision": "blocked",
                "run_id": run["id"],
                "status": status,
                "blocked_reason": run.get("blocked_reason") or run.get("error"),
            }
            is_error = True
        elif agreement := handoff_agreement(data):
            try:
                approved_run = workflow.state.approve_handoff(run["id"], agreement["diff_hash"], approved_by=agreement["approved_by"])
            except (KeyError, TypeError, ValueError, sqlite3.IntegrityError) as exc:
                return call_text(expected_handoff_error(exc), is_error=True)
            decision = {
                "decision": "approved",
                "run_id": approved_run["id"],
                "diff_hash": approved_run.get("diff_hash"),
                "approved_by": agreement["approved_by"],
            }
            is_error = False
        else:
            decision = {
                "decision": "needs_fix",
                "run_id": run["id"],
                "phase": run.get("phase"),
                "budget_remaining": data["budget_remaining"],
                "next_expected_actor": data["next_expected_actor"],
            }
            is_error = False
        workflow.state.add_event(run["id"], "handoff.decide", decision)
        return call_json(decision, is_error=is_error)
    else:
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(result.__dict__, indent=2)}], "isError": result.status == "blocked"}


def call_text(text: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_json(data: Any, is_error: bool = False) -> dict[str, Any]:
    return call_text(json.dumps(data, indent=2), is_error=is_error)


def required_argument(arguments: dict[str, Any], key: str) -> Any:
    if key not in arguments or arguments[key] is None:
        raise KeyError(key)
    return arguments[key]


def handoff_budget_argument(arguments: dict[str, Any]) -> int:
    if "handoff_budget" not in arguments or arguments["handoff_budget"] is None:
        return 6
    value = arguments["handoff_budget"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("handoff_budget must be an integer")
    return value


def handoff_agreement(data: dict[str, Any]) -> dict[str, Any] | None:
    approvals: dict[str, set[str]] = {}
    for event in data.get("events") or []:
        if event.get("event") != "handoff.update":
            continue
        event_data = event.get("data")
        if not isinstance(event_data, dict):
            continue
        report = event_data.get("report")
        if not isinstance(report, dict) or report.get("approved") is not True:
            continue
        actor = normalized_actor(event_data.get("actor") or report.get("actor"))
        diff_hash = normalized_diff_hash(event_data.get("diff_hash") or report.get("diff_hash"))
        if actor is None or diff_hash is None:
            continue
        approvals.setdefault(diff_hash, set()).add(actor)

    for diff_hash, actors in approvals.items():
        if {"claude", "codex"}.issubset(actors):
            return {"diff_hash": diff_hash, "approved_by": sorted(actors)}
    return None


def normalized_actor(value: Any) -> str | None:
    if value is None:
        return None
    actor = str(value).strip().lower()
    return actor if actor in {"claude", "codex"} else None


def normalized_diff_hash(value: Any) -> str | None:
    if value is None:
        return None
    diff_hash = str(value).strip()
    return diff_hash or None


def expected_handoff_error(exc: Exception) -> str:
    if isinstance(exc, KeyError):
        key = exc.args[0] if exc.args else "argument"
        return f"Missing required argument: {key}"
    return str(exc)


def task_result(data: dict[str, Any]) -> dict[str, Any]:
    run = data.get("run") or {}
    run_id = run.get("id") or run.get("run_id")
    status = run.get("status", "unknown")
    return {
        "id": run_id,
        "status": status,
        "result": data,
    }


if __name__ == "__main__":
    raise SystemExit(main())
