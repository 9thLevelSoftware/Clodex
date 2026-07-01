from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


BEGIN_MARKER = "<!-- BEGIN CLODEX -->"
END_MARKER = "<!-- END CLODEX -->"
TOML_BEGIN_MARKER = "# BEGIN CLODEX"
TOML_END_MARKER = "# END CLODEX"
DEFAULT_HANDOFF_BUDGET = 6


class ManagedBlockError(ValueError):
    pass


@dataclass(frozen=True)
class NativeFilePlan:
    path: Path
    action: str
    current_status: str
    content: str
    preview: str


def normalize_block_body(body: str) -> str:
    return body if body.endswith("\n") else body + "\n"


def replace_managed_block(existing: str, body: str, *, force: bool = False) -> tuple[str, bool]:
    return _replace_managed_block(existing, body, BEGIN_MARKER, END_MARKER, force=force)


def replace_toml_managed_block(existing: str, body: str, *, force: bool = False) -> tuple[str, bool]:
    return _replace_managed_block(existing, body, TOML_BEGIN_MARKER, TOML_END_MARKER, force=force)


def build_claude_block() -> str:
    return f"""# Clodex Native Collaboration

Claude Code is the default strategist and orchestrator for Clodex work.
Codex is the default engineer for implementation, fixes, and focused audit work.

When a user asks for non-trivial design, implementation, debugging, refactoring, or review work:

1. Prefer the MCP tool `clodex_handoff_create` to create or resume a durable handoff.
2. Use `clodex_handoff_get` before acting on an existing handoff.
3. Produce the plan, design, acceptance criteria, risks, and test commands before implementation.
4. Delegate implementation-oriented work to Codex through Clodex instead of manually telling the user to run Codex.
5. Use `clodex_handoff_update` to record plan artifacts, phase changes, review notes, changed files, tests, unresolved issues, and diff hashes.
6. Use `clodex_handoff_decide` before claiming completion when Codex has implemented or audited changes.
7. Stop and summarize for the user when Clodex reports `blocked`.

Default role contract:

- Claude Code is the default strategist.
- Codex is the default engineer.
- Owner starts as `claude`.
- Handoff budget is {DEFAULT_HANDOFF_BUDGET}.
- Phase order is `planning`, `implementation`, `audit`, `fix`, `decision`.

If MCP tools are unavailable, use these CLI fallbacks:

```bash
clodex task start "<task>"
clodex task get <run-id>
clodex task cancel <run-id>
clodex audit --diff
clodex status
```
"""


def build_agents_block() -> str:
    return f"""# Clodex Native Collaboration

Codex is the default engineer for Clodex work, while Claude Code is the default strategist.

When Clodex context is present:

1. Treat incoming Clodex handoffs as implementation, audit, or fix work according to the current phase.
2. Use `clodex_handoff_get` before acting on an existing run.
3. Implement from the accepted Claude plan and avoid expanding scope without recording the reason.
4. Use `clodex_handoff_update` to record changed files, tests run, unresolved issues, audit verdicts, and diff hash.
5. Use `clodex_handoff_create` only when the user asks Codex to start a native collaboration directly.
6. Use `clodex_handoff_decide` before claiming completion after implementation or audit work.
7. Ask for clarification through Clodex when product intent or acceptance criteria are unclear.
8. Respect owner, phase, and handoff budget state. When the budget is exhausted, summarize the disagreement for the user.

Default role contract:

- Claude Code is the default strategist.
- Codex is the default engineer.
- Owner starts as `claude`.
- handoff budget is {DEFAULT_HANDOFF_BUDGET}.
- Phase order is `planning`, `implementation`, `audit`, `fix`, `decision`.

Prefer MCP tools. If MCP is unavailable, use these CLI fallbacks:

```bash
clodex task start "<task>"
clodex task get <run-id>
clodex task cancel <run-id>
clodex audit --diff
clodex status
```
"""


def _replace_managed_block(
    existing: str,
    body: str,
    begin_marker: str,
    end_marker: str,
    *,
    force: bool,
) -> tuple[str, bool]:
    block_body = normalize_block_body(body)
    replacement = f"{begin_marker}\n{block_body}{end_marker}\n"
    begin_count = existing.count(begin_marker)
    end_count = existing.count(end_marker)

    if begin_count == 0 and end_count == 0:
        updated = existing + _append_separator(existing) + replacement
        return updated, updated != existing

    begin = existing.find(begin_marker)
    end = existing.find(end_marker)
    if begin_count != 1 or end_count != 1 or begin == -1 or end == -1 or end < begin:
        if not force:
            raise ManagedBlockError(f"Malformed Clodex managed block for {begin_marker!r}")
        marker_positions = [pos for pos in (begin, end) if pos != -1]
        prefix = existing[: min(marker_positions)] if marker_positions else existing
        updated = prefix + _prefix_separator(prefix) + replacement
        return updated, updated != existing

    end_after = _after_marker_line(existing, end + len(end_marker))
    updated = existing[:begin] + replacement + existing[end_after:]
    return updated, updated != existing


def _append_separator(existing: str) -> str:
    if not existing or existing.endswith("\n\n") or existing.endswith("\r\n\r\n"):
        return ""
    if existing.endswith("\n"):
        return "\n"
    return "\n\n"


def _prefix_separator(prefix: str) -> str:
    if not prefix or prefix.endswith("\n"):
        return ""
    return "\n"


def _after_marker_line(existing: str, offset: int) -> int:
    if existing.startswith("\r\n", offset):
        return offset + 2
    if existing.startswith("\n", offset):
        return offset + 1
    return offset
