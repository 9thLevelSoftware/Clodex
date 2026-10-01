# Changelog

## 0.2.0

A large reliability and capability release. The headline: Clodex could not complete a real run
against current `claude` / `codex` CLIs before 0.1.1, and the default Codex model retires on
2026-10-14. 0.2.0 fixes that and builds on it.

### Upgrading from 0.1.x

1. `clodex doctor`: checks your CLIs (login, supported flags), the live Codex model catalog and
   your `CLODEX.md`, with a fix for every problem it finds. Add `--strict` to fail on warnings.
2. `clodex init --migrate --dry-run`, then without `--dry-run`: rewrites retired Codex models and
   unsupported efforts in the `CLODEX.md` front matter (comments and line endings are kept).
   Add `--split-claude` to move the old flat `claude.model` / `claude.effort` onto the new
   `claude.plan` / `claude.audit` roles (the old layout keeps working without it).
3. `clodex init --force`: refreshes the managed blocks in `CLAUDE.md` / `AGENTS.md` with the new
   delegation instructions. If you used `clodex init --global`, run it again: it now writes to
   `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`, and registers the Claude server through
   `claude mcp`. You can delete the old `~/CLAUDE.md`, `~/AGENTS.md`, `~/CLODEX.md` and
   `~/.mcp.json` that earlier versions created.
4. `clodex hooks install` if you used the hooks: the hook settings were rewritten (the old ones
   used the wrong shape and some event names that do not exist).

Behavior changes to be aware of:

- Default Codex model is `gpt-6.1-sol` at `xhigh`. Claude planning defaults to `opus` / `max` and
  audits to `opus` / `high` (separate settings now).
- `clodex build`, `audit` and `task start` refuse to run on a retired Codex model
  (`CLODEX_ALLOW_RETIRED_MODEL=1` overrides).
- `clodex apply` only applies approved runs whose patch still matches the approved diff.
- Exit codes: `0` ok, `1` blocked/refused, `2` usage or configuration error, `3` failed,
  `4` cancelled. Output without `--json` is now plain text.
- The MCP `tasks/update` method never existed in the spec and is gone; `tasks/*` now follow the
  2025-11-25 Tasks utility. `actor` and `owner` on handoffs must be `claude` or `codex`.
- State database schema v4 (additive; old databases upgrade in place).

### Added

- **Native delegation:** `clodex_delegate` runs Codex (implement / fix / audit) in the handoff's
  isolated worktree and records the result; Codex can ask questions instead of guessing
  (`clodex_clarify` / `clodex_answer` / `clodex_messages`), and answers reach its next prompt.
  `clodex_handoff_decide` follows your configured reviewers and quorum
  (`unanimous` / `majority` / N) and refuses while a delegation runs or a question is open.
- **Structured output:** plan and audit verdicts use JSON schemas via `claude --json-schema` and
  `codex exec --output-schema`, validated and retried once.
- **MCP:** protocol version negotiation, spec Tasks (`tasks/get|result|list|cancel`, task-augmented
  `clodex_build`), correct JSON-RPC errors, ordered handling with non-blocking long work.
- **CLI:** `audit --base REF | --commit SHA`, `clean <run>`, `doctor --strict`, `init --migrate`,
  `hooks install|uninstall --scope`, plain-text output, commands work from any subdirectory.
- **Config:** nested YAML (vendored PyYAML for npm installs), `claude.plan` / `claude.audit`,
  `codex.audit`, `audit.max_diff_bytes`, `workspace.apply_mode: auto`, `mcp.async_tasks`.
- `clodex eval run` is now an offline self-test of the harness (no agents, nothing written to your repo).
- GitHub Actions CI on Ubuntu and Windows across Python 3.12 - 3.14.

### Fixed

- Real runs could not reach `approved`: `codex exec` dropped `--ask-for-approval`, Claude's JSON
  result envelope was never unwrapped, `codex review` rejects a prompt, and `package.json` was
  invalid JSON.
- An audit with no required reviewers counted as approved; a failing optional reviewer aborted the
  whole run; unexpected errors left runs `running` forever with their worktree behind.
- Diffs were read and written with newline translation, corrupting CRLF and non-UTF-8 content.
- Cancelling or timing out killed only a shim while the agent kept running; workers that died were
  never noticed; a late worker could overwrite a cancellation.
- The Claude plugin manifest was invalid and could not load; hooks used the wrong settings shape,
  silently wrote into Claude's context and could exit with the "block" code; the installer nested
  directories on re-run and left half-installs; launchers acted on the Clodex checkout instead of
  your repo; user-scope MCP servers could not find the project.

## 0.1.1

Hotfix: unblock real runs (`codex exec` flags, Claude result envelope, `package.json`), default to
`gpt-6.1-sol`, and warn when `CLODEX.md` still pins the retiring `gpt-5.5`.

## 0.1.0

Initial release.
