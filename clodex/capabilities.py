"""Detect which flags the installed Claude / Codex CLIs support by reading their `--help`.

Version numbers say little about which flags exist, so this probes the real help text and
caches the result per CLI version in `.clodex/capabilities.json`. Command builders consult the
cache and drop optional flags a CLI is known to lack; an empty cache means "assume supported".
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

CACHE_NAME = "capabilities.json"

# Without these a run cannot work at all.
REQUIRED_FLAGS: dict[str, tuple[str, ...]] = {
    "claude": ("--effort", "--permission-mode", "--output-format", "--model"),
    "codex": ("--sandbox", "--model", "--cd"),
}
# Clodex degrades gracefully without these (prompt + schema validation + stdout parsing).
OPTIONAL_FLAGS: dict[str, tuple[str, ...]] = {
    "claude": ("--json-schema", "--fallback-model", "--max-budget-usd"),
    "codex": ("--output-schema", "--output-last-message", "--approve-for-me", "--ephemeral"),
}
HELP_COMMANDS: dict[str, list[str]] = {
    "claude": ["claude", "--help"],
    "codex": ["codex", "exec", "--help"],
}
VERSION_COMMANDS: dict[str, list[str]] = {
    "claude": ["claude", "--version"],
    "codex": ["codex", "--version"],
}


def flags_in_help(text: str, flags: tuple[str, ...]) -> list[str]:
    return [flag for flag in flags if re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", text)]


def _run(argv: list[str]) -> str | None:
    exe = shutil.which(argv[0])
    if not exe:
        return None
    try:
        result = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (result.stdout or "") + (result.stderr or "") if result.returncode == 0 else None


def _version(cli: str) -> str | None:
    output = _run(VERSION_COMMANDS[cli])
    return output.strip().splitlines()[0] if output and output.strip() else None


def probe_cli(cli: str) -> dict[str, Any]:
    """`{version, probed, supported, missing_required, missing_optional}`; probed is False if help was unreadable."""
    version = _version(cli)
    help_text = _run(HELP_COMMANDS[cli])
    if help_text is None:
        return {"version": version, "probed": False, "supported": [], "missing_required": [], "missing_optional": []}
    required, optional = REQUIRED_FLAGS[cli], OPTIONAL_FLAGS[cli]
    found = flags_in_help(help_text, required + optional)
    return {
        "version": version,
        "probed": True,
        "supported": found,
        "missing_required": [flag for flag in required if flag not in found],
        "missing_optional": [flag for flag in optional if flag not in found],
    }


def cache_path(repo_root: Path) -> Path:
    return repo_root / ".clodex" / CACHE_NAME


def load(repo_root: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(cache_path(repo_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def probe_all(repo_root: Path, clis: tuple[str, ...] = ("claude", "codex"), use_cache: bool = True) -> dict[str, dict[str, Any]]:
    """Probe each CLI, reusing the cached result while its version is unchanged."""
    cached = load(repo_root) if use_cache else {}
    result: dict[str, dict[str, Any]] = {}
    for cli in clis:
        version = _version(cli)
        previous = cached.get(cli)
        if previous and previous.get("probed") and version and previous.get("version") == version:
            result[cli] = previous
        else:
            result[cli] = probe_cli(cli)
    try:
        path = cache_path(repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass
    return result


def supports(repo_root: Path, cli: str, flag: str) -> bool | None:
    """True/False if the cache knows, None if unknown (then callers assume supported)."""
    info = load(repo_root).get(cli)
    if not info or not info.get("probed"):
        return None
    return flag in info.get("supported", [])
