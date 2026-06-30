---
name: clodex-workflow
description: Use when coordinating Claude Code CLI planning with Codex CLI implementation and dual adversarial audit.
---

# Clodex Workflow

Use `clodex` for local-first dual-agent work:

1. `clodex plan "<task>"` asks Claude Code CLI to produce a structured plan.
2. `clodex build "<task>"` runs Claude planning, Codex implementation, and dual audit.
3. `clodex audit --diff` audits the current uncommitted diff with both Claude and Codex.
4. `clodex status` shows recent local task and run state.

Defaults:

- Claude planner/auditor: `claude -p --model opus --effort max --permission-mode plan`.
- Codex engineer/auditor: `codex exec` or `codex review` with `gpt-5.5` and `model_reasoning_effort="xhigh"`.
- Subscription CLI auth is preferred. API keys are fallback-only for CI/headless use.

Do not mark a run complete unless `.clodex/runs/<run-id>/05-agreement.json`
contains `approved: true`.
