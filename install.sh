#!/usr/bin/env bash
# install.sh — Install the bu command into a target directory via symlink.
#
# Usage:
#   ./install.sh <TARGET_DIR>
#
# The target directory must exist. A symbolic link named 'bu' will be created
# pointing to the bu executable. If 'bu' already exists in the target directory,
# the script exits with an error.

set -euo pipefail

# ── usage ────────────────────────────────────────────────────────────────
usage() {
    cat >&2 << 'USAGE'
Usage: ./install.sh <TARGET_DIR>

Install the bu command into TARGET_DIR via a symbolic link.

Arguments:
  TARGET_DIR    Directory to install the bu symlink into (must exist).
                This directory should be on your PATH.

Examples:
  ./install.sh /usr/local/bin
  ./install.sh ~/.local/bin
USAGE
}

# ── check arguments ──────────────────────────────────────────────────────
if [ $# -ne 1 ]; then
    echo "Error: TARGET_DIR is required." >&2
    usage
    exit 1
fi

TARGET_DIR="$1"

if [ ! -d "$TARGET_DIR" ]; then
    echo "Error: '$TARGET_DIR' is not a directory or does not exist." >&2
    exit 1
fi

# ── find the bu executable ───────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BU_EXEC=""

# 1. Try the venv-relative path first (development install)
if [ -x "$SCRIPT_DIR/.venv/bin/bu" ]; then
    BU_EXEC="$SCRIPT_DIR/.venv/bin/bu"
# 2. Try 'which bu' to find a system-installed bu
elif command -v bu &>/dev/null; then
    BU_EXEC="$(command -v bu)"
else
    echo "Error: Cannot find the 'bu' executable." >&2
    echo "Make sure you have run 'pip install -e .' from the repository root," >&2
    echo "or that 'bu' is already on your PATH." >&2
    exit 1
fi

# Resolve to an absolute path
BU_EXEC="$(cd "$(dirname "$BU_EXEC")" && pwd)/$(basename "$BU_EXEC")"

# ── check for existing file ──────────────────────────────────────────────
LINK_PATH="$TARGET_DIR/bu"

if [ -e "$LINK_PATH" ] || [ -L "$LINK_PATH" ]; then
    echo "Error: '$LINK_PATH' already exists." >&2
    exit 1
fi

# ── create the symlink ───────────────────────────────────────────────────
ln -s "$BU_EXEC" "$LINK_PATH"

echo "Installed: $LINK_PATH -> $BU_EXEC"
echo "Make sure '$TARGET_DIR' is on your PATH."
