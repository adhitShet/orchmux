#!/usr/bin/env bash
# orchmux completion log — appends new completions, never clears (scrollable)

BASE_URL="http://localhost:9889"
trap 'exit 0' INT TERM

DM="\033[2m"; RS="\033[0m"
echo -e "${DM}── orchmux log ─────────────────────────────────────────────────────────────${RS}"

python3 - "$BASE_URL" <<'PY'
import sys, json, time, urllib.request, urllib.error

base = sys.argv[1]
seen_completed = set()
seen_questions = set()

GR="\033[1;32m"; RD="\033[1;31m"; YL="\033[1;33m"; DM="\033[2m"; RS="\033[0m"

def fetch(path):
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None

while True:
    # Completions
    data = fetch("/completed")
    if data:
        for c in reversed(data):
            tid = c.get("task_id") or c.get("completed_at", "")
            if tid in seen_completed:
                continue
            seen_completed.add(tid)
            icon  = "✅" if c.get("success") else "❌"
            color = GR if c.get("success") else RD
            domain = c.get("domain", "?")
            sess   = c.get("session", "")
            result = (c.get("result") or "")[:120]
            ts     = c.get("completed_at", "")
            print(f"{color}{icon} {ts}  [{domain}] {sess}{RS}", flush=True)
            if result:
                print(f"   {result}", flush=True)

    # Answered questions
    qs = fetch("/questions")
    if qs:
        for q in qs.get("answered", []):
            qid = q.get("id") or q.get("asked_at", "")
            if qid in seen_questions:
                continue
            seen_questions.add(qid)
            sess = f"[{q['session']}] " if q.get("session") else ""
            print(f"{YL}❓ {q.get('asked_at','')}  {sess}{(q.get('message') or '')[:80]}{RS}", flush=True)
            if q.get("answer"):
                print(f"   → {(q['answer'])[:100]}", flush=True)

    time.sleep(5)
PY
