from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agents import AgentRunner
from .artifacts import ArtifactStore, current_diff, hash_text, make_run_id, make_task_id
from .commands import claude_audit_command, claude_plan_command, codex_exec_command, codex_review_command
from .config import ClodexConfig, load_config
from .jsonutil import extract_json_object
from .prompts import audit_prompt, fix_prompt, implementation_prompt, plan_prompt
from .state import StateStore


@dataclass
class WorkflowResult:
    status: str
    run_id: str | None
    task_id: str | None
    artifacts_dir: str | None
    data: dict[str, Any]


class ClodexWorkflow:
    def __init__(self, repo_root: Path | None = None):
        self.config: ClodexConfig = load_config(repo_root)
        self.repo_root = self.config.repo_root
        self._state: StateStore | None = None
        self.runner = AgentRunner(self.repo_root)

    @property
    def state(self) -> StateStore:
        if self._state is None:
            self._state = StateStore(self.config.state_path)
        return self._state

    def dry_run_commands(self) -> dict[str, str]:
        return {
            "claude_plan": claude_plan_command(self.config).display(),
            "codex_build": codex_exec_command(self.config, self.repo_root).display(),
            "claude_audit": claude_audit_command(self.config).display(),
            "codex_audit": codex_review_command(self.config).display(),
        }

    def plan(self, task: str, dry_run: bool = False) -> WorkflowResult:
        if dry_run:
            return WorkflowResult("dry-run", None, None, None, {"commands": self.dry_run_commands(), "task": task})

        task_id = make_task_id(task)
        run_id = make_run_id(task_id)
        artifacts = ArtifactStore(self.config, run_id)
        self.state.upsert_task(task_id, task, "planning")
        self.state.create_run(run_id, task_id, task, "planning")
        prompt = plan_prompt(self.config, task)
        plan_json = self._run_json_with_retry(claude_plan_command(self.config), prompt, "Claude planning")
        artifacts.write_json("01-claude-plan.json", plan_json)
        self.state.add_message(task_id, "planning", json.dumps(plan_json, indent=2))
        self.state.update_run(run_id, "planned")
        self.state.update_task(task_id, "planned")
        return WorkflowResult("planned", run_id, task_id, str(artifacts.path), plan_json)

    def build(self, task: str, dry_run: bool = False) -> WorkflowResult:
        if dry_run:
            return WorkflowResult("dry-run", None, None, None, {"commands": self.dry_run_commands(), "task": task})

        task_id = make_task_id(task)
        run_id = make_run_id(task_id)
        artifacts = ArtifactStore(self.config, run_id)
        self.state.upsert_task(task_id, task, "running")
        self.state.create_run(run_id, task_id, task, "running")

        plan_json = self._run_json_with_retry(claude_plan_command(self.config), plan_prompt(self.config, task), "Claude planning")
        artifacts.write_json("01-claude-plan.json", plan_json)

        implementation = self.runner.run(codex_exec_command(self.config, self.repo_root), implementation_prompt(plan_json, task))
        artifacts.write_text("02-codex-implementation.md", self._format_agent_report(implementation.stdout, implementation.stderr))
        if not implementation.ok:
            self.state.update_run(run_id, "blocked")
            self.state.update_task(task_id, "blocked")
            return WorkflowResult("blocked", run_id, task_id, str(artifacts.path), {"error": "Codex implementation failed"})

        result = self._audit_loop(task_id, run_id, artifacts, plan_json)
        return result

    def audit(self, dry_run: bool = False) -> WorkflowResult:
        if dry_run:
            return WorkflowResult("dry-run", None, None, None, {"commands": self.dry_run_commands()})
        task = "Audit current uncommitted changes"
        task_id = make_task_id(task)
        run_id = make_run_id(task_id)
        artifacts = ArtifactStore(self.config, run_id)
        self.state.upsert_task(task_id, task, "auditing")
        self.state.create_run(run_id, task_id, task, "auditing")
        plan_json = {"goal": task, "acceptance_criteria": ["Current diff is safe to ship"], "implementation_spec": []}
        artifacts.write_json("01-claude-plan.json", plan_json)
        return self._audit_loop(task_id, run_id, artifacts, plan_json)

    def _audit_loop(self, task_id: str, run_id: str, artifacts: ArtifactStore, plan_json: dict[str, Any]) -> WorkflowResult:
        agreement: dict[str, Any] = {}
        for attempt in range(self.config.max_fix_loops + 1):
            diff = current_diff(self.repo_root)
            diff_hash = hash_text(diff)
            artifacts.write_text("changes.diff", diff)
            claude_audit = self._run_json_with_retry(
                claude_audit_command(self.config),
                audit_prompt("Claude Code", plan_json, diff, diff_hash),
                "Claude audit",
            )
            codex_audit = self._run_json_with_retry(
                codex_review_command(self.config),
                audit_prompt("Codex", plan_json, diff, diff_hash),
                "Codex audit",
            )
            artifacts.write_json(f"audit-attempt-{attempt}-claude.json", claude_audit)
            artifacts.write_json(f"audit-attempt-{attempt}-codex.json", codex_audit)
            artifacts.write_json("03-claude-audit.json", claude_audit)
            artifacts.write_json("04-codex-audit.json", codex_audit)
            self.state.add_audit(run_id, "claude", self._approved(claude_audit, diff_hash), claude_audit.get("diff_hash"), json.dumps(claude_audit))
            self.state.add_audit(run_id, "codex", self._approved(codex_audit, diff_hash), codex_audit.get("diff_hash"), json.dumps(codex_audit))
            agreement = {
                "approved": self._approved(claude_audit, diff_hash) and self._approved(codex_audit, diff_hash),
                "attempt": attempt,
                "diff_hash": diff_hash,
                "claude_approved": self._approved(claude_audit, diff_hash),
                "codex_approved": self._approved(codex_audit, diff_hash),
            }
            artifacts.write_json("05-agreement.json", agreement)
            self.state.update_run(run_id, "approved" if agreement["approved"] else "needs-fix", diff_hash)
            if agreement["approved"]:
                self.state.update_task(task_id, "done")
                return WorkflowResult("approved", run_id, task_id, str(artifacts.path), agreement)
            if attempt >= self.config.max_fix_loops:
                break
            findings = self._required_fixes(claude_audit, codex_audit)
            fix = self.runner.run(codex_exec_command(self.config, self.repo_root), fix_prompt(plan_json, findings))
            artifacts.write_text(f"fix-attempt-{attempt}.md", self._format_agent_report(fix.stdout, fix.stderr))
            if not fix.ok:
                break
        self.state.update_run(run_id, "blocked", agreement.get("diff_hash"))
        self.state.update_task(task_id, "blocked")
        return WorkflowResult("blocked", run_id, task_id, str(artifacts.path), agreement)

    def _run_json_with_retry(self, command, prompt: str, label: str) -> dict[str, Any]:
        last_error = ""
        current_prompt = prompt
        for attempt in range(2):
            result = self.runner.run(command, current_prompt)
            if not result.ok:
                raise RuntimeError(f"{label} failed with exit code {result.returncode}: {result.stderr.strip()}")
            try:
                return extract_json_object(result.stdout)
            except ValueError as exc:
                last_error = str(exc)
                current_prompt = (
                    prompt
                    + "\n\nYour previous response was not valid JSON. Return only the required JSON object. "
                    + f"Parser error: {last_error}"
                )
        raise RuntimeError(f"{label} did not return parseable JSON: {last_error}")

    @staticmethod
    def _approved(audit: dict[str, Any], diff_hash: str) -> bool:
        return bool(audit.get("approved")) and audit.get("diff_hash") == diff_hash

    @staticmethod
    def _required_fixes(*audits: dict[str, Any]) -> list[str]:
        fixes: list[str] = []
        for audit in audits:
            for fix in audit.get("required_fixes", []) or []:
                fixes.append(str(fix))
            for finding in audit.get("findings", []) or []:
                if isinstance(finding, dict):
                    fixes.append(str(finding.get("message", finding)))
                else:
                    fixes.append(str(finding))
        return fixes or ["Resolve audit disagreement and make the diff satisfy the accepted plan."]

    @staticmethod
    def _format_agent_report(stdout: str, stderr: str) -> str:
        report = stdout.strip()
        if stderr.strip():
            report += ("\n\n" if report else "") + "stderr:\n" + stderr.strip()
        return report + "\n"
