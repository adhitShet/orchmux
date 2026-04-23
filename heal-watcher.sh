#!/bin/bash
# Auto-heal orchmux-watcher — run via cron every 2 minutes
# This is the external safety net since the watcher can't restart itself.

ORCHMUX="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="orchmux-watcher"

# Check if session exists and watcher process is alive inside it
if tmux has-session -t "$SESSION" 2>/dev/null; then
    # Session exists — check if watcher.py is actually running
    if pgrep -f "watcher.py" > /dev/null 2>&1; then
        exit 0  # healthy
    fi
    # Session alive but watcher.py not running — send restart
    tmux send-keys -t "$SESSION" "" ""  # wake pane
    tmux send-keys -t "$SESSION" "cd $ORCHMUX && $ORCHMUX/.venv/bin/python watcher.py" Enter
else
    # Session dead — recreate it
    source "$HOME/.claude/hooks/.env" 2>/dev/null || true
    if command -v ip >/dev/null 2>&1; then
      BIND=$(ip addr show tailscale0 2>/dev/null | grep -oP 'inet \K[\d.]+' | head -1)
    else
      BIND=$(ifconfig tailscale0 2>/dev/null | awk '/inet /{print $2}')
    fi
    export ORCHMUX_BIND_HOST="${BIND:-127.0.0.1}"
    tmux new-session -d -s "$SESSION" -c "$ORCHMUX"
    tmux send-keys -t "$SESSION" "cd $ORCHMUX && $ORCHMUX/.venv/bin/python watcher.py" Enter
fi

echo "[heal-watcher] $(date) restarted $SESSION"
