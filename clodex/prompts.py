from __future__ import annotations

import json
import re
from typing import Any

from .config import ClodexConfig


def plan_prompt(config: ClodexConfig, task: str) -> str:
    return f"""You are the Claude Code planning wave for Clodex.

Workflow contract:
{config.prompt_body or "Use the Clodex dual-agent workflow."}

Task:
{task}

Return only one JSON object with this shape:
{{
  "goal": "clear goal",
  "scope": ["in scope item"],
  "out_of_scope": ["excluded item"],
  "implementation_spec": ["decision-complete implementation step"],
  "acceptance_criteria": ["observable success criterion"],
  "risks": ["risk or assumption"],
  "test_commands": ["command to run"]
}}
"""


def implementation_prompt(plan: dict[str, Any], task: str) -> str:
    return f"""You are the Codex engineering wave for Clodex.

Implement only the accepted Claude plan below. Keep changes scoped, preserve user changes, add/update tests where appropriate, and run relevant verification.

Original task:
{task}

Accepted Claude plan JSON:
{json.dumps(plan, indent=2, sort_keys=True)}

When finished, print a concise Markdown report with:
- files changed
- tests run and pass/fail status
- unresolved issues, if any
"""


def audit_prompt(agent_name: str, plan: dict[str, Any], diff: str, diff_hash: str, reviewer_id: str | None = None, persona: str | None = None) -> str:
    reviewer = reviewer_id or agent_name.lower().replace(" ", "-")
    selected_persona = persona or agent_name
    return f"""You are the {agent_name} adversarial auditor in Clodex.

Audit the diff against the accepted plan. Be strict: reject correctness bugs, missing tests, unsafe behavior, scope creep, broken CLI contracts, or unverified claims.

Reviewer ID: {reviewer}
Persona: {selected_persona}
Diff hash: {diff_hash}

Accepted plan:
{json.dumps(plan, indent=2, sort_keys=True)}

Diff:
```diff
{diff}
```

Return only one JSON object:
{{
  "approved": true,
  "diff_hash": "{diff_hash}",
  "reviewer_id": "{reviewer}",
  "persona": "{selected_persona}",
  "summary": "short verdict",
  "findings": [
    {{"severity": "critical|high|medium|low|info", "file": "path or null", "line": 1, "message": "finding"}}
  ],
  "required_fixes": ["specific fix"]
}}
"""


def fix_prompt(plan: dict[str, Any], findings: list[str]) -> str:
    return f"""You are Codex fixing a Clodex audit rejection.

Apply only the required fixes below. Do not broaden scope. Re-run relevant verification.

Accepted plan:
{json.dumps(plan, indent=2, sort_keys=True)}

Required fixes:
{json.dumps(findings, indent=2)}

When finished, print a concise Markdown report with changed files and tests run.
"""


def audit_diff_excerpt(diff: str, max_bytes: int) -> str:
    """Cap the diff embedded in an audit prompt; the diff hash still covers the whole diff."""
    encoded = diff.encode("utf-8", errors="replace")
    if max_bytes <= 0 or len(encoded) <= max_bytes:
        return diff
    shown = encoded[:max_bytes].decode("utf-8", errors="ignore")
    files = list(dict.fromkeys(re.findall(r"^diff --git a/(.+?) b/", diff, flags=re.MULTILINE)))
    return (
        f"{shown}\n\n[diff truncated: showing the first {max_bytes} of {len(encoded)} bytes. "
        f"Changed files ({len(files)}): {', '.join(files)}. "
        "Reject if the omitted part could hide a problem you cannot rule out.]\n"
    )


def delegate_prompt(mode: str, task: str, instructions: str | None, fixes: list[str] | None = None, answers: list[tuple[str, str]] | None = None) -> str:
    """The prompt for a Codex job a native handoff delegated (implement or fix)."""
    lines = [
        "You are the Codex engineering wave for Clodex, working a native Claude/Codex handoff.",
        "",
        "Work only inside the current directory: it is an isolated checkout made for this handoff. "
        "Keep changes scoped, preserve existing user changes, add or update tests where appropriate, and run the relevant verification.",
        "",
        "Original task:",
        task,
    ]
    if instructions:
        lines += ["", "Instructions from Claude:", instructions]
    if answers:
        lines += ["", "Clarifications from Claude (your earlier questions, now answered):"]
        for question, answer in answers:
            lines += [f"- Q: {question}", f"  A: {answer}"]
    if mode == "fix":
        lines += ["", "Required fixes:", json.dumps(fixes or ["Resolve the open review findings."], indent=2), "", "Apply only the required fixes. Do not broaden scope."]
    lines += [
        "",
        "If you cannot proceed without a decision from Claude (product intent or acceptance criteria are unclear), "
        "do not guess: stop and end your final message with a single JSON object "
        '{"clarifications": ["one specific question", "..."]} and nothing after it.',
        "",
        "Otherwise, when finished, print a concise Markdown report with:",
        "- files changed",
        "- tests run and pass/fail status",
        "- unresolved issues, if any",
    ]
    return "\n".join(lines) + "\n"
