from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore, make_run_id, make_task_id
from .config import ClodexConfig, load_config
from .state import StateStore
from .workflow import WorkflowResult


class TaskManager:
    def __init__(self, repo_root: Path | None = None):
        self.config: ClodexConfig = load_config(repo_root)
        self.repo_root = self.config.repo_root
        self._state: StateStore | None = None

    @property
    def state(self) -> StateStore:
        if self._state is None:
            self._state = StateStore(self.config.state_path)
        return self._state

    def start(
        self,
        task: str,
        workspace_backend: str | None = None,
        approval_profile: str | None = None,
        dry_run: bool = False,
    ) -> WorkflowResult:
        task_id = make_task_id(task)
        run_id = make_run_id(task_id)
        selected_workspace = workspace_backend or self.config.workspace["backend"]
        selected_profile = approval_profile or self.config.codex["approval_profile"]
        if dry_run:
            artifacts_path = self.config.runs_root / run_id
            return WorkflowResult(
                "dry-run",
                run_id,
                task_id,
                str(artifacts_path),
                {"workspace": selected_workspace, "approval_profile": selected_profile},
            )

        artifacts = ArtifactStore(self.config, run_id)
        self.state.upsert_task(task_id, task, "queued")
        self.state.create_run(run_id, task_id, task, "queued", artifacts_dir=str(artifacts.path))
        stdout = (artifacts.path / "worker.stdout.log").open("w", encoding="utf-8")
        stderr = (artifacts.path / "worker.stderr.log").open("w", encoding="utf-8")
        argv = [
            sys.executable,
            "-m",
            "clodex",
            "task",
            "worker",
            run_id,
            "--workspace",
            selected_workspace,
            "--approval-profile",
            selected_profile,
        ]
        env = os.environ.copy()
        root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        process = subprocess.Popen(argv, cwd=self.repo_root, stdout=stdout, stderr=stderr, env=env)
        stdout.close()
        stderr.close()
        self.state.update_run(run_id, "queued", pid=process.pid, artifacts_dir=str(artifacts.path))
        return WorkflowResult("queued", run_id, task_id, str(artifacts.path), {"pid": process.pid, "workspace": selected_workspace})

    def get(self, run_id: str) -> dict[str, Any] | None:
        run = self.state.get_run(run_id)
        if run is None:
            return None
        return {"run": run}

    def list(self) -> dict[str, Any]:
        return {"runs": self.state.list_runs(), "tasks": self.state.list_tasks()}

    def cancel(self, run_id: str) -> WorkflowResult:
        run = self.state.get_run(run_id)
        if run is None:
            raise ValueError(f"Unknown run: {run_id}")
        self.state.request_cancel(run_id)
        pid = run.get("pid")
        if pid:
            self._terminate(int(pid))
        self.state.complete_cancel(run_id)
        updated = self.state.get_run(run_id) or run
        return WorkflowResult(str(updated["status"]), run_id, updated.get("task_id"), updated.get("artifacts_dir"), {"cancel_requested": True})

    @staticmethod
    def _terminate(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
