"""Delegation: a native handoff asks Codex to implement, fix or audit, and a worker does the job.

Claude orchestrates over MCP; Codex runs as a non-interactive subprocess without MCP access, so
the worker is the one that records Codex's result on the handoff (and, for audits, Codex's
verdict) using the same state as every other handoff update.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .agents import AgentRunner
from .artifacts import ArtifactStore, current_diff, hash_text
from .commands import codex_exec_command, codex_review_command
from .config import ClodexConfig, load_config
from .models import ensure_usable
from .procs import START_GRACE, kill_tree, parse_time, popen_isolation_kwargs, worker_state
from .prompts import audit_diff_excerpt, audit_prompt, delegate_prompt
from .quorum import required_fixes, resolve_reviewer
from .state import DELEGATION_ACTIVE, DELEGATION_FINISHED, DELEGATION_MODES, StateStore, now_iso
from .workspace import WorkspaceManager

POLL_SECONDS = 0.5
SUMMARY_LIMIT = 4000
MAX_CLARIFICATIONS = 10
_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class WaitAborted(Exception):
    """The caller stopped waiting (cancelled request or closed connection)."""


def extract_clarifications(text: str) -> list[str]:
    """Questions Codex asked instead of guessing: `{"clarifications": ["...", ...]}` in its final message.

    Entries may be strings or `{"question": "..."}`. Anything else (including other JSON objects
    in a report that quotes code) is ignored; at most MAX_CLARIFICATIONS are kept.
    """
    candidates = list(_FENCED_JSON.findall(text))
    for match in re.finditer(r"\{", text):  # the last JSON object in the message, fenced or not
        candidates.append(text[match.start():])
    for candidate in reversed(candidates):
        try:
            payload, _ = json.JSONDecoder().raw_decode(candidate.lstrip())
        except ValueError:
            continue
        items = payload.get("clarifications") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            continue
        questions = []
        for item in items:
            question = item.get("question") if isinstance(item, dict) else item
            if isinstance(question, str) and question.strip():
                questions.append(question.strip())
        if questions:
            return questions[:MAX_CLARIFICATIONS]
    return []


def delegation_worker_state(delegation: dict[str, Any]) -> str:
    return worker_state(delegation.get("pid"), delegation.get("heartbeat_at"), delegation.get("started_at") or delegation.get("created_at"))


def reconcile_delegation(state: StateStore, delegation: dict[str, Any]) -> dict[str, Any]:
    """Notice a worker that died (or never started) without finishing, instead of leaving it running forever."""
    if str(delegation["status"]) not in DELEGATION_ACTIVE:
        return delegation
    health = delegation_worker_state(delegation)
    if not delegation.get("pid"):
        # Just created and not yet spawned is normal; a delegation that never got a worker is not.
        parsed = parse_time(delegation.get("created_at"))
        if parsed is not None and (time.time() - parsed.timestamp()) > START_GRACE:
            return state.update_delegation(delegation["id"], status="failed", error="the worker was never started")
        return delegation
    if health == "dead":
        return state.update_delegation(delegation["id"], status="failed", error="worker process exited before finishing the delegation")
    return {**delegation, "worker_state": health}


def cancel_active_delegation(state: StateStore, run_id: str) -> bool:
    """Stop the handoff's running delegation, if any. Returns True if a worker was stopped.

    The process is only signalled while its heartbeat is fresh, so a reused pid is never killed.
    """
    delegation = state.active_delegation(run_id)
    if delegation is None:
        return False
    stopped = False
    if delegation_worker_state(delegation) == "alive":
        kill_tree(int(delegation["pid"]))
        stopped = True
    state.update_delegation(delegation["id"], status="cancelled", error="cancelled with the handoff")
    return stopped


def open_fixes(data: dict[str, Any], reviewers: list[dict[str, Any]]) -> list[str]:
    """What reviewers have asked to be fixed on the latest diff and not yet approved."""
    latest_hash: str | None = None
    verdicts: dict[str, dict[str, Any]] = {}
    for event in data.get("events") or []:
        if event.get("event") != "handoff.update" or not isinstance(event.get("data"), dict):
            continue
        event_data = event["data"]
        report = event_data.get("report") if isinstance(event_data.get("report"), dict) else {}
        diff_hash = (event_data.get("diff_hash") or report.get("diff_hash") or None)
        if diff_hash and diff_hash != latest_hash:
            latest_hash, verdicts = str(diff_hash), {}
        reviewer = resolve_reviewer(report.get("reviewer_id") or event_data.get("actor"), reviewers)
        if reviewer is None or not isinstance(report.get("approved"), bool):
            continue
        verdicts[str(reviewer["id"])] = {**report, "reviewer_id": str(reviewer["id"]), "approved": report["approved"], "diff_hash": latest_hash, "_required": bool(reviewer.get("required", True))}
    rejected = [verdict for verdict in verdicts.values() if not verdict["approved"]]
    return required_fixes(rejected, latest_hash) if latest_hash and rejected else []


class DelegationManager:
    def __init__(self, repo_root: Path | None = None):
        self.config: ClodexConfig = load_config(repo_root)
        self.repo_root = self.config.repo_root
        self._state: StateStore | None = None

    @property
    def state(self) -> StateStore:
        if self._state is None:
            self._state = StateStore(self.config.state_path)
        return self._state

    def start(self, run_id: str, instructions: str | None = None, mode: str = "implement", approval_profile: str | None = None) -> dict[str, Any]:
        if mode not in DELEGATION_MODES:
            raise ValueError(f"unknown delegation mode: {mode} (use one of {sorted(DELEGATION_MODES)})")
        data = self.state.get_handoff(run_id)
        if data is None:
            raise ValueError(f"Unknown run: {run_id}")
        run = data["run"]
        if run["status"] != "handoff":
            raise ValueError(f"the handoff is {run['status']}; nothing can be delegated")
        if data["budget_remaining"] < 1:
            raise ValueError("handoff budget exhausted: nothing can be delegated")
        if mode == "implement" and not (instructions and instructions.strip()):
            raise ValueError("instructions are required to delegate an implementation")
        ensure_usable(self.config)
        active = self.state.active_delegation(run_id)
        if active is not None:
            reconcile_delegation(self.state, active)  # a dead worker must not block the handoff forever
        self._ensure_workspace(run)

        delegation = self.state.start_delegation(run_id, mode, instructions, approval_profile or str(self.config.codex.get("approval_profile", "ci")))
        try:
            process = self._spawn(run_id, int(delegation["id"]), str(delegation["approval_profile"]))
        except Exception as exc:  # noqa: BLE001
            self.state.update_delegation(delegation["id"], status="failed", error=f"could not start the worker: {exc}")
            raise
        delegation = self.state.update_delegation(delegation["id"], pid=process.pid)
        self.state.add_event(run_id, "handoff.delegate", {"delegation": delegation["id"], "mode": mode})
        return delegation

    def _ensure_workspace(self, run: dict[str, Any]) -> None:
        if run.get("workspace_path"):
            return
        # The handoff was created without a workspace: give Codex an isolated worktree now.
        workspace = WorkspaceManager(self.repo_root, self.config).prepare(str(run["id"]), None)
        self.state.update_run(str(run["id"]), "handoff", workspace_path=str(workspace.path))
        self.state.add_workspace_lock(str(run["id"]), str(workspace.source_path), str(workspace.path), workspace.backend)

    def _spawn(self, run_id: str, delegation_id: int, approval_profile: str) -> subprocess.Popen:
        artifacts = ArtifactStore(self.config, run_id, self.state)
        out = (artifacts.path / f"delegation-{delegation_id}.stdout.log").open("w", encoding="utf-8")
        err = (artifacts.path / f"delegation-{delegation_id}.stderr.log").open("w", encoding="utf-8")
        env = os.environ.copy()
        root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        argv = [sys.executable, "-m", "clodex", "task", "delegate-worker", run_id, str(delegation_id), "--approval-profile", approval_profile]
        try:
            return subprocess.Popen(argv, cwd=self.repo_root, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, **popen_isolation_kwargs())
        finally:
            out.close()
            err.close()

    def get(self, delegation_id: int) -> dict[str, Any] | None:
        delegation = self.state.get_delegation(delegation_id)
        return reconcile_delegation(self.state, delegation) if delegation else None

    def reconcile_active(self, run_id: str) -> None:
        active = self.state.active_delegation(run_id)
        if active is not None:
            reconcile_delegation(self.state, active)

    def wait(self, delegation_id: int, should_stop: Callable[[], bool] | None = None, timeout: float | None = None) -> dict[str, Any]:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            delegation = self.get(delegation_id)
            if delegation is None:
                raise ValueError(f"unknown delegation: {delegation_id}")
            if str(delegation["status"]) in DELEGATION_FINISHED:
                return delegation
            if should_stop is not None and should_stop():
                raise WaitAborted
            if deadline is not None and time.monotonic() >= deadline:
                return delegation
            time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------- the worker


def run_delegation(repo_root: Path | None, run_id: str, delegation_id: int) -> dict[str, Any]:
    """Executed by `clodex task delegate-worker`. Always leaves the delegation in a finished state."""
    config = load_config(repo_root)
    state = StateStore(config.state_path)
    delegation = state.get_delegation(delegation_id)
    if delegation is None or delegation["run_id"] != run_id:
        raise ValueError(f"unknown delegation {delegation_id} for {run_id}")
    state.update_delegation(delegation_id, status="running", started_at=now_iso())
    try:
        outcome = _execute(config, state, run_id, delegation)
    except Exception as exc:  # noqa: BLE001 - record any failure on the delegation
        error = f"{type(exc).__name__}: {exc}"
        state.update_delegation(delegation_id, status="failed", error=error)
        _record_failure(state, run_id, delegation, error)
        (ArtifactStore(config, run_id, state).path / f"delegation-{delegation_id}.error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        return state.get_delegation(delegation_id) or {}
    return state.update_delegation(delegation_id, status="completed", summary=outcome["summary"][:SUMMARY_LIMIT], diff_hash=outcome.get("diff_hash"))


def _record_failure(state: StateStore, run_id: str, delegation: dict[str, Any], error: str) -> None:
    """Tell the orchestrator (via the handoff) that the job failed, without spending handoff budget."""
    try:
        state.update_handoff(run_id, actor="codex", report={"delegation": delegation["id"], "mode": delegation["mode"], "error": error})
    except ValueError:
        pass  # the handoff finished or was cancelled meanwhile


def _prepare_diff(workspace: Path, repo_root: Path) -> tuple[str, str]:
    if workspace.resolve() != repo_root.resolve():  # an isolated worktree: show new files in the diff too
        names = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=workspace, capture_output=True, check=False).stdout
        files = [item.decode("utf-8", errors="replace") for item in names.split(b"\0") if item]
        if files:
            subprocess.run(["git", "add", "-N", "--", *files], cwd=workspace, capture_output=True, check=False)
    diff = current_diff(workspace)
    return diff, hash_text(diff)


def _execute(config: ClodexConfig, state: StateStore, run_id: str, delegation: dict[str, Any]) -> dict[str, Any]:
    data = state.get_handoff(run_id)
    if data is None:
        raise ValueError(f"Unknown run: {run_id}")
    run = data["run"]
    workspace = Path(str(run["workspace_path"]))
    if not workspace.is_dir():
        raise ValueError(f"the handoff workspace is gone: {workspace}")
    runner = AgentRunner(workspace)
    artifacts = ArtifactStore(config, run_id, state)
    mode = str(delegation["mode"])
    instructions = (delegation.get("instructions") or "").strip() or None
    task = str(run["prompt"])

    if mode == "audit":
        return _audit(config, state, run_id, delegation, run, runner, artifacts, workspace, instructions, task)

    fixes = open_fixes(data, config.reviewers) if mode == "fix" else None
    answered = state.answered_clarifications(run_id)
    prompt = delegate_prompt(mode, task, instructions, fixes, [(item["question"], item["answer"]) for item in answered])
    result = runner.run(codex_exec_command(config, workspace, approval_profile=delegation.get("approval_profile")), prompt)
    report_path = artifacts.write_text(f"delegation-{delegation['id']}-codex.md", _report_text(result.stdout, result.stderr))
    if not result.ok:
        raise RuntimeError(f"codex exited with code {result.returncode}: {(result.stderr or result.stdout).strip()[:500]}")
    diff, diff_hash = _prepare_diff(workspace, config.repo_root)
    diff_path = artifacts.write_text("changes.diff", diff, exact=True)
    if answered:
        state.mark_delivered([int(item["id"]) for item in answered])  # Codex has now seen them
    summary = result.stdout.strip() or "(codex printed no report)"
    questions = extract_clarifications(result.stdout)
    for question in questions:  # before the handoff update: that may block the handoff, which then takes no messages
        state.add_clarification(run_id, "codex", question)
    if questions and not diff.strip():
        summary = "Codex needs clarification: " + "; ".join(questions)
    state.update_handoff(
        run_id,
        phase="implementation" if mode == "implement" else "fix",
        actor="codex",
        increment_handoff=True,
        diff_hash=diff_hash,
        report={
            "summary": summary[:SUMMARY_LIMIT],
            "changed": bool(diff.strip()),
            **({"clarifications": questions} if questions else {}),
            "delegation": delegation["id"],
            "mode": mode,
            "artifacts": [str(report_path), str(diff_path)],
        },
    )
    return {"summary": summary, "diff_hash": diff_hash}


def _audit(config, state, run_id, delegation, run, runner, artifacts, workspace, instructions, task) -> dict[str, Any]:
    from .workflow import ClodexWorkflow  # deferred: workflow imports a lot

    diff, diff_hash = _prepare_diff(workspace, config.repo_root)
    if not diff.strip():
        raise ValueError("there is nothing to audit: the workspace has no changes")
    reviewer = resolve_reviewer("codex", config.reviewers)
    reviewer_id = str(reviewer["id"]) if reviewer else "codex-audit"
    persona = str(reviewer.get("persona", reviewer_id)) if reviewer else "audit"
    timeout = int(reviewer.get("timeout", 600)) if reviewer else 600
    plan = {"goal": task, "implementation_spec": [instructions] if instructions else [], "acceptance_criteria": ["The diff is correct, scoped and safe to ship"]}
    shown = audit_diff_excerpt(diff, int(config.audit.get("max_diff_bytes", 200_000)))
    verdict = ClodexWorkflow(config.repo_root).run_agent_json(
        runner,
        codex_review_command(config, workspace),
        audit_prompt("Codex", plan, shown, diff_hash, reviewer_id, persona),
        f"{reviewer_id} audit",
        timeout=timeout,
    )
    verdict_path = artifacts.write_json(f"delegation-{delegation['id']}-audit.json", verdict)
    diff_path = artifacts.write_text("changes.diff", diff, exact=True)
    verdict.update({"reviewer_id": reviewer_id, "delegation": delegation["id"], "mode": "audit", "artifacts": [str(verdict_path), str(diff_path)]})
    # The verdict counts for the diff we actually audited, whatever hash the model echoed back.
    state.update_handoff(run_id, phase="audit", actor="codex", increment_handoff=True, diff_hash=diff_hash, report=verdict)
    return {"summary": str(verdict.get("summary") or ("approved" if verdict.get("approved") else "rejected")), "diff_hash": diff_hash}


def _report_text(stdout: str, stderr: str) -> str:
    report = stdout.strip()
    if stderr.strip():
        report += ("\n\n" if report else "") + "stderr:\n" + stderr.strip()
    return report
