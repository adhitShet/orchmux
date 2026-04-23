#!/usr/bin/env bash
# Bottom-right: recent completions + status (scrollable — appends)
BASE_URL="http://localhost:9889"
trap 'exit 0' INT TERM

DM="\033[2m"; RS="\033[0m"
echo -e "${DM}── completions & status ────────────────────────────────────────────────────${RS}"

python3 - <<'PY'
import json, time, urllib.request

BASE = "http://localhost:9889"
GR="\033[1;32m"; RD="\033[1;31m"; YL="\033[1;33m"; DM="\033[2m"; RS="\033[0m"
seen = set()

def fetch(path):
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None

def trunc(s,n): return s[:n-2]+".." if len(s)>n else s

while True:
    data = fetch("/completed")
    if data:
        for c in reversed(data):
            tid = c.get("task_id") or c.get("completed_at","")
            if tid in seen: continue
            seen.add(tid)
            icon  = "✅" if c.get("success") else "❌"
            color = GR if c.get("success") else RD
            domain = c.get("domain","?"); sess = c.get("session","")
            result = (c.get("result") or "")[:200]
            ts = c.get("completed_at","")
            task = trunc((c.get("task") or "")[:60], 60)
            print(f"{color}{icon} {ts}  [{domain}] {sess}{RS}", flush=True)
            if task: print(f"   {DM}task: {task}{RS}", flush=True)
            if result: print(f"   {result}", flush=True)
            print("", flush=True)
    time.sleep(5)
PY
