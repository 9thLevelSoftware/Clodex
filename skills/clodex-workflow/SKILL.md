---
name: clodex-workflow
description: Use when coordinating Claude Code CLI planning with Codex CLI implementation and dual adversarial audit.
---

# Clodex Workflow

Prefer native Clodex coordination when the repository has been initialized with
`clodex init`: use the MCP handoff tools first, and the `clodex` CLI as the fallback. The
harness commands remain valid for scripted or non-MCP workflows.

## Native handoffs (MCP)

1. `clodex_handoff_create` with `workspace: "git-worktree"` for an isolated checkout.
2. Record the plan with `clodex_handoff_update`.
3. `clodex_delegate` mode `implement` (`instructions` = the plan; `wait: true` or poll
   `clodex_handoff_get`). Codex works in the worktree and its result is recorded for you.
4. Review the diff and record your own verdict with `clodex_handoff_update` (actor `claude`, the
   diff hash from `clodex_handoff_get`, `report.approved`, `report.required_fixes`).
5. `clodex_delegate` mode `audit` for Codex's verdict; if anyone rejected, mode `fix` and audit again.
6. If `clodex_handoff_get` shows `open_clarifications`, answer them with `clodex_answer` and
   delegate again; answers are included in Codex's next prompt.
7. `clodex_handoff_decide`; only an `approved` decision means the configured reviewers agree on
   this exact diff. Then `clodex apply <run-id>`.

## Harness commands

1. `clodex plan "<task>"` asks Claude Code CLI to produce a structured plan.
2. `clodex build "<task>"` runs Claude planning, Codex implementation, and multi-reviewer audit in an isolated worktree.
3. `clodex apply <run-id>` applies an approved worktree patch back to the source checkout (`--check` to dry-run).
4. `clodex clean <run-id>` removes the kept worktree of a finished run.
5. `clodex task start/get/cancel/list` manages durable async runs.
6. `clodex audit [--diff | --base REF | --commit SHA]` audits the uncommitted diff, a branch, or one commit with both Claude and Codex.
7. `clodex doctor` checks CLIs, login, models and settings; `clodex status` shows recent runs.

Exit codes: `0` ok, `1` blocked or refused, `2` usage/config error, `3` failed, `4` cancelled.

## Defaults

- Claude planner: `claude -p --model opus --effort max --permission-mode plan` (JSON via `--json-schema`).
- Claude auditor: the same with `--effort high`. Set `claude.plan.*` / `claude.audit.*` in `CLODEX.md` to change either.
- Codex engineer/auditor: `codex exec` with `gpt-6.1-sol` and `model_reasoning_effort="xhigh"`.
- Builds default to `.clodex/workspaces/<run-id>/`; use `--workspace local` only when in-place changes are intentional.
- Approval profiles are `ci`, `local`, and `auto_review`; `ci` is the deterministic default.
- Subscription CLI auth is preferred. API keys are fallback-only for CI/headless use.

Do not mark a run complete unless `.clodex/runs/<run-id>/05-agreement.json`
contains `approved: true`, or `clodex_handoff_decide` returned `approved`.
