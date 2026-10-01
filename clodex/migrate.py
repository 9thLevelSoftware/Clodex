"""`clodex init --migrate`: fix CLODEX.md settings that no longer work.

Edits only the YAML front matter, line by line, so comments, ordering and line endings
survive. It is idempotent: a migrated file yields no further changes.
"""

from __future__ import annotations

import re
from datetime import date

from .models import nearest_effort, retirement, supported_efforts

_KEY = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z_][\w-]*) *:(?P<rest>.*?)(?P<eol>\r?\n?)$")
_VALUE = re.compile(r"^(?P<lead> *)(?P<open>[\"']?)(?P<value>[^\s#\"']*)(?P<close>[\"']?)(?P<tail>\s*(?:#.*)?)$")


def _is_content(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _front_matter(lines: list[str]) -> tuple[int, int] | None:
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return 1, index
    return None


def _find_key(lines: list[str], span: tuple[int, int], key: str) -> tuple[int, tuple[int, int]] | None:
    """Locate `key:` among the direct children of `span`; returns (line index, span of its own children)."""
    start, end = span
    child_indent = next((_indent(lines[i]) for i in range(start, end) if _is_content(lines[i])), None)
    if child_indent is None:
        return None
    for index in range(start, end):
        match = _KEY.match(lines[index])
        if match and _indent(lines[index]) == child_indent and match.group("key") == key:
            stop = index + 1
            while stop < end and (not _is_content(lines[stop]) or _indent(lines[stop]) > child_indent):
                stop += 1
            return index, (index + 1, stop)
    return None


def _get_scalar(line: str) -> str | None:
    match = _KEY.match(line)
    value = _VALUE.match(match.group("rest")) if match else None
    return value.group("value") if value and value.group("value") else None


def _set_scalar(lines: list[str], index: int, new_value: str) -> None:
    match = _KEY.match(lines[index])
    assert match is not None
    value = _VALUE.match(match.group("rest"))
    assert value is not None
    lines[index] = (
        f"{match.group('indent')}{match.group('key')}:{value.group('lead') or ' '}"
        f"{value.group('open')}{new_value}{value.group('close')}{value.group('tail')}{match.group('eol')}"
    )


def migrate_contract(text: str, split_claude: bool = False, today: date | None = None) -> tuple[str, list[str]]:
    """Return (new text, human-readable list of changes)."""
    lines = text.splitlines(keepends=True)
    front = _front_matter(lines)
    if front is None:
        return text, []
    changes: list[str] = []

    codex = _find_key(lines, front, "codex")
    if codex is not None:
        scopes = [("codex", codex[1])]
        audit = _find_key(lines, codex[1], "audit")
        if audit is not None:
            scopes.append(("codex.audit", audit[1]))
        for name, span in scopes:
            model_at = _find_key(lines, span, "model")
            effort_at = _find_key(lines, span, "reasoning_effort")
            model = _get_scalar(lines[model_at[0]]) if model_at else None
            if model and model_at:
                info = retirement(model, today)
                if info is not None and info.successor:
                    _set_scalar(lines, model_at[0], info.successor)
                    state = "retired" if info.retired else "retires"
                    changes.append(f"{name}.model: {model} -> {info.successor} ({state} {info.on.isoformat()})")
                    model = info.successor
            effort = _get_scalar(lines[effort_at[0]]) if effort_at else None
            efforts = supported_efforts(model) if model else None
            if effort and effort_at and efforts and effort not in efforts:
                fixed = nearest_effort(effort, efforts)
                _set_scalar(lines, effort_at[0], fixed)
                changes.append(f"{name}.reasoning_effort: {effort} -> {fixed} (not supported by {model})")

    if split_claude:
        changes.extend(_split_claude(lines, front))

    return "".join(lines), changes


def _split_claude(lines: list[str], front: tuple[int, int]) -> list[str]:
    claude = _find_key(lines, front, "claude")
    if claude is None:
        return []
    header, span = claude
    if _find_key(lines, span, "plan") or _find_key(lines, span, "audit"):
        return []  # already split
    model_at = _find_key(lines, span, "model")
    effort_at = _find_key(lines, span, "effort")
    if not model_at and not effort_at:
        return []
    model = (_get_scalar(lines[model_at[0]]) if model_at else None) or "opus"
    effort = (_get_scalar(lines[effort_at[0]]) if effort_at else None) or "max"
    audit_effort = "high" if effort == "max" else effort
    child_indent = next((_indent(lines[i]) for i in range(span[0], span[1]) if _is_content(lines[i])), _indent(lines[header]) + 2)
    pad = " " * child_indent
    eol = "\r\n" if lines[header].endswith("\r\n") else "\n"
    block = [
        f"{pad}plan:{eol}",
        f"{pad}  model: {model}{eol}",
        f"{pad}  effort: {effort}{eol}",
        f"{pad}audit:{eol}",
        f"{pad}  model: {model}{eol}",
        f"{pad}  effort: {audit_effort}{eol}",
    ]
    flat = sorted(item[0] for item in (model_at, effort_at) if item)
    insert_at = flat[0]
    for index in reversed(flat):
        del lines[index]
    lines[insert_at:insert_at] = block
    return [f"claude: split flat model/effort into plan ({model}/{effort}) and audit ({model}/{audit_effort})"]
