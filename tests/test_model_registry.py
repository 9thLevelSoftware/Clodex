"""Tests: model registry, doctor v2, capability probing, retired-model refusal and migration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest import mock

from clodex.capabilities import flags_in_help, supports
from clodex.commands import claude_plan_command, codex_review_command
from clodex.config import load_config
from clodex.doctor import run_doctor
from clodex.migrate import migrate_contract
from clodex.models import (
    ModelRetiredError,
    nearest_effort,
    parse_catalog,
    refresh_codex_catalog,
    retirement,
    supported_efforts,
    validate,
)
from clodex.tasks import TaskManager
from clodex.workflow import ClodexWorkflow
from tests.support import FAKE_CODEX_CATALOG, ROOT, FakeCliPath, TempRepo

BEFORE = date(2026, 9, 30)
AFTER = date(2026, 10, 15)


def write_contract(repo: Path, front_matter: str) -> None:
    (repo / "CLODEX.md").write_text(f"---\n{front_matter}---\nbody\n", encoding="utf-8")


def messages(diagnostics, level: str) -> list[str]:
    return [item.message for item in diagnostics if item.level == level]


class RegistryTests(unittest.TestCase):
    def test_retirement_dates(self):
        self.assertIsNone(retirement("gpt-6.1-sol"))
        self.assertIsNone(retirement("something-custom"))
        soon = retirement("gpt-5.5", BEFORE)
        self.assertFalse(soon.retired)
        self.assertEqual((soon.on, soon.successor), (date(2026, 10, 14), "gpt-6.1-sol"))
        self.assertTrue(retirement("gpt-5.5", date(2026, 10, 14)).retired)
        self.assertTrue(retirement("gpt-5.5", AFTER).retired)
        self.assertTrue(retirement("gpt-5.4", BEFORE).retired)

    def test_efforts_prefer_the_live_catalog_over_the_builtin_table(self):
        self.assertEqual(supported_efforts("gpt-6-luna"), ("low", "medium", "high", "xhigh", "max"))
        self.assertIsNone(supported_efforts("unknown-model"))
        live = {"gpt-6-luna": {"efforts": ["low", "high"]}}
        self.assertEqual(supported_efforts("gpt-6-luna", live), ("low", "high"))
        self.assertEqual(supported_efforts("gpt-6.1-sol", live), supported_efforts("gpt-6.1-sol"), "models missing from the catalog fall back")

    def test_nearest_effort(self):
        luna = supported_efforts("gpt-6-luna")
        self.assertEqual(nearest_effort("ultra", luna), "max")
        self.assertEqual(nearest_effort("high", luna), "high")
        self.assertEqual(nearest_effort("xhigh", ("low", "high")), "high")
        self.assertEqual(nearest_effort("low", ("medium", "high")), "medium", "nothing below: use the lowest supported")

    def test_parse_catalog(self):
        catalog = parse_catalog(json.dumps(FAKE_CODEX_CATALOG))
        self.assertEqual(catalog["gpt-6-luna"]["efforts"][-1], "max")
        self.assertEqual(catalog["gpt-6.1-sol"]["default_effort"], "low")
        plain = parse_catalog(json.dumps([{"id": "x", "supported_reasoning_efforts": ["low", "high"]}]))
        self.assertEqual(plain["x"]["efforts"], ["low", "high"])


class CatalogCacheTests(unittest.TestCase):
    def test_catalog_is_fetched_cached_refreshed_and_survives_failure(self):
        now = datetime(2026, 9, 30, tzinfo=UTC)
        with TempRepo() as repo:
            cache = repo / ".clodex" / "models-cache.json"
            with FakeCliPath(codex_catalog=FAKE_CODEX_CATALOG):
                first = refresh_codex_catalog(cache, now=now)
                self.assertIn("gpt-6.1-sol", first)
                self.assertTrue(cache.is_file())
                with mock.patch("clodex.models.subprocess.run", side_effect=AssertionError("fresh cache must not refetch")):
                    self.assertEqual(refresh_codex_catalog(cache, now=now + timedelta(hours=1)), first)
                changed = {"models": [{"slug": "gpt-7", "supported_reasoning_levels": [{"effort": "low"}]}]}
            with FakeCliPath(codex_catalog=changed):
                refreshed = refresh_codex_catalog(cache, now=now + timedelta(hours=25))
                self.assertEqual(list(refreshed), ["gpt-7"])
            with FakeCliPath(codex_catalog=None):  # `codex debug models` fails: keep the stale copy
                self.assertEqual(list(refresh_codex_catalog(cache, now=now + timedelta(hours=60))), ["gpt-7"])
            with mock.patch("clodex.models.shutil.which", return_value=None):  # no codex installed, nothing cached
                self.assertIsNone(refresh_codex_catalog(repo / "no-such-cache.json"))


class ValidateTests(unittest.TestCase):
    def check(self, front_matter: str, catalog=None, on=BEFORE):
        with TempRepo() as repo:
            write_contract(repo, front_matter)
            return validate(load_config(repo), catalog, on)

    def test_default_config_is_clean(self):
        with TempRepo() as repo:
            self.assertEqual(validate(load_config(repo), None, BEFORE), [])

    def test_retiring_and_retired_codex_models(self):
        soon = self.check("codex:\n  model: gpt-5.5\n", on=BEFORE)
        self.assertEqual([d.level for d in soon], ["warning"])
        self.assertIn("retires on 2026-10-14", soon[0].message)
        self.assertIn("gpt-6.1-sol", soon[0].fix)
        gone = self.check("codex:\n  model: gpt-5.5\n", on=AFTER)
        self.assertEqual([d.level for d in gone], ["error"])
        self.assertIn("retired on 2026-10-14", gone[0].message)

    def test_unsupported_effort_for_model(self):
        errors = messages(self.check("codex:\n  model: gpt-6-luna\n  reasoning_effort: ultra\n"), "error")
        self.assertEqual(len(errors), 1)
        self.assertIn("not supported by 'gpt-6-luna'", errors[0])
        self.assertEqual(self.check("codex:\n  model: gpt-6.1-sol\n  reasoning_effort: ultra\n"), [])

    def test_live_catalog_overrides_builtin_and_flags_unknown_models(self):
        catalog = parse_catalog(json.dumps({"models": [{"slug": "gpt-6-luna", "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]}]}))
        errors = messages(self.check("codex:\n  model: gpt-6-luna\n  reasoning_effort: max\n", catalog), "error")
        self.assertTrue(errors and "supported: low, high" in errors[0])
        warnings = messages(self.check("codex:\n  model: gpt-9-mystery\n  reasoning_effort: high\n", catalog), "warning")
        self.assertTrue(any("not in the Codex model catalog" in w for w in warnings))
        self.assertEqual(self.check("codex:\n  model: gpt-9-mystery\n  reasoning_effort: high\n"), [], "no catalog: unknown models are allowed")

    def test_audit_override_is_checked_separately(self):
        found = self.check("codex:\n  audit:\n    model: gpt-5.5\n", on=AFTER)
        self.assertEqual([d.where for d in found if d.level == "error"], ["codex.audit.model"])

    def test_claude_checks(self):
        found = self.check("claude:\n  plan: {model: opus-ish, effort: turbo}\n")
        self.assertTrue(any("not a known Claude alias" in m for m in messages(found, "warning")))
        self.assertTrue(any("'turbo'" in m for m in messages(found, "error")))
        self.assertEqual(self.check("claude:\n  plan: {model: claude-opus-5-5, effort: xhigh}\n  audit: {model: 'opus[1m]', effort: low}\n"), [])

    def test_reviewer_and_misc_settings(self):
        reviewers = json.dumps([
            {"id": "a", "backend": "claude", "required": False},
            {"id": "a", "backend": "gemini", "required": False},
        ])
        found = self.check(f"audit:\n  quorum: most\n  reviewers: {reviewers}\ncodex:\n  sandbox: yolo\n  approval_profile: never\nworkspace:\n  backend: docker\n")
        text = " | ".join(messages(found, "error"))
        for expected in ("duplicate reviewer id 'a'", "unknown backend 'gemini'", "no required reviewers", "Unsupported audit.quorum", "unknown sandbox", "unknown approval profile", "unknown workspace backend"):
            self.assertIn(expected, text)


class CapabilityTests(unittest.TestCase):
    def test_flags_in_help_matches_whole_flags_only(self):
        text = "  --json-schema <schema>\n  --effort-level foo\n  --max-budget-usd <n>\n"
        self.assertEqual(flags_in_help(text, ("--json-schema", "--effort", "--max-budget-usd")), ["--json-schema", "--max-budget-usd"])


class DoctorTests(unittest.TestCase):
    def test_healthy_setup_has_no_diagnostics_and_writes_caches(self):
        with TempRepo() as repo, FakeCliPath(codex_catalog=FAKE_CODEX_CATALOG):
            code, data = run_doctor(repo)
            self.assertEqual(code, 0, data["diagnostics"])
            self.assertEqual(data["diagnostics"], [])
            self.assertEqual(data["auth"], {"claude": {"status": "logged-in"}, "codex": {"status": "logged-in"}})
            self.assertEqual(data["capabilities"]["claude"]["missing_required"], [])
            self.assertTrue((repo / ".clodex" / "capabilities.json").is_file())
            self.assertTrue((repo / ".clodex" / "models-cache.json").is_file())

    def test_missing_optional_flag_warns_and_commands_degrade(self):
        with TempRepo() as repo, FakeCliPath(help_lacks=("--json-schema", "--output-schema", "--output-last-message")):
            write_contract(repo, "claude:\n  json_schema: true\n")
            code, data = run_doctor(repo)
            self.assertEqual(code, 0)
            self.assertEqual(len(messages_from(data, "warning")), 3)
            self.assertEqual(run_doctor(repo, strict=True)[0], 1, "--strict fails on warnings")
            config = load_config(repo)
            self.assertIs(supports(repo, "claude", "--json-schema"), False)
            self.assertIs(supports(repo, "codex", "--output-schema"), False)
            self.assertNotIn("--json-schema", claude_plan_command(config).argv)
            audit = codex_review_command(config, repo)
            self.assertNotIn("--output-schema", audit.argv)
            self.assertFalse(audit.capture_last_message)
            self.assertEqual(audit.schema_name, "audit_verdict", "output is still validated against the schema")

    def test_full_support_keeps_all_flags(self):
        with TempRepo() as repo, FakeCliPath():
            write_contract(repo, "claude:\n  json_schema: true\n")
            run_doctor(repo)
            config = load_config(repo)
            self.assertIn("--json-schema", claude_plan_command(config).argv)
            self.assertIn("--output-schema", codex_review_command(config, repo).argv)
            self.assertTrue(codex_review_command(config, repo).capture_last_message)

    def test_missing_required_flag_is_an_error(self):
        with TempRepo() as repo, FakeCliPath(help_lacks=("--effort",)):
            code, data = run_doctor(repo)
            self.assertEqual(code, 1)
            self.assertTrue(any("--effort" in m for m in messages_from(data, "error")))
            self.assertFalse(data["ok"])

    def test_logged_out_clis_are_errors(self):
        for kwargs, who in (({"claude_logged_in": False}, "claude"), ({"codex_logged_in": False}, "codex")):
            with TempRepo() as repo, FakeCliPath(**kwargs):
                code, data = run_doctor(repo)
                self.assertEqual(code, 1, who)
                self.assertEqual(data["auth"][who]["status"], "logged-out")
                self.assertTrue(any(f"{who} is not logged in" in m for m in messages_from(data, "error")))

    def test_config_problems_from_the_registry_show_up(self):
        with TempRepo() as repo, FakeCliPath(codex_catalog=FAKE_CODEX_CATALOG):
            write_contract(repo, "codex:\n  model: gpt-6-luna\n  reasoning_effort: ultra\n")
            code, data = run_doctor(repo)
            self.assertEqual(code, 1)
            self.assertTrue(any("not supported by 'gpt-6-luna'" in m for m in messages_from(data, "error")))

    def test_probe_can_be_skipped(self):
        with TempRepo() as repo, FakeCliPath(help_lacks=("--effort",)):
            code, data = run_doctor(repo, probe=False)
            self.assertEqual(code, 0)
            self.assertNotIn("capabilities", data)


def messages_from(data: dict, level: str) -> list[str]:
    return [item["message"] for item in data["diagnostics"] if item["level"] == level]


class RetiredModelRefusalTests(unittest.TestCase):
    def retired_contract(self, repo: Path, scope: str = "codex:\n  model: gpt-5.4\n") -> None:
        write_contract(repo, scope)

    def test_build_and_audit_refuse_but_plan_and_dry_runs_work(self):
        with TempRepo() as repo, FakeCliPath():
            self.retired_contract(repo)
            workflow = ClodexWorkflow(repo)
            with self.assertRaisesRegex(ModelRetiredError, "gpt-5.4.*retired on 2026-08-31.*init --migrate"):
                workflow.build("implement fixture", workspace_backend="local")
            with self.assertRaises(ModelRetiredError):
                workflow.audit()
            self.assertEqual(workflow.build("implement fixture", dry_run=True).status, "dry-run")
            self.assertEqual(workflow.plan("plan fixture").status, "planned", "planning only uses Claude")

    def test_audit_override_model_is_checked_too(self):
        with TempRepo() as repo, FakeCliPath():
            self.retired_contract(repo, "codex:\n  audit:\n    model: gpt-5.4\n")
            with self.assertRaisesRegex(ModelRetiredError, "codex.audit.model"):
                ClodexWorkflow(repo).build("implement fixture", workspace_backend="local")

    def test_environment_override_allows_the_run(self):
        with TempRepo() as repo, FakeCliPath(), mock.patch.dict(os.environ, {"CLODEX_ALLOW_RETIRED_MODEL": "1"}):
            self.retired_contract(repo)
            self.assertEqual(ClodexWorkflow(repo).build("implement fixture", workspace_backend="local").status, "approved")

    def test_task_start_refuses_before_creating_a_run(self):
        with TempRepo() as repo:
            self.retired_contract(repo)
            manager = TaskManager(repo)
            with self.assertRaises(ModelRetiredError):
                manager.start("slow fixture")
            self.assertEqual(manager.state.list_runs(), [])
            self.assertEqual(manager.start("slow fixture", dry_run=True).status, "dry-run")

    def test_worker_for_an_already_queued_run_records_the_failure(self):
        with TempRepo() as repo:
            self.retired_contract(repo)
            workflow = ClodexWorkflow(repo)
            workflow.state.upsert_task("t", "task", "queued")
            workflow.state.create_run("r1", "t", "do it", "queued")
            result = workflow.run_existing("r1")
            self.assertEqual(result.status, "failed")
            run = workflow.state.get_run("r1")
            self.assertEqual(run["status"], "failed")
            self.assertIn("retired", run["error"])

    def test_cli_exits_2_with_a_clear_message(self):
        with TempRepo() as repo:
            self.retired_contract(repo)
            result = subprocess.run(
                [sys.executable, "-m", "clodex", "build", "something"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("retired on 2026-08-31", result.stderr)
            self.assertNotIn("Traceback", result.stderr)


class MigrateTests(unittest.TestCase):
    def test_retiring_model_is_replaced_keeping_quotes_comments_and_order(self):
        text = '---\nversion: 1\ncodex:\n  model: "gpt-5.5"   # pinned by us\n  reasoning_effort: xhigh\n  sandbox: workspace-write\n---\nbody stays\n'
        new, changes = migrate_contract(text, today=BEFORE)
        self.assertEqual(new, text.replace('"gpt-5.5"', '"gpt-6.1-sol"'))
        self.assertEqual(changes, ["codex.model: gpt-5.5 -> gpt-6.1-sol (retires 2026-10-14)"])
        self.assertEqual(migrate_contract(new, today=BEFORE), (new, []), "idempotent")

    def test_crlf_endings_are_preserved(self):
        text = "---\r\ncodex:\r\n  model: gpt-5.4\r\n  reasoning_effort: high\r\n---\r\nbody\r\n"
        new, changes = migrate_contract(text)
        self.assertEqual(new, text.replace("gpt-5.4", "gpt-6.1-sol"))
        self.assertNotIn("\n", new.replace("\r\n", ""))
        self.assertEqual(len(changes), 1)

    def test_unsupported_effort_is_clamped_to_the_nearest_supported_level(self):
        text = "---\ncodex:\n  model: gpt-6-luna\n  reasoning_effort: ultra   # too high\n---\n"
        new, changes = migrate_contract(text)
        self.assertIn("reasoning_effort: max   # too high", new)
        self.assertIn("ultra -> max", changes[0])
        sol, none = migrate_contract("---\ncodex:\n  model: gpt-5.5\n  reasoning_effort: ultra\n---\n", today=BEFORE)
        self.assertIn("model: gpt-6.1-sol", sol)
        self.assertIn("reasoning_effort: ultra", sol, "ultra is valid on the successor, so it stays")

    def test_audit_override_is_migrated_too(self):
        text = "---\ncodex:\n  model: gpt-6.1-sol\n  audit:\n    model: gpt-5.5\n    reasoning_effort: xhigh\n---\n"
        new, changes = migrate_contract(text, today=BEFORE)
        self.assertIn("    model: gpt-6.1-sol\n    reasoning_effort: xhigh", new)
        self.assertTrue(changes[0].startswith("codex.audit.model"))

    def test_files_without_front_matter_or_codex_section_are_untouched(self):
        for text in ("no front matter\nmodel: gpt-5.5\n", "---\nclaude:\n  model: opus\n---\n", ""):
            self.assertEqual(migrate_contract(text), (text, []))

    def test_split_claude_creates_plan_and_audit_roles(self):
        text = "---\nclaude:\n  model: sonnet\n  effort: max\n  permission_mode: plan\ncodex:\n  model: gpt-6.1-sol\n---\nbody\n"
        new, changes = migrate_contract(text, split_claude=True)
        self.assertEqual(
            new,
            "---\nclaude:\n  plan:\n    model: sonnet\n    effort: max\n  audit:\n    model: sonnet\n    effort: high\n  permission_mode: plan\n"
            "codex:\n  model: gpt-6.1-sol\n---\nbody\n",
        )
        self.assertEqual(len(changes), 1)
        self.assertEqual(migrate_contract(new, split_claude=True), (new, []), "already split: nothing to do")
        with TempRepo() as repo:
            (repo / "CLODEX.md").write_text(new, encoding="utf-8")
            config = load_config(repo)
            self.assertEqual((config.claude_role("plan")["model"], config.claude_role("plan")["effort"]), ("sonnet", "max"))
            self.assertEqual((config.claude_role("audit")["model"], config.claude_role("audit")["effort"]), ("sonnet", "high"))

    def test_split_claude_is_opt_in_and_handles_a_lone_effort(self):
        text = "---\nclaude:\n  effort: high\n---\n"
        self.assertEqual(migrate_contract(text), (text, []))
        new, _ = migrate_contract(text, split_claude=True)
        self.assertIn("  plan:\n    model: opus\n    effort: high\n  audit:\n    model: opus\n    effort: high\n", new)

    def test_shipped_contract_needs_no_migration(self):
        text = (ROOT / "CLODEX.md").read_text(encoding="utf-8")
        self.assertEqual(migrate_contract(text, split_claude=True, today=AFTER), (text, []))


class MigrateCliTests(unittest.TestCase):
    def run_cli(self, repo: Path, *args: str) -> tuple[int, dict]:
        result = subprocess.run(
            [sys.executable, "-m", "clodex", "--json", "init", "--migrate", *args],
            cwd=repo,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        return result.returncode, json.loads(result.stdout)

    def test_dry_run_reports_without_writing_then_real_run_writes(self):
        with TempRepo() as repo:
            original = "---\ncodex:\n  model: gpt-5.4\n---\nbody\n"
            (repo / "CLODEX.md").write_text(original, encoding="utf-8", newline="")
            code, data = self.run_cli(repo, "--dry-run")
            self.assertEqual((code, data["written"]), (0, False))
            self.assertEqual(len(data["changes"]), 1)
            self.assertEqual((repo / "CLODEX.md").read_text(encoding="utf-8"), original)
            code, data = self.run_cli(repo)
            self.assertTrue(data["written"])
            self.assertIn("model: gpt-6.1-sol", (repo / "CLODEX.md").read_text(encoding="utf-8"))
            self.assertEqual(self.run_cli(repo)[1], {**self.run_cli(repo)[1], "changes": [], "written": False})

    def test_missing_contract_is_an_error(self):
        with TempRepo() as repo:
            (repo / "CLODEX.md").unlink()
            code, data = self.run_cli(repo)
            self.assertEqual(code, 1)
            self.assertFalse(data["ok"])


if __name__ == "__main__":
    unittest.main()
