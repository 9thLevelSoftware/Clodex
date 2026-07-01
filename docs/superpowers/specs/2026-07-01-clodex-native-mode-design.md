# Clodex Native Mode Design

## Summary

Clodex should become a native collaboration layer for Claude Code and Codex, not a CLI workflow that users manually babysit. The main user path is:

```bash
npm install -g clodex
clodex init
```

After initialization, users open Claude Code or Codex normally and ask for work normally. The agents learn from generated project instructions when to collaborate, how to hand off work, when to audit, and how to fall back if MCP is unavailable.

The existing Python harness remains the execution engine: SQLite state, worktree isolation, audit quorum, traces, MCP tools, and CLI fallbacks. Native mode adds initialization, managed agent instructions, repo-scoped MCP configuration, and semantic handoff tools.

## Goals

- Make Claude Code and Codex collaborate naturally inside their normal CLIs.
- Preserve repo-specific project instructions and avoid overwriting user content.
- Use MCP as the primary coordination channel.
- Keep CLI commands as precise fallbacks when MCP is unavailable.
- Prevent Claude/Codex handoff loops with owner and budget enforcement.
- Reposition README/package messaging around native agent collaboration.

## Non-Goals

- Do not wrap or replace Claude Code or Codex sessions.
- Do not introduce file-based inboxes for the first native-mode version.
- Do not make global behavior the default.
- Do not require API keys; subscription CLI auth remains the local default.
- Do not rewrite the harness around MCP. MCP is an interface, not the core engine.

## Native Setup

Add:

```bash
clodex init [--global] [--dry-run] [--no-mcp-config] [--force]
clodex native status
clodex native doctor
```

Default `clodex init` is repo-local. It creates or updates:

- `CLAUDE.md`
- `AGENTS.md`
- `CLODEX.md`
- `.mcp.json`
- `.codex/config.toml`

Global mode is explicit:

```bash
clodex init --global
```

The initializer must be idempotent. It may create missing files, but for existing files it only inserts or replaces managed blocks:

```md
<!-- BEGIN CLODEX -->
...
<!-- END CLODEX -->
```

Unmanaged user content must be preserved byte-for-byte outside the managed block.

`--dry-run` prints planned file actions and previews managed-block content without writing files. `--no-mcp-config` writes instruction files but skips `.mcp.json` and `.codex/config.toml`. `--force` allows replacing malformed existing Clodex-managed blocks.

## Agent Behavior Contract

Native mode uses advisory delegation. Agents should delegate or cross-check non-trivial work, but direct small edits remain acceptable.

Claude Code is the default strategist:

- Recognize non-trivial work as a Clodex collaboration candidate.
- Create or update a Clodex handoff through MCP.
- Produce the plan/design/spec before implementation.
- Delegate implementation to Codex through Clodex.
- Review Codex results against the accepted plan.
- Require Clodex agreement before claiming completion.
- Use CLI fallbacks only if MCP tools are unavailable.

Codex is the default engineer:

- Treat incoming Clodex tasks as implementation work unless assigned audit or planning.
- Ask Claude for clarification through Clodex when product intent is unclear.
- Implement from the accepted plan.
- Record changed files, tests, unresolved issues, and diff hash.
- Participate in adversarial audit.
- Avoid claiming completion until Clodex agreement passes.

Both agents must respect run ownership and handoff budgets.

Default run ownership:

- owner: `claude`
- handoff budget: `6`
- phase sequence: `planning`, `implementation`, `audit`, `fix`, `decision`

When the handoff budget is exhausted, Clodex marks the run `blocked` and instructs the active agent to summarize the disagreement for the user.

## Coordination Interfaces

Keep existing lower-level tools:

- `clodex_task_start`
- `clodex_task_get`
- `clodex_task_cancel`
- `clodex_plan`
- `clodex_build`
- `clodex_audit`
- `clodex_status`

Add native semantic MCP tools:

