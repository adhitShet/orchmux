#!/usr/bin/env bash
# orchmux.sh — launch/stop the orchmux system
set -euo pipefail

ORCHMUX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_URL="http://localhost:9889/health"
WATCHER_PIDS="/tmp/orchmux-watchers.pid"
QUEUE_DIR="$ORCHMUX_DIR/queue"
SUP_CFG="/tmp/orchmux-supervisor"

get_sessions() {
  python3 -c "
import yaml
with open('$ORCHMUX_DIR/workers.yaml') as f: d = yaml.safe_load(f)
[print(s) for cfg in d.get('workers',{}).values() for s in cfg.get('sessions',[])]
"
}

tmux_ok() { tmux has-session -t "$1" 2>/dev/null; }

# ── stop ───────────────────────────────────────────────────────────────────────
if [[ "${1:-start}" == "stop" ]]; then
  [[ -f "$WATCHER_PIDS" ]] && { xargs kill 2>/dev/null < "$WATCHER_PIDS" || true; rm -f "$WATCHER_PIDS"; echo "  watchers stopped"; }
  for s in orchmux-server orchmux-supervisor orchmux-monitor orchmux-telegram orchmux-watcher; do
    tmux_ok "$s" && tmux kill-session -t "$s" && echo "  $s killed"
  done
  # Kill all persistent worker sessions
  while IFS= read -r session; do
    tmux_ok "$session" && tmux kill-session -t "$session" && echo "  worker $session killed"
  done < <(get_sessions)
  if [[ -d "$ORCHMUX_DIR/worker-workdirs" ]]; then
    rm -rf "$ORCHMUX_DIR/worker-workdirs"
    echo "  worker-workdirs cleaned up"
  fi
  echo "orchmux stopped"; exit 0
fi

# ── Load Telegram env ──────────────────────────────────────────────────────────
TG_ENV="$HOME/.claude/hooks/.env"
if [[ -f "$TG_ENV" ]]; then
  export $(grep -v '^#' "$TG_ENV" | xargs) 2>/dev/null || true
fi

# ── Tailscale bind: dashboard only reachable on VPN, never public internet ─────
TAILSCALE_IP=$(ip addr show tailscale0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)
if [[ -n "$TAILSCALE_IP" ]]; then
  export ORCHMUX_BIND_HOST="$TAILSCALE_IP"
  echo "  dashboard: http://${TAILSCALE_IP}:9889/dashboard (Tailscale only)"
else
  export ORCHMUX_BIND_HOST="127.0.0.1"
  echo "  dashboard: http://127.0.0.1:9889/dashboard (Tailscale not connected)"
fi

# ── 1. MCP server (auto-restart loop) ─────────────────────────────────────────
if curl -sf "$SERVER_URL" >/dev/null 2>&1; then
  echo "  server already running"
else
  tmux kill-session -t orchmux-server 2>/dev/null || true
  tmux new-session -d -s orchmux-server -c "$ORCHMUX_DIR" \
    "while true; do
       env TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID \
         $ORCHMUX_DIR/.venv/bin/python server/server.py
       echo '[orchmux-server] crashed — restarting in 3s...'
       sleep 3
     done"
  for _ in $(seq 1 20); do curl -sf "$SERVER_URL" >/dev/null 2>&1 && break; sleep 0.5; done
fi

# ── 2. Supervisor session ──────────────────────────────────────────────────────
mkdir -p "$SUP_CFG"
[[ -f "$HOME/.claude/.credentials.json" ]] && ln -sf "$HOME/.claude/.credentials.json" "$SUP_CFG/.credentials.json"
[[ -f "$HOME/.claude/settings.json" ]]     && ln -sf "$HOME/.claude/settings.json"     "$SUP_CFG/settings.json"
tmux_ok orchmux-supervisor || \
  tmux new-session -d -s orchmux-supervisor -c "$ORCHMUX_DIR/supervisor" \
    "env CLAUDE_CONFIG_DIR=$SUP_CFG claude --dangerously-skip-permissions"

# ── 3. Monitor (4-pane layout) ────────────────────────────────────────────────
# top-left: worker table | top-right: questions
# bottom-left: live output (scrollable) | bottom-right: completions (scrollable)
if ! tmux_ok orchmux-monitor; then
  tmux new-session -d -s orchmux-monitor \
    "while true; do bash $ORCHMUX_DIR/monitor-table.sh; sleep 2; done"
  _P0=$(tmux list-panes -t orchmux-monitor -F "#{pane_id}" | head -1)
  tmux split-window -t "$_P0" -h -l 60 \
    "while true; do bash $ORCHMUX_DIR/monitor-questions.sh; sleep 2; done"
  _P1=$(tmux list-panes -t orchmux-monitor -F "#{pane_id}" | sed -n '2p')
  tmux split-window -t "$_P0" -v -l 18 \
    "while true; do bash $ORCHMUX_DIR/monitor-live.sh; sleep 2; done"
  tmux split-window -t "$_P1" -v -l 18 \
    "while true; do bash $ORCHMUX_DIR/monitor-status.sh; sleep 2; done"
  tmux set-option -t orchmux-monitor history-limit 5000
fi

