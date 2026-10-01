"""Tests: async tasks, hooks, trace export, evals."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from clodex.workflow import ClodexWorkflow
from tests.support import ROOT, TempRepo, FakeCliPath


class TasksHooksTests(unittest.TestCase):
    def test_task_start_get_cancel_lifecycle(self):
        with TempRepo() as repo, FakeCliPath(sleep_seconds=1):
            start = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "task", "start", "--workspace", "local", "slow fixture"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(start.returncode, 0, start.stdout + start.stderr)
            run_id = json.loads(start.stdout)["run_id"]
            cancel = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "task", "cancel", run_id],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(cancel.returncode, 0, cancel.stdout + cancel.stderr)
            for _ in range(20):
                status = subprocess.run(
                    [sys.executable, "-m", "clodex", "--json", "task", "get", run_id],
                    cwd=repo,
                    env={**os.environ, "PYTHONPATH": str(ROOT)},
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
                data = json.loads(status.stdout)
                if data["run"]["status"] in {"cancelled", "approved", "blocked"}:
                    break
                time.sleep(0.1)
            self.assertIn(data["run"]["status"], {"cancel_requested", "cancelled"})

    def test_hooks_print_and_ingest(self):
        with TempRepo() as repo:
            printed = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "hooks", "print"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(printed.returncode, 0, printed.stderr)
            config = json.loads(printed.stdout)
            self.assertIn("hooks", config)
            ingested = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "hooks", "ingest", "--run-id", "run-hooks"],
                cwd=repo,
                input=json.dumps({"event": "SessionStart"}),
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(ingested.returncode, 0, ingested.stdout + ingested.stderr)

    def test_trace_export_and_eval_run(self):
        with TempRepo() as repo, FakeCliPath():
            result = ClodexWorkflow(repo).build("implement fixture", workspace_backend="local")
            exported = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "trace", "export", result.run_id, "--format", "jsonl"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(exported.returncode, 0, exported.stderr)
            self.assertIn("run.start", exported.stdout)
            evaluated = subprocess.run(
                [sys.executable, "-m", "clodex", "--json", "eval", "run"],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(evaluated.returncode, 0, evaluated.stdout + evaluated.stderr)
