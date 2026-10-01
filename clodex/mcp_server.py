from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import __version__
from .config import resolve_repo_root
from .quorum import evaluate_handoff, resolve_reviewer
from .schemas import validate as validate_schema
from .tasks import TaskManager
from .workflow import ClodexWorkflow
from .workspace import DirtyWorkspaceError, WorkspaceManager


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
        "description": "Run Claude planning, Codex implementation, and dual audit. Supports task augmentation: pass `task` in tools/call params to run it as a durable MCP task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "dry_run": {"type": "boolean"},
                "workspace": {"type": "string", "enum": ["git-worktree", "local"]},
                "approval_profile": {"type": "string", "enum": ["ci", "local", "auto_review"]},
            },
            "required": ["task"],
        },
        "execution": {"taskSupport": "optional"},
    },
    {
        "name": "clodex_audit",
        "title": "Audit with Clodex",
        "description": "Run Claude and Codex adversarial audit over current uncommitted changes, everything since a base ref, or one commit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean"},
                "base": {"type": "string", "description": "Audit everything since this ref diverged from HEAD"},
                "commit": {"type": "string", "description": "Audit a single commit"},
            },
        },
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
                "workspace": {
                    "type": "string",
                    "enum": ["none", "git-worktree", "local"],
                    "description": "Give the handoff its own isolated git worktree (or the repo itself) for Codex to work in. Default none.",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "clodex_handoff_update",
        "title": "Update Clodex handoff",
        "description": "Record native handoff phase, actor, report, status, and budget usage. A report with `approved` and a diff hash is a verdict; `reviewer_id` names which configured reviewer it is for (default: the first required reviewer of the actor's backend).",
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


SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
TASKS_MIN_VERSION = "2025-11-25"  # the Tasks utility (experimental) first appears in this revision
POLL_INTERVAL_MS = 2000
RELATED_TASK_KEY = "io.modelcontextprotocol/related-task"
IMMEDIATE_RESPONSE_KEY = "io.modelcontextprotocol/model-immediate-response"
TASK_PAGE_SIZE = 20

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

TOOL_INDEX = {tool["name"]: tool for tool in TOOLS}
# Tools that run through the durable worker and can therefore be task-augmented.
TASK_TOOLS = {name for name, tool in TOOL_INDEX.items() if (tool.get("execution") or {}).get("taskSupport") in {"optional", "required"}}
# Handled by TaskManager alone; everything else needs the workflow/state.
TASK_MANAGER_TOOLS = {"clodex_task_start", "clodex_task_get", "clodex_task_cancel"}


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class _Aborted(Exception):
    """The client cancelled the request (or disconnected); send no response."""


def error_response(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def task_status(run_status: str) -> str:
    """Map a Clodex run status onto the MCP task status enum."""
    if run_status in {"approved", "applied", "completed"}:
        return "completed"
    if run_status in {"blocked", "failed"}:
        return "failed"
    if run_status == "cancelled":
        return "cancelled"
    return "working"


def task_object(run: dict[str, Any]) -> dict[str, Any]:
    status = task_status(str(run.get("status")))
    message = f"run {run.get('status')}"
    if status == "failed":
        message = str(run.get("error") or run.get("blocked_reason") or message)
    return {
        "taskId": run["id"],
        "status": status,
        "statusMessage": message,
        "createdAt": run.get("created_at"),
        "lastUpdatedAt": run.get("updated_at"),
        "ttl": None,  # runs are kept until deleted, i.e. unlimited
        "pollInterval": POLL_INTERVAL_MS,
    }


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode("ascii")).decode("ascii")


def decode_cursor(cursor: Any) -> int:
    try:
        text = base64.urlsafe_b64decode(str(cursor).encode("ascii")).decode("ascii")
        prefix, number = text.split(":", 1)
        offset = int(number)
        if prefix != "o" or offset < 0:
            raise ValueError
        return offset
    except (ValueError, UnicodeError):
        raise RpcError(INVALID_PARAMS, "Invalid cursor") from None


def validate_arguments(tool: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    """A message for the model if the arguments are wrong, else None."""
    schema = tool["inputSchema"]
    for key in schema.get("required", []):
        if key not in arguments or arguments[key] is None:
            return f"Missing required argument: {key}"
    if tool["name"] in HANDOFF_TOOL_NAMES:
        return None  # the handoff tools report their own, more specific, type errors
    errors = validate_schema(arguments, schema)
    return "Invalid arguments: " + "; ".join(errors[:5]) if errors else None


class McpServer:
    """Line-delimited JSON-RPC over stdio.

    Requests are handled in order on the reader thread; only long-running work (a synchronous
    build/plan/audit, or waiting in tasks/result) moves to a worker thread, so it can never
    stall pings, polling or cancellation.
    """

    def __init__(self, out: Any = None, workers: int = 8):
        self.out = out or sys.stdout
        self.workers = workers
        self.version = LATEST_PROTOCOL_VERSION
        self._write_lock = threading.Lock()
        self._closing = threading.Event()
        self._inflight: dict[Any, threading.Event] = {}
        self._inflight_lock = threading.Lock()

    # ------------------------------------------------------------ transport

    def send(self, message: dict[str, Any]) -> None:
        with self._write_lock:
            self.out.write(json.dumps(message) + "\n")
            self.out.flush()

    def serve(self, lines: Any) -> int:
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="clodex-mcp") as pool:
            for line in lines:
                if line.strip():
                    self.dispatch_line(line, pool)
            self._closing.set()  # client went away: unblock anything waiting on a task
        return 0

    def dispatch_line(self, line: str, pool: ThreadPoolExecutor) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            self.send(error_response(None, PARSE_ERROR, "Parse error"))
            return
        if isinstance(message, list):
            self.send(error_response(None, INVALID_REQUEST, "Batch requests are not supported"))
            return
        if not isinstance(message, dict):
            self.send(error_response(None, INVALID_REQUEST, "Invalid request"))
            return
        method = message.get("method")
        if not isinstance(method, str):
            if "result" in message or "error" in message:
                return  # a response to a request of ours; we never send any
            self.send(error_response(message.get("id"), INVALID_REQUEST, "Invalid request: missing method"))
            return
        params = message.get("params")
        if "id" not in message:
            self.handle_notification(method, params)  # notifications never get a response
            return
        request_id = message["id"]
        if self.is_blocking(method, params):
            pool.submit(self.run_request, request_id, method, params)
        else:
            # In arrival order, so a client may pipeline create -> update -> get and rely on it.
            self.run_request(request_id, method, params)

    # Work that can take minutes. Everything else is quick and stays inline.
    LONG_TOOLS = frozenset({"clodex_plan", "clodex_build", "clodex_audit"})

    def is_blocking(self, method: str, params: Any) -> bool:
        if method == "tasks/result":
            return True  # waits for the task to finish
        if method != "tools/call" or not isinstance(params, dict) or params.get("name") not in self.LONG_TOOLS:
            return False
        arguments = params.get("arguments")
        if isinstance(arguments, dict) and arguments.get("dry_run"):
            return False
        # A task-augmented build only starts the worker and returns immediately.
        return not (isinstance(params.get("task"), dict) and self.tasks_enabled and params.get("name") in TASK_TOOLS)

    def handle_notification(self, method: str, params: Any) -> None:
        if method == "notifications/cancelled" and isinstance(params, dict):
            with self._inflight_lock:
                event = self._inflight.get(params.get("requestId"))
            if event is not None:
                event.set()

    def run_request(self, request_id: Any, method: str, params: Any) -> None:
        cancelled = threading.Event()
        with self._inflight_lock:
            self._inflight[request_id] = cancelled
        try:
            try:
                result = self.handle(method, params, cancelled)
                response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "result": result}
            except RpcError as exc:
                response = error_response(request_id, exc.code, exc.message, exc.data)
            except _Aborted:
                return
            except Exception as exc:  # noqa: BLE001 - a bad request must never take the server down
                response = error_response(request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
            if not cancelled.is_set():
                self.send(response)
        finally:
            with self._inflight_lock:
                self._inflight.pop(request_id, None)

    # ------------------------------------------------------------ methods

    @property
    def tasks_enabled(self) -> bool:
        return self.version >= TASKS_MIN_VERSION

    def handle(self, method: str, params: Any, cancelled: threading.Event) -> dict[str, Any]:
        if params is not None and not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params must be an object")
        params = params or {}
        if method == "initialize":
            return self.initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self.tools_call(params)
        if self.tasks_enabled:
            if method == "tasks/get":
                return task_object(self.task_run(params))
            if method == "tasks/result":
                return self.tasks_result(params, cancelled)
            if method == "tasks/list":
                return self.tasks_list(params)
            if method == "tasks/cancel":
                return self.tasks_cancel(params)
        raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            raise RpcError(INVALID_PARAMS, "initialize requires a protocolVersion")
        # Echo the client's version if we speak it, else answer with our newest.
        self.version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        capabilities: dict[str, Any] = {"tools": {"listChanged": False}}
        if self.tasks_enabled:
            capabilities["tasks"] = {"list": {}, "cancel": {}, "requests": {"tools": {"call": {}}}}
        return {
            "protocolVersion": self.version,
            "capabilities": capabilities,
            "serverInfo": {"name": "clodex-mcp-server", "title": "Clodex", "version": __version__},
        }

    # ------------------------------------------------------------ tools

    def tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise RpcError(INVALID_PARAMS, "tools/call requires a tool name")
        tool = TOOL_INDEX.get(name)
        if tool is None:
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return call_text("Arguments must be an object", is_error=True)
        problem = validate_arguments(tool, arguments)
        if problem:
            return call_text(problem, is_error=True)  # tool-level error so the model can correct itself
        if isinstance(params.get("task"), dict) and self.tasks_enabled:
            if name not in TASK_TOOLS:
                raise RpcError(METHOD_NOT_FOUND, f"Tool does not support task augmentation: {name}")
            return self.create_task(name, arguments)
        return tool_call(name, arguments)

    def create_task(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments.get("dry_run"):
            raise RpcError(INVALID_PARAMS, "dry_run is not supported for task-augmented calls")
        manager = TaskManager(resolve_repo_root())
        started = manager.start(
            str(arguments["task"]),
            workspace_backend=arguments.get("workspace"),
            approval_profile=arguments.get("approval_profile"),
        )
        run = manager.state.get_run(started.run_id)
        return {
            "task": task_object(run),
            "_meta": {IMMEDIATE_RESPONSE_KEY: f"Clodex build started as task {started.run_id}. Poll tasks/get; fetch the outcome with tasks/result."},
        }

    # ------------------------------------------------------------ tasks

    def task_run(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = params.get("taskId", params.get("id"))  # `id` is the pre-spec spelling
        if not isinstance(task_id, str) or not task_id:
            raise RpcError(INVALID_PARAMS, "taskId is required")
        data = TaskManager(resolve_repo_root()).get(task_id)
        # Only runs started through the worker are tasks (they are the ones with a process).
        if data is None or not data["run"].get("pid"):
            raise RpcError(INVALID_PARAMS, "Failed to retrieve task: Task not found")
        return data["run"]

    def tasks_result(self, params: dict[str, Any], cancelled: threading.Event) -> dict[str, Any]:
        run = self.task_run(params)
        manager = TaskManager(resolve_repo_root())
        while task_status(str(run["status"])) == "working":
            if cancelled.wait(0.5) or self._closing.is_set():
                raise _Aborted
            data = manager.get(str(run["id"]))
            if data is None:
                raise RpcError(INVALID_PARAMS, "Failed to retrieve task: Task not found")
            run = data["run"]
        summary: dict[str, Any] = {key: run.get(key) for key in ("id", "status", "diff_hash", "error", "blocked_reason", "artifacts_dir", "workspace_path")}
        agreement = Path(str(run.get("artifacts_dir") or "")) / "05-agreement.json"
        if agreement.is_file():
            try:
                summary["agreement"] = json.loads(agreement.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        result = call_json(summary, is_error=task_status(str(run["status"])) != "completed")
        result["_meta"] = {RELATED_TASK_KEY: {"taskId": run["id"]}}
        return result

    def tasks_list(self, params: dict[str, Any]) -> dict[str, Any]:
        offset = decode_cursor(params["cursor"]) if params.get("cursor") is not None else 0
        runs = TaskManager(resolve_repo_root()).state.list_runs(limit=TASK_PAGE_SIZE + 1, offset=offset, with_worker=True)
        page = runs[:TASK_PAGE_SIZE]
        result: dict[str, Any] = {"tasks": [task_object(run) for run in page]}
        if len(runs) > TASK_PAGE_SIZE:
            result["nextCursor"] = encode_cursor(offset + TASK_PAGE_SIZE)
        return result

    def tasks_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        run = self.task_run(params)
        status = task_status(str(run["status"]))
        if status != "working":
            raise RpcError(INVALID_PARAMS, f"Cannot cancel task: already in terminal status '{status}'")
        manager = TaskManager(resolve_repo_root())
        manager.cancel(str(run["id"]))
        return task_object(manager.state.get_run(str(run["id"])) or run)


def main() -> int:
    return McpServer().serve(sys.stdin)


def tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    root = resolve_repo_root()
    workflow = None if name in TASK_MANAGER_TOOLS else ClodexWorkflow(root)
    if name == "clodex_plan":
        result = workflow.plan(str(arguments["task"]), dry_run=bool(arguments.get("dry_run", False)))
    elif name == "clodex_build":
        result = workflow.build(
            str(arguments["task"]),
            dry_run=bool(arguments.get("dry_run", False)),
            workspace_backend=arguments.get("workspace"),
            approval_profile=arguments.get("approval_profile"),
        )
    elif name == "clodex_audit":
        try:
            result = workflow.audit(dry_run=bool(arguments.get("dry_run", False)), base=arguments.get("base"), commit=arguments.get("commit"))
        except ValueError as exc:  # bad ref etc.: let the model correct itself
            return call_text(str(exc), is_error=True)
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
        result = TaskManager(root).start(
            str(arguments["task"]),
            workspace_backend=arguments.get("workspace"),
            approval_profile=arguments.get("approval_profile"),
            dry_run=bool(arguments.get("dry_run", False)),
        )
        return {"content": [{"type": "text", "text": json.dumps({"id": result.run_id, "status": result.status, "result": {"run": result.__dict__}}, indent=2)}], "isError": False}
    elif name == "clodex_task_get":
        data = TaskManager(root).get(str(arguments["run_id"]))
        if data is None:
            return {"content": [{"type": "text", "text": f"Unknown run: {arguments['run_id']}"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(data, indent=2)}], "isError": False}
    elif name == "clodex_task_cancel":
        try:
            result = TaskManager(root).cancel(str(arguments["run_id"]))
        except ValueError as exc:
            return call_text(str(exc), is_error=True)
        return {"content": [{"type": "text", "text": json.dumps(result.__dict__, indent=2)}], "isError": False}
    elif name == "clodex_handoff_create":
        run_id = str(arguments.get("run_id") or f"native-{uuid.uuid4().hex[:12]}")
        workspace = None
        manager = WorkspaceManager(workflow.repo_root, workflow.config)
        try:
            handoff_budget = handoff_budget_argument(arguments)
            task = str(required_argument(arguments, "task"))
            backend = arguments.get("workspace")
            if backend not in (None, "none", "git-worktree", "local"):
                raise ValueError("workspace must be one of: none, git-worktree, local")
            if backend in {"git-worktree", "local"}:
                if workflow.state.get_run(run_id) is not None:
                    raise ValueError(f"run already exists: {run_id}")  # before a worktree is created for it
                workspace = manager.prepare(run_id, backend)
            run = workflow.state.create_handoff(
                run_id,
                task,
                owner=str(arguments.get("owner") or "claude"),
                phase=str(arguments.get("phase") or "planning"),
                handoff_budget=handoff_budget,
                workspace_path=str(workspace.path) if workspace else None,
            )
            if workspace is not None:
                workflow.state.add_workspace_lock(run_id, str(workspace.source_path), str(workspace.path), workspace.backend)
        except (KeyError, TypeError, ValueError, sqlite3.IntegrityError, DirtyWorkspaceError, subprocess.CalledProcessError) as exc:
            if workspace is not None and workspace.is_worktree:
                manager.release(workspace.path)  # do not leave a worktree behind for a handoff that was never created
            return call_text(expected_handoff_error(exc), is_error=True)
        return call_json({**run, **({"workspace": workspace.as_dict()} if workspace else {})})
    elif name == "clodex_handoff_update":
        try:
            report_arg = arguments.get("report") if isinstance(arguments.get("report"), dict) else None
            named = (report_arg or {}).get("reviewer_id")
            if named is not None and resolve_reviewer(named, workflow.config.reviewers) is None:
                configured = ", ".join(str(r.get("id")) for r in workflow.config.reviewers)
                raise ValueError(f"unknown reviewer_id: {named} (configured reviewers: {configured})")
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
        agreement = evaluate_handoff(data, workflow.config.reviewers, workflow.config.audit.get("quorum", "unanimous"))
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
        elif agreement["approved"]:
            try:
                approved_run = workflow.state.approve_handoff(run["id"], agreement["diff_hash"], approved_by=agreement["approved_by"])
            except (KeyError, TypeError, ValueError, sqlite3.IntegrityError) as exc:
                return call_text(expected_handoff_error(exc), is_error=True)
            decision = {
                "decision": "approved",
                "run_id": approved_run["id"],
                "diff_hash": approved_run.get("diff_hash"),
                "approved_by": agreement["approved_by"],
                "approved_reviewers": agreement["approved_reviewers"],
            }
            is_error = False
        else:
            decision = {
                "decision": "needs_fix",
                "run_id": run["id"],
                "phase": run.get("phase"),
                "budget_remaining": data["budget_remaining"],
                "next_expected_actor": data["next_expected_actor"],
                "required_pending": agreement["required_pending"],
                "approved_reviewers": agreement["approved_reviewers"],
                "quorum": agreement["quorum"],
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


def expected_handoff_error(exc: Exception) -> str:
    if isinstance(exc, KeyError):
        key = exc.args[0] if exc.args else "argument"
        return f"Missing required argument: {key}"
    if isinstance(exc, subprocess.CalledProcessError):
        return (exc.stderr or "").strip() or str(exc)
    return str(exc)


if __name__ == "__main__":
    raise SystemExit(main())
