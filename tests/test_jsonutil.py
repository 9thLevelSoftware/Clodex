"""Tests: model output / Claude envelope parsing."""

from __future__ import annotations

import json
import unittest
from clodex.jsonutil import AgentEnvelopeError, extract_json_object


class JsonUtilTests(unittest.TestCase):
    def test_json_extraction_handles_fenced_json(self):
        value = extract_json_object("```json\n{\"approved\": true}\n```")
        self.assertTrue(value["approved"])

    def test_claude_envelope_string_result_is_unwrapped(self):
        envelope = {"type": "result", "subtype": "success", "is_error": False, "result": '{"goal": "x"}'}
        self.assertEqual(extract_json_object(json.dumps(envelope)), {"goal": "x"})
        envelope["structured_output"] = {"goal": "structured"}
        self.assertEqual(extract_json_object(json.dumps(envelope)), {"goal": "structured"})

    def test_claude_envelope_error_is_raised(self):
        envelope = {"type": "result", "is_error": True, "result": "Not logged in"}
        with self.assertRaises(AgentEnvelopeError):
            extract_json_object(json.dumps(envelope))
