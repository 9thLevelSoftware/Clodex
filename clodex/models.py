"""Known Claude / Codex models: effort levels, retirement dates and config validation."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max", "ultra")
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CLAUDE_ALIASES = frozenset({"opus", "sonnet", "haiku", "fable", "best", "opusplan", "default"})
_CLAUDE_MODEL = re.compile(r"^(claude-[a-z0-9][a-z0-9._-]*|(opus|sonnet|haiku|fable)(\[[0-9a-z]+\])?)$")
CATALOG_TTL = timedelta(hours=24)

_UP_TO_ULTRA = ("low", "medium", "high", "xhigh", "max", "ultra")
_UP_TO_MAX = ("low", "medium", "high", "xhigh", "max")
_UP_TO_XHIGH = ("low", "medium", "high", "xhigh")


@dataclass(frozen=True)
class CodexModel:
    slug: str
    efforts: tuple[str, ...]
    retires: str | None = None  # ISO date the model stops working
    successor: str | None = None


# Snapshot of the Codex catalog (`codex debug models`) as of 2026-09. The live catalog,
# when available, takes precedence for efforts; this table carries the retirement dates.
BUILTIN_CODEX: dict[str, CodexModel] = {
    model.slug: model
    for model in (
        CodexModel("gpt-6.1-sol", _UP_TO_ULTRA),
        CodexModel("gpt-6-sol", _UP_TO_ULTRA),
        CodexModel("gpt-6-astra", _UP_TO_ULTRA),
        CodexModel("gpt-6-luna", _UP_TO_MAX),
        CodexModel("gpt-5.6-sol", _UP_TO_ULTRA),
        CodexModel("gpt-5.6-terra", _UP_TO_ULTRA),
        CodexModel("gpt-5.6-luna", _UP_TO_MAX),
        CodexModel("gpt-5.5", _UP_TO_XHIGH, retires="2026-10-14", successor="gpt-6.1-sol"),
        CodexModel("gpt-5.4", _UP_TO_XHIGH, retires="2026-08-31", successor="gpt-6.1-sol"),
        CodexModel("gpt-5.4-mini", _UP_TO_XHIGH, retires="2026-08-31", successor="gpt-6.1-sol"),
        CodexModel("gpt-5.3-codex-spark", _UP_TO_XHIGH, retires="2026-09-14", successor="gpt-6.1-sol"),
    )
}


def today() -> date:
    return date.today()


@dataclass(frozen=True)
class Retirement:
    model: str
    on: date
    successor: str | None
    retired: bool


def retirement(model: str, on: date | None = None) -> Retirement | None:
    """Retirement info for a model that is retired or has a retirement date; None otherwise."""
    entry = BUILTIN_CODEX.get(model)
    if entry is None or not entry.retires:
        return None
    retire_date = date.fromisoformat(entry.retires)
    return Retirement(model, retire_date, entry.successor, (on or today()) >= retire_date)


class ModelRetiredError(ValueError):
    """The configured Codex model no longer exists, so a run would fail immediately."""


def ensure_usable(config: Any, on: date | None = None) -> None:
    """Refuse to start a run on a retired Codex model. CLODEX_ALLOW_RETIRED_MODEL=1 overrides."""
    if os.environ.get("CLODEX_ALLOW_RETIRED_MODEL"):
        return
    for where, settings in (("codex.model", config.codex_role("build")), ("codex.audit.model", config.codex_role("audit"))):
        info = retirement(str(settings["model"]), on)
        if info is not None and info.retired:
            fix = f" Run `clodex init --migrate` (or set `{where}: {info.successor}`)." if info.successor else ""
            raise ModelRetiredError(
                f"Codex model '{info.model}' ({where}) retired on {info.on.isoformat()}.{fix} "
                "Set CLODEX_ALLOW_RETIRED_MODEL=1 to try anyway."
            )


def supported_efforts(model: str, catalog: dict[str, dict[str, Any]] | None = None) -> tuple[str, ...] | None:
    """Reasoning efforts a Codex model accepts, from the live catalog if present, else the built-in table."""
    if catalog and model in catalog and catalog[model].get("efforts"):
        return tuple(catalog[model]["efforts"])
    entry = BUILTIN_CODEX.get(model)
    return entry.efforts if entry else None


def nearest_effort(effort: str, supported: tuple[str, ...]) -> str:
    """The highest supported effort not above `effort`; the lowest supported one if none is."""
    if effort in supported:
        return effort
    rank = EFFORT_ORDER.index(effort) if effort in EFFORT_ORDER else len(EFFORT_ORDER)
    at_or_below = [level for level in supported if level in EFFORT_ORDER and EFFORT_ORDER.index(level) <= rank]
    return at_or_below[-1] if at_or_below else supported[0]


# ---------------------------------------------------------------- live catalog


def parse_catalog(raw: str) -> dict[str, dict[str, Any]]:
    """Parse `codex debug models` JSON into {slug: {efforts, visibility, default_effort}}."""
    data = json.loads(raw)
    models = data.get("models", []) if isinstance(data, dict) else data
    catalog: dict[str, dict[str, Any]] = {}
    for item in models:
        slug = item.get("slug") or item.get("id")
        if not slug:
            continue
        levels = item.get("supported_reasoning_levels") or item.get("supported_reasoning_efforts") or []
        efforts = [level.get("effort") if isinstance(level, dict) else level for level in levels]
        catalog[str(slug)] = {
            "efforts": [str(effort) for effort in efforts if effort],
            "visibility": item.get("visibility"),
            "default_effort": item.get("default_reasoning_level") or item.get("default_reasoning_effort"),
        }
    return catalog


def refresh_codex_catalog(cache_path: Path | None = None, now: datetime | None = None, force: bool = False) -> dict[str, dict[str, Any]] | None:
    """The live Codex model catalog, cached for 24h. None when Codex is unavailable and nothing is cached."""
    now = now or datetime.now(UTC)
    cached = _read_cache(cache_path)
    if cached and not force and now - cached[0] < CATALOG_TTL:
        return cached[1]
    exe = shutil.which("codex")
    if exe:
        try:
            result = subprocess.run([exe, "debug", "models"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
            if result.returncode == 0:
                catalog = parse_catalog(result.stdout)
                if catalog:
                    _write_cache(cache_path, now, catalog)
                    return catalog
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return cached[1] if cached else None  # stale beats nothing


def _read_cache(path: Path | None) -> tuple[datetime, dict[str, dict[str, Any]]] | None:
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return datetime.fromisoformat(data["fetched_at"]), data["models"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_cache(path: Path | None, now: datetime, catalog: dict[str, dict[str, Any]]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": now.isoformat(), "models": catalog}, indent=1) + "\n", encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- validation


@dataclass(frozen=True)
class Diagnostic:
    level: str  # "error" | "warning"
    where: str
    message: str
    fix: str | None = None

    def as_dict(self) -> dict[str, str]:
        data = {"level": self.level, "where": self.where, "message": self.message}
        if self.fix:
            data["fix"] = self.fix
        return data


def codex_model_diagnostics(where: str, model: str, effort: str, catalog: dict[str, dict[str, Any]] | None, on: date | None = None) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    info = retirement(model, on)
    if info is not None:
        fix = f"set `{where.rsplit('.', 1)[0]}.model: {info.successor}` (or run `clodex init --migrate`)" if info.successor else None
        if info.retired:
            found.append(Diagnostic("error", where, f"Codex model '{model}' retired on {info.on.isoformat()}", fix))
        else:
            found.append(Diagnostic("warning", where, f"Codex model '{model}' retires on {info.on.isoformat()}", fix))
    elif catalog and model not in catalog and model not in BUILTIN_CODEX:
        found.append(Diagnostic("warning", where, f"'{model}' is not in the Codex model catalog (`codex debug models`)"))
    efforts = supported_efforts(model, catalog)
    if efforts is not None and effort not in efforts and not (info and info.retired):
        found.append(
            Diagnostic(
                "error",
                where.rsplit(".", 1)[0] + ".reasoning_effort",
                f"effort '{effort}' is not supported by '{model}' (supported: {', '.join(efforts)})",
                f"use one of: {', '.join(efforts)}",
            )
        )
    elif efforts is None and effort not in EFFORT_ORDER:
        found.append(Diagnostic("warning", where.rsplit(".", 1)[0] + ".reasoning_effort", f"unknown reasoning effort '{effort}'"))
    return found


def claude_model_diagnostics(where: str, model: str, effort: str) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    if not _CLAUDE_MODEL.match(model):
        found.append(Diagnostic("warning", f"{where}.model", f"'{model}' is not a known Claude alias or model id", f"aliases: {', '.join(sorted(CLAUDE_ALIASES - {'default'}))}"))
    if effort not in CLAUDE_EFFORTS:
        found.append(Diagnostic("error", f"{where}.effort", f"effort '{effort}' is not valid for claude (use {', '.join(CLAUDE_EFFORTS)})"))
    return found


def validate(config: Any, catalog: dict[str, dict[str, Any]] | None = None, on: date | None = None) -> list[Diagnostic]:
    """Check a loaded CLODEX.md config (duck-typed ClodexConfig) for problems that would break a run."""
    from .quorum import parse_quorum

    found: list[Diagnostic] = []
    for role in ("plan", "audit"):
        settings = config.claude_role(role)
        found.extend(claude_model_diagnostics(f"claude.{role}", str(settings["model"]), str(settings["effort"])))
    build = config.codex_role("build")
    found.extend(codex_model_diagnostics("codex.model", str(build["model"]), str(build["reasoning_effort"]), catalog, on))
    audit = config.codex_role("audit")
    if (audit["model"], audit["reasoning_effort"]) != (build["model"], build["reasoning_effort"]):
        found.extend(codex_model_diagnostics("codex.audit.model", str(audit["model"]), str(audit["reasoning_effort"]), catalog, on))

    codex = config.codex
    if str(codex.get("sandbox")) not in {"read-only", "workspace-write", "danger-full-access"}:
        found.append(Diagnostic("error", "codex.sandbox", f"unknown sandbox '{codex.get('sandbox')}'", "use read-only, workspace-write or danger-full-access"))
    if str(codex.get("approval_profile")) not in {"ci", "local", "auto_review"}:
        found.append(Diagnostic("error", "codex.approval_profile", f"unknown approval profile '{codex.get('approval_profile')}'", "use ci, local or auto_review"))
    if str(config.workspace.get("backend")) not in {"git-worktree", "local"}:
        found.append(Diagnostic("error", "workspace.backend", f"unknown workspace backend '{config.workspace.get('backend')}'", "use git-worktree or local"))

    reviewers = config.reviewers
    seen: set[str] = set()
    for index, reviewer in enumerate(reviewers):
        where = f"audit.reviewers[{index}]"
        reviewer_id = str(reviewer.get("id", ""))
        if not reviewer_id:
            found.append(Diagnostic("error", where, "reviewer has no id"))
        elif reviewer_id in seen:
            found.append(Diagnostic("error", where, f"duplicate reviewer id '{reviewer_id}'"))
        seen.add(reviewer_id)
        if str(reviewer.get("backend")) not in {"claude", "codex"}:
            found.append(Diagnostic("error", where, f"reviewer '{reviewer_id}' has unknown backend '{reviewer.get('backend')}'", "use claude or codex"))
    required = [reviewer for reviewer in reviewers if reviewer.get("required", True)]
    if not required:
        found.append(Diagnostic("error", "audit.reviewers", "no required reviewers: every run would end blocked", "mark at least one reviewer required: true"))
    try:
        parse_quorum(config.audit.get("quorum", "unanimous"))
    except ValueError as exc:
        found.append(Diagnostic("error", "audit.quorum", str(exc), "use unanimous, majority or a number"))
    return found
