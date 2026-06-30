from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import load_config


def run_doctor(repo_root: Path | None = None) -> tuple[int, dict[str, Any]]:
    config = load_config(repo_root)
    checks: dict[str, Any] = {
        "python": {
            "ok": sys.version_info >= (3, 12),
            "version": sys.version.split()[0],
        },
        "repo_root": str(config.repo_root),
        "contract": {
            "ok": (config.repo_root / "CLODEX.md").exists(),
            "path": str(config.repo_root / "CLODEX.md"),
            "max_fix_loops": config.max_fix_loops,
        },
        "git": check_command(["git", "--version"]),
        "claude": check_command(["claude", "--version"]),
        "codex": check_command(["codex", "--version"]),
        "state_path": str(config.state_path),
        "runs_root": str(config.runs_root),
    }
    ok = (
        checks["python"]["ok"]
        and checks["contract"]["ok"]
        and checks["git"]["ok"]
        and checks["claude"]["ok"]
        and checks["codex"]["ok"]
    )
    checks["ok"] = ok
    return (0 if ok else 1), checks


def check_command(argv: list[str]) -> dict[str, Any]:
    exe = shutil.which(argv[0])
    if not exe:
        return {"ok": False, "path": None, "version": None}
    try:
        result = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    except OSError as exc:
        return {"ok": False, "path": exe, "version": None, "error": str(exc)}
    output = (result.stdout or result.stderr).strip().splitlines()
    return {
        "ok": result.returncode == 0,
        "path": exe,
        "version": output[0] if output else "",
    }
