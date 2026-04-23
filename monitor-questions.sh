#!/usr/bin/env bash
# Top-right: pending questions (refreshes)
BASE_URL="http://localhost:9889"
trap 'exit 0' INT TERM

SELF=$(tmux display-message -p "#{session_name}:#{window_index}.#{pane_index}" 2>/dev/null)

python3 - <<'PY'
import sys, json, time, urllib.request, datetime

BASE = "http://localhost:9889"
YL="\033[1;33m"; GR="\033[1;32m"; DM="\033[2m"; RS="\033[0m"

def fetch(path):
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None

def trunc(s,n): return s[:n-2]+".." if len(s)>n else s

while True:
    try:
        import subprocess
        cols = int(subprocess.run(["tput","cols"],capture_output=True,text=True).stdout.strip() or "40")
    except Exception:
        cols = 40
    W = cols - 2

    qs = fetch("/questions") or {}
    pending  = qs.get("pending", [])
    answered = qs.get("answered", [])

    now = datetime.datetime.now().strftime("%H:%M:%S")
    out = ["\033[H\033[J"]
    out.append("╔" + "═"*W + "╗")
    label = f"  QUESTIONS  {now}"
    out.append("║" + YL + label + RS + " "*(W-len(label)) + "║")
    out.append("╠" + "═"*W + "╣")

    if pending:
        out.append("║" + YL + f"  PENDING ({len(pending)})" + RS + " "*(W-len(f"  PENDING ({len(pending)})")) + "║")
        for q in pending[:6]:
            sess = f"[{q['session']}] " if q.get("session") else ""
            line = trunc(f"  {q.get('asked_at','')}  {sess}{q.get('message','')}", W)
            out.append("║" + YL + line + RS + " "*(W-len(line)) + "║")
    else:
        out.append("║  " + DM + "(no pending questions)" + RS + " "*(W-24) + "║")

    if answered:
        out.append("╠" + "═"*W + "╣")
        out.append("║" + GR + f"  ANSWERED ({len(answered)})" + RS + " "*(W-len(f"  ANSWERED ({len(answered)})")) + "║")
        for q in answered[-4:]:
            sess = f"[{q['session']}] " if q.get("session") else ""
            line = trunc(f"  {q.get('asked_at','')}  {sess}{q.get('message','')}", W)
            out.append("║" + GR + line + RS + " "*(W-len(line)) + "║")
            if q.get("answer"):
                ans = trunc(f"  → {q['answer']}", W)
                out.append("║" + ans + " "*(W-len(ans)) + "║")

    out.append("╚" + "═"*W + "╝")
    print("\n".join(out), flush=True)
    time.sleep(4)
PY
