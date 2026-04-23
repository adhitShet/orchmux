#!/usr/bin/env bash
# Bottom-left: live pane output for busy/waiting/blocked workers (scrollable — appends)
BASE_URL="http://localhost:9889"
trap 'exit 0' INT TERM

DM="\033[2m"; RS="\033[0m"
echo -e "${DM}── live output ─────────────────────────────────────────────────────────────${RS}"

python3 - <<'PY'
import sys, subprocess, json, time, urllib.request

BASE = "http://localhost:9889"
YL="\033[1;33m"; RD="\033[1;31m"; CY="\033[1;36m"; MG="\033[1;35m"; DM="\033[2m"; RS="\033[0m"

_UI_NOISE = ("bypass permissions","shift+tab","? for shortcuts","enter to confirm",
             "Claude Code","Sonnet","Opus","Welcome to","▐▛","▝▜","▘▘","⏵⏵","⏸")

def is_noise(l):
    s=l.strip()
    if not s: return True
    if any(n in l for n in _UI_NOISE): return True
    if all(c in "─━═ │╭╰╮╯" for c in s): return True
    return False

def fetch(path):
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None

def trunc(s,n): return s[:n-2]+".." if len(s)>n else s

def elapsed(s):
    if not s: return "-"
    m,sec=divmod(int(s),60)
    return f"{m}m{sec:02d}s" if m else f"{sec}s"

def capture(session, lines=25):
    r = subprocess.run(["tmux","capture-pane","-t",session,"-p","-S",f"-{lines}"],
                       capture_output=True,text=True)
    return r.stdout if r.returncode==0 else ""

last_snapshot = {}

while True:
    try:
        cols = int(subprocess.run(["tput","cols"],capture_output=True,text=True).stdout.strip() or "78")
    except Exception:
        cols = 78
    W = cols

    status = fetch("/status")
    if not status:
        time.sleep(3)
        continue

    for domain, cfg in status.items():
        for w in cfg.get("workers", []):
            st = (w.get("status") or "").lower()
            if st not in ("busy","waiting","blocked"):
                continue
            session = w.get("session","")
            if not session: continue

            pane = capture(session, lines=30)
            sig = pane[-500:] if pane else ""
            if last_snapshot.get(session) == sig:
                continue
            last_snapshot[session] = sig

            color = YL if st=="busy" else (MG if st=="waiting" else RD)
            el = elapsed(w.get("elapsed_seconds"))
            hdr = trunc(f"┌─ {session}  [{st.upper()}  {el}] ", W-2)
            print(f"{color}{hdr}{'─'*max(0,W-2-len(hdr))}{RS}", flush=True)

            real = [l.rstrip() for l in pane.splitlines()
                    if not is_noise(l) and not l.strip().startswith("❯")]
            show = real[-8:] if len(real)>8 else real
            for l in show:
                clean = l.strip()
                if clean.startswith(("●","✻","*","✶","✢")):
                    print(f"{CY}  {trunc(clean, W-4)}{RS}", flush=True)
                else:
                    print(f"  {trunc(clean, W-4)}", flush=True)

    time.sleep(4)
PY