- `clodex_handoff_create`
- `clodex_handoff_update`
- `clodex_handoff_get`
- `clodex_handoff_decide`

The handoff tools are wrappers over the existing run/task/audit state. They should not create a parallel storage system.

`clodex_handoff_create` creates a durable run with owner, phase, task summary, and handoff budget.

`clodex_handoff_update` records phase changes, agent reports, plan JSON, implementation summaries, audit verdicts, changed files, tests, unresolved issues, and handoff count increments.

`clodex_handoff_get` returns the current run state, active phase, owner, handoff count, budget remaining, latest artifacts, and next expected actor.

`clodex_handoff_decide` evaluates whether the run is `approved`, `needs_fix`, or `blocked` using existing diff hash and audit quorum logic.

If MCP is unavailable, generated instructions must provide CLI fallback commands:

```bash
clodex task start "<task>"
clodex task get <run-id>
clodex task cancel <run-id>
clodex audit --diff
clodex status
```

## Repo-Scoped MCP Configuration

`clodex init` writes repo-scoped MCP config by default.

Claude Code config:

- `.mcp.json`
- points to `clodex mcp-server`
- uses stdio transport

Codex config:

- `.codex/config.toml`
- points to `clodex mcp-server`
- uses stdio transport

The implementation should preserve existing MCP server entries and only add or update the Clodex entry. If a config file exists and cannot be parsed safely, `clodex init` should fail with a specific error unless `--force` is provided.

## Documentation Positioning

The README should lead with native collaboration:

```bash
npm install -g clodex
clodex init
```

Primary message:

> Use Claude Code or Codex as usual. Clodex teaches each agent how to coordinate with the other.

The command catalog should move lower in the README as infrastructure/reference material. `clodex build` and `clodex apply` remain useful, but they should not be the headline workflow.

## Data Model Additions

Extend run/task state with:

- `owner`
- `phase`
- `handoff_count`
- `handoff_budget`
- `last_actor`
- `blocked_reason`

Trace events should include:

- `handoff.create`
- `handoff.update`
- `handoff.decide`
- `handoff.blocked`
- `native.init`
- `native.status`
- `native.doctor`

Existing migrations must preserve old state databases.

## Testing

Add tests for:

- `clodex init --dry-run` previews `CLAUDE.md`, `AGENTS.md`, `.mcp.json`, and `.codex/config.toml`.
- `clodex init` creates missing native files.
- Existing `CLAUDE.md` and `AGENTS.md` keep unmanaged content unchanged.
- Re-running `clodex init` updates only the managed Clodex block.
- Malformed managed blocks fail unless `--force` is passed.
- `--no-mcp-config` skips MCP config files.
- `.mcp.json` preserves non-Clodex servers.
- `.codex/config.toml` preserves non-Clodex config.
- `clodex native status` reports installed, missing, and stale native-mode components.
- `clodex native doctor` validates Claude CLI, Codex CLI, MCP config, npm launcher, Python version, and managed block freshness.
- MCP handoff tools create, update, get, and decide runs.
- Handoff budget exhaustion blocks the run.
- CLI fallback commands still work when MCP is not used.

## Implementation Order

1. Add managed-block file update helpers.
2. Add native instruction templates for Claude and Codex.
3. Add repo-scoped MCP config generation.
4. Add `clodex init`, `clodex native status`, and `clodex native doctor`.
5. Add run ownership and handoff budget fields/migrations.
6. Add MCP handoff tools.
7. Update README and package positioning.
8. Add tests and verification commands.

## Acceptance Criteria

- A user can run `clodex init` in a repo and then use Claude Code or Codex normally.
- Generated instructions tell each agent when and how to use Clodex without requiring the user to invoke `clodex build`.
- Existing project instructions are preserved outside managed blocks.
- Repo-scoped MCP config is generated by default and can be skipped.
- Handoff loops are bounded by enforced run state, not prompt guidance alone.
- The README clearly presents Clodex as native Claude/Codex collaboration.
