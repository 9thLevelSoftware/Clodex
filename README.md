# Climi — Multi-Agent Workflow: Kimi K2.5 for Claude Code (v2.0)

[![Kimi CLI](https://img.shields.io/badge/Kimi%20CLI-%E2%89%A51.7.0-blue)]()
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux%20%7C%20Windows-green)]()
[![License](https://img.shields.io/badge/License-MIT-yellow)]()

> **Climi** = **C**laude + **Ki**mi. A delegation framework that turns Claude Code into the architect/brain and Kimi K2.5 into the implementer/hands.

## Overview

Integrate **Kimi K2.5** as an autonomous R&D subagent for Claude Code. **Claude is the Brain** (architect, coordinator, reviewer) and **Kimi is the Hands** (developer, implementer, debugger). Claude designs and delegates; Kimi implements and executes.

Climi wires Kimi into Claude Code three ways:

- A **role-aware wrapper script** that delegates to Kimi with one of seven specialized agent personas (or a generic template).
- An **MCP bridge** that exposes Kimi as callable tools (`kimi_analyze`, `kimi_audit`, `kimi_trace`, `kimi_verify`) over JSON-RPC for any MCP-aware client.
- **Git hooks** that auto-delegate coding tasks at `pre-commit`, `post-checkout`, and `pre-push`.

## What's New in v2.0

- **MCP Server:** Expose Kimi as callable tools for external AI systems
- **Git Hooks:** Auto-delegate coding tasks on commit/push/checkout
- **Model Selection:** Intelligent K2 vs K2.5 selection based on task type
- **Cost Estimation:** Preview token costs before delegation
- **Slash Commands:** New `/kimi-mcp` and `/kimi-hooks` commands

**Division of Labor:**

| Claude (Brain) | Kimi (Hands) |
|----------------|--------------|
| Design & plan | Implement features |
| Review & approve | Debug & fix bugs |
| Coordinate work | Refactor code |
| Make decisions | Run tests |

**Key benefits:**
- Delegate implementation work to Kimi while you focus on architecture
- 7 specialized agent roles: 4 action roles (full tool access) + 3 analysis roles (read-only)
- Template-based prompts for common workflows
- Git diff injection for post-change verification
- **v2.0:** MCP bridge for external AI integration
- **v2.0:** Git hooks for automated delegation
- **v2.0:** Smart model selection (K2 vs K2.5)

## Quick Start

### 1. Install

```bash
# Clone and install
git clone https://github.com/9thLevelSoftware/Climi.git
cd Climi

# Standard install (v1.0 features)
./install.sh

# Full v2.0 install (with MCP + hooks)
./install.sh --with-hooks
```

### 2. Configure MCP (Optional)

```bash
# Set up MCP for Claude Code integration
kimi-mcp-setup install
```

### 3. Use

```bash
# Basic usage
./skills/kimi.agent.wrapper.sh -r reviewer "Review this codebase"

# With thinking mode
./skills/kimi.agent.wrapper.sh -r reviewer --thinking "Explain the architecture"

# Slash commands in Claude Code
/kimi-analyze src/ "How is auth implemented?"
/kimi-mcp start
/kimi-hooks install
```

## Installation

### Prerequisites

| Dependency | Required | Installation |
|------------|----------|--------------|
| **Kimi CLI** | Yes (>=1.7.0) | `uv tool install kimi-cli` or `pip install kimi-cli` |
| **Bash** | Yes | macOS/Linux: built-in, Windows: Git Bash |
| **jq** | Yes (v2.0) | `brew install jq` or `apt-get install jq` |
| **Git** | Optional | For `--diff` feature |
| **Python** | Yes (>=3.12) | Required by Kimi CLI |

**Note:** jq is required for MCP server and hooks functionality in v2.0.

### Install Options

```bash
# Interactive install (prompts for target)
./install.sh

# Global install (all projects, ~/.claude/)
./install.sh --global

# Local install (current directory only)
./install.sh --local

# Full v2.0 install (with MCP + hooks)
./install.sh --with-hooks

# Dry run (preview what will be installed)
./install.sh --dry-run

# Custom target
./install.sh --target /path/to/target
```

### Windows Users

Use the PowerShell shim for native PowerShell support:

```powershell
# From PowerShell
.\kimi.ps1 -r reviewer "Review this code"

# Or use Git Bash directly
bash skills/kimi.agent.wrapper.sh -r reviewer "Review this code"
```

The PowerShell shim (`kimi.ps1`) automatically finds Git Bash, WSL, or MSYS2 to execute the bash wrapper.

### PATH Configuration

Ensure `~/.local/bin` is in your PATH for v2.0 commands:

```bash
# Add to ~/.bashrc or ~/.zshrc
export PATH="$HOME/.local/bin:$PATH"
```

### Upgrading

Run `./install.sh` again. The installer:
- Detects existing installations
- Offers to create timestamped backups
- Updates wrapper scripts, roles, templates
- Preserves your custom configurations
- Installs new v2.0 components alongside v1.0

### Uninstalling

```bash
# Preview what will be removed
./uninstall.sh --dry-run

# Remove installation
./uninstall.sh
```

## Usage

### Basic Invocation

```bash
# With a role
./skills/kimi.agent.wrapper.sh -r <role> "prompt"

# With a template
./skills/kimi.agent.wrapper.sh -t <template> "prompt"

# With both
./skills/kimi.agent.wrapper.sh -r reviewer -t feature "Plan new user authentication"

# With git diff context
./skills/kimi.agent.wrapper.sh -t verify --diff "Verify my changes are correct"

# With automatic model selection (K2 vs K2.5)
./skills/kimi.agent.wrapper.sh -r reviewer --auto-model "Review this codebase"

# Set working directory
./skills/kimi.agent.wrapper.sh -r reviewer -w /path/to/project "Review this project"
```

### Command Options

```
USAGE:
    ./skills/kimi.agent.wrapper.sh [OPTIONS] "prompt"

WRAPPER OPTIONS:
    -r, --role ROLE        Agent role (maps to .kimi/agents/ROLE.yaml)
    -m, --model MODEL      Kimi model (default: kimi-code/kimi-for-coding)
    -w, --work-dir PATH    Working directory for Kimi
    -t, --template TPL     Template to prepend (maps to .kimi/templates/TPL.md)
    --auto-model           Enable automatic K2 vs K2.5 selection per task
    --diff                 Include git diff (HEAD vs working tree) in prompt
    --dry-run              Show command without executing
    --verbose              Show wrapper debug output
    -h, --help             Show help and exit

KIMI CLI OPTIONS (pass-through):
    --thinking             Enable thinking mode for deeper reasoning
    --no-thinking          Disable thinking mode
    -y, --yes, --yolo      Auto-approve all actions
    --print                Run in non-interactive print mode
    (and any other kimi CLI flags)

ENVIRONMENT:
    KIMI_PATH              Override kimi binary location
    KIMI_FORCE_MODEL       Force model selection (k2 or k2.5)
```

## Agent Roles

All agents are defined in `.kimi/agents/` as YAML + Markdown pairs.

### Analysis Roles (Read-Only)

Analysis roles have restricted tool access - they can read files and search but cannot modify anything. Safe for code review and auditing.

| Role | Purpose | Use Case |
|------|---------|----------|
| **reviewer** | Code quality assessment | Code review, bug detection, best practices |
| **auditor** | Architecture evaluation | Tech debt, patterns, system-level issues |
| **security** | Vulnerability assessment | OWASP, secrets scanning, security audit |

### Action Roles (Full Access)

Action roles have full tool access including shell, file writes, and subagent creation. They can investigate and fix issues autonomously.

| Role | Purpose | Use Case |
|------|---------|----------|
| **debugger** | Bug investigation | Trace bugs, find root cause, apply fixes |
| **refactorer** | Code restructuring | Pattern improvements, DRY, maintainability |
| **implementer** | Feature implementation | Build new features, greenfield freedom |
| **simplifier** | Complexity reduction | Dead code removal, consolidation |

### Tool Access Matrix

| Tool | Analysis Roles | Action Roles |
|------|----------------|--------------|
| ReadFile, Glob, Grep | Yes | Yes |
| SearchWeb | Yes | Yes |
| Shell | No | Yes |
| WriteFile | No | Yes |
| StrReplaceFile | No | Yes |
| CreateSubagent | No | Yes |

## Templates

Templates provide pre-built prompt structures for common tasks. Located in `.kimi/templates/`.

| Template | Flag | Use Case |
|----------|------|----------|
| `feature` | `-t feature` | Plan new feature implementation |
| `bug` | `-t bug` | Investigate and analyze bugs |
| `verify` | `-t verify` | Post-change verification (use with `--diff`) |
| `architecture` | `-t architecture` | System architecture review |
| `implement-ready` | `-t implement-ready` | Generate implementation guidance |
| `fix-ready` | `-t fix-ready` | Generate fix instructions |

### Example Template Usage

```bash
# Plan a new feature
./skills/kimi.agent.wrapper.sh -r reviewer -t feature "Add password reset functionality"

# Verify changes after implementation
./skills/kimi.agent.wrapper.sh -t verify --diff "Verify the authentication changes"

# Bug investigation
./skills/kimi.agent.wrapper.sh -r debugger -t bug "NullPointerException in UserService.kt:245"
```

## Claude Code Slash Commands

Slash command definitions live in `dist/.claude/commands/kimi/` and are copied into your project (or `~/.claude/`) by `install.sh`. When installed, they are available in Claude Code as:

| Command | Role | Description |
|---------|------|-------------|
| `/kimi-analyze` | reviewer | Codebase analysis and exploration |
| `/kimi-audit` | auditor | Architecture and quality audit |
| `/kimi-trace` | debugger | Bug tracing with full tool access |
| `/kimi-verify` | (template) | Post-change verification with diff |
| `/kimi-mcp` | - | MCP server management (v2.0) |
| `/kimi-hooks` | - | Git hooks management (v2.0) |

### Usage in Claude Code

```
/kimi-analyze src/ How is authentication implemented?
/kimi-audit . What patterns need improvement?
/kimi-trace "Error at auth.ts:145"
/kimi-verify Check my changes are correct
/kimi-mcp start
/kimi-hooks install
```

## MCP Bridge (v2.0)

The MCP bridge (`mcp-bridge/`) exposes Kimi as four JSON-RPC tools over stdio so any MCP-aware client (Claude Desktop, custom agents, IDE plugins) can call Kimi directly:

- `kimi_analyze` — invoke the `reviewer` role
- `kimi_audit` — invoke the `auditor` role
- `kimi_trace` — invoke the `debugger` role
- `kimi_verify` — invoke the `verify` template

Setup:

```bash
# Install the MCP bridge (run during ./install.sh --with-hooks or standalone)
./install.sh --with-hooks

# Register the MCP server with Kimi CLI
kimi-mcp-setup install

# Inspect / start / stop the server
kimi-mcp start
kimi-mcp status
kimi-mcp stop
```

Configuration lives at `~/.config/kimi-mcp/config.json` (default seeded from `mcp-bridge/config/default.json`). See [docs/MCP-SETUP.md](docs/MCP-SETUP.md) for full details.

## Git Hooks (v2.0)

Climi ships three git hooks (in `hooks/hooks/`) that automatically trigger Kimi delegations at well-known git events:

| Hook | Trigger | Behavior |
|------|---------|----------|
| `pre-commit` | Before a commit is written | Auto-delegate staging-area review to the `reviewer` role |
| `post-checkout` | After `git checkout` / branch switch | Re-run verification on the new HEAD |
| `pre-push` | Before a push to remote | Auto-delegate `verify` template against outgoing commits |

Install hooks into a repo with:

```bash
kimi-hooks install          # installs into .git/hooks of the current repo
kimi-hooks list             # show installed hooks
kimi-hooks uninstall        # remove them
```

See [docs/HOOKS-GUIDE.md](docs/HOOKS-GUIDE.md) for configuration options and how to scope hooks to specific paths.

## Documentation

- [MCP Setup Guide](docs/MCP-SETUP.md) — Configure MCP server
- [Hooks Guide](docs/HOOKS-GUIDE.md) — Git hooks configuration
- [Model Selection](docs/MODEL-SELECTION.md) — K2 vs K2.5 guide
- [Claude Code Integration](skills/Claude-Code-Integration.md) — End-to-end integration walkthrough

## Migrating from v1.0

v2.0 is backward compatible. Existing installations continue working.

To upgrade:
```bash
./install.sh  # Installs new components alongside existing
```

New components are additive — your existing workflow remains unchanged.

## Configuration

### CLAUDE.md Integration

The Kimi workflow section is shipped inside the skill definition at `dist/.claude/skills/kimi-delegation/SKILL.md`. Add a `Kimi R&D Subagent` section to your project's `CLAUDE.md` so Claude Code honors the delegation rules. A typical section looks like:

```markdown
## Kimi R&D Subagent

**Division of labor:** Claude = Brain (architect, coordinator, reviewer), Kimi = Hands (developer, implementer, debugger)

**Workflow:** Design (Claude) -> Implement (Kimi) -> Review (Claude) -> Verify (Kimi)

## Delegation Rules

**Delegate to Kimi:** Feature implementation, bug fixes, refactoring, test writing, multi-file changes.

**Keep for yourself:** Architecture decisions, design reviews, approvals, user communication.

## Roles

**Action:** `implementer` `debugger` `refactorer` `simplifier`
**Analysis:** `reviewer` `auditor` `security`
```

### Context File

Create `.kimi/context.md` or `KimiContext.md` in your project root. This content is automatically injected into every Kimi query.

```markdown
# Project Context

- Framework: Next.js 15 with App Router
- Language: TypeScript strict mode
- Database: PostgreSQL with Prisma ORM
- Testing: Jest + Testing Library

## Key Conventions

- Use server actions for mutations
- Prefer React Server Components
- All API routes require authentication
```

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `KIMI_PATH` | Override kimi binary location (useful on Windows) |
| `KIMI_FORCE_MODEL` | Force model selection when `--auto-model` is set (`k2` or `k2.5`) |

## Architecture

```
+---------------------------------------------------------+
|                    Claude Code                           |
|  +-- /kimi-* slash commands (dist/.claude/commands/)    |
|  +-- SKILL.md (dist/.claude/skills/kimi-delegation/)     |
|  +-- CLAUDE.md (user configures delegation rules)        |
+----------------------------+----------------------------+
                             | invokes
                             v
+---------------------------------------------------------+
|               skills/kimi.agent.wrapper.sh                |
|  +-- CLI validation (kimi >= 1.7.0)                      |
|  +-- Role resolution (.kimi/agents/*.yaml)               |
|  +-- Template loading (.kimi/templates/*.md)             |
|  +-- Context injection (context.md, --diff)              |
|  +-- Prompt assembly + auto-model (K2 vs K2.5)           |
+----------------------------+----------------------------+
                             | delegates to
                             v
+---------------------------------------------------------+
|              Kimi CLI (kimi-for-coding)                  |
|  +-- K2.5 large context analysis                         |
|  +-- Agent-based tool access                             |
|  +-- Structured output                                   |
+---------------------------------------------------------+

        v2.0 add-ons (independent of the wrapper above)

+----------------------------+       +--------------------------+
| mcp-bridge/bin/            |       | hooks/hooks/             |
|   kimi-mcp-server          |       |   pre-commit             |
|  (JSON-RPC over stdio:     |       |   post-checkout          |
|   kimi_analyze, audit,     |       |   pre-push               |
|   trace, verify)           |       | (auto-delegate at git    |
+----------------------------+       |  events)                 |
                                     +--------------------------+
```

### Repository Layout

```
Climi/
+-- install.sh                      # Automated installer
+-- uninstall.sh                    # Clean uninstaller
+-- kimi.ps1                        # PowerShell shim (Windows)
|
+-- docs/                           # v2.0 Documentation
|   +-- MCP-SETUP.md                # MCP server setup guide
|   +-- HOOKS-GUIDE.md              # Git hooks configuration
|   +-- MODEL-SELECTION.md          # K2 vs K2.5 guide
|
+-- skills/
|   +-- kimi.agent.wrapper.sh       # Core wrapper script
|   +-- kimi-model-selector.sh      # Model selector (v2.0)
|   +-- kimi-cost-estimator.sh      # Cost estimator (v2.0)
|   +-- Claude-Code-Integration.md  # Integration walkthrough
|
+-- bin/                            # v2.0 CLI wrappers (installed to ~/.local/bin)
|   +-- kimi-mcp                    # MCP CLI wrapper
|   +-- kimi-mcp-setup              # MCP Kimi-CLI registration helper
|   +-- kimi-hooks                  # Hooks manager
|   +-- kimi-hooks-setup            # Hooks installer
|
+-- mcp-bridge/                     # v2.0 MCP server
|   +-- bin/kimi-mcp-server         # JSON-RPC server entry point
|   +-- lib/                        # Server internals (mcp-core, tools, etc.)
|   +-- config/                     # Default config + model rules
|   +-- tests/                      # Bridge tests
|
+-- hooks/                          # v2.0 git hooks (installed into .git/hooks)
|   +-- hooks/pre-commit
|   +-- hooks/post-checkout
|   +-- hooks/pre-push
|   +-- lib/                        # Shared hook helpers
|   +-- config/                     # Hook configuration
|
+-- .kimi/
|   +-- agents/                     # 7 agent roles
|   |   +-- reviewer.yaml + .md
|   |   +-- auditor.yaml + .md
|   |   +-- security.yaml + .md
|   |   +-- debugger.yaml + .md
|   |   +-- refactorer.yaml + .md
|   |   +-- implementer.yaml + .md
|   |   +-- simplifier.yaml + .md
|   +-- templates/                  # 6 query templates
|       +-- feature.md
|       +-- bug.md
|       +-- verify.md
|       +-- architecture.md
|       +-- implement-ready.md
|       +-- fix-ready.md
|
+-- dist/                           # Claude Code artifacts (copied by install.sh)
    +-- .claude/
        +-- commands/kimi/          # Slash commands
        |   +-- kimi-analyze.md
        |   +-- kimi-audit.md
        |   +-- kimi-trace.md
        |   +-- kimi-verify.md
        |   +-- kimi-mcp.md         # v2.0
        |   +-- kimi-hooks.md       # v2.0
        +-- skills/kimi-delegation/
            +-- SKILL.md            # Claude skill definition
```

## Response Format

All Kimi agents return structured output for consistent parsing:

```markdown
## SUMMARY
[1-2 sentence overview of findings]

## FILES
- path/to/file.ext:LINE - description
- path/to/another.ext:LINE - description

## ANALYSIS
[Detailed findings, code analysis, explanations]

## RECOMMENDATIONS
1. First actionable item
2. Second actionable item
3. Third actionable item
```

## Troubleshooting

### "kimi not found"

```bash
# Install kimi CLI
uv tool install kimi-cli
# or
pip install kimi-cli

# Verify installation
kimi --version

# If PATH issues persist (common on Windows), set KIMI_PATH:
export KIMI_PATH=/path/to/kimi
```

### "jq: command not found" (v2.0)

```bash
# macOS
brew install jq

# Ubuntu/Debian
sudo apt-get install jq

# Windows (Git Bash)
choco install jq
```

### MCP/hooks commands not found

```bash
# Check if ~/.local/bin is in PATH
echo $PATH | grep ".local/bin"

# Add to PATH
export PATH="$HOME/.local/bin:$PATH"

# Re-run installer
./install.sh
```

### "role not found"

```bash
# Check .kimi/agents/ directory exists and contains YAML files
ls -la .kimi/agents/

# Available roles are shown in error message
# Verify the YAML file exists: .kimi/agents/<role>.yaml
```

### "template not found"

```bash
# Check .kimi/templates/ directory
ls -la .kimi/templates/

# Available templates are shown in error message
# Verify the file exists: .kimi/templates/<template>.md
```

### Windows Issues

1. **Git Bash not found:** Install Git for Windows, ensure "Git Bash" option is selected
2. **PowerShell shim fails:** Run `.\kimi.ps1 --verbose` to see which bash it's trying to use
3. **Path issues:** Set `KIMI_PATH` environment variable to kimi.exe location

### Version Warnings

```bash
# Warning: kimi CLI x.y.z is below minimum 1.7.0
# Upgrade kimi CLI:
uv tool upgrade kimi-cli
# or
pip install --upgrade kimi-cli
```

## Requirements

| Component | Minimum Version | Notes |
|-----------|-----------------|-------|
| **Kimi CLI** | >= 1.7.0 | `--quiet`, `--agent-file` support |
| **Bash** | Any modern | Git Bash on Windows |
| **jq** | >= 1.6 | Required for MCP/hooks (v2.0) |
| **Python** | >= 3.12 | Required by Kimi CLI |
| **Git** | Any | Optional, for `--diff` feature |
| **OS** | macOS, Linux, Windows | Windows via WSL/Git Bash |

## License

MIT License.

## Contributing

1. Fork the repository
2. Make your changes
3. Test with `./skills/kimi.agent.wrapper.sh --dry-run -r reviewer "test"`
4. Ensure all roles and templates load correctly
5. Submit a pull request

### Adding Custom Roles

Create a new role in `.kimi/agents/`:

```yaml
# .kimi/agents/my-role.yaml
version: 1
agent:
  extend: default
  name: my-role
  system_prompt_path: ./my-role.md
  # exclude_tools: [...] # Add for read-only roles
```

```markdown
# .kimi/agents/my-role.md
# My Custom Role

You are a [description]. Your task is to [objective].

## Process
1. First step
2. Second step

## Output Format
Use SUMMARY/FILES/ANALYSIS/RECOMMENDATIONS structure.

## Constraints
- Constraint 1
- Constraint 2
```

### Adding Custom Templates

Create a new template in `.kimi/templates/`:

```markdown
# .kimi/templates/my-template.md
# My Template

You are helping with [task type].

## Context
- Working directory: ${KIMI_WORK_DIR}
- Current time: ${KIMI_NOW}

## Task
[Instructions for the task]

## Output Format
[Expected output structure]
```
