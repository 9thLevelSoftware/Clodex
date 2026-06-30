---
version: 1
name: Clodex Dual CLI Workflow
max_fix_loops: 2
workspace_root: .clodex/workspaces
runs_root: .clodex/runs
state_path: .clodex/state.sqlite3
claude:
  model: opus
  effort: max
  permission_mode: plan
codex:
  model: gpt-5.5
  reasoning_effort: xhigh
  sandbox: workspace-write
audit:
  quorum: unanimous
  personas: [security, performance, portability, test-gap]
---
# Clodex Workflow Contract

Clodex coordinates Claude Code CLI and Codex CLI as separate roles.

Claude is the first-wave brainstormer, planner, and product designer. It must
produce a structured implementation plan before code changes are delegated.

Codex is the system architect, engineer, and developer. It implements only from
the accepted Claude plan and records what changed, what tests ran, and what
remains unresolved.

Both agents then perform adversarial audit against the same final diff hash.
Clodex advances only when both agents return explicit approval.

Default behavior is local-first. Subscription CLI authentication is preferred
for both Claude Code and Codex. API keys are optional fallback mechanisms for
CI or headless environments, not the default local workflow.