# ── 3b. Telegram bot (auto-restart loop) ──────────────────────────────────────
tmux_ok orchmux-telegram || \
  tmux new-session -d -s orchmux-telegram -c "$ORCHMUX_DIR" \
    "while true; do
       $ORCHMUX_DIR/.venv/bin/python $ORCHMUX_DIR/telegram_bot.py
       echo '[orchmux-telegram] crashed — restarting in 3s...'
       sleep 3
     done"

# ── 3c. Watcher (auto-restart loop) ───────────────────────────────────────────
tmux_ok orchmux-watcher || \
  tmux new-session -d -s orchmux-watcher -c "$ORCHMUX_DIR" \
    "while true; do
       $ORCHMUX_DIR/.venv/bin/python $ORCHMUX_DIR/watcher.py
       echo '[orchmux-watcher] crashed — restarting in 3s...'
       sleep 3
     done"

# ── 4. inotifywait watchers (Linux only) ──────────────────────────────────────
# inotifywait is Linux-only. On macOS, queue-file nudges are disabled — workers
# will still pick up queued tasks on the next watcher poll (every 5s), just
# without the instant push. Install inotify-tools on Linux for real-time dispatch.
if ! command -v inotifywait >/dev/null 2>&1; then
  if [[ "$(uname)" == "Darwin" ]]; then
    echo "  NOTE: inotifywait not available on macOS — queue nudges disabled (tasks still poll every 5s)"
  else
    echo "  WARNING: inotifywait not found — queue watchers disabled (apt install inotify-tools)"
  fi
else
  mkdir -p "$QUEUE_DIR"
  [[ -f "$WATCHER_PIDS" ]] && { xargs kill 2>/dev/null < "$WATCHER_PIDS" || true; rm -f "$WATCHER_PIDS"; }
  while IFS= read -r session; do
    if tmux_ok "$session"; then
      queue_file="$QUEUE_DIR/${session}.yaml"
      touch "$queue_file"
      (inotifywait -q -m -e close_write "$queue_file" 2>/dev/null | \
        while read -r _; do
          sleep 0.5  # debounce: wait for any burst of writes to settle
          # Only nudge if session is still at idle prompt (not mid-task)
          pane=$(tmux capture-pane -t "$session" -p -S -5 2>/dev/null)
          if echo "$pane" | grep -q "❯"; then
            tmux send-keys -t "$session" "" Enter 2>/dev/null || true
          fi
        done) &
      echo "$!" >> "$WATCHER_PIDS"
    fi
  done < <(get_sessions)
fi

# ── 5. Worker workdirs + launch persistent workers ────────────────────────────
# Workers use global ~/.claude auth (no CLAUDE_CONFIG_DIR — avoids re-login).
# Domain CLAUDE.md goes in their cwd. Hooks registered in ~/.claude/settings.json.
python3 - <<'PYEOF'
import yaml, os, shutil, subprocess, time
from pathlib import Path

orchmux   = Path(os.environ["HOME"]) / "orchmux"
base      = orchmux / "worker-workdirs"
queue_dir = orchmux / "queue"
res_dir   = orchmux / "results"

with open(orchmux / "workers.yaml") as f:
    data = yaml.safe_load(f)

def tmux_ok(s):
    return subprocess.run(["tmux", "has-session", "-t", s], capture_output=True).returncode == 0

launched = 0
for domain, cfg in data.get("workers", {}).items():
    for session in cfg.get("sessions", []):
        work_dir = base / session
        work_dir.mkdir(parents=True, exist_ok=True)
        claude_src = orchmux / "worker" / domain / "CLAUDE.md"
        if claude_src.exists():
            shutil.copy2(str(claude_src), str(work_dir / "CLAUDE.md"))

        if tmux_ok(session):
            print(f"  exists: {session}")
            continue

        # Launch with global auth — no CLAUDE_CONFIG_DIR
        queue_dir.mkdir(exist_ok=True)
        res_dir.mkdir(exist_ok=True)
        subprocess.run([
            "tmux", "new-session", "-d", "-s", session, "-c", str(work_dir),
            "-e", f"ORCHMUX_SESSION={session}",
            "-e", f"ORCHMUX_WORKER_ID={session}",
            "-e", f"ORCHMUX_DOMAIN={domain}",
            "-e", f"ORCHMUX_QUEUE={queue_dir}/{session}.yaml",
            "-e", f"ORCHMUX_RESULTS={res_dir}/{session}.yaml",
        ], check=True)
        # Send command into existing shell — avoids ENOENT hook issues
        subprocess.run(["tmux", "send-keys", "-t", session,
                        "claude --dangerously-skip-permissions", "Enter"])
        print(f"  launched: {session}")
        launched += 1

if launched:
    print(f"  waiting for {launched} workers to authenticate...")
    time.sleep(5)
PYEOF

# ── 6. Status ──────────────────────────────────────────────────────────────────
n=$(python3 -c "
import yaml
with open('$ORCHMUX_DIR/workers.yaml') as f: d = yaml.safe_load(f)
print(sum(len(c.get('sessions',[])) for c in d.get('workers',{}).values()))
")
echo "orchmux ready"
echo "  supervisor: tmux a -t orchmux-supervisor"
echo "  monitor:    tmux a -t orchmux-monitor"
echo "  telegram:   tmux a -t orchmux-telegram"
echo "  server:     $SERVER_URL"
echo "  workers:    $n registered"
echo "  worker configs: $ORCHMUX_DIR/worker-configs/ (restart sessions to apply)"
