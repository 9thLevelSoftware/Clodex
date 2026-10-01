"""Tests: npm package and launchers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest import mock
from clodex.npm_bridge import main as npm_bridge_main
from tests.support import ROOT


class PackagingTests(unittest.TestCase):
    def test_package_json_is_valid_json(self):
        data = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "clodex")

    def test_npm_package_exposes_bins_and_files(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["name"], "clodex")
        self.assertEqual(package["bin"]["clodex"], "npm/clodex.js")
        self.assertEqual(package["bin"]["clodex-mcp-server"], "npm/clodex-mcp-server.js")
        self.assertIn("clodex/*.py", package["files"])
        self.assertIn("npm/*.js", package["files"])

    def test_npm_bridge_invokes_python_cli(self):
        with mock.patch("runpy.run_module") as run_module, mock.patch.object(sys, "argv", ["bridge", "--json", "doctor"]):
            with self.assertRaises(SystemExit) as exit_context:
                npm_bridge_main()
        self.assertEqual(exit_context.exception.code, 0)
        run_module.assert_called_once_with("clodex", run_name="__main__", alter_sys=True)

    def test_node_launcher_dry_run_executes_python_module(self):
        result = subprocess.run(
            ["node", str(ROOT / "npm" / "clodex.js"), "--json", "build", "--dry-run", "npm smoke"],
            cwd=ROOT,
            env={**os.environ, "CLODEX_PYTHON": sys.executable},
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["status"], "dry-run")
        self.assertEqual(data["data"]["task"], "npm smoke")
