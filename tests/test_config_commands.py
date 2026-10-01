"""Tests: config / argv construction and the strict fake CLIs."""

from __future__ import annotations

import contextlib
import io
import unittest
from clodex import config as config_module
from clodex.agents import AgentRunner
from clodex.commands import (
    AgentCommand,
    claude_plan_command,
    codex_exec_command,
    codex_review_command,
)
from clodex.config import load_config
from tests.support import TempRepo, FakeCliPath


class ConfigCommandTests(unittest.TestCase):
    def test_config_and_commands_use_requested_defaults(self):
        with TempRepo() as repo:
            config = load_config(repo)
            self.assertEqual(config.claude["model"], "opus")
            self.assertEqual(config.claude["effort"], "max")
            self.assertEqual(config.codex["model"], "gpt-6.1-sol")
            self.assertEqual(config.codex["reasoning_effort"], "xhigh")
            self.assertEqual(config.workspace["backend"], "git-worktree")
            self.assertEqual(config.workspace["apply_mode"], "manual")
            self.assertEqual(config.codex["approval_profile"], "ci")
            self.assertTrue(config.mcp["async_tasks"])
            self.assertTrue(config.tracing["enabled"])
            self.assertGreaterEqual(len(config.reviewers), 2)
            self.assertIn("--permission-mode", claude_plan_command(config).argv)
            self.assertIn("model_reasoning_effort=\"xhigh\"", codex_exec_command(config, repo).argv)
            audit_argv = codex_review_command(config, repo).argv
            self.assertEqual(audit_argv[:2], ["codex", "exec"])
            self.assertIn("read-only", audit_argv)

    def test_approval_profiles_change_codex_command(self):
        with TempRepo() as repo:
            config = load_config(repo)
            ci = codex_exec_command(config, repo).argv
            local = codex_exec_command(config, repo, approval_profile="local").argv
            auto = codex_exec_command(config, repo, approval_profile="auto_review").argv
            self.assertIn('approval_policy="never"', ci)
            self.assertIn('approval_policy="never"', local)
            self.assertIn("--approve-for-me", auto)
            self.assertNotIn('approval_policy="never"', auto)
            for argv in (ci, local, auto):
                self.assertNotIn("--ask-for-approval", argv)

    def test_fake_codex_rejects_removed_ask_for_approval_flag(self):
        with TempRepo() as repo, FakeCliPath():
            old_argv = ["codex", "exec", "-m", "gpt-6.1-sol", "--ask-for-approval", "never", "-"]
            result = AgentRunner(repo).run(AgentCommand("old-build", old_argv), "prompt")
            self.assertEqual(result.returncode, 2)
            self.assertIn("--ask-for-approval", result.stderr)
            old_review = ["codex", "review", "--uncommitted", "-"]
            self.assertEqual(AgentRunner(repo).run(AgentCommand("old-audit", old_review), "prompt").returncode, 2)

    def test_retiring_codex_model_warns_with_replacement(self):
        with TempRepo() as repo:
            contract = repo / "CLODEX.md"
            contract.write_text(contract.read_text(encoding="utf-8").replace("gpt-6.1-sol", "gpt-5.5"), encoding="utf-8")
            config_module._warned_models.discard("gpt-5.5")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                config = load_config(repo)
            self.assertEqual(config.codex["model"], "gpt-5.5")
            self.assertIn("gpt-5.5", stderr.getvalue())
            self.assertIn("gpt-6.1-sol", stderr.getvalue())
            self.assertIn("2026-10-14", stderr.getvalue())
