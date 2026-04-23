#!/usr/bin/env bash
# orchmux live status monitor — polls /status every 3 seconds

BASE_URL="http://localhost:9889"
STATUS_URL="$BASE_URL/status"
QUESTIONS_URL="$BASE_URL/questions"
COMPLETED_URL="$BASE_URL/completed"
trap 'echo ""; exit 0' INT TERM

render() {
python3 - "$1" "$2" "$3" <<'PY'
import sys, json, datetime, subprocess

status_data   = json.loads(sys.argv[1])
questions_raw = json.loads(sys.argv[2]) if sys.argv[2] else {"pending": [], "answered": []}
completed     = json.loads(sys.argv[3]) if sys.argv[3] else []

YL = "\033[1;33m"; RD = "\033[1;31m"; GR = "\033[1;32m"
CY = "\033[1;36m"; MG = "\033[1;35m"; DM = "\033[2m"; RS = "\033[0m"
W = 78

def row(s): return "║  " + s + " " * (W - 2 - len(s)) + "║"
def trunc(s, n): return s[:n-2] + ".." if len(s) > n else s
def elapsed(s):
    if not s: return "-"
    m, sec = divmod(int(s), 60)
    return f"{m}m{sec:02d}s" if m else f"{sec}s"

_UI_NOISE = ("bypass permissions", "shift+tab", "? for shortcuts",
             "enter to confirm", "Claude Code", "Sonnet", "Opus",
             "Welcome to", "▐▛", "▝▜", "▘▘", "⏵⏵", "⏸")

def is_noise(line):
    s = line.strip()
    if not s: return True
    if any(n in line for n in _UI_NOISE): return True
    if all(c in "─━═ │╭╰╮╯" for c in s): return True
    return False

def capture_pane(session, lines=25):
    r = subprocess.run(["tmux", "capture-pane", "-t", session, "-p", "-S", f"-{lines}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""

now = datetime.datetime.now().strftime("%H:%M:%S")

# Flatten workers
all_workers = []
total_queued = 0
total_temp   = 0
for domain, cfg in status_data.items():
    q = cfg.get("queue_depth", 0)
    total_queued += q
    for w in cfg.get("workers", []):
        w["domain"] = domain
        w["queue_depth"] = q
        if w.get("worker_type") == "temp":
            total_temp += 1
        all_workers.append(w)

busy_workers    = [w for w in all_workers if w.get("status","").lower() in ("busy","waiting","blocked")]
busy_count      = sum(1 for w in all_workers if w.get("status","").lower() == "busy")
pending_qs      = questions_raw.get("pending", [])
answered_qs     = questions_raw.get("answered", [])

# ── header ────────────────────────────────────────────────────────────────────
print("╔" + "═" * W + "╗")
hdr = f"  orchmux  {now}   workers: {busy_count}/{len(all_workers)} busy   queued: {total_queued}   temp: {total_temp}"
if pending_qs:
    hdr += f"   {YL}❓{len(pending_qs)}{RS}"
hdr_pad = W - len(hdr) + (len(YL)+len(RS) if pending_qs else 0)
print("║" + hdr + " " * hdr_pad + "║")
print("╠" + "═" * W + "╣")

# ── worker table ──────────────────────────────────────────────────────────────
hdr = f"  {'DOMAIN':<12} {'SESSION':<20} {'STATUS':<9} {'TIME':<7} TASK"
print(row(hdr))
print("╠" + "═" * W + "╣")

for w in all_workers:
    domain   = trunc(str(w.get("domain", "")), 12)
    session  = trunc(str(w.get("session", "")), 20)
    st       = w.get("status") or "idle"
    stup     = st.upper()
    task     = trunc(str(w.get("current_task") or ""), 20)
    el       = elapsed(w.get("elapsed_seconds"))
    progress = w.get("pane_progress", "")
    if any(n in (progress or "") for n in ("bypass permissions","shift+tab","⏵⏵")):
        progress = ""
    line     = f"  {domain:<12} {session:<20} {stup:<9} {el:<7} {task}"
    if stup == "BUSY":
        print("║" + YL + line + RS + " " * (W - len(line)) + "║")
        if progress:
            pl = trunc(f"    └ {progress}", W - 2)
            print("║" + CY + pl + RS + " " * (W - len(pl)) + "║")
    elif stup == "WAITING":
        print("║" + MG + line + RS + " " * (W - len(line)) + "║")
        ql = trunc(f"    └ ❓ {task}", W - 2)
        print("║" + MG + ql + RS + " " * (W - len(ql)) + "║")
    elif stup in ("ERROR", "BLOCKED"):
        print("║" + RD + line + RS + " " * (W - len(line)) + "║")
        if stup == "BLOCKED":
            bl = trunc(f"    └ ⚠️  tmux a -t {session}", W - 2)
            print("║" + RD + bl + RS + " " * (W - len(bl)) + "║")
    else:
        print(row(line))

if not all_workers:
    print(row("  (no workers registered)"))

# ── live pane output for busy / waiting / blocked ─────────────────────────────
if busy_workers:
    print("╠" + "═" * W + "╣")
    print(row(f"  LIVE OUTPUT"))
    for w in busy_workers:
        session = w.get("session", "")
        st      = w.get("status", "").upper()
        el      = elapsed(w.get("elapsed_seconds"))
        color   = YL if st == "BUSY" else (MG if st == "WAITING" else RD)

        # Section header per worker
        hdr_txt = trunc(f"  ┌─ {session}  [{st}  {el}] ", W - 2)
        print("║" + color + hdr_txt + "─" * max(0, W - 2 - len(hdr_txt)) + RS + "║")

        pane = capture_pane(session, lines=30)
        real = [l.rstrip() for l in pane.splitlines()
                if not is_noise(l) and not l.strip().startswith("❯")]

        # Find last block after the submitted task (after ✻ or ● or *, last 15 lines)
        block = real[-15:] if len(real) > 15 else real

        # Show last 10 lines of that block
        show = block[-10:] if len(block) > 10 else block
        if not show:
            print(row(f"    (no output yet)"))
        for l in show:
            # Colour tool-call lines differently
            clean = l.strip()
            if clean.startswith("●") or clean.startswith("✻") or clean.startswith("*"):
                pl = trunc(f"  {l.strip()}", W - 2)
                print("║" + CY + "  " + pl + RS + " " * (W - 2 - len(pl)) + "║")
            else:
                print(row(trunc(f"  {l.strip()}", W - 2)))

# ── pending questions ─────────────────────────────────────────────────────────
if pending_qs:
    print("╠" + "═" * W + "╣")
    label = f"  PENDING QUESTIONS ({len(pending_qs)})"
    print("║" + YL + label + RS + " " * (W - len(label)) + "║")
    for q in pending_qs[:4]:
        sess = f"[{q['session']}] " if q.get("session") else ""
        print(row(trunc(f"  {q['asked_at']}  {sess}{q['message']}", W - 2)))

# ── recently completed ────────────────────────────────────────────────────────
if completed:
    print("╠" + "═" * W + "╣")
    print(row(f"  RECENTLY COMPLETED"))
    for c in completed[:3]:
        icon   = "✅" if c.get("success") else "❌"
        domain = trunc(str(c.get("domain", "?")), 8)
        result = trunc(str(c.get("result", "")), W - 24)
        line   = trunc(f"  {icon} {c['completed_at']}  [{domain}]  {result}", W - 2)
        color  = GR if c.get("success") else RD
        print("║" + color + line + RS + " " * (W - len(line)) + "║")

print("╚" + "═" * W + "╝")
PY
}

SELF_SESSION=$(tmux display-message -p "#{session_name}" 2>/dev/null)

printf '\033[2J'
while true; do
    # Pause refresh while user is scrolling (tmux copy mode)
    if [ -n "$SELF_SESSION" ]; then
        IN_MODE=$(tmux display-message -t "$SELF_SESSION" -p "#{pane_in_mode}" 2>/dev/null)
        if [ "$IN_MODE" = "1" ]; then
            sleep 1
            continue
        fi
    fi

    printf '\033[H'
    RESP=$(curl -sf --max-time 2 "$STATUS_URL" 2>/dev/null)
    if [ -z "$RESP" ]; then
        printf "\n  orchmux server offline — retrying...\n\n"
    else
        QS=$(curl -sf --max-time 1 "$QUESTIONS_URL" 2>/dev/null || echo '{"pending":[],"answered":[]}')
        DONE=$(curl -sf --max-time 1 "$COMPLETED_URL" 2>/dev/null || echo '[]')
        render "$RESP" "$QS" "$DONE"
    fi
    printf '\033[J'
    sleep 3
done
