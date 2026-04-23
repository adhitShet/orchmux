#!/usr/bin/env bash
# Top-left: worker table only (no live output, no questions, no completions)
BASE_URL="http://localhost:9889"
trap 'exit 0' INT TERM

SELF=$(tmux display-message -p "#{session_name}:#{window_index}.#{pane_index}" 2>/dev/null)

python3 - <<'PY'
import sys, subprocess, json, datetime, time, urllib.request

BASE = "http://localhost:9889"
YL="\033[1;33m"; RD="\033[1;31m"; GR="\033[1;32m"
CY="\033[1;36m"; MG="\033[1;35m"; DM="\033[2m"; RS="\033[0m"

_UI_NOISE = ("bypass permissions","shift+tab","? for shortcuts","enter to confirm",
             "Claude Code","Sonnet","Opus","Welcome to","▐▛","▝▜","▘▘","⏵⏵","⏸")

def fetch(path):
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None

def trunc(s, n): return s[:n-2]+".." if len(s)>n else s
def elapsed(s):
    if not s: return "-"
    m,sec=divmod(int(s),60)
    return f"{m}m{sec:02d}s" if m else f"{sec}s"

while True:
    try:
        cols = int(subprocess.run(["tput","cols"],capture_output=True,text=True).stdout.strip() or "78")
    except Exception:
        cols = 78
    W = cols - 2

    status = fetch("/status")
    if not status:
        print("\033[H\033[J\n  orchmux server offline…", flush=True)
        time.sleep(3)
        continue

    now = datetime.datetime.now().strftime("%H:%M:%S")
    all_workers = []
    total_queued = 0; total_temp = 0
    for domain, cfg in status.items():
        q = cfg.get("queue_depth", 0); total_queued += q
        for w in cfg.get("workers", []):
            w["domain"] = domain; w["queue_depth"] = q
            if w.get("worker_type") == "temp": total_temp += 1
            all_workers.append(w)

    busy_count = sum(1 for w in all_workers if w.get("status","").lower()=="busy")

    def row(s): return "║  " + s + " "*(W-2-len(s)) + "║"

    out = ["\033[H\033[J"]
    out.append("╔" + "═"*W + "╗")
    hdr = f"  orchmux  {now}   workers: {busy_count}/{len(all_workers)} busy   queued: {total_queued}   temp: {total_temp}"
    out.append("║" + hdr + " "*(W-len(hdr)) + "║")
    out.append("╠" + "═"*W + "╣")
    out.append(row(f"  {'DOMAIN':<12} {'SESSION':<20} {'STATUS':<9} {'TIME':<7} TASK"))
    out.append("╠" + "═"*W + "╣")

    for w in all_workers:
        domain  = trunc(str(w.get("domain","")), 12)
        session = trunc(str(w.get("session","")), 20)
        st      = (w.get("status") or "idle").upper()
        task    = trunc(str(w.get("current_task") or ""), 25)
        el      = elapsed(w.get("elapsed_seconds"))
        line    = f"  {domain:<12} {session:<20} {st:<9} {el:<7} {task}"
        if st=="BUSY":
            out.append("║"+YL+line+RS+" "*(W-len(line))+"║")
        elif st=="WAITING":
            out.append("║"+MG+line+RS+" "*(W-len(line))+"║")
        elif st in ("ERROR","BLOCKED"):
            out.append("║"+RD+line+RS+" "*(W-len(line))+"║")
        else:
            out.append(row(line))

    if not all_workers:
        out.append(row("  (no workers registered)"))
    out.append("╚" + "═"*W + "╝")
    print("\n".join(out), flush=True)
    time.sleep(3)
PY
