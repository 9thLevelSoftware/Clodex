# Clodex

Clodex is a native collaboration layer for **Claude Code CLI** and **Codex CLI**.
It installs repo instructions and MCP tools that let either agent hand work to
the other without the user manually driving every step.

- **Claude Code** plans first with Opus at max effort and audits at high effort.
- **Codex** implements from that accepted plan with GPT-6.1 Sol at xhigh reasoning.
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
instructions only. From a subdirectory, `clodex init` still works on the repository root.

Those two files are meant to be committed, so they use the portable `clodex mcp-server`
command (wrapped as `cmd /c clodex mcp-server` when `init` runs on Windows and only a `.cmd`
shim exists, because Windows MCP clients cannot start those directly). Re-run `clodex init`
if a teammate on another platform needs the other form.

### User-level install (`clodex init --global`)

Writes where each agent actually reads user-level files, not your home directory root:

| What | Where |
| --- | --- |
| Claude instructions | `~/.claude/CLAUDE.md` (or `$CLAUDE_CONFIG_DIR/CLAUDE.md`) |
| Codex instructions | `~/.codex/AGENTS.md` (or `$CODEX_HOME/AGENTS.md`) |
| Codex MCP server | `~/.codex/config.toml` |
| Claude MCP server | registered with `claude mcp add-json clodex ... --scope user` (Claude Code owns `~/.claude.json`) |

These use an absolute, environment-independent command (the Node launcher for npm installs,
otherwise your Python), so they work regardless of PATH or working directory. Claude Code starts
user-scope servers with `~/.claude` as the working directory, so Clodex finds your project from
`$CLAUDE_PROJECT_DIR`, which Claude Code sets. If the `claude` CLI is not on PATH, `init --global`
prints the command to run yourself.

### Claude Code hooks

```bash
clodex hooks install            # .claude/settings.local.json (default, not committed)
clodex hooks install --scope user
clodex hooks uninstall
```

Records `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `SubagentStart/Stop`, `TaskCreated`,
`TaskCompleted`, `WorktreeCreate/Remove` and `Stop` events to
`.clodex/runs/<session-id>/events/claude-hooks.jsonl` and the trace. Your other hooks and settings
are left alone and re-running is safe. The hook command is silent and never exits with code 2
(Claude Code treats that as "block"). Tool events are deliberately not hooked: each hook is a
process, which would slow every tool call.

### Plugin

The repository is also a Claude Code plugin (`claude --plugin-dir <this repo>`): slash commands
`/clodex:clodex-plan`, `/clodex:clodex-build` and `/clodex:clodex-audit`, the `clodex-workflow`
skill, and the MCP server (needs `node`; the launcher finds Python). `claude plugin validate .
--strict` passes. `install.sh` installs a self-contained copy under `~/.clodex/plugin` and a
launcher in `~/.local/bin`; re-running replaces rather than nests, and `uninstall.sh` removes it.

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
| `clodex doctor [--strict]` | Check Python, git, both CLIs (login, supported flags), the live Codex model catalog, and `CLODEX.md` settings; `--strict` fails on warnings |
| `clodex init --migrate [--split-claude] [--dry-run]` | Update `CLODEX.md` settings that no longer work (retired models, unsupported efforts) |
| `clodex plan "<task>"` | Run Claude planning only |
| `clodex build "<task>"` | Run plan, implementation, and dual audit loop in an isolated worktree |
| `clodex audit [--diff \| --base REF \| --commit SHA]` | Audit the uncommitted diff (default), everything since `REF` diverged from `HEAD`, or one commit |
| `clodex run "<task>"` | Alias for `build` |
| `clodex apply <run-id>` | Apply an approved worktree patch back to the source checkout (`--check` to dry-run, `--force` for unapproved runs) |
| `clodex clean <run-id>` | Remove the kept git worktree of a finished run (artifacts and patch stay) |
| `clodex task start/get/cancel/list` | Manage durable async runs |
| `clodex trace export <run-id>` | Print a run trace as JSONL |
| `clodex hooks print/install/uninstall/ingest` | Manage the Claude Code hook integration (`install --scope local\|project\|user`) |
| `clodex eval run` | Run local harness smoke evals |
| `clodex queue add/list/update` | Manage the local task ledger |
| `clodex status` | Show recent tasks and runs |
| `clodex mcp-server` | Run the stdio MCP server |

## Workflow Contract

`CLODEX.md` is the repo-owned workflow policy. It has YAML front matter plus a
prompt body. Defaults:

```yaml
claude:
  permission_mode: plan
  plan:
    model: opus
    effort: max
  audit:
    model: opus
    effort: high
codex:
  model: gpt-6.1-sol
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

`clodex apply` only applies **approved** runs, and only if `apply.patch` still matches the
approved diff hash; a local-workspace run is reported as already in the working tree. A run
that fails unexpectedly is marked `failed` (traceback in `error.txt`) and its worktree is
removed; approved and blocked runs keep theirs until `clodex clean <run-id>`.

### Exit codes and output

`0` success; `1` the work was refused or rejected (`blocked`, `apply-refused`, ...); `2` usage or
configuration error (including unknown run ids and bad git refs, reported as one line; set
`CLODEX_DEBUG=1` for the traceback); `3` the run failed unexpectedly; `4` it was cancelled.
Without `--json`, output is plain text; `--json` is the stable machine-readable form. Commands
find the repository from any subdirectory (or `CLODEX_REPO_ROOT`).

