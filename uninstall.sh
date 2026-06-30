#!/usr/bin/env bash
set -euo pipefail

SCRIPT_VERSION="0.1.0"
TARGET_DIR="$HOME/.clodex"
BIN_DIR="$HOME/.local/bin"
DRY_RUN=false
ASSUME_YES=false

usage() {
    cat <<EOF
Clodex Uninstaller v${SCRIPT_VERSION}

Usage: uninstall.sh [OPTIONS]

Options:
  -t, --target PATH   Remove plugin metadata from PATH (default: ~/.clodex)
  -b, --bin PATH      Remove clodex launcher from PATH (default: ~/.local/bin)
  -y, --yes           Do not prompt before removing files
      --dry-run       Show actions without removing files
  -h, --help          Show this help
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
        -y|--yes)
            ASSUME_YES=true
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

items=()

add_if_exists() {
    local path="$1"
    if [[ -e "$path" ]]; then
        items+=("$path")
    fi
}

add_if_exists "$BIN_DIR/clodex"
add_if_exists "$TARGET_DIR/claude-plugin"
add_if_exists "$TARGET_DIR/codex-plugin"
add_if_exists "$TARGET_DIR/dist"
add_if_exists "$TARGET_DIR/clodex-workflow-skill"

echo "Clodex Uninstaller v${SCRIPT_VERSION}"
echo "Target: $TARGET_DIR"
echo "Bin:    $BIN_DIR"
echo ""

if [[ "${#items[@]}" -eq 0 ]]; then
    echo "No Clodex installation was found for the selected paths."
    exit 0
fi

echo "This will remove:"
for item in "${items[@]}"; do
    if [[ "$DRY_RUN" == true ]]; then
        echo "  [dry-run] $item"
    else
        echo "  $item"
    fi
done
echo ""

echo "This will not remove:"
echo "  .clodex/ state directories inside your project checkouts"
echo "  Claude Code or Codex CLI auth/session data"
echo "  Any source repository files"
echo ""

if [[ "$DRY_RUN" == true ]]; then
    echo "dry-run: no files were removed"
    exit 0
fi

if [[ "$ASSUME_YES" != true ]]; then
    read -r -p "Remove these files? [y/N] " reply
    case "$reply" in
        [Yy]|[Yy][Ee][Ss]) ;;
        *)
            echo "Uninstall cancelled."
            exit 0
            ;;
    esac
fi

removed=0
for item in "${items[@]}"; do
    rm -rf -- "$item"
    echo "removed: $item"
    removed=$((removed + 1))
done

if [[ -d "$TARGET_DIR" && -z "$(ls -A "$TARGET_DIR" 2>/dev/null)" ]]; then
    rmdir "$TARGET_DIR"
    echo "removed empty target: $TARGET_DIR"
fi

echo "Uninstall complete. Removed ${removed} item(s)."
