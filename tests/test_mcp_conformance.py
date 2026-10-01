"""Tests: MCP protocol conformance (lifecycle, errors, tools, spec Tasks utility)."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

from clodex import __version__
from clodex import mcp_server
from clodex.mcp_server import RELATED_TASK_KEY, McpServer, task_status
from clodex.tasks import TaskManager
from tests.support import FakeCliPath, TempRepo


class Sink:
    """Collects the server's output, one JSON message per line."""

    def __init__(self):
        self.messages: list[dict] = []
        self.cond = threading.Condition()

    def write(self, text: str) -> None:
        for line in text.splitlines():
            if line.strip():
                with self.cond:
                    self.messages.append(json.loads(line))
                    self.cond.notify_all()

    def flush(self) -> None:
        pass

    def wait_for(self, request_id, timeout: float = 30.0) -> dict | None:
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for message in self.messages:
                    if message.get("id") == request_id:
                        return message
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.cond.wait(remaining)


class LiveServer:
    """An McpServer running on a thread, fed through a queue so tests can interleave input and output."""

    def __init__(self, protocol: str = "2025-11-25", initialize: bool = True):
        self.sink = Sink()
        self.feed: queue.Queue = queue.Queue()
        self.server = McpServer(out=self.sink)
        self.thread = threading.Thread(target=self.server.serve, args=(iter(self.feed.get, None),), daemon=True)
        self.thread.start()
        self._next_id = 100
        if initialize:
            response = self.call("initialize", {"protocolVersion": protocol, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
            assert "result" in response, response

    def send_raw(self, text: str) -> None:
        self.feed.put(text)

    def send(self, method: str, params=None, request_id=None, notification: bool = False) -> int | None:
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notification:
            request_id = self._next_id if request_id is None else request_id
            self._next_id = max(self._next_id, request_id) + 1
            message["id"] = request_id
        self.feed.put(json.dumps(message))
        return request_id

    def call(self, method: str, params=None, timeout: float = 30.0) -> dict:
        request_id = self.send(method, params)
        response = self.sink.wait_for(request_id, timeout)
        assert response is not None, f"no response to {method}"
        return response

    def tool(self, name: str, arguments=None, **extra) -> dict:
        params = {"name": name, "arguments": arguments or {}, **extra}
        return self.call("tools/call", params)

    def silence(self, seconds: float = 0.4) -> list[dict]:
        before = len(self.sink.messages)
        time.sleep(seconds)
        return self.sink.messages[before:]

    def close(self, timeout: float = 30.0) -> bool:
        self.feed.put(None)
        self.thread.join(timeout)
        return not self.thread.is_alive()


class ServerCase(unittest.TestCase):
    def setUp(self):
        # Task workers are detached processes whose Popen handles are never reaped here.
        warning_context = warnings.catch_warnings()
        warning_context.__enter__()
        self.addCleanup(warning_context.__exit__, None, None, None)
        warnings.simplefilter("ignore", ResourceWarning)
        self.repo_cm = TempRepo()
        self.repo = self.repo_cm.__enter__()
        self.addCleanup(self.repo_cm.__exit__, None, None, None)
        env = mock.patch.dict(os.environ, {"CLODEX_REPO_ROOT": str(self.repo)})
        env.start()
        self.addCleanup(env.stop)
        self.servers: list[LiveServer] = []
        self.addCleanup(self.cleanup_servers)

    def start(self, **kwargs) -> LiveServer:
        live = LiveServer(**kwargs)
        self.servers.append(live)
        return live

    def cleanup_servers(self):
        for live in self.servers:
            live.close(timeout=60)
        manager = TaskManager(self.repo)
        for run in manager.state.list_runs(limit=100):
            if str(run["status"]) in {"queued", "running", "planning", "auditing", "needs-fix", "cancel_requested", "handoff"}:
                try:
                    manager.cancel(str(run["id"]))
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass


class LifecycleTests(ServerCase):
    def test_version_negotiation_and_capabilities(self):
        live = self.start(initialize=False)
        result = live.call("initialize", {"protocolVersion": "2025-11-25"})["result"]
        self.assertEqual(result["protocolVersion"], "2025-11-25")
        self.assertEqual(result["capabilities"]["tasks"], {"list": {}, "cancel": {}, "requests": {"tools": {"call": {}}}})
        self.assertEqual(result["serverInfo"]["version"], __version__)
        old = self.start(initialize=False).call("initialize", {"protocolVersion": "2025-06-18"})["result"]
        self.assertEqual(old["protocolVersion"], "2025-06-18")
        self.assertNotIn("tasks", old["capabilities"], "tasks only exist from 2025-11-25")
        unknown = self.start(initialize=False).call("initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertEqual(unknown["protocolVersion"], "2025-11-25", "answer with our newest")
        missing = self.start(initialize=False).call("initialize", {})
        self.assertEqual(missing["error"]["code"], -32602)

    def test_ping_and_unknown_method_keep_the_request_id(self):
        live = self.start()
        self.assertEqual(live.call("ping")["result"], {})
        response = live.call("nope/nothing")
        self.assertEqual((response["id"], response["error"]["code"]), (live._next_id - 1, -32601))

    def test_malformed_input_gets_spec_errors(self):
        live = self.start()
        live.send_raw("{not json")
        live.send_raw("[1, 2]")
        live.send_raw('"just a string"')
        live.send_raw(json.dumps({"jsonrpc": "2.0", "id": 77}))
        deadline = time.monotonic() + 10
        while len(live.sink.messages) < 5 and time.monotonic() < deadline:  # +1 for initialize
            time.sleep(0.05)
        codes = [(m.get("id"), m["error"]["code"]) for m in live.sink.messages if "error" in m]
        self.assertEqual(codes, [(None, -32700), (None, -32600), (None, -32600), (77, -32600)])

    def test_notifications_and_stray_responses_get_no_reply(self):
        live = self.start()
        live.send("notifications/initialized", notification=True)
        live.send("made/up", {"x": 1}, notification=True)
        live.send("notifications/cancelled", {"requestId": 12345}, notification=True)
        live.send_raw(json.dumps({"jsonrpc": "2.0", "id": 5, "result": {}}))  # a reply to a request we never sent
        self.assertEqual(live.silence(), [])

    def test_params_must_be_an_object_and_errors_keep_ids(self):
        live = self.start()
        response = live.call("tools/call", "oops")
        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(live.call("tools/call", {"arguments": {}})["error"]["code"], -32602)

    def test_unexpected_exceptions_become_internal_errors_with_the_id(self):
        live = self.start()
        with mock.patch.object(mcp_server, "tool_call", side_effect=RuntimeError("boom")):
            response = live.tool("clodex_status")
        self.assertEqual(response["error"]["code"], -32603)
        self.assertIn("boom", response["error"]["message"])
        self.assertEqual(live.call("ping")["result"], {}, "the server survives")


class ToolTests(ServerCase):
    def test_tool_list_marks_only_build_as_task_capable(self):
        live = self.start()
        tools = {tool["name"]: tool for tool in live.call("tools/list")["result"]["tools"]}
        self.assertEqual(tools["clodex_build"]["execution"], {"taskSupport": "optional"})
        self.assertEqual([name for name, tool in tools.items() if "execution" in tool], ["clodex_build"])
        self.assertIn("workspace", tools["clodex_build"]["inputSchema"]["properties"])
        self.assertIn("approval_profile", tools["clodex_build"]["inputSchema"]["properties"])

    def test_unknown_tool_is_a_protocol_error_and_touches_no_state(self):
        live = self.start()
        response = live.tool("clodex_nope")
        self.assertEqual(response["error"]["code"], -32602)
        self.assertFalse((self.repo / ".clodex" / "state.sqlite3").exists())

    def test_bad_arguments_are_tool_errors_the_model_can_fix(self):
        live = self.start()
        for name, arguments, expected in (
            ("clodex_build", {}, "Missing required argument: task"),
            ("clodex_plan", {"task": None}, "Missing required argument: task"),
            ("clodex_build", {"task": "x", "dry_run": "yes"}, "Invalid arguments"),
            ("clodex_build", {"task": "x", "workspace": "docker"}, "Invalid arguments"),
            ("clodex_task_get", {}, "Missing required argument: run_id"),
        ):
            result = live.tool(name, arguments)["result"]
            self.assertTrue(result["isError"], name)
            self.assertIn(expected, result["content"][0]["text"])
        not_object = live.call("tools/call", {"name": "clodex_status", "arguments": [1]})["result"]
        self.assertTrue(not_object["isError"])

    def test_repo_root_comes_from_the_environment_not_the_cwd(self):
        live = self.start()
        result = live.tool("clodex_status")["result"]
        self.assertFalse(result["isError"])
        self.assertTrue((self.repo / ".clodex" / "state.sqlite3").is_file())

    def test_dry_run_build_accepts_workspace_and_profile(self):
        live = self.start()
        result = live.tool("clodex_build", {"task": "x", "dry_run": True, "workspace": "local", "approval_profile": "auto_review"})["result"]
        self.assertFalse(result["isError"])
        self.assertIn('"status": "dry-run"', result["content"][0]["text"])


class OrderingAndConcurrencyTests(ServerCase):
    def test_pipelined_state_changes_are_applied_in_order(self):
        live = self.start()
        live.send("tools/call", {"name": "clodex_handoff_create", "arguments": {"run_id": "h1", "task": "t"}}, request_id=1)
        live.send("tools/call", {"name": "clodex_handoff_update", "arguments": {"run_id": "h1", "phase": "audit", "actor": "claude"}}, request_id=2)
        live.send("tools/call", {"name": "clodex_handoff_get", "arguments": {"run_id": "h1"}}, request_id=3)
        got = live.sink.wait_for(3)
        self.assertFalse(got["result"]["isError"], got)
        self.assertEqual(json.loads(got["result"]["content"][0]["text"])["run"]["phase"], "audit")
        order = [m["id"] for m in live.sink.messages if m.get("id") in {1, 2, 3}]
        self.assertEqual(order, [1, 2, 3])

    def test_a_long_synchronous_build_does_not_block_other_requests(self):
        with FakeCliPath(sleep_seconds=3):
            live = self.start()
            build_id = live.send("tools/call", {"name": "clodex_build", "arguments": {"task": "slow", "workspace": "local"}})
            time.sleep(0.3)
            started = time.monotonic()
            self.assertEqual(live.call("ping", timeout=5)["result"], {})
            self.assertLess(time.monotonic() - started, 2.5, "ping must not wait for the build")
            self.assertIsNone(live.sink.wait_for(build_id, 0.01), "the build is still running")
            self.assertIsNotNone(live.sink.wait_for(build_id, 120), "the build eventually answers")


class TaskTests(ServerCase):
    def start_task(self, live: LiveServer, **arguments) -> dict:
        args = {"task": "implement fixture", "workspace": "local", **arguments}
        response = live.call("tools/call", {"name": "clodex_build", "arguments": args, "task": {"ttl": 60000}})
        self.assertIn("result", response, response)
        return response["result"]

    def test_task_augmented_build_returns_a_task_immediately(self):
        with FakeCliPath():
            live = self.start()
            result = self.start_task(live)
            task = result["task"]
            self.assertEqual(task["status"], "working")
            self.assertIsInstance(task["taskId"], str)
            self.assertIsNone(task["ttl"])
            self.assertEqual(task["pollInterval"], mcp_server.POLL_INTERVAL_MS)
            for key in ("createdAt", "lastUpdatedAt", "statusMessage"):
                self.assertTrue(task[key])
            self.assertIn(mcp_server.IMMEDIATE_RESPONSE_KEY, result["_meta"])
            fetched = live.call("tasks/get", {"taskId": task["taskId"]})["result"]
            self.assertEqual(fetched["taskId"], task["taskId"])
            self.assertIn(fetched["status"], {"working", "completed"})

    def test_tasks_result_blocks_until_done_then_returns_the_tool_result(self):
        with FakeCliPath():
            live = self.start()
            task_id = self.start_task(live)["task"]["taskId"]
            response = live.call("tasks/result", {"taskId": task_id}, timeout=90)
            result = response["result"]
            self.assertFalse(result["isError"], result)
            self.assertEqual(result["_meta"][RELATED_TASK_KEY], {"taskId": task_id})
            summary = json.loads(result["content"][0]["text"])
            self.assertEqual(summary["status"], "approved")
            self.assertTrue(summary["agreement"]["approved"])
            self.assertEqual(live.call("tasks/get", {"taskId": task_id})["result"]["status"], "completed")

    def test_failed_run_is_a_failed_task_with_an_error_result(self):
        with FakeCliPath(reject_reviewers=("codex-architecture",)):
            (self.repo / "CLODEX.md").write_text("---\nmax_fix_loops: 0\n---\nbody\n", encoding="utf-8")
            live = self.start()
            task_id = self.start_task(live)["task"]["taskId"]
            result = live.call("tasks/result", {"taskId": task_id}, timeout=90)["result"]
            self.assertTrue(result["isError"])
            task = live.call("tasks/get", {"taskId": task_id})["result"]
            self.assertEqual(task["status"], "failed")

    def test_unknown_and_non_worker_tasks_are_invalid_params(self):
        live = self.start()
        for method in ("tasks/get", "tasks/result", "tasks/cancel"):
            for params in ({"taskId": "missing"}, {}, {"taskId": 5}):
                self.assertEqual(live.call(method, params)["error"]["code"], -32602, (method, params))
        TaskManager(self.repo).state.upsert_task("t", "t", "done")
        TaskManager(self.repo).state.create_run("sync-run", "t", "p", "approved")  # no worker pid: not a task
        self.assertEqual(live.call("tasks/get", {"taskId": "sync-run"})["error"]["code"], -32602)

    def test_tasks_list_paginates_with_opaque_cursors(self):
        manager = TaskManager(self.repo)
        manager.state.upsert_task("t", "t", "done")
        for index in range(23):
            manager.state.create_run(f"run-{index:02d}", "t", "p", "approved", pid=1)
        live = self.start()
        first = live.call("tasks/list")["result"]
        self.assertEqual(len(first["tasks"]), 20)
        self.assertIn("nextCursor", first)
        second = live.call("tasks/list", {"cursor": first["nextCursor"]})["result"]
        self.assertEqual(len(second["tasks"]), 3)
        self.assertNotIn("nextCursor", second)
        ids = [t["taskId"] for t in first["tasks"] + second["tasks"]]
        self.assertEqual(len(set(ids)), 23)
        self.assertEqual(live.call("tasks/list", {"cursor": "garbage"})["error"]["code"], -32602)
        self.assertEqual(live.call("tasks/get", {"taskId": ids[0]})["result"]["taskId"], ids[0], "listed tasks are gettable")

    def test_cancel_moves_a_working_task_to_cancelled_and_rejects_repeats(self):
        with FakeCliPath(sleep_seconds=60):
            live = self.start()
            task_id = self.start_task(live)["task"]["taskId"]
            cancelled = live.call("tasks/cancel", {"taskId": task_id}, timeout=60)["result"]
            self.assertEqual((cancelled["taskId"], cancelled["status"]), (task_id, "cancelled"))
            again = live.call("tasks/cancel", {"taskId": task_id})
            self.assertEqual(again["error"]["code"], -32602)
            self.assertIn("terminal status 'cancelled'", again["error"]["message"])
            result = live.call("tasks/result", {"taskId": task_id}, timeout=10)["result"]
            self.assertTrue(result["isError"])
            self.assertEqual(json.loads(result["content"][0]["text"])["status"], "cancelled")

    def test_cancelled_notification_aborts_a_waiting_tasks_result_without_a_response(self):
        with FakeCliPath(sleep_seconds=60):
            live = self.start()
            task_id = self.start_task(live)["task"]["taskId"]
            waiting = live.send("tasks/result", {"taskId": task_id})
            self.assertEqual(live.call("ping")["result"], {}, "waiting must not block the loop")
            live.send("notifications/cancelled", {"requestId": waiting}, notification=True)
            self.assertIsNone(live.sink.wait_for(waiting, 2.0), "a cancelled request gets no response")

    def test_closing_the_connection_releases_a_waiting_tasks_result(self):
        with FakeCliPath(sleep_seconds=60):
            live = self.start()
            task_id = self.start_task(live)["task"]["taskId"]
            live.send("tasks/result", {"taskId": task_id})
            time.sleep(0.5)
            started = time.monotonic()
            self.assertTrue(live.close(timeout=15), "serve() must return after EOF")
            self.assertLess(time.monotonic() - started, 10)

    def test_task_augmentation_rules(self):
        with FakeCliPath():
            live = self.start()
            not_supported = live.call("tools/call", {"name": "clodex_plan", "arguments": {"task": "x"}, "task": {}})
            self.assertEqual(not_supported["error"]["code"], -32601)
            dry = live.call("tools/call", {"name": "clodex_build", "arguments": {"task": "x", "dry_run": True}, "task": {}})
            self.assertEqual(dry["error"]["code"], -32602)
            self.assertEqual(live.call("tools/call", {"name": "clodex_build", "arguments": {}, "task": {}})["result"]["isError"], True)

    def test_older_protocol_versions_have_no_tasks(self):
        live = self.start(protocol="2025-06-18")
        self.assertEqual(live.call("tasks/get", {"taskId": "x"})["error"]["code"], -32601)
        self.assertEqual(live.call("tasks/list")["error"]["code"], -32601)
        ignored = live.call("tools/call", {"name": "clodex_build", "arguments": {"task": "x", "dry_run": True}, "task": {"ttl": 1}})
        self.assertFalse(ignored["result"]["isError"], "task metadata is ignored when the capability was not negotiated")


class StatusMappingTests(unittest.TestCase):
    def test_run_status_to_task_status(self):
        expected = {
            "queued": "working", "running": "working", "planning": "working", "auditing": "working",
            "needs-fix": "working", "cancel_requested": "working", "handoff": "working",
            "approved": "completed", "applied": "completed", "completed": "completed",
            "blocked": "failed", "failed": "failed", "cancelled": "cancelled",
        }
        for run_status, wanted in expected.items():
            self.assertEqual(task_status(run_status), wanted, run_status)


if __name__ == "__main__":
    unittest.main()
