#!/usr/bin/env bash
set -euo pipefail

SCRIPT_VERSION="0.1.0"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="$HOME/.clodex"
BIN_DIR="$HOME/.local/bin"
DRY_RUN=false
FORCE=false

usage() {
    cat <<EOF
Clodex Installer v${SCRIPT_VERSION}

Usage: install.sh [OPTIONS]

Options:
  -t, --target PATH   Install plugin metadata to PATH (default: ~/.clodex)
  -b, --bin PATH      Install clodex launcher to PATH (default: ~/.local/bin)
  -f, --force         Overwrite existing launcher
      --dry-run       Show actions without writing files
  -h, --help          Show this help

Clodex uses subscription CLI auth by default:
  claude auth login
  codex login
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -t|--target)
            TARGET_DIR="${2:?--target requires a path}"
            shift 2
            ;;
        -b|--bin)
            BIN_DIR="${2:?--bin requires a path}"
            shift 2
            ;;
        -f|--force)
            FORCE=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

log() {
    printf '%s\n' "$*"
}

need_command() {
    local cmd="$1"
    local resolved=""
    for candidate in "$cmd" "$cmd.exe" "$cmd.cmd" "$cmd.bat"; do
        if command -v "$candidate" >/dev/null 2>&1; then
            resolved="$(command -v "$candidate")"
            break
        fi
    done
    if [[ -n "$resolved" ]]; then
        log "ok: $cmd ($resolved)"
        return 0
    fi
    log "missing: $cmd"
    return 1
}

python_cmd() {
    if command -v python >/dev/null 2>&1; then
        python - <<'PY' >/dev/null 2>&1 && { echo python; return 0; }
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
    fi
    if command -v python3 >/dev/null 2>&1; then
        python3 - <<'PY' >/dev/null 2>&1 && { echo python3; return 0; }
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
    fi
    return 1
}

PYTHON_BIN="$(python_cmd || true)"
if [[ -z "$PYTHON_BIN" ]]; then
    log "missing: Python >= 3.12"
    exit 1
fi
log "ok: $($PYTHON_BIN --version)"

missing=0
need_command git || missing=1
need_command claude || missing=1
need_command codex || missing=1
if [[ "$missing" -ne 0 ]]; then
    log ""
    log "Install or authenticate missing CLIs, then rerun:"
    log "  claude auth login"
    log "  codex login"
    exit 1
fi

log ""
log "Source: $SCRIPT_DIR"
log "Target: $TARGET_DIR"
log "Bin:    $BIN_DIR"

if [[ "$DRY_RUN" == true ]]; then
    log "dry-run: would install clodex launcher and plugin metadata"
    exit 0
fi

mkdir -p "$TARGET_DIR" "$BIN_DIR"
cp -R "$SCRIPT_DIR/.claude-plugin" "$TARGET_DIR/claude-plugin"
cp -R "$SCRIPT_DIR/.codex-plugin" "$TARGET_DIR/codex-plugin"
cp -R "$SCRIPT_DIR/dist" "$TARGET_DIR/dist"
cp -R "$SCRIPT_DIR/skills/clodex-workflow" "$TARGET_DIR/clodex-workflow-skill"

LAUNCHER="$BIN_DIR/clodex"
if [[ -e "$LAUNCHER" && "$FORCE" != true ]]; then
    log "launcher exists: $LAUNCHER"
    log "rerun with --force to overwrite"
    exit 1
fi

cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="$SCRIPT_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$PYTHON_BIN" -m clodex "\$@"
EOF
chmod +x "$LAUNCHER"

log "installed: $LAUNCHER"
log "run: clodex doctor"
