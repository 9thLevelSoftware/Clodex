from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import capabilities
from .config import ConfigError, load_config
from .models import Diagnostic, refresh_codex_catalog, validate


def run_doctor(repo_root: Path | None = None, strict: bool = False, probe: bool = True) -> tuple[int, dict[str, Any]]:
    """Check the local setup. Errors always fail; with `strict`, warnings fail too."""
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
    base_ok = (
        checks["python"]["ok"]
        and checks["contract"]["ok"]
        and checks["git"]["ok"]
        and checks["claude"]["ok"]
        and checks["codex"]["ok"]
    )

    diagnostics: list[Diagnostic] = []
    catalog = None
    if probe and checks["codex"]["ok"]:
        catalog = refresh_codex_catalog(config.repo_root / ".clodex" / "models-cache.json")
    if probe:
        installed = tuple(cli for cli in ("claude", "codex") if checks[cli]["ok"])
        found = capabilities.probe_all(config.repo_root, installed) if installed else {}
        checks["capabilities"] = found
        for cli, info in found.items():
            for flag in info.get("missing_required", []):
                diagnostics.append(Diagnostic("error", f"{cli} CLI", f"`{cli}` does not support {flag}, which Clodex requires", f"upgrade {cli}"))
            for flag in info.get("missing_optional", []):
                diagnostics.append(Diagnostic("warning", f"{cli} CLI", f"`{cli}` does not support {flag}; Clodex will run without it", f"upgrade {cli} for full functionality"))
        checks["auth"] = check_auth(checks)
        for cli, status in checks["auth"].items():
            if status.get("status") == "logged-out":
                diagnostics.append(Diagnostic("error", f"{cli} auth", f"{cli} is not logged in", f"run `{'claude auth login' if cli == 'claude' else 'codex login'}`"))
    try:
        diagnostics.extend(validate(config, catalog))
    except ConfigError as exc:  # pragma: no cover - load_config already raised for broken front matter
        diagnostics.append(Diagnostic("error", "CLODEX.md", str(exc)))

    errors = [item for item in diagnostics if item.level == "error"]
    warnings = [item for item in diagnostics if item.level == "warning"]
    checks["diagnostics"] = [item.as_dict() for item in diagnostics]
    checks["ok"] = bool(base_ok and not errors and not (strict and warnings))
    return (0 if checks["ok"] else 1), checks


def check_auth(checks: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Whether each installed CLI is logged in. `unknown` when the CLI cannot say."""
    status: dict[str, dict[str, str]] = {}
    if checks["claude"]["ok"]:
        output = _run_text(["claude", "auth", "status"])
        state = "unknown"
        if output is not None:
            try:
                state = "logged-in" if json.loads(output).get("loggedIn") else "logged-out"
            except ValueError:
                state = "unknown"
        status["claude"] = {"status": state}
    if checks["codex"]["ok"]:
        exe = shutil.which("codex")
        state = "unknown"
        if exe:
            try:
                result = subprocess.run([exe, "login", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, stdin=subprocess.DEVNULL, check=False)
                text = (result.stdout + result.stderr).lower()
                if result.returncode == 0:
                    state = "logged-out" if "not logged in" in text else "logged-in"
                elif "not logged in" in text:
                    state = "logged-out"
            except (OSError, subprocess.TimeoutExpired):
                pass
        status["codex"] = {"status": state}
    return status


def _run_text(argv: list[str]) -> str | None:
    exe = shutil.which(argv[0])
    if not exe:
        return None
    try:
        result = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def check_command(argv: list[str]) -> dict[str, Any]:
    exe = shutil.which(argv[0])
    if not exe:
        return {"ok": False, "path": None, "version": None}
    try:
        result = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, check=False)
    except OSError as exc:
        return {"ok": False, "path": exe, "version": None, "error": str(exc)}
    output = (result.stdout or result.stderr).strip().splitlines()
    return {
        "ok": result.returncode == 0,
        "path": exe,
        "version": output[0] if output else "",
    }