### Models and `clodex doctor`

`clodex doctor` validates the configuration against what is actually installed:

- **Models:** retired Codex models are errors, ones retiring soon are warnings (both name the
  replacement). Efforts are checked against the live catalog (`codex debug models`, cached for
  24h in `.clodex/models-cache.json`), so a stale `reasoning_effort` is caught before a run.
- **CLI flags:** the installed `claude` and `codex` are probed once per version
  (`.clodex/capabilities.json`). Missing optional flags (`--json-schema`, `--output-schema`, ...)
  are warnings and Clodex simply runs without them; missing required flags are errors.
- **Login:** `claude auth status` and `codex login status`.
- **Settings:** reviewers, quorum, sandbox, approval profile and workspace backend.

`clodex build`, `audit` and `task start` refuse to start on a retired Codex model
(`clodex init --migrate` fixes the file; `CLODEX_ALLOW_RETIRED_MODEL=1` overrides). Migration edits
only the YAML front matter, keeps comments and line endings, and is safe to re-run.

### Audit quorum

`audit.quorum` decides when the reviewers agree: `unanimous` (default), `majority`, or a number
N. Only reviewers with `required: true` count; optional reviewers are recorded but never block.
A reviewer that fails or times out counts as not approved. If a *required* reviewer cannot run,
the run is `blocked` with that error rather than sent back to Codex for a fix. Very large diffs
are truncated in the audit prompt (`audit.max_diff_bytes`, default 200000); the diff hash still
covers the whole diff.

### Async tasks

`clodex task start` runs the build in a detached worker. `task cancel` stops the worker and any
agent it started, and removes the run's worktree. Workers write a heartbeat; `task get/list`
mark a run `failed` if its worker died without finishing.

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

Start it with:

```bash
python -m clodex mcp-server
```

The server speaks MCP `2025-11-25` (and negotiates down to `2025-06-18`, `2025-03-26` or
`2024-11-05`). It works on the repository containing its working directory, or the one named by
`CLODEX_REPO_ROOT`, which is the reliable way to point a client at a project.

- **Tool errors vs protocol errors:** wrong or missing tool arguments come back as a tool result
  with `isError: true` (so the model can correct itself); an unknown tool or malformed JSON-RPC is
  a protocol error.
- **Responsiveness:** requests are handled in arrival order, so a client may pipeline
  `handoff_create` -> `handoff_update` -> `handoff_get`. Only long-running work (a synchronous
  plan/build/audit, or waiting in `tasks/result`) runs on a worker thread, so pings, polling and
  cancellation are never blocked.
- **Tasks (experimental in the spec):** `clodex_build` supports task augmentation. Call
  `tools/call` with a `task` object and it returns a task immediately, backed by a durable worker;
  then use `tasks/get`, `tasks/result` (blocks until done, returns the tool result), `tasks/list`
  and `tasks/cancel`. The Clodex `run_id` is the task id. Run statuses map to task statuses:
  `queued`/`running`/`planning`/`auditing`/`needs-fix` = `working`, `approved`/`applied` =
  `completed`, `blocked`/`failed` = `failed`, `cancelled` = `cancelled`. Task ids are not
  access-controlled: this is a local, single-user server, so only expose it to clients you trust.
  The pre-spec `tasks/update` method is gone.
- `clodex_audit` accepts `base` (everything since that ref diverged) and `commit` (one commit) as
  well as the default uncommitted diff.

### Native handoffs

- **Decisions follow your audit settings.** `clodex_handoff_decide` evaluates the reports recorded
  with `clodex_handoff_update` against the same `audit.reviewers` and `audit.quorum` as a classic
  run (`unanimous`, `majority` or a number; only `required` reviewers count). A report is a verdict
  when it has `approved` and a diff hash (on the update or in the report); it only counts for that
  hash, a new hash resets everything, and a hashless `approved: false` withdraws the reviewer's
  approval. `report.reviewer_id` names which configured reviewer the verdict is for; without it
  the actor (`claude` / `codex`) stands for the first *required* reviewer of that backend, so the
  default two-reviewer flow needs no extra fields. An unknown `reviewer_id` is rejected. The
  decision lists `required_pending` and `approved_reviewers` so the orchestrator knows whom to ask.
- **Workspaces.** `clodex_handoff_create` takes `workspace: "git-worktree" | "local" | "none"`
  (default `none`). A worktree is an isolated checkout under `.clodex/workspaces/<run-id>` (the
  repo must have no uncommitted changes to tracked files); if the handoff cannot be recorded the
  worktree is rolled back. `clodex clean <run-id>` removes it once the handoff has finished.
- **Ledger.** A handoff gets a row in the task ledger (shown by `clodex status`, kept in step
  with the run: `done` / `blocked` / `failed`), every verdict report becomes an `audits` row, and
  `report.artifacts` (paths, or `{name, path, kind}`) become artifact rows.

## Safety

Clodex defaults to git worktree isolation, Codex `workspace-write` sandboxing,
and Claude plan mode. Dangerous full-access workflows are intentionally not the
default. A run is complete only when `05-agreement.json` has `approved: true`
for the final diff hash.
