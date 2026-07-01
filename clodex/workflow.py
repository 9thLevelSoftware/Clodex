from __future__ import annotations

import json
import subprocess
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
from .trace import TraceWriter
from .workspace import DirtyWorkspaceError, WorkspaceManager, WorkspaceRef


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

    @property
    def state(self) -> StateStore:
        if self._state is None:
            self._state = StateStore(self.config.state_path)
        return self._state

    def dry_run_commands(self, workspace_root: Path | None = None, approval_profile: str | None = None) -> dict[str, str]:
        root = workspace_root or self.repo_root
        return {
            "claude_plan": claude_plan_command(self.config).display(),
            "codex_build": codex_exec_command(self.config, root, approval_profile=approval_profile).display(),
            "claude_audit": claude_audit_command(self.config).display(),
            "codex_audit": codex_review_command(self.config).display(),
        }

    def plan(self, task: str, dry_run: bool = False) -> WorkflowResult:
        if dry_run:
            return WorkflowResult("dry-run", None, None, None, {"commands": self.dry_run_commands(), "task": task})

        task_id = make_task_id(task)
        run_id = make_run_id(task_id)
        artifacts = ArtifactStore(self.config, run_id)
        trace = TraceWriter(artifacts.path, run_id, self.state if self.config.tracing.get("enabled") else None)
        self.state.upsert_task(task_id, task, "planning")
        self.state.create_run(run_id, task_id, task, "planning", artifacts_dir=str(artifacts.path))
        trace.event("run.start", {"command": "plan", "task": task})
        prompt = plan_prompt(self.config, task)
        plan_json = self._run_json_with_retry(AgentRunner(self.repo_root), claude_plan_command(self.config), prompt, "Claude planning", trace)
        artifacts.write_json("01-claude-plan.json", plan_json)
        self.state.add_message(task_id, "planning", json.dumps(plan_json, indent=2))
        self.state.update_run(run_id, "planned")
        self.state.update_task(task_id, "planned")
        trace.event("run.complete", {"status": "planned"})
        return WorkflowResult("planned", run_id, task_id, str(artifacts.path), plan_json)

    def build(
        self,
        task: str,
        dry_run: bool = False,
        workspace_backend: str | None = None,
        approval_profile: str | None = None,
        apply_changes: bool = False,
    ) -> WorkflowResult:
        if dry_run:
            return WorkflowResult(
                "dry-run",
                None,
                None,
                None,
                {
                    "commands": self.dry_run_commands(approval_profile=approval_profile),
                    "task": task,
                    "workspace": workspace_backend or self.config.workspace["backend"],
                    "approval_profile": approval_profile or self.config.codex["approval_profile"],
                },
            )

        task_id = make_task_id(task)
        run_id = make_run_id(task_id)
        artifacts = ArtifactStore(self.config, run_id)
        self.state.upsert_task(task_id, task, "running")
        self.state.create_run(run_id, task_id, task, "running", artifacts_dir=str(artifacts.path))
        return self._execute_build(task, task_id, run_id, artifacts, workspace_backend, approval_profile, apply_changes)

    def run_existing(
        self,
        run_id: str,
        workspace_backend: str | None = None,
        approval_profile: str | None = None,
        apply_changes: bool = False,
    ) -> WorkflowResult:
        run = self.state.get_run(run_id)
        if run is None:
            raise ValueError(f"Unknown run: {run_id}")
        artifacts = ArtifactStore(self.config, run_id)
        return self._execute_build(str(run["prompt"]), str(run["task_id"]), run_id, artifacts, workspace_backend, approval_profile, apply_changes)

    def audit(self, dry_run: bool = False) -> WorkflowResult:
        if dry_run:
            return WorkflowResult("dry-run", None, None, None, {"commands": self.dry_run_commands()})
        task = "Audit current uncommitted changes"
        task_id = make_task_id(task)
        run_id = make_run_id(task_id)
        artifacts = ArtifactStore(self.config, run_id)
        trace = TraceWriter(artifacts.path, run_id, self.state if self.config.tracing.get("enabled") else None)
        self.state.upsert_task(task_id, task, "auditing")
        self.state.create_run(run_id, task_id, task, "auditing", workspace_path=str(self.repo_root), artifacts_dir=str(artifacts.path))
        plan_json = {"goal": task, "acceptance_criteria": ["Current diff is safe to ship"], "implementation_spec": []}
        artifacts.write_json("01-claude-plan.json", plan_json)
        return self._audit_loop(task_id, run_id, artifacts, plan_json, AgentRunner(self.repo_root), self.repo_root, trace, None)

    def apply_run(self, run_id: str | None, check: bool = False) -> WorkflowResult:
        if not run_id:
            raise ValueError("run_id is required")
        run = self.state.get_run(run_id)
        if run is None:
            raise ValueError(f"Unknown run: {run_id}")
        artifacts_dir = Path(str(run.get("artifacts_dir") or self.config.runs_root / run_id))
        patch = artifacts_dir / "apply.patch"
        if not patch.exists():
            patch = artifacts_dir / "changes.diff"
        if not patch.exists():
            raise ValueError(f"No patch artifact found for run: {run_id}")
        argv = ["git", "apply", "--check" if check else "--whitespace=nowarn", str(patch)]
        result = subprocess.run(argv, cwd=self.repo_root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if result.returncode != 0:
            return WorkflowResult("apply-check-failed" if check else "apply-failed", run_id, run.get("task_id"), str(artifacts_dir), {"stderr": result.stderr})
        if check:
            return WorkflowResult("apply-check", run_id, run.get("task_id"), str(artifacts_dir), {"patch": str(patch)})
        self.state.update_run(run_id, "applied")
        return WorkflowResult("applied", run_id, run.get("task_id"), str(artifacts_dir), {"patch": str(patch)})

    def _execute_build(
        self,
        task: str,
        task_id: str,
        run_id: str,
        artifacts: ArtifactStore,
        workspace_backend: str | None,
        approval_profile: str | None,
        apply_changes: bool,
    ) -> WorkflowResult:
        trace = TraceWriter(artifacts.path, run_id, self.state if self.config.tracing.get("enabled") else None)
        trace.event("run.start", {"command": "build", "task": task})
        try:
            workspace = WorkspaceManager(self.repo_root, self.config).prepare(run_id, workspace_backend)
        except DirtyWorkspaceError as exc:
            self.state.update_run(run_id, "blocked", error=str(exc))
            self.state.update_task(task_id, "blocked")
            trace.event("workspace.blocked", {"error": str(exc)})
            return WorkflowResult("blocked", run_id, task_id, str(artifacts.path), {"error": str(exc)})

        WorkspaceManager(self.repo_root, self.config).write_metadata(artifacts.path, workspace)
        self.state.add_workspace_lock(run_id, str(workspace.source_path), str(workspace.path), workspace.backend)
        self.state.update_run(run_id, "running", workspace_path=str(workspace.path), artifacts_dir=str(artifacts.path))
        trace.event("workspace.ready", workspace.as_dict())

        runner = AgentRunner(workspace.path)
        if self.state.cancellation_requested(run_id):
            self.state.complete_cancel(run_id)
            trace.event("run.cancelled", {})
            return WorkflowResult("cancelled", run_id, task_id, str(artifacts.path), {"workspace": workspace.as_dict()})

        plan_json = self._run_json_with_retry(runner, claude_plan_command(self.config), plan_prompt(self.config, task), "Claude planning", trace)
        artifacts.write_json("01-claude-plan.json", plan_json)
        if self._cancelled(run_id, trace):
            return WorkflowResult("cancelled", run_id, task_id, str(artifacts.path), {"workspace": workspace.as_dict()})

        implementation = runner.run(codex_exec_command(self.config, workspace.path, approval_profile=approval_profile), implementation_prompt(plan_json, task))
        trace.event("command.complete", {"name": implementation.command.name, "returncode": implementation.returncode})
        artifacts.write_text("02-codex-implementation.md", self._format_agent_report(implementation.stdout, implementation.stderr))
        if not implementation.ok:
            self.state.update_run(run_id, "blocked", error="Codex implementation failed")
            self.state.update_task(task_id, "blocked")
            return WorkflowResult("blocked", run_id, task_id, str(artifacts.path), {"error": "Codex implementation failed", "workspace": workspace.as_dict()})
        if self._cancelled(run_id, trace):
            return WorkflowResult("cancelled", run_id, task_id, str(artifacts.path), {"workspace": workspace.as_dict()})

        result = self._audit_loop(task_id, run_id, artifacts, plan_json, runner, workspace.path, trace, approval_profile, include_untracked=workspace.is_worktree)
        result.data["workspace"] = workspace.as_dict()
        if result.status == "approved" and workspace.is_worktree:
            self._include_untracked(workspace.path)
            patch = artifacts.write_text("apply.patch", current_diff(workspace.path))
            result.data["apply_patch"] = str(patch)
            if apply_changes:
                return self.apply_run(run_id)
        return result

    def _audit_loop(
        self,
        task_id: str,
        run_id: str,
        artifacts: ArtifactStore,
        plan_json: dict[str, Any],
        runner: AgentRunner,
        diff_root: Path,
        trace: TraceWriter,
        approval_profile: str | None,
        include_untracked: bool = False,
    ) -> WorkflowResult:
        agreement: dict[str, Any] = {}
        for attempt in range(self.config.max_fix_loops + 1):
            if include_untracked:
                self._include_untracked(diff_root)
            diff = current_diff(diff_root)
            diff_hash = hash_text(diff)
            artifacts.write_text("changes.diff", diff)
            verdicts = self._run_reviewers(artifacts, plan_json, diff, diff_hash, runner, trace, attempt)
            agreement = self._agreement(verdicts, diff_hash, attempt)
            artifacts.write_json("05-agreement.json", agreement)
            self.state.update_run(run_id, "approved" if agreement["approved"] else "needs-fix", diff_hash)
            trace.event("audit.agreement", agreement)
            if agreement["approved"]:
                self.state.update_task(task_id, "done")
                trace.event("run.complete", {"status": "approved"})
                return WorkflowResult("approved", run_id, task_id, str(artifacts.path), agreement)
            if attempt >= self.config.max_fix_loops:
                break
            findings = self._required_fixes(*verdicts)
            fix = runner.run(codex_exec_command(self.config, diff_root, approval_profile=approval_profile), fix_prompt(plan_json, findings))
            artifacts.write_text(f"fix-attempt-{attempt}.md", self._format_agent_report(fix.stdout, fix.stderr))
            trace.event("fix.complete", {"attempt": attempt, "returncode": fix.returncode})
            if not fix.ok or self._cancelled(run_id, trace):
                break
        self.state.update_run(run_id, "blocked", agreement.get("diff_hash"))
        self.state.update_task(task_id, "blocked")
        trace.event("run.complete", {"status": "blocked"})
        return WorkflowResult("blocked", run_id, task_id, str(artifacts.path), agreement)

    @staticmethod
    def _include_untracked(repo_root: Path) -> None:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0 or not result.stdout:
            return
        files = [item.decode("utf-8", errors="replace") for item in result.stdout.split(b"\0") if item]
        if files:
            subprocess.run(["git", "add", "-N", "--", *files], cwd=repo_root, check=False, capture_output=True)

    def _run_reviewers(
        self,
        artifacts: ArtifactStore,
        plan_json: dict[str, Any],
        diff: str,
        diff_hash: str,
        runner: AgentRunner,
        trace: TraceWriter,
        attempt: int,
    ) -> list[dict[str, Any]]:
        verdicts: list[dict[str, Any]] = []
        first_claude: dict[str, Any] | None = None
        first_codex: dict[str, Any] | None = None
        for reviewer in self.config.reviewers:
            backend = str(reviewer.get("backend", "codex"))
            reviewer_id = str(reviewer.get("id", backend))
            persona = str(reviewer.get("persona", reviewer_id))
            timeout = int(reviewer.get("timeout", 600))
            command = claude_audit_command(self.config) if backend == "claude" else codex_review_command(self.config)
            verdict = self._run_json_with_retry(
                runner,
                command,
                audit_prompt(backend.title(), plan_json, diff, diff_hash, reviewer_id, persona),
                f"{reviewer_id} audit",
                trace,
                timeout=timeout,
            )
            verdict.setdefault("reviewer_id", reviewer_id)
            verdict.setdefault("persona", persona)
            verdict["_required"] = bool(reviewer.get("required", True))
            artifacts.write_json(f"reviewers/{reviewer_id}.json", verdict)
            artifacts.write_json(f"audit-attempt-{attempt}-{reviewer_id}.json", verdict)
            self.state.add_audit(artifacts.run_id, reviewer_id, self._approved(verdict, diff_hash), verdict.get("diff_hash"), json.dumps(verdict))
            trace.event("audit.verdict", {"reviewer_id": reviewer_id, "approved": self._approved(verdict, diff_hash), "required": verdict["_required"]})
            verdicts.append(verdict)
            if backend == "claude" and first_claude is None:
                first_claude = verdict
            if backend == "codex" and first_codex is None:
                first_codex = verdict
        if first_claude is not None:
            artifacts.write_json("03-claude-audit.json", first_claude)
        if first_codex is not None:
            artifacts.write_json("04-codex-audit.json", first_codex)
        return verdicts

    def _run_json_with_retry(
        self,
        runner: AgentRunner,
        command,
        prompt: str,
        label: str,
        trace: TraceWriter | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        last_error = ""
        current_prompt = prompt
        for _attempt in range(2):
            if trace:
                trace.event("command.start", {"name": command.name})
            result = runner.run(command, current_prompt, timeout=timeout)
            if trace:
                trace.event("command.complete", {"name": command.name, "returncode": result.returncode})
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

    def _cancelled(self, run_id: str, trace: TraceWriter) -> bool:
        if self.state.cancellation_requested(run_id):
            self.state.complete_cancel(run_id)
            trace.event("run.cancelled", {})
            return True
        return False

    @staticmethod
    def _approved(audit: dict[str, Any], diff_hash: str) -> bool:
        return bool(audit.get("approved")) and audit.get("diff_hash") == diff_hash

    @staticmethod
    def _agreement(verdicts: list[dict[str, Any]], diff_hash: str, attempt: int) -> dict[str, Any]:
        required = [verdict for verdict in verdicts if verdict.get("_required", True)]
        reviewer_status = {
            str(verdict.get("reviewer_id")): {
                "approved": ClodexWorkflow._approved(verdict, diff_hash),
                "required": bool(verdict.get("_required", True)),
                "diff_hash": verdict.get("diff_hash"),
                "persona": verdict.get("persona"),
            }
            for verdict in verdicts
        }
        return {
            "approved": all(ClodexWorkflow._approved(verdict, diff_hash) for verdict in required),
            "attempt": attempt,
            "diff_hash": diff_hash,
            "reviewers": reviewer_status,
            "claude_approved": reviewer_status.get("claude-plan", {}).get("approved", False),
            "codex_approved": reviewer_status.get("codex-architecture", {}).get("approved", False),
        }

    @staticmethod
    def _required_fixes(*audits: dict[str, Any]) -> list[str]:
        fixes: list[str] = []
        for audit in audits:
            if not audit.get("_required", True) and audit.get("approved"):
                continue
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
