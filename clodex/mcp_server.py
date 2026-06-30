from __future__ import annotations

import json
import sys
from typing import Any

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
]


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
                "capabilities": {"tools": {"listChanged": False}},
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
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
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
    else:
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(result.__dict__, indent=2)}], "isError": result.status == "blocked"}


if __name__ == "__main__":
    raise SystemExit(main())
