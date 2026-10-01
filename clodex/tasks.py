from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore, make_run_id, make_task_id
from .config import ClodexConfig, load_config
from .procs import kill_tree, pid_alive, popen_isolation_kwargs
from .state import TERMINAL_STATUSES, StateStore
from .workflow import WorkflowResult
from .workspace import WorkspaceManager

ACTIVE_STATUSES = {"queued", "running", "planning", "auditing", "needs-fix", "cancel_requested"}
HEARTBEAT_INTERVAL = 5.0
# A live worker beats every few seconds; a pid with an older heartbeat is probably a
# reused pid, so it is never signalled.
STALE_AFTER = 60.0
START_GRACE = 30.0


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class Heartbeat:
    """Keeps `runs.heartbeat_at` fresh while a worker process is doing the run."""

    def __init__(self, state: StateStore, run_id: str, interval: float = HEARTBEAT_INTERVAL):
        self.state = state
        self.run_id = run_id
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, name="clodex-heartbeat", daemon=True)

    def _beat(self) -> None:
        while True:
            try:
                self.state.touch_run(self.run_id)
            except Exception:  # noqa: BLE001 - a missed beat must never kill the worker
                pass
            if self._stop.wait(self.interval):
                return

    def __enter__(self) -> "Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


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

        artifacts = ArtifactStore(self.config, run_id, self.state)
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
        # Own process group/session: cancel can then stop the worker and the agents it started.
        process = subprocess.Popen(
            argv,
            cwd=self.repo_root,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            env=env,
            **popen_isolation_kwargs(),
        )
        stdout.close()
        stderr.close()
        self.state.update_run(run_id, "queued", pid=process.pid, artifacts_dir=str(artifacts.path))
        return WorkflowResult("queued", run_id, task_id, str(artifacts.path), {"pid": process.pid, "workspace": selected_workspace})

    def worker_state(self, run: dict[str, Any]) -> str:
        """`none` (no worker), `dead`, `stale` (alive pid but no recent heartbeat) or `alive`."""
        pid = run.get("pid")
        if not pid:
            return "none"
        if not pid_alive(int(pid)):
            return "dead"
        heartbeat = _parse_time(run.get("heartbeat_at"))
        reference = heartbeat or _parse_time(run.get("started_at")) or _parse_time(run.get("created_at"))
        limit = STALE_AFTER if heartbeat else START_GRACE
        if reference is not None and (datetime.now(UTC) - reference).total_seconds() > limit:
            return "stale"
        return "alive"

    def reconcile(self, run: dict[str, Any]) -> dict[str, Any]:
        """Notice a worker that died without finishing its run, instead of leaving it queued forever."""
        if str(run["status"]) not in ACTIVE_STATUSES:
            return run
        state = self.worker_state(run)
        if state == "dead":
            run_id = str(run["id"])
            if str(run["status"]) == "cancel_requested":
                self.state.complete_cancel(run_id)
            else:
                self.state.update_run(run_id, "failed", error="worker process exited before finishing the run")
                if run.get("task_id"):
                    self.state.update_task(str(run["task_id"]), "failed")
            self._release_workspace(run)
            return self.state.get_run(run_id) or run
        return {**run, "worker_state": state}

    def get(self, run_id: str) -> dict[str, Any] | None:
        run = self.state.get_run(run_id)
        if run is None:
            return None
        return {"run": self.reconcile(run)}

    def list(self) -> dict[str, Any]:
        return {"runs": [self.reconcile(run) for run in self.state.list_runs()], "tasks": self.state.list_tasks()}

    def cancel(self, run_id: str) -> WorkflowResult:
        run = self.state.get_run(run_id)
        if run is None:
            raise ValueError(f"Unknown run: {run_id}")
        status = str(run["status"])
        if status in TERMINAL_STATUSES:
            return WorkflowResult(status, run_id, run.get("task_id"), run.get("artifacts_dir"), {"cancel_requested": False, "note": f"run already {status}"})
        self.state.request_cancel(run_id)
        killed = False
        if self.worker_state(run) == "alive":
            kill_tree(int(run["pid"]))
            killed = True
        self.state.complete_cancel(run_id)
        released = self._release_workspace(run)
        updated = self.state.get_run(run_id) or run
        return WorkflowResult(
            str(updated["status"]),
            run_id,
            updated.get("task_id"),
            updated.get("artifacts_dir"),
            {"cancel_requested": True, "worker_stopped": killed, "workspace_released": released},
        )

    def _release_workspace(self, run: dict[str, Any]) -> bool:
        path = run.get("workspace_path")
        if not path:
            return False
        manager = WorkspaceManager(self.repo_root, self.config)
        released = False
        for _ in range(10):  # the stopped worker may take a moment to let go of the directory
            released = manager.release(str(path))
            if released or not Path(str(path)).exists():
                break
            time.sleep(0.2)
        if released:
            self.state.release_workspace_lock(str(run["id"]))
        return released
