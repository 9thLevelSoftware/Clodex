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


# ---------------------------------------------------------------- native handoffs


def normalized_actor(value: Any) -> str | None:
    if value is None:
        return None
    actor = str(value).strip().lower()
    return actor if actor in {"claude", "codex"} else None


def normalized_diff_hash(value: Any) -> str | None:
    if value is None:
        return None
    diff_hash = str(value).strip()
    return diff_hash or None


def resolve_reviewer(identifier: Any, reviewers: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Map a report's reviewer id, or just its actor (`claude` / `codex`), onto a configured reviewer.

    An actor with no explicit reviewer id stands for the first *required* reviewer of its backend
    (the first of that backend if none is required), so the default claude/codex flow keeps working.
    """
    if identifier is None:
        return None
    name = str(identifier).strip()
    for reviewer in reviewers:
        if str(reviewer.get("id")) == name:
            return reviewer
    backend = normalized_actor(name)
    if backend is None:
        return None
    same_backend = [r for r in reviewers if str(r.get("backend")) == backend]
    required = [r for r in same_backend if r.get("required", True)]
    return (required or same_backend or [None])[0]


def evaluate_handoff(data: dict[str, Any], reviewers: list[dict[str, Any]], quorum: Any = "unanimous") -> dict[str, Any]:
    """Whether the reports recorded on a handoff satisfy the configured reviewers and quorum.

    Same rules as the classic audit: a report approves only for the diff hash it names, a new
    hash resets every approval, and a hashless rejection withdraws that reviewer's approval.
    """
    latest_hash: str | None = None
    approvals: dict[str, bool] = {}
    for event in data.get("events") or []:
        if event.get("event") != "handoff.update":
            continue
        event_data = event.get("data")
        if not isinstance(event_data, dict):
            continue
        report = event_data.get("report")
        if not isinstance(report, dict):
            report = {}
        reviewer = resolve_reviewer(report.get("reviewer_id") or event_data.get("reviewer_id") or event_data.get("actor") or report.get("actor"), reviewers)
        diff_hash = normalized_diff_hash(event_data.get("diff_hash") or report.get("diff_hash"))
        reviewer_id = str(reviewer["id"]) if reviewer else None
        if diff_hash is None:
            if latest_hash is not None and reviewer_id is not None and report.get("approved") is False:
                approvals[reviewer_id] = False
            continue
        if diff_hash != latest_hash:
            latest_hash = diff_hash
            approvals = {}
        if reviewer_id is None:
            continue
        if report.get("approved") is True:
            approvals[reviewer_id] = True
        elif report.get("approved") is False:
            approvals[reviewer_id] = False

    verdicts = [
        {
            "reviewer_id": str(reviewer["id"]),
            "approved": bool(approvals.get(str(reviewer["id"]))),
            "diff_hash": latest_hash if approvals.get(str(reviewer["id"])) else None,
            "persona": reviewer.get("persona"),
            "_required": bool(reviewer.get("required", True)),
        }
        for reviewer in reviewers
    ]
    result = evaluate(verdicts, latest_hash or "", 0, quorum)
    result["approved"] = bool(result["approved"] and latest_hash)
    result["diff_hash"] = latest_hash
    by_id = {str(reviewer["id"]): reviewer for reviewer in reviewers}
    approving = [rid for rid in result["required_approved_by"] if rid in by_id]
    result["approved_reviewers"] = approving
    # `approved_by` lists the agents (claude / codex) behind the approving reviewers, as it always has.
    result["approved_by"] = sorted({str(by_id[rid].get("backend")) for rid in approving})
    return result
