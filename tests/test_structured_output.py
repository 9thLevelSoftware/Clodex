"""Tests: structured agent output, JSON schemas and per-role model settings."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clodex.agents import AgentRunner
from clodex.commands import (
    AgentCommand,
    claude_audit_command,
    claude_plan_command,
    codex_exec_command,
    codex_review_command,
    inline_schema_supported,
)
from clodex.config import load_config
from clodex.schemas import SCHEMA_NAMES, load_schema, schema_path, validate
from clodex.workflow import ClodexWorkflow
from tests.support import ROOT, FakeCliPath, TempRepo


def write_contract(repo: Path, front_matter: str) -> None:
    (repo / "CLODEX.md").write_text(f"---\n{front_matter}---\nbody\n", encoding="utf-8")


def flag_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def assert_strict(testcase: unittest.TestCase, schema: dict, path: str = "$") -> None:
    """OpenAI strict mode: every property required, no extra properties, recursively."""
    if schema.get("type") == "object":
        testcase.assertIs(schema.get("additionalProperties"), False, path)
        testcase.assertEqual(set(schema.get("required", [])), set(schema.get("properties", {})), path)
        for name, sub in schema.get("properties", {}).items():
            assert_strict(testcase, sub, f"{path}.{name}")
    if schema.get("type") == "array":
        assert_strict(testcase, schema["items"], f"{path}[]")


COMPLETE_PLAN = {
    "goal": "g",
    "scope": ["s"],
    "out_of_scope": [],
    "implementation_spec": ["step"],
    "acceptance_criteria": ["ok"],
    "risks": [],
    "test_commands": ["pytest"],
}


class SchemaTests(unittest.TestCase):
    def test_schema_files_load_and_are_strict_mode_compatible(self):
        for name in SCHEMA_NAMES:
            schema = load_schema(name)
            self.assertTrue(schema_path(name).is_file(), name)
            # Claude's validator rejects the draft 2020-12 `$schema` URI.
            self.assertNotIn("$schema", schema, name)
            assert_strict(self, schema)

    def test_plan_validation(self):
        schema = load_schema("plan")
        self.assertEqual(validate(COMPLETE_PLAN, schema), [])
        incomplete = {key: value for key, value in COMPLETE_PLAN.items() if key != "risks"}
        self.assertIn("$.risks: missing required property", validate(incomplete, schema))
        wrong_type = {**COMPLETE_PLAN, "scope": "not a list"}
        self.assertTrue(any("$.scope" in error for error in validate(wrong_type, schema)))

    def test_verdict_validation_allows_null_location_and_checks_enum_and_bool(self):
        schema = load_schema("audit_verdict")
        verdict = {
            "approved": True,
            "diff_hash": "h",
            "reviewer_id": "r",
            "persona": "p",
            "summary": "s",
            "findings": [{"severity": "low", "file": None, "line": None, "message": "m"}],
            "required_fixes": [],
        }
        self.assertEqual(validate(verdict, schema), [])
        bad_severity = {**verdict, "findings": [{"severity": "huge", "file": "f", "line": 3, "message": "m"}]}
        self.assertTrue(any("severity" in error for error in validate(bad_severity, schema)))
        self.assertTrue(any("approved" in error for error in validate({**verdict, "approved": "yes"}, schema)))
        self.assertTrue(any("line" in error for error in validate({**verdict, "findings": [{"severity": "low", "file": None, "line": True, "message": "m"}]}, schema)))

    def test_package_ships_schema_files(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertIn("clodex/schemas/*.json", package["files"])
        self.assertIn("schemas/*.json", (ROOT / "pyproject.toml").read_text(encoding="utf-8"))


class RoleConfigTests(unittest.TestCase):
    def test_legacy_flat_claude_keys_drive_both_roles_and_role_keys_win(self):
        with TempRepo() as repo:
            write_contract(repo, "claude:\n  model: sonnet\n  effort: low\n")
            config = load_config(repo)
            for role in ("plan", "audit"):
                self.assertEqual(config.claude_role(role)["model"], "sonnet")
                self.assertEqual(config.claude_role(role)["effort"], "low")
            write_contract(repo, "claude:\n  model: sonnet\n  effort: low\n  audit:\n    effort: medium\n")
            config = load_config(repo)
            self.assertEqual(config.claude_role("plan")["effort"], "low")
            self.assertEqual(config.claude_role("audit")["effort"], "medium")
            self.assertEqual(config.claude_role("audit")["model"], "sonnet")

    def test_codex_audit_overrides_do_not_affect_builds(self):
        with TempRepo() as repo:
            write_contract(repo, "codex:\n  model: gpt-6-sol\n  reasoning_effort: xhigh\n  audit:\n    model: gpt-6-luna\n    reasoning_effort: high\n")
            config = load_config(repo)
            build = codex_exec_command(config, repo).argv
            audit = codex_review_command(config, repo).argv
            self.assertEqual(flag_value(build, "-m"), "gpt-6-sol")
            self.assertIn('model_reasoning_effort="xhigh"', build)
            self.assertEqual(flag_value(audit, "-m"), "gpt-6-luna")
            self.assertIn('model_reasoning_effort="high"', audit)

    def test_fallback_model_and_budget_flags_are_optional(self):
        with TempRepo() as repo:
            plain = claude_plan_command(load_config(repo)).argv
            self.assertNotIn("--fallback-model", plain)
            self.assertNotIn("--max-budget-usd", plain)
            write_contract(repo, "claude:\n  fallback_model: [sonnet, haiku]\n  max_budget_usd: 2.5\n")
            config = load_config(repo)
            for argv in (claude_plan_command(config).argv, claude_audit_command(config).argv):
                self.assertEqual(flag_value(argv, "--fallback-model"), "sonnet,haiku")
                self.assertEqual(flag_value(argv, "--max-budget-usd"), "2.5")


class CommandTests(unittest.TestCase):
    def test_inline_schema_policy(self):
        self.assertFalse(inline_schema_supported("auto", "C:/tools/claude.cmd"))
        self.assertFalse(inline_schema_supported("auto", "C:/tools/CLAUDE.BAT"))
        self.assertTrue(inline_schema_supported("auto", "C:/tools/claude.exe"))
        self.assertTrue(inline_schema_supported("auto", "/usr/local/bin/claude"))
        self.assertTrue(inline_schema_supported(True, "C:/tools/claude.cmd"))
        self.assertTrue(inline_schema_supported("true", "C:/tools/claude.cmd"))
        self.assertFalse(inline_schema_supported(False, "/usr/local/bin/claude"))
        self.assertFalse(inline_schema_supported("off", "/usr/local/bin/claude"))

    def test_json_schema_flag_is_compact_valid_json_and_hidden_in_display(self):
        with TempRepo() as repo:
            write_contract(repo, "claude:\n  json_schema: true\n")
            config = load_config(repo)
            plan = claude_plan_command(config)
            audit = claude_audit_command(config)
            self.assertEqual(json.loads(flag_value(plan.argv, "--json-schema")), load_schema("plan"))
            self.assertEqual(json.loads(flag_value(audit.argv, "--json-schema")), load_schema("audit_verdict"))
            self.assertNotIn("\n", flag_value(plan.argv, "--json-schema"))
            self.assertIn("<plan schema>", plan.display())
            self.assertNotIn("additionalProperties", plan.display())
            write_contract(repo, "claude:\n  json_schema: false\n")
            self.assertNotIn("--json-schema", claude_plan_command(load_config(repo)).argv)

    def test_codex_audit_command_uses_output_schema_file_and_captures_last_message(self):
        with TempRepo() as repo:
            command = codex_review_command(load_config(repo), repo)
            self.assertEqual(command.argv[-1], "-")
            self.assertEqual(Path(flag_value(command.argv, "--output-schema")), schema_path("audit_verdict"))
            self.assertEqual(command.schema_name, "audit_verdict")
            self.assertTrue(command.capture_last_message)
            self.assertFalse(codex_exec_command(load_config(repo), repo).capture_last_message)


class RunnerTests(unittest.TestCase):
    def test_runner_reads_final_message_from_output_file_and_cleans_up(self):
        created: list[str] = []
        real_mkstemp = tempfile.mkstemp

        def tracking_mkstemp(*args, **kwargs):
            handle, path = real_mkstemp(*args, **kwargs)
            created.append(path)
            return handle, path

        with TempRepo() as repo, FakeCliPath():
            command = AgentCommand("codex-build", ["codex", "exec", "-C", str(repo), "-"], capture_last_message=True)
            with mock.patch("clodex.agents.tempfile.mkstemp", side_effect=tracking_mkstemp):
                result = AgentRunner(repo).run(command, "prompt")
            self.assertTrue(result.ok, result.stderr)
            self.assertEqual(result.last_message, "implemented")
            self.assertEqual(result.output, "implemented")
            self.assertEqual(len(created), 1)
            self.assertFalse(Path(created[0]).exists())

    def test_runner_without_capture_uses_stdout(self):
        with TempRepo() as repo, FakeCliPath():
            command = AgentCommand("codex-build", ["codex", "exec", "-C", str(repo), "-"])
            result = AgentRunner(repo).run(command, "prompt")
            self.assertIsNone(result.last_message)
            self.assertEqual(result.output.strip(), "implemented")


class WorkflowStructuredOutputTests(unittest.TestCase):
    def test_build_with_inline_json_schema_reaches_approved(self):
        with TempRepo() as repo, FakeCliPath():
            write_contract(repo, "claude:\n  json_schema: true\n")
            result = ClodexWorkflow(repo).build("implement fixture", workspace_backend="local")
            self.assertEqual(result.status, "approved")
            self.assertEqual(result.data["reviewers"]["claude-plan"]["approved"], True)
            self.assertEqual(result.data["reviewers"]["codex-architecture"]["approved"], True)

    def test_result_string_is_used_when_structured_output_is_absent(self):
        with TempRepo() as repo, FakeCliPath(no_structured_output=True):
            write_contract(repo, "claude:\n  json_schema: true\n")
            result = ClodexWorkflow(repo).plan("plan fixture")
            self.assertEqual(result.status, "planned")
            self.assertEqual(result.data["goal"], "test goal")

    def test_schema_violation_is_retried_once_then_succeeds(self):
        with TempRepo() as repo, FakeCliPath(schema_violation_once=True):
            result = ClodexWorkflow(repo).plan("plan fixture")
            self.assertEqual(result.status, "planned")
            plan = json.loads((Path(result.artifacts_dir) / "01-claude-plan.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(plan), sorted(COMPLETE_PLAN))

    def test_dry_run_lists_distinct_plan_and_audit_commands(self):
        with TempRepo() as repo:
            write_contract(repo, "claude:\n  json_schema: true\n")
            commands = ClodexWorkflow(repo).dry_run_commands()
            self.assertIn("--effort max", commands["claude_plan"])
            self.assertIn("--effort high", commands["claude_audit"])
            self.assertIn("<plan schema>", commands["claude_plan"])
            self.assertIn("<audit_verdict schema>", commands["claude_audit"])
            self.assertIn("--output-schema", commands["codex_audit"])


if __name__ == "__main__":
    unittest.main()
