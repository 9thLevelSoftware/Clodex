"""Tests: the handoff clarification channel, actor validation and decide guards."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from clodex.delegate import extract_clarifications
from clodex.prompts import delegate_prompt
from clodex.state import StateStore
from tests.support import FakeCliPath
from tests.test_delegate import DelegateCase
from tests.test_handoff_quorum import HandoffCase


class ExtractClarificationsTests(unittest.TestCase):
    def test_finds_questions_in_bare_fenced_and_mixed_output(self):
        self.assertEqual(extract_clarifications('{"clarifications": ["Which DB?"]}'), ["Which DB?"])
        fenced = 'Blocked.\n\n```json\n{"clarifications": ["A?", {"question": "B?"}]}\n```\n'
        self.assertEqual(extract_clarifications(fenced), ["A?", "B?"])
        mixed = 'Report with code: `{"a": 1}`\n\nI need input.\n{"clarifications": ["Only the last JSON counts?"]}'
        self.assertEqual(extract_clarifications(mixed), ["Only the last JSON counts?"])

    def test_ignores_everything_that_is_not_a_clarification_request(self):
        for text in ("", "all done", '{"clarifications": []}', '{"clarifications": "nope"}', '{"other": ["x"]}', '{"clarifications": [1, null, "  ", {"q": "x"}]}', "{broken json", "[1, 2]"):
            self.assertEqual(extract_clarifications(text), [], text)

    def test_blank_entries_are_dropped_and_the_count_is_capped(self):
        many = {"clarifications": ["  keep  ", "", *[f"q{i}" for i in range(30)]]}
        found = extract_clarifications(json.dumps(many))
        self.assertEqual(found[0], "keep")
        self.assertEqual(len(found), 10)


class PromptTests(unittest.TestCase):
    def test_prompt_tells_codex_how_to_ask_and_delivers_answers(self):
        plain = delegate_prompt("implement", "Add search", "Use the existing index")
        self.assertIn('{"clarifications": [', plain)
        self.assertIn("do not guess", plain)
        self.assertNotIn("Clarifications from Claude", plain)
        answered = delegate_prompt("fix", "Add search", None, ["handle empty input"], [("Which DB?", "SQLite"), ("Compat?", "Yes")])
        self.assertIn("Clarifications from Claude", answered)
        self.assertIn("- Q: Which DB?\n  A: SQLite", answered)
        self.assertLess(answered.index("Clarifications from Claude"), answered.index("Required fixes"))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "s.sqlite3")
        self.store.create_handoff("h", "Add search", owner="claude")

    def test_lifecycle_open_answered_delivered(self):
        question = self.store.add_clarification("h", "codex", "  Which DB?  ")
        self.assertEqual((question["kind"], question["status"], question["actor"], question["body"]), ("clarification", "open", "codex", "Which DB?"))
        self.assertEqual(self.store.answered_clarifications("h"), [], "nothing to deliver before an answer")
        answer = self.store.answer_clarification("h", question["id"], "SQLite", "claude")
        self.assertEqual((answer["kind"], answer["reply_to"], answer["actor"]), ("answer", question["id"], "claude"))
        self.assertEqual(self.store.list_messages("h", kind="clarification")[0]["status"], "answered")
        pending = self.store.answered_clarifications("h")
        self.assertEqual([(p["question"], p["answer"], p["asked_by"], p["answered_by"]) for p in pending], [("Which DB?", "SQLite", "codex", "claude")])
        self.store.mark_delivered([question["id"]])
        self.assertEqual(self.store.answered_clarifications("h"), [])
        self.assertEqual(self.store.list_messages("h", kind="clarification")[0]["status"], "delivered")
        events = [e["event"] for e in self.store.get_handoff("h")["events"]]
        self.assertEqual(events[-2:], ["handoff.clarify", "handoff.answer"])

    def test_rules(self):
        question = self.store.add_clarification("h", "codex", "Q?")
        with self.assertRaisesRegex(ValueError, "the other agent answers"):
            self.store.answer_clarification("h", question["id"], "mine", "codex")
        self.store.answer_clarification("h", question["id"], "A", "claude")
        with self.assertRaisesRegex(ValueError, "already answered"):
            self.store.answer_clarification("h", question["id"], "again", "claude")
        with self.assertRaisesRegex(ValueError, "unknown clarification"):
            self.store.answer_clarification("h", 9999, "x", "claude")
        self.store.create_handoff("other", "t")
        other = self.store.add_clarification("other", "claude", "Elsewhere?")
        with self.assertRaisesRegex(ValueError, "unknown clarification"):
            self.store.answer_clarification("h", other["id"], "cross-run", "codex")
        for bad_question in ("", "   "):
            with self.assertRaises(ValueError):
                self.store.add_clarification("h", "claude", bad_question)
        with self.assertRaisesRegex(ValueError, "unknown run"):
            self.store.add_clarification("nope", "claude", "Q?")
        with self.assertRaisesRegex(ValueError, "empty"):
            self.store.answer_clarification("h", self.store.add_clarification("h", "claude", "Q2?")["id"], " ", "codex")

    def test_finished_handoffs_take_no_messages(self):
        self.store.update_handoff("h", actor="claude", status="blocked", blocked_reason="stop")
        with self.assertRaisesRegex(ValueError, "cannot take new messages"):
            self.store.add_clarification("h", "claude", "Q?")

    def test_open_questions_hand_the_turn_to_the_answerer(self):
        self.assertEqual(self.store.get_handoff("h")["next_expected_actor"], "claude")
        question = self.store.add_clarification("h", "claude", "Codex, which flag?")
        data = self.store.get_handoff("h")
        self.assertEqual((data["next_expected_actor"], [m["id"] for m in data["open_clarifications"]]), ("codex", [question["id"]]))
        self.store.answer_clarification("h", question["id"], "--fast", "codex")
        data = self.store.get_handoff("h")
        self.assertEqual((data["next_expected_actor"], data["open_clarifications"]), ("claude", []))

    def test_list_filters_and_validation(self):
        q1 = self.store.add_clarification("h", "codex", "one")
        self.store.add_clarification("h", "codex", "two")
        self.store.answer_clarification("h", q1["id"], "ans", "claude")
        self.assertEqual([m["body"] for m in self.store.list_messages("h", status="open")], ["two"])
        self.assertEqual([m["kind"] for m in self.store.list_messages("h", kind="answer")], ["answer"])
        self.assertEqual(len(self.store.list_messages("h")), 3)
        with self.assertRaises(ValueError):
            self.store.list_messages("h", status="pending")

    def test_actors_and_owners_are_validated_and_normalized(self):
        self.store.update_handoff("h", actor=" Claude ")
        self.assertEqual(self.store.get_run("h")["last_actor"], "claude")
        for bad in ("bob", "", "gemini"):
            with self.assertRaisesRegex(ValueError, "unknown actor"):
                self.store.update_handoff("h", actor=bad)
        with self.assertRaisesRegex(ValueError, "unknown owner"):
            self.store.update_handoff("h", owner="nobody")
        with self.assertRaisesRegex(ValueError, "unknown owner"):
            self.store.create_handoff("x", "t", owner="nobody")
        self.assertEqual(self.store.create_handoff("y", "t", owner="Codex")["owner"], "codex")

    def test_v3_database_upgrades_keeping_old_messages_readable(self):
        db = Path(self.tmp.name) / "v3.sqlite3"
        con = sqlite3.connect(db)
        con.executescript(
            """
            create table schema_version(version integer not null); insert into schema_version values (3);
            create table messages(id integer primary key autoincrement, task_id text, topic text not null, body text not null, created_at text not null);
            insert into messages(task_id, topic, body, created_at) values ('t', 'planning', 'old note', '2026-01-01T00:00:00Z');
            """
        )
        con.commit()
        con.close()
        store = StateStore(db)
        self.assertEqual(store.schema_version(), 4)
        with store.session() as conn:
            columns = {row["name"] for row in conn.execute("pragma table_info(messages)")}
            old = dict(conn.execute("select * from messages").fetchone())
        self.assertTrue({"run_id", "actor", "kind", "reply_to", "status"} <= columns)
        self.assertEqual((old["body"], old["kind"], old["run_id"]), ("old note", None, None))
        store.create_handoff("h", "t")
        self.assertEqual(store.add_clarification("h", "claude", "Q?")["status"], "open")


class ToolTests(HandoffCase):
    def test_clarify_answer_and_messages_tools(self):
        self.call("clodex_handoff_create", run_id="h", task="Add search")
        is_error, message = self.call("clodex_clarify", run_id="h", actor="codex", question="Which DB?")
        self.assertFalse(is_error, message)
        self.assertEqual((message["status"], message["actor"]), ("open", "codex"))
        is_error, listed = self.call("clodex_messages", run_id="h", status="open")
        self.assertEqual([m["id"] for m in listed["messages"]], [message["id"]])
        is_error, answer = self.call("clodex_answer", run_id="h", message_id=message["id"], answer="SQLite", actor="claude")
        self.assertFalse(is_error, answer)
        self.assertEqual(self.call("clodex_messages", run_id="h", status="open")[1]["messages"], [])
        self.assertEqual(len(self.call("clodex_messages", run_id="h")[1]["messages"]), 2)
        data = self.call("clodex_handoff_get", run_id="h")[1]
        self.assertEqual(data["open_clarifications"], [])

    def test_bad_input_is_a_tool_error(self):
        self.call("clodex_handoff_create", run_id="h", task="t")
        for name, arguments, expected in (
            ("clodex_clarify", {"run_id": "h", "actor": "claude", "question": " "}, "needs a question"),
            ("clodex_clarify", {"run_id": "nope", "actor": "claude", "question": "Q?"}, "unknown run"),
            ("clodex_answer", {"run_id": "h", "message_id": 99, "answer": "x", "actor": "claude"}, "unknown clarification"),
            ("clodex_messages", {"run_id": "nope"}, "Unknown run: nope"),
        ):
            is_error, message = self.call(name, **arguments)
            self.assertTrue(is_error, name)
            self.assertIn(expected, message)
        from clodex import mcp_server

        server = mcp_server.McpServer(out=object())
        for name, arguments, expected in (
            ("clodex_clarify", {"run_id": "h", "actor": "bob", "question": "Q?"}, "Invalid arguments"),
            ("clodex_clarify", {"run_id": "h", "actor": "claude"}, "Missing required argument: question"),
            ("clodex_answer", {"run_id": "h", "message_id": "one", "answer": "x", "actor": "claude"}, "Invalid arguments"),
            ("clodex_messages", {"run_id": "h", "status": "pending"}, "Invalid arguments"),
        ):
            result = server.tools_call({"name": name, "arguments": arguments})
            self.assertTrue(result["isError"], (name, arguments))
            self.assertIn(expected, result["content"][0]["text"])

    def test_unknown_actor_on_update_and_create_is_a_tool_error(self):
        is_error, message = self.call("clodex_handoff_create", run_id="o", task="t", owner="bob")
        self.assertTrue(is_error)
        self.assertIn("unknown owner", message)
        self.call("clodex_handoff_create", run_id="h", task="t")
        is_error, message = self.call("clodex_handoff_update", run_id="h", actor="bob")
        self.assertTrue(is_error)
        self.assertIn("unknown actor", message)

    def test_decide_will_not_approve_over_an_open_question_and_does_once_answered(self):
        self.call("clodex_handoff_create", run_id="h", task="t")
        self.call("clodex_handoff_update", run_id="h", actor="claude", diff_hash="d", report={"approved": True})
        self.call("clodex_handoff_update", run_id="h", actor="codex", diff_hash="d", report={"approved": True})
        question = self.call("clodex_clarify", run_id="h", actor="codex", question="Is v2 in scope?")[1]
        is_error, decision = self.call("clodex_handoff_decide", run_id="h")
        self.assertFalse(is_error)
        self.assertEqual(decision["decision"], "needs_fix", "the reviewers agree, but a question is open")
        self.assertEqual(decision["open_clarifications"], [{"message_id": question["id"], "asked_by": "codex", "question": "Is v2 in scope?"}])
        self.assertEqual(decision["next_expected_actor"], "claude")
        self.assertEqual(decision["approved_reviewers"], ["claude-plan", "codex-architecture"])
        self.call("clodex_answer", run_id="h", message_id=question["id"], answer="No", actor="claude")
        self.assertEqual(self.call("clodex_handoff_decide", run_id="h")[1]["decision"], "approved")

    def test_decide_refuses_while_a_delegation_is_running(self):
        self.call("clodex_handoff_create", run_id="h", task="t")
        self.call("clodex_handoff_update", run_id="h", actor="claude", diff_hash="d", report={"approved": True})
        self.call("clodex_handoff_update", run_id="h", actor="codex", diff_hash="d", report={"approved": True})
        delegation = self.state.start_delegation("h", "implement", "x")
        self.state.update_delegation(delegation["id"], status="running", pid=os.getpid(), started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.state.touch_delegation(delegation["id"])
        is_error, message = self.call("clodex_handoff_decide", run_id="h")
        self.assertTrue(is_error)
        self.assertIn("delegation is still running", message)
        self.state.update_delegation(delegation["id"], status="completed")
        self.assertEqual(self.call("clodex_handoff_decide", run_id="h")[1]["decision"], "approved")


class WorkerFlowTests(DelegateCase):
    def test_codex_asks_claude_answers_and_the_answers_reach_the_next_delegation(self):
        with FakeCliPath(codex_clarifies=True):
            self.create()
            is_error, result = self.delegate(instructions="Add search")
            self.assertFalse(is_error, result)
            delegation = result["delegation"]
            self.assertEqual(delegation["status"], "completed")
            self.assertTrue(delegation["summary"].startswith("Codex needs clarification: Which database should be used?"))
            self.assertFalse((self.workspace() / "implemented.txt").exists(), "Codex asked instead of guessing")

            data = self.handoff()
            asked = data["open_clarifications"]
            self.assertEqual([(m["actor"], m["body"]) for m in asked], [("codex", "Which database should be used?"), ("codex", "Must it stay backwards compatible?")])
            self.assertEqual(data["next_expected_actor"], "claude")
            self.assertEqual(data["run"]["handoff_count"], 1, "handing the question back is a handoff")
            report = next(e for e in reversed(data["events"]) if e["event"] == "handoff.update")["data"]["report"]
            self.assertEqual(report["clarifications"], [m["body"] for m in asked])
            self.assertFalse(report["changed"])

            self.assertEqual(self.call("clodex_handoff_decide", run_id="d")[1]["decision"], "needs_fix")
            for message in asked:
                self.assertFalse(self.call("clodex_answer", run_id="d", message_id=message["id"], answer="SQLite, and yes", actor="claude")[0])
            self.assertEqual(self.handoff()["open_clarifications"], [])

            is_error, second = self.delegate(instructions="Add search")
            self.assertFalse(is_error, second)
            self.assertEqual(second["delegation"]["status"], "completed")
            self.assertTrue((self.workspace() / "implemented.txt").is_file(), "with the answers in the prompt Codex proceeded")
            statuses = {m["id"]: m["status"] for m in self.call("clodex_messages", run_id="d", kind="clarification")[1]["messages"]}
            self.assertEqual(set(statuses.values()), {"delivered"})
            self.assertEqual(self.handoff()["run"]["handoff_count"], 2)

    def test_answers_are_delivered_once(self):
        with FakeCliPath(codex_clarifies=True):
            self.create()
            self.delegate(instructions="Add search")
            for message in self.handoff()["open_clarifications"]:
                self.call("clodex_answer", run_id="d", message_id=message["id"], answer="yes", actor="claude")
            self.assertEqual(len(self.state.answered_clarifications("d")), 2)
            self.delegate(instructions="Add search")
            self.assertEqual(self.state.answered_clarifications("d"), [], "already delivered")


if __name__ == "__main__":
    unittest.main()
