---
description: Plan with Claude, implement with Codex, then audit with both
argument-hint: <task description>
allowed-tools: Bash(clodex build:*)
---

# /clodex-build

Run:

```bash
clodex build "$ARGUMENTS"
```

Use this for the full Claude plan, Codex implementation, and dual audit loop.
By default, Clodex builds in `.clodex/workspaces/<run-id>/`; apply an approved
run with `clodex apply <run-id>`.
