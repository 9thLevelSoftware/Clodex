---
description: Audit the current uncommitted changes with Claude and Codex
argument-hint: [--base REF | --commit SHA]
allowed-tools: Bash(clodex audit:*)
---

# /clodex-audit

Run:

```bash
clodex audit $ARGUMENTS
```

With no arguments this audits the current uncommitted changes with Claude and Codex.
Pass `--base <ref>` to audit everything since a branch diverged, or `--commit <sha>` for one commit.
