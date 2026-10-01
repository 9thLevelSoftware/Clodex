"""Tests: validate the server's wire messages against the official MCP SDK's 2025-11-25 schema models.

Skipped unless the `mcp` package (which ships `mcp_types`) is installed: `pip install mcp`.
These catch a different class of bug than the hand-written conformance tests: a field the
spec requires, a wrong type, or an unexpected extra property.
"""

from __future__ import annotations

import unittest

from tests.support import FakeCliPath
from tests.test_mcp_conformance import ServerCase

try:
    from mcp_types import _v2025_11_25 as wire
except ImportError:  # pragma: no cover - depends on the environment
    wire = None


def check(testcase: unittest.TestCase, model_name: str, payload: dict) -> None:
    model = getattr(wire, model_name)
    try:
        model.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - surface any pydantic error with the payload
        testcase.fail(f"{model_name} rejected {payload!r}: {exc}")


@unittest.skipIf(wire is None, "the `mcp` SDK is not installed")
class WireSchemaTests(ServerCase):
    def test_handshake_and_tool_messages_match_the_sdk_schema(self):
        live = self.start(initialize=False)
        initialize = live.call("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        check(self, "InitializeResult", initialize["result"])
        check(self, "ListToolsResult", live.call("tools/list")["result"])
        check(self, "CallToolResult", live.tool("clodex_status")["result"])
        check(self, "CallToolResult", live.tool("clodex_build", {})["result"])  # a tool-level error is still a CallToolResult
        check(self, "CallToolResult", live.tool("clodex_build", {"task": "x", "dry_run": True})["result"])

    def test_task_messages_match_the_sdk_schema(self):
        with FakeCliPath():
            live = self.start()
            created = live.call("tools/call", {"name": "clodex_build", "arguments": {"task": "implement fixture", "workspace": "local"}, "task": {"ttl": 60000}})["result"]
            check(self, "CreateTaskResult", created)
            task_id = created["task"]["taskId"]
            check(self, "GetTaskResult", live.call("tasks/get", {"taskId": task_id})["result"])
            check(self, "ListTasksResult", live.call("tasks/list")["result"])
            check(self, "CallToolResult", live.call("tasks/result", {"taskId": task_id}, timeout=90)["result"])
            check(self, "GetTaskResult", live.call("tasks/get", {"taskId": task_id})["result"])

    def test_cancelled_task_matches_the_sdk_schema(self):
        with FakeCliPath(sleep_seconds=60):
            live = self.start()
            created = live.call("tools/call", {"name": "clodex_build", "arguments": {"task": "slow", "workspace": "local"}, "task": {}})["result"]
            check(self, "CancelTaskResult", live.call("tasks/cancel", {"taskId": created["task"]["taskId"]}, timeout=60)["result"])

    def test_error_responses_match_the_jsonrpc_schema(self):
        live = self.start()
        for method, params in (("nope/x", None), ("tasks/get", {"taskId": "missing"}), ("tools/call", {"name": "clodex_nope"})):
            response = live.call(method, params)
            check(self, "JSONRPCErrorResponse", response)


if __name__ == "__main__":
    unittest.main()
