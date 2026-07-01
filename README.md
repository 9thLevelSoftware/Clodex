# Clodex

Clodex is a native collaboration layer for **Claude Code CLI** and **Codex CLI**.
It installs repo instructions and MCP tools that let either agent hand work to
the other without the user manually driving every step.

- **Claude Code** plans first with Opus at max effort.
- **Codex** implements from that accepted plan with GPT-5.5 at xhigh reasoning.
- **Both agents audit the same final diff hash** and must agree before the task
  is considered complete.

Clodex borrows practical patterns from Symphony-style workflow contracts,
Chorus-style adversarial review, Openroom-style local rooms/artifacts, and
Claude Teams-style task ledgers. It does not require a cloud service, public
relay, tmux, or a third-party model delegate.

## Native Claude/Codex Collaboration

```bash
npm install -g clodex
clodex init
```

Use Claude Code or Codex as usual. Clodex adds repo instructions and MCP tools
that teach each agent how to coordinate with the other: Claude plans, Codex
implements, both audit, and Clodex enforces durable handoff state and bounded
agreement.

`clodex init` writes managed Clodex blocks to `CLAUDE.md`, `AGENTS.md`, and
`CLODEX.md`. By default it also configures the repo MCP server through
`.mcp.json` and `.codex/config.toml`; use `--no-mcp-config` when you want
instructions only.

Native coordination uses these MCP tools:

- `clodex_handoff_create`
- `clodex_handoff_update`
- `clodex_handoff_get`
- `clodex_handoff_decide`

If MCP is unavailable, use the CLI fallbacks:

```bash
clodex task start "<task>"
clodex task get <run-id>
clodex audit --diff
clodex status
```

## Requirements

| Dependency | Purpose |
| --- | --- |
| Python 3.12+ | Clodex orchestrator, SQLite state, MCP server |
| Git | diff hashing and repository state |
| Claude Code CLI | planning and Claude audit |
| Codex CLI | implementation and Codex audit |

Subscription CLI auth is the default:

```bash
claude auth login
codex login
```

API keys or long-lived tokens are fallback-only for CI/headless automation.

## Harness Commands

The native workflow is the default developer experience. The command harness
remains available for scripted, CI, dry-run, and non-MCP workflows.

Install from source:

```bash
./install.sh --dry-run
./install.sh --force
```

Install with npm:

```bash
npm install -g clodex
clodex doctor
```

Run without installing:

```bash
npx clodex --json build --dry-run "Add a small feature"
```

From this checkout without installing:

```bash
python -m clodex doctor
python -m clodex init --dry-run
python -m clodex build --dry-run "Add a small feature"
```

PowerShell:

```powershell
.\clodex.ps1 doctor
.\clodex.ps1 init --dry-run
.\clodex.ps1 build --dry-run "Add a small feature"
```

| Command | Purpose |
| --- | --- |
| `clodex init` | Install native Claude/Codex instructions and MCP config |
| `clodex native status` | Show native instruction and MCP config state |
| `clodex native doctor` | Run native setup checks, CLI readiness, and launcher checks |
| `clodex doctor` | Check Python, git, Claude Code, Codex, and `CLODEX.md` |
| `clodex plan "<task>"` | Run Claude planning only |
| `clodex build "<task>"` | Run plan, implementation, and dual audit loop in an isolated worktree |
| `clodex audit --diff` | Audit current uncommitted changes |
| `clodex run "<task>"` | Alias for `build` |
| `clodex apply <run-id>` | Apply an approved worktree patch back to the source checkout |
| `clodex task start/get/cancel/list` | Manage durable async runs |
| `clodex trace export <run-id>` | Print a run trace as JSONL |
| `clodex hooks print/install/ingest` | Generate or ingest Claude Code hook events |
| `clodex eval run` | Run local harness smoke evals |
| `clodex queue add/list/update` | Manage the local task ledger |
| `clodex status` | Show recent tasks and runs |
| `clodex mcp-server` | Run the stdio MCP server |

## Workflow Contract

`CLODEX.md` is the repo-owned workflow policy. It has YAML front matter plus a
prompt body. Defaults:

```yaml
claude:
  model: opus
  effort: max
  permission_mode: plan
codex:
  model: gpt-5.5
  reasoning_effort: xhigh
  sandbox: workspace-write
  approval_profile: ci
workspace:
  backend: git-worktree
  apply_mode: manual
max_fix_loops: 2
```

Run artifacts are written to `.clodex/runs/<run-id>/`:

- `01-claude-plan.json`
- `02-codex-implementation.md`
- `03-claude-audit.json`
- `04-codex-audit.json`
- `05-agreement.json`
- `changes.diff`
- `apply.patch`
- `trace.jsonl`
- `workspace.json`
- `reviewers/*.json`

Local task/run state is stored in `.clodex/state.sqlite3`.

By default, `clodex build` executes inside `.clodex/workspaces/<run-id>/`.
The source checkout is not modified until `clodex apply <run-id>` succeeds.
Use `--workspace local` for compatibility with the earlier in-place behavior.

## MCP Tools

The MCP server exposes:

- `clodex_plan`
- `clodex_build`
- `clodex_audit`
- `clodex_status`
- `clodex_task_create`
- `clodex_task_update`
- `clodex_task_start`
- `clodex_task_get`
- `clodex_task_cancel`
- `clodex_handoff_create`
- `clodex_handoff_update`
- `clodex_handoff_get`
- `clodex_handoff_decide`

The server also handles MCP-style `tasks/get`, `tasks/update`, and
`tasks/cancel` JSON-RPC methods using the Clodex `run_id` as the task id.

Start it with:

```bash
python -m clodex mcp-server
```

## Safety

Clodex defaults to git worktree isolation, Codex `workspace-write` sandboxing,
and Claude plan mode. Dangerous full-access workflows are intentionally not the
default. A run is complete only when `05-agreement.json` has `approved: true`
for the final diff hash.
