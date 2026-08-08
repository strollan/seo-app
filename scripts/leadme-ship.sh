#!/usr/bin/env bash
# leadme-ship.sh — the one-command LeadMeLeads ship pipeline.
#
# Run from a committed feature branch:
#   ./scripts/leadme-ship.sh
#
# Pushes the branch, opens (or reuses) a PR into main, merges it, deletes
# the remote branch, then deploys the exact merged main SHA to production
# (backup, pull, compile, restart, smoke-test). Safe to re-run at any stage.
#
# This wrapper only resolves the right Python interpreter and execs the
# real implementation (scripts/leadme_ship_pr.py) — it does not itself do
# any git/gh/ssh work, so there is exactly one place that logic lives.
#
# IMPORTANT: this script's own failures must never kill the caller's shell.
# It is invoked as `./scripts/leadme-ship.sh`, i.e. as a new process, so a
# `set -e`/non-zero exit here only ends this script, not the interactive
# shell that launched it — no `exec`-ing into the parent shell, no `source`.
set -uo pipefail

SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
    TARGET="$(readlink "$SOURCE")"
    case "$TARGET" in
        /*) SOURCE="$TARGET" ;;
        *) SOURCE="$(dirname "$SOURCE")/$TARGET" ;;
    esac
done
SCRIPT_DIR="$(cd "$(dirname "$SOURCE")" && pwd)"

PYTHON_BIN="$HOME/.venvs/leadmeleads/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3)"
fi

"$PYTHON_BIN" "$SCRIPT_DIR/leadme_ship_pr.py" "$@"
