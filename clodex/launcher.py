"""How other programs (Claude Code hooks, MCP clients) should start Clodex.

Those programs spawn Clodex without our shell environment: no PYTHONPATH, sometimes a
different PATH, and on Windows they cannot spawn a `.cmd` shim directly. So instead of
trusting a bare `clodex` on PATH, build a command that works wherever Clodex is installed.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import clodex


def _is_installed() -> bool:
    """True when the package lives in site-packages (pip) rather than a source checkout."""
    parts = Path(clodex.__file__).resolve().parts
    return "site-packages" in parts or "dist-packages" in parts


def _bootstrap(root: Path) -> str:
    # A source checkout is not importable from a bare interpreter, so put it on sys.path inline.
    return f"import sys; sys.path.insert(0, {str(root)!r}); from clodex.cli import main; raise SystemExit(main(sys.argv[1:]))"


def clodex_argv(*subcommand: str, npm_launcher: str | None = None, python: str | None = None, installed: bool | None = None) -> list[str]:
    """An absolute, environment-independent command line that runs `clodex <subcommand...>`."""
    launcher = npm_launcher if npm_launcher is not None else os.environ.get("CLODEX_NPM_LAUNCHER")
    if launcher and Path(launcher).is_file():
        return [shutil.which("node") or "node", str(launcher), *subcommand]
    interpreter = python or sys.executable
    if _is_installed() if installed is None else installed:
        return [interpreter, "-m", "clodex", *subcommand]
    return [interpreter, "-c", _bootstrap(Path(clodex.__file__).resolve().parents[1]), *subcommand]


def mcp_server_entry(*, portable: bool, windows: bool | None = None, which: Any = shutil.which) -> dict[str, Any]:
    """The MCP server definition for `command`/`args`.

    `portable` is for files that get committed and shared (`.mcp.json`, `.codex/config.toml`):
    use the bare `clodex` command, wrapped in `cmd /c` where only a `.cmd` shim exists, since
    Windows MCP clients cannot spawn those directly. Otherwise (user-level config for this
    machine) use the absolute, environment-independent command.
    """
    if portable:
        windows = os.name == "nt" if windows is None else windows
        exe = which("clodex")
        if windows and exe and str(exe).lower().endswith((".cmd", ".bat")):
            return {"command": "cmd", "args": ["/c", "clodex", "mcp-server"]}
        return {"command": "clodex", "args": ["mcp-server"]}
    argv = clodex_argv("mcp-server")
    return {"command": argv[0], "args": argv[1:]}
