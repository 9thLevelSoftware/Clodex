"""Tests: npm package and launchers."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from unittest import mock
from clodex import __version__
from clodex.npm_bridge import main as npm_bridge_main
from tests.support import ROOT


def python_version(value: str) -> str:
    """Normalize npm-style prerelease versions (0.2.0-dev.0, 0.2.0-rc.1) to PEP 440."""
    value = re.sub(r"-dev\.?(\d+)", r".dev\1", value)
    return re.sub(r"-(a|b|rc)\.?(\d+)", r"\1\2", value)


class PackagingTests(unittest.TestCase):
    def test_version_has_a_single_source(self):
        self.assertIn('dynamic = ["version"]', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertNotRegex((ROOT / "pyproject.toml").read_text(encoding="utf-8"), r"(?m)^version\s*=\s*\"")
        self.assertEqual(python_version(json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]), __version__)
        for manifest in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
            version = json.loads((ROOT / manifest).read_text(encoding="utf-8"))["version"]
            self.assertEqual(python_version(version), __version__, manifest)
        for script in ("install.sh", "uninstall.sh"):
            match = re.search(r'(?m)^SCRIPT_VERSION="([^"]+)"', (ROOT / script).read_text(encoding="utf-8"))
            self.assertIsNotNone(match, script)
            self.assertEqual(python_version(match.group(1)), __version__, script)
        self.assertNotIn('"version": "0.', (ROOT / "clodex" / "mcp_server.py").read_text(encoding="utf-8"))

    def test_npm_pack_ships_every_module_and_vendored_yaml(self):
        npm = shutil.which("npm")
        if npm is None:
            self.skipTest("npm not available")
        result = subprocess.run([npm, "pack", "--dry-run", "--json"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        packed = {item["path"] for item in json.loads(result.stdout)[0]["files"]}
        expected = {path.relative_to(ROOT).as_posix() for path in (ROOT / "clodex").rglob("*.py")}
        self.assertTrue(expected <= packed, sorted(expected - packed))
        self.assertIn("clodex/_vendor/yaml/__init__.py", packed)
        self.assertIn("clodex/_vendor/yaml/LICENSE", packed)

    def test_package_json_is_valid_json(self):
        data = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "clodex")

    def test_npm_package_exposes_bins_and_files(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["name"], "clodex")
        self.assertEqual(package["bin"]["clodex"], "npm/clodex.js")
        self.assertEqual(package["bin"]["clodex-mcp-server"], "npm/clodex-mcp-server.js")
        self.assertIn("clodex/**/*.py", package["files"])
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
