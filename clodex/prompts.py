from __future__ import annotations

import json
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
    {{"severity": "high|medium|low", "file": "path", "line": 1, "message": "finding"}}
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
