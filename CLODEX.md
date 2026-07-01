---
version: 1
name: Clodex Dual CLI Workflow
max_fix_loops: 2
workspace_root: .clodex/workspaces
runs_root: .clodex/runs
state_path: .clodex/state.sqlite3
workspace:
  backend: git-worktree
  apply_mode: manual
claude:
  model: opus
  effort: max
  permission_mode: plan
codex:
  model: gpt-5.5
  reasoning_effort: xhigh
  sandbox: workspace-write
  approval_profile: ci
audit:
  quorum: unanimous
  personas: [security, performance, portability, test-gap]
  reviewers: [{"id": "claude-plan", "backend": "claude", "persona": "plan-adherence", "required": true, "timeout": 600}, {"id": "codex-architecture", "backend": "codex", "persona": "architecture", "required": true, "timeout": 600}, {"id": "security", "backend": "codex", "persona": "security", "required": false, "timeout": 600}, {"id": "performance", "backend": "codex", "persona": "performance", "required": false, "timeout": 600}, {"id": "portability", "backend": "codex", "persona": "portability", "required": false, "timeout": 600}, {"id": "test-gap", "backend": "claude", "persona": "test-gap", "required": false, "timeout": 600}]
mcp:
  async_tasks: true
tracing:
  enabled: true
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

Default builds run in isolated git worktrees and leave source checkouts
unchanged until a run is explicitly applied.

Default behavior is local-first. Subscription CLI authentication is preferred
for both Claude Code and Codex. API keys are optional fallback mechanisms for
CI or headless environments, not the default local workflow.
