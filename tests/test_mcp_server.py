"""Tests: stdio MCP server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from tests.support import ROOT


class McpServerTests(unittest.TestCase):
    def test_mcp_tools_list(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        result = subprocess.run(
            [sys.executable, "-m", "clodex", "mcp-server"],
            input=json.dumps(request) + "\n",
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout.splitlines()[0])
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertIn("clodex_build", names)
        self.assertIn("clodex_task_update", names)
        self.assertIn("clodex_task_start", names)
        self.assertIn("clodex_task_get", names)
        self.assertIn("clodex_task_cancel", names)

    def test_mcp_tools_list_includes_handoff_tools(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        result = subprocess.run(
            [sys.executable, "-m", "clodex", "mcp-server"],
            input=json.dumps(request) + "\n",
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout.splitlines()[0])
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertIn("clodex_handoff_create", names)
        self.assertIn("clodex_handoff_update", names)
        self.assertIn("clodex_handoff_get", names)
        self.assertIn("clodex_handoff_decide", names)

    def test_mcp_tasks_get_unknown_run(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"taskId": "missing"}}
        result = subprocess.run(
            [sys.executable, "-m", "clodex", "mcp-server"],
            input=json.dumps(request) + "\n",
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(response["error"]["code"], -32602)  # spec: invalid or unknown taskId

    def test_mcp_handoff_create_update_get_and_decide(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_create",
                            "arguments": {"run_id": "run-mcp", "task": "native task", "owner": "claude", "handoff_budget": 2},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-mcp",
                                "phase": "implementation",
                                "actor": "claude",
                                "increment_handoff": True,
                                "report": {"summary": "plan accepted"},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_get", "arguments": {"run_id": "run-mcp"}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_decide", "arguments": {"run_id": "run-mcp"}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_get", "arguments": {"run_id": "run-mcp"}},
                    },
                ]
            ) + "\n"
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=payload,
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertFalse(responses[0]["result"]["isError"])
        created = json.loads(responses[0]["result"]["content"][0]["text"])
        self.assertEqual(created["id"], "run-mcp")
        self.assertEqual(created["handoff_budget"], 2)

        self.assertFalse(responses[1]["result"]["isError"])
        updated = json.loads(responses[1]["result"]["content"][0]["text"])
        self.assertEqual(updated["phase"], "implementation")
        self.assertEqual(updated["handoff_count"], 1)

        get_data = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertEqual(get_data["run"]["id"], "run-mcp")
        self.assertEqual(get_data["run"]["phase"], "implementation")
        self.assertEqual(get_data["budget_remaining"], 1)
        self.assertEqual(get_data["next_expected_actor"], "codex")

        self.assertFalse(responses[3]["result"]["isError"])
        decision = json.loads(responses[3]["result"]["content"][0]["text"])
        self.assertEqual(decision["decision"], "needs_fix")
        self.assertEqual(decision["phase"], "implementation")
        self.assertEqual(decision["budget_remaining"], 1)
        self.assertEqual(decision["next_expected_actor"], "codex")

        final_data = json.loads(responses[4]["result"]["content"][0]["text"])
        self.assertTrue(any(event["event"] == "handoff.decide" for event in final_data["events"]))

    def test_mcp_handoff_decide_approves_matching_claude_and_codex_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_create",
                            "arguments": {"run_id": "run-approve", "task": "native task", "handoff_budget": 4},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-approve",
                                "phase": "audit",
                                "actor": "claude",
                                "diff_hash": "abc123",
                                "report": {"approved": True, "summary": "matches plan"},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-approve",
                                "phase": "audit",
                                "actor": "codex",
                                "diff_hash": "abc123",
                                "report": {"approved": True, "summary": "implementation sound"},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_decide", "arguments": {"run_id": "run-approve"}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_get", "arguments": {"run_id": "run-approve"}},
                    },
                ]
            ) + "\n"
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=payload,
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        for response in responses[:4]:
            self.assertFalse(response["result"]["isError"])
        decision = json.loads(responses[3]["result"]["content"][0]["text"])
        self.assertEqual(decision["decision"], "approved")
        self.assertEqual(decision["diff_hash"], "abc123")
        self.assertEqual(decision["approved_by"], ["claude", "codex"])

        data = json.loads(responses[4]["result"]["content"][0]["text"])
        self.assertEqual(data["run"]["status"], "approved")
        self.assertEqual(data["run"]["phase"], "decision")
        self.assertEqual(data["run"]["diff_hash"], "abc123")
        self.assertTrue(any(event["event"] == "handoff.approved" for event in data["events"]))
        self.assertTrue(any(event["event"] == "handoff.decide" for event in data["events"]))

    def test_mcp_handoff_decide_requires_approvals_for_latest_diff_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_create",
                            "arguments": {"run_id": "run-stale-approval", "task": "native task", "handoff_budget": 6},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-stale-approval",
                                "actor": "claude",
                                "diff_hash": "abc123",
                                "report": {"approved": True},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-stale-approval",
                                "actor": "codex",
                                "diff_hash": "abc123",
                                "report": {"approved": True},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-stale-approval",
                                "actor": "codex",
                                "diff_hash": "def456",
                                "report": {"approved": False, "summary": "new diff needs review"},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_decide", "arguments": {"run_id": "run-stale-approval"}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 6,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-stale-approval",
                                "actor": "claude",
                                "diff_hash": "def456",
                                "report": {"approved": True},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-stale-approval",
                                "actor": "codex",
                                "diff_hash": "def456",
                                "report": {"approved": True},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 8,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_decide", "arguments": {"run_id": "run-stale-approval"}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_get", "arguments": {"run_id": "run-stale-approval"}},
                    },
                ]
            ) + "\n"
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=payload,
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        first_decision = json.loads(responses[4]["result"]["content"][0]["text"])
        self.assertFalse(responses[4]["result"]["isError"])
        self.assertEqual(first_decision["decision"], "needs_fix")

        final_decision = json.loads(responses[7]["result"]["content"][0]["text"])
        self.assertFalse(responses[7]["result"]["isError"])
        self.assertEqual(final_decision["decision"], "approved")
        self.assertEqual(final_decision["diff_hash"], "def456")

        data = json.loads(responses[8]["result"]["content"][0]["text"])
        self.assertEqual(data["run"]["status"], "approved")
        self.assertEqual(data["run"]["diff_hash"], "def456")

    def test_mcp_handoff_decide_treats_hashless_rejection_as_withdrawal(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_create",
                            "arguments": {"run_id": "run-withdraw", "task": "native task", "handoff_budget": 6},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-withdraw",
                                "actor": "claude",
                                "diff_hash": "abc123",
                                "report": {"approved": True},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-withdraw",
                                "actor": "codex",
                                "diff_hash": "abc123",
                                "report": {"approved": True},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-withdraw",
                                "actor": "codex",
                                "report": {"approved": False, "summary": "withdrawing approval"},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_decide", "arguments": {"run_id": "run-withdraw"}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 6,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {
                                "run_id": "run-withdraw",
                                "actor": "codex",
                                "diff_hash": "abc123",
                                "report": {"approved": True},
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_decide", "arguments": {"run_id": "run-withdraw"}},
                    },
                ]
            ) + "\n"
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=payload,
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        withdrawn = json.loads(responses[4]["result"]["content"][0]["text"])
        self.assertFalse(responses[4]["result"]["isError"])
        self.assertEqual(withdrawn["decision"], "needs_fix")

        approved = json.loads(responses[6]["result"]["content"][0]["text"])
        self.assertFalse(responses[6]["result"]["isError"])
        self.assertEqual(approved["decision"], "approved")
        self.assertEqual(approved["diff_hash"], "abc123")

    def test_mcp_handoff_budget_exhaustion_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_create",
                            "arguments": {"run_id": "run-budget", "task": "native task", "handoff_budget": 0},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {"run_id": "run-budget", "actor": "claude", "increment_handoff": True},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_decide", "arguments": {"run_id": "run-budget"}},
                    },
                ]
            ) + "\n"
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=payload,
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        created = json.loads(responses[0]["result"]["content"][0]["text"])
        self.assertEqual(created["handoff_budget"], 0)

        self.assertTrue(responses[1]["result"]["isError"])
        update_data = json.loads(responses[1]["result"]["content"][0]["text"])
        self.assertEqual(update_data["status"], "blocked")
        self.assertEqual(update_data["handoff_count"], 1)
        self.assertEqual(update_data["blocked_reason"], "handoff budget exhausted")

        self.assertTrue(responses[2]["result"]["isError"])
        decision = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertEqual(decision["decision"], "blocked")
        self.assertEqual(decision["blocked_reason"], "handoff budget exhausted")

    def test_mcp_handoff_update_rejects_direct_approved_without_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_create",
                            "arguments": {"run_id": "run-invalid", "task": "native task"},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_update",
                            "arguments": {"run_id": "run-invalid", "status": "approved", "actor": "claude"},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_get", "arguments": {"run_id": "run-invalid"}},
                    },
                ]
            ) + "\n"
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=payload,
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertFalse(responses[0]["result"]["isError"])
        self.assertTrue(responses[1]["result"]["isError"])
        self.assertIn("handoff status cannot be set", responses[1]["result"]["content"][0]["text"])
        data = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertEqual(data["run"]["status"], "handoff")
        self.assertEqual(data["run"]["handoff_count"], 0)
        self.assertIsNone(data["run"]["last_actor"])

    def test_mcp_handoff_create_invalid_budget_returns_tool_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = {
                "jsonrpc": "2.0",
                "id": 41,
                "method": "tools/call",
                "params": {
                    "name": "clodex_handoff_create",
                    "arguments": {"run_id": "run-invalid-budget", "task": "native task", "handoff_budget": "not-an-int"},
                },
            }
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=json.dumps(request) + "\n",
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(response["id"], 41)
        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(response["result"]["content"][0]["text"], "handoff_budget must be an integer")

    def test_mcp_handoff_create_negative_budget_returns_tool_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = {
                "jsonrpc": "2.0",
                "id": 42,
                "method": "tools/call",
                "params": {
                    "name": "clodex_handoff_create",
                    "arguments": {"run_id": "run-negative-budget", "task": "native task", "handoff_budget": -1},
                },
            }
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=json.dumps(request) + "\n",
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(response["id"], 42)
        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])
        self.assertIn("handoff_budget must be non-negative", response["result"]["content"][0]["text"])

    def test_mcp_handoff_create_rejects_bool_and_float_budgets(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "jsonrpc": "2.0",
                        "id": 49,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_create",
                            "arguments": {"run_id": "run-bool-budget", "task": "native task", "handoff_budget": True},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 50,
                        "method": "tools/call",
                        "params": {
                            "name": "clodex_handoff_create",
                            "arguments": {"run_id": "run-float-budget", "task": "native task", "handoff_budget": 1.5},
                        },
                    },
                ]
            ) + "\n"
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=payload,
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual({response["id"] for response in responses}, {49, 50})
        for response in responses:
            self.assertNotIn("error", response)
            self.assertTrue(response["result"]["isError"])
            self.assertEqual(response["result"]["content"][0]["text"], "handoff_budget must be an integer")

    def test_mcp_handoff_create_duplicate_run_id_returns_tool_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            create = {
                "jsonrpc": "2.0",
                "id": 43,
                "method": "tools/call",
                "params": {
                    "name": "clodex_handoff_create",
                    "arguments": {"run_id": "run-duplicate", "task": "native task"},
                },
            }
            duplicate = {
                "jsonrpc": "2.0",
                "id": 44,
                "method": "tools/call",
                "params": {
                    "name": "clodex_handoff_create",
                    "arguments": {"run_id": "run-duplicate", "task": "native task"},
                },
            }
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=json.dumps(create) + "\n" + json.dumps(duplicate) + "\n",
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(responses[0]["id"], 43)
        self.assertFalse(responses[0]["result"]["isError"])
        self.assertEqual(responses[1]["id"], 44)
        self.assertNotIn("error", responses[1])
        self.assertTrue(responses[1]["result"]["isError"])
        self.assertIn("UNIQUE constraint failed", responses[1]["result"]["content"][0]["text"])

    def test_mcp_handoff_missing_required_arguments_return_tool_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "jsonrpc": "2.0",
                        "id": 45,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_create", "arguments": {"run_id": "run-missing-task"}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 46,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_update", "arguments": {"actor": "claude"}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 47,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_get", "arguments": {}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 48,
                        "method": "tools/call",
                        "params": {"name": "clodex_handoff_decide", "arguments": {}},
                    },
                ]
            ) + "\n"
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "mcp-server"],
                input=payload,
                cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        expected = {
            45: "Missing required argument: task",
            46: "Missing required argument: run_id",
            47: "Missing required argument: run_id",
            48: "Missing required argument: run_id",
        }
        self.assertEqual({response["id"] for response in responses}, set(expected))
        for response in responses:
            self.assertNotIn("error", response)
            self.assertTrue(response["result"]["isError"])
            self.assertEqual(response["result"]["content"][0]["text"], expected[response["id"]])
