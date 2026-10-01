"""Audit agreement: decide whether the reviewers' verdicts approve a diff."""

from __future__ import annotations

from typing import Any

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def verdict_approved(verdict: dict[str, Any], diff_hash: str) -> bool:
    """A verdict approves only if it says so, for exactly this diff, and the reviewer did not fail."""
    return bool(verdict.get("approved")) and verdict.get("diff_hash") == diff_hash and not verdict.get("_error")


def parse_quorum(value: Any) -> tuple[str, int]:
    """`unanimous`, `majority`, or an integer N (N required approvals)."""
    if isinstance(value, bool):
        raise ValueError(f"Unsupported audit.quorum: {value!r}")
    if isinstance(value, int):
        number = value
    else:
        text = str(value).strip().lower()
        if text in {"unanimous", "all"}:
            return "unanimous", 0
        if text == "majority":
            return "majority", 0
        if not text.isdigit():
            raise ValueError(f"Unsupported audit.quorum: {value!r} (use unanimous, majority or a number)")
        number = int(text)
    if number < 1:
        raise ValueError(f"audit.quorum must be at least 1, got {number}")
    return "count", number


def quorum_met(approved: int, total: int, quorum: Any) -> bool:
    """Whether `approved` of `total` required reviewers satisfies the quorum. No required reviewers never passes."""
    if total == 0:
        return False
    kind, number = parse_quorum(quorum)
    if kind == "unanimous":
        return approved == total
    if kind == "majority":
        return approved * 2 > total
    return approved >= min(number, total)


def evaluate(verdicts: list[dict[str, Any]], diff_hash: str, attempt: int = 0, quorum: Any = "unanimous") -> dict[str, Any]:
    """Combine reviewer verdicts. Only required reviewers count toward the quorum."""
    required = [verdict for verdict in verdicts if verdict.get("_required", True)]
    reviewers = {
        str(verdict.get("reviewer_id")): {
            "approved": verdict_approved(verdict, diff_hash),
            "required": bool(verdict.get("_required", True)),
            "diff_hash": verdict.get("diff_hash"),
            "persona": verdict.get("persona"),
            **({"error": verdict["_error"]} if verdict.get("_error") else {}),
        }
        for verdict in verdicts
    }
    approved_by = [str(v.get("reviewer_id")) for v in required if verdict_approved(v, diff_hash)]
    pending = [str(v.get("reviewer_id")) for v in required if not verdict_approved(v, diff_hash)]
    return {
        "approved": quorum_met(len(approved_by), len(required), quorum),
        "attempt": attempt,
        "diff_hash": diff_hash,
        "quorum": str(quorum),
        "reviewers": reviewers,
        "required_approved_by": approved_by,
        "required_pending": pending,
    }


def required_fixes(verdicts: list[dict[str, Any]], diff_hash: str) -> list[str]:
    """Fixes asked for by required reviewers that did not approve, de-duplicated, most severe first."""
    fixes: list[str] = []
    findings: list[tuple[int, str]] = []
    for verdict in verdicts:
        if not verdict.get("_required", True) or verdict.get("_error") or verdict_approved(verdict, diff_hash):
            continue
        fixes.extend(str(fix) for fix in verdict.get("required_fixes") or [])
        for finding in verdict.get("findings") or []:
            if isinstance(finding, dict):
                severity = str(finding.get("severity") or "info")
                where = finding.get("file")
                if where and finding.get("line") is not None:
                    where = f"{where}:{finding['line']}"
                label = f"[{severity}] " + (f"{where}: " if where else "")
                findings.append((SEVERITY_ORDER.get(severity, len(SEVERITY_ORDER)), label + str(finding.get("message", ""))))
            else:
                findings.append((len(SEVERITY_ORDER), str(finding)))
    ordered = fixes + [text for _, text in sorted(findings, key=lambda item: item[0])]
    return list(dict.fromkeys(ordered)) or ["Resolve audit disagreement and make the diff satisfy the accepted plan."]
