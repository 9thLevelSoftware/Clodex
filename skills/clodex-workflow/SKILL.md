---
name: clodex-workflow
description: Use when coordinating Claude Code CLI planning with Codex CLI implementation and dual adversarial audit.
---

# Clodex Workflow

Use `clodex` for local-first dual-agent work:

1. `clodex plan "<task>"` asks Claude Code CLI to produce a structured plan.
2. `clodex build "<task>"` runs Claude planning, Codex implementation, and multi-reviewer audit in an isolated worktree.
3. `clodex apply <run-id>` applies an approved worktree patch back to the source checkout.
4. `clodex task start/get/cancel/list` manages durable async runs.
5. `clodex audit --diff` audits the current uncommitted diff with both Claude and Codex.
6. `clodex status` shows recent local task and run state.

Defaults:

- Claude planner/auditor: `claude -p --model opus --effort max --permission-mode plan`.
- Codex engineer/auditor: `codex exec` or `codex review` with `gpt-5.5` and `model_reasoning_effort="xhigh"`.
- Builds default to `.clodex/workspaces/<run-id>/`; use `--workspace local` only when in-place changes are intentional.
- Approval profiles are `ci`, `local`, and `auto_review`; `ci` is the deterministic default.
- Subscription CLI auth is preferred. API keys are fallback-only for CI/headless use.

Do not mark a run complete unless `.clodex/runs/<run-id>/05-agreement.json`
contains `approved: true`.
