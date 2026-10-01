"""Tests: process-tree control, worker supervision and cancellation."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path

from clodex.agents import AgentRunner
from clodex.commands import claude_plan_command
from clodex.config import load_config
from clodex.procs import kill_tree, pid_alive, popen_isolation_kwargs
from clodex.tasks import Heartbeat, TaskManager
from clodex.workflow import ClodexWorkflow
from tests.support import FakeCliPath, TempRepo


def wait_for(predicate, timeout: float, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def finished_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class ProcessHelperTests(unittest.TestCase):
    def test_pid_alive(self):
        self.assertTrue(pid_alive(os.getpid()))
        self.assertFalse(pid_alive(None))
        self.assertFalse(pid_alive(0))
        self.assertFalse(pid_alive(finished_pid()))

    def test_exited_but_unreaped_child_is_not_alive(self):
        # On POSIX an exited child stays a zombie until its parent waits; it must not count as alive.
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        self.addCleanup(child.wait)
        self.assertTrue(wait_for(lambda: not pid_alive(child.pid), 15), "zombie child reported alive")

    def test_kill_tree_stops_the_process_and_its_children(self):
        child_pid_file = Path(self.id() + ".pid")
        script = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({str(child_pid_file)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(60)\n"
        )
        self.addCleanup(lambda: child_pid_file.unlink(missing_ok=True))
        parent = subprocess.Popen([sys.executable, "-c", script], **popen_isolation_kwargs())
        self.addCleanup(lambda: parent.poll() is None and parent.kill())
        self.assertTrue(wait_for(lambda: read_pid(child_pid_file) is not None, 15))
        child_pid = read_pid(child_pid_file)
        self.assertTrue(pid_alive(parent.pid) and pid_alive(child_pid))
        kill_tree(parent.pid)
        self.assertTrue(wait_for(lambda: not pid_alive(parent.pid), 10), "parent still alive")
        self.assertTrue(wait_for(lambda: not pid_alive(child_pid), 10), "child outlived its parent")
        parent.wait(timeout=10)

    def test_kill_tree_ignores_bad_pids(self):
        kill_tree(0)
        kill_tree(-5)
        kill_tree(os.getpid())  # never kills the caller
        self.assertTrue(pid_alive(os.getpid()))


class AgentTimeoutTests(unittest.TestCase):
    def test_timeout_stops_the_agent_process_not_just_its_shim(self):
        with TempRepo() as repo, FakeCliPath(sleep_seconds=30):
            config = load_config(repo)
            started = time.monotonic()
            result = AgentRunner(repo).run(claude_plan_command(config), "prompt", timeout=2)
            self.assertTrue(result.timed_out)
            self.assertEqual(result.returncode, 124)
            self.assertLess(time.monotonic() - started, 20, "should not wait for the orphan to finish sleeping")
            pid = read_pid(repo / ".fake-pid-claude")
            self.assertIsNotNone(pid, "fake agent never started")
            self.assertTrue(wait_for(lambda: not pid_alive(pid), 10), "agent process survived the timeout")


class WorkerSupervisionTests(unittest.TestCase):
    def setUp(self):
        self.repo_cm = TempRepo()
        self.repo = self.repo_cm.__enter__()
        self.addCleanup(self.repo_cm.__exit__, None, None, None)
        self.manager = TaskManager(self.repo)
        self.state = self.manager.state
        self.state.upsert_task("t", "task", "running")

    def make_run(self, run_id: str, status: str, pid: int | None, heartbeat_age: float | None = None) -> None:
        self.state.create_run(run_id, "t", "prompt", status, pid=pid)
        if heartbeat_age is not None:
            stamp = (datetime.now(UTC) - timedelta(seconds=heartbeat_age)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            with self.state.session() as con:
                con.execute("update runs set heartbeat_at=? where id=?", (stamp, run_id))

    def test_dead_worker_is_detected_and_the_run_failed(self):
        self.make_run("r1", "running", finished_pid())
        data = self.manager.get("r1")
        self.assertEqual(data["run"]["status"], "failed")
        self.assertIn("worker process exited", data["run"]["error"])
        task = next(t for t in self.state.list_tasks() if t["id"] == "t")
        self.assertEqual(task["status"], "failed")

    def test_dead_worker_of_a_cancelling_run_completes_the_cancel(self):
        self.make_run("r1", "cancel_requested", finished_pid())
        self.assertEqual(self.manager.get("r1")["run"]["status"], "cancelled")

    def test_list_reconciles_every_active_run_and_leaves_finished_ones(self):
        self.make_run("dead", "queued", finished_pid())
        self.make_run("done", "approved", finished_pid())
        runs = {run["id"]: run["status"] for run in self.manager.list()["runs"]}
        self.assertEqual(runs, {"dead": "failed", "done": "approved"})

    def test_worker_state_classification(self):
        self.make_run("fresh", "running", os.getpid(), heartbeat_age=1)
        self.make_run("quiet", "running", os.getpid(), heartbeat_age=600)
        self.make_run("gone", "running", finished_pid())
        self.make_run("nopid", "queued", None)
        states = {rid: self.manager.worker_state(self.state.get_run(rid)) for rid in ("fresh", "quiet", "gone", "nopid")}
        self.assertEqual(states, {"fresh": "alive", "quiet": "stale", "gone": "dead", "nopid": "none"})

    def test_cancel_never_signals_a_live_pid_without_a_recent_heartbeat(self):
        # This pid is the test process itself: a stale heartbeat means it may be a reused pid.
        self.make_run("r1", "running", os.getpid(), heartbeat_age=600)
        result = self.manager.cancel("r1")
        self.assertEqual(result.status, "cancelled")
        self.assertFalse(result.data["worker_stopped"])
        self.assertTrue(pid_alive(os.getpid()))

    def test_cancel_of_a_finished_run_changes_nothing(self):
        self.make_run("r1", "approved", None)
        result = self.manager.cancel("r1")
        self.assertEqual(result.status, "approved")
        self.assertFalse(result.data["cancel_requested"])
        self.assertEqual(self.state.get_run("r1")["status"], "approved")
        with self.assertRaises(ValueError):
            self.manager.cancel("missing")

    def test_heartbeat_updates_while_running_and_not_after_finishing(self):
        self.make_run("r1", "running", None)
        self.assertIsNone(self.state.get_run("r1")["heartbeat_at"])
        with Heartbeat(self.state, "r1", interval=0.05):
            self.assertTrue(wait_for(lambda: self.state.get_run("r1")["heartbeat_at"] is not None, 5))
        self.state.update_run("r1", "approved")
        self.state.touch_run("r1")
        before = self.state.get_run("r1")["heartbeat_at"]
        time.sleep(1.1)
        self.state.touch_run("r1")
        self.assertEqual(self.state.get_run("r1")["heartbeat_at"], before, "finished runs are no longer touched")


class CancelEndToEndTests(unittest.TestCase):
    def setUp(self):
        # TaskManager.start hands back a detached worker whose Popen handle is never reaped here.
        context = warnings.catch_warnings()
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        warnings.simplefilter("ignore", ResourceWarning)

    def test_cancel_stops_the_worker_and_the_agent_it_is_running(self):
        with TempRepo() as repo, FakeCliPath(sleep_seconds=60):
            manager = TaskManager(repo)
            started = manager.start("slow fixture", workspace_backend="local")
            run_id = started.run_id
            worker_pid = started.data["pid"]
            agent_pid_file = repo / ".fake-pid-claude"
            self.assertTrue(wait_for(lambda: read_pid(agent_pid_file) is not None, 45), "worker never reached the agent")
            agent_pid = read_pid(agent_pid_file)
            self.assertTrue(pid_alive(worker_pid) and pid_alive(agent_pid))
            result = manager.cancel(run_id)
            self.assertEqual(result.status, "cancelled")
            self.assertTrue(result.data["worker_stopped"])
            self.assertTrue(wait_for(lambda: not pid_alive(worker_pid), 10), "worker survived cancel")
            self.assertTrue(wait_for(lambda: not pid_alive(agent_pid), 10), "agent survived cancel")
            self.assertEqual(manager.state.get_run(run_id)["status"], "cancelled")

    def test_cancelled_worktree_run_releases_its_worktree(self):
        with TempRepo() as repo, FakeCliPath(sleep_seconds=60):
            manager = TaskManager(repo)
            started = manager.start("slow fixture", workspace_backend="git-worktree")
            workspace = Path(manager.config.workspace_root) / started.run_id
            self.assertTrue(wait_for(lambda: workspace.exists() and read_pid(workspace / ".fake-pid-claude") is not None, 45))
            result = manager.cancel(started.run_id)
            self.assertEqual(result.status, "cancelled")
            self.assertTrue(result.data["workspace_released"])
            self.assertFalse(workspace.exists())
            worktrees = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
            self.assertEqual(worktrees.count("worktree "), 1)


if __name__ == "__main__":
    unittest.main()
