#!/bin/bash
# Auto-install gstack in Claude Code on the web containers. Fresh containers
# start without ~/.claude/skills/gstack, and check-gstack.sh (required mode)
# blocks skill usage until it exists. Local machines are untouched: developers
# run the documented one-time install themselves (see CLAUDE.md / README).
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

GSTACK_DIR="$HOME/.claude/skills/gstack"

if [ -d "$GSTACK_DIR/bin" ]; then
  echo "gstack already installed."
  exit 0
fi

# A directory without bin/ is a partial install from an interrupted run.
if [ -e "$GSTACK_DIR" ]; then
  rm -rf "$GSTACK_DIR"
fi

echo "Installing gstack (one-time per container)..."
git clone --single-branch --depth 1 https://github.com/garrytan/gstack.git "$GSTACK_DIR"
(cd "$GSTACK_DIR" && ./setup --team -q)
echo "gstack installed: $(cat "$GSTACK_DIR/VERSION" 2>/dev/null || echo unknown)"
