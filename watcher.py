#!/usr/bin/env python3
"""
orchmux session watcher — universal [DONE] detector for all workers.

Polls every 5s. For each session with a pending queue task:
  - Captures tmux pane output
  - Detects [DONE] → extracts result → calls /complete
  - Detects trailing ? → routes question to Telegram
  - Timeout (default 30min) → auto-completes with last output
Works for Claude (hook fallback), Codex, Kimi — any tmux-based worker.
"""

import json
import os
import re
import ssl
import subprocess
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# Allow self-signed certs for local HTTPS connections to orchmux server
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

import yaml

ORCHMUX        = Path.home() / "orchmux"
QUEUE_DIR      = ORCHMUX / "queue"
SESSION_ID_DIR = ORCHMUX / "session-ids"
SESSION_ID_DIR.mkdir(parents=True, exist_ok=True)
POLL_SEC  = 5
TIMEOUT_SEC = 30 * 60  # 30 minutes

def _server_url() -> str:
    """Resolve server URL — uses https if cert exists, tries Tailscale IP first."""
    from pathlib import Path as _Path
    cert = _Path(__file__).parent / "server" / "cert.pem"
    scheme = "https" if cert.exists() else "http"
    bind = os.environ.get("ORCHMUX_BIND_HOST", "")
    if bind:
        return f"{scheme}://{bind}:9889"
    r = subprocess.run(
        ["ip", "addr", "show", "tailscale0"],
        capture_output=True, text=True)
    import re as _re
    m = _re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", r.stdout)
    if m:
        return f"{scheme}://{m.group(1)}:9889"
    return f"{scheme}://127.0.0.1:9889"

SERVER = _server_url()

# Track what we've already seen per session to avoid re-processing
_last_seen: dict[str, str] = {}

_BUSY_SIGNS  = ("thinking", "Running", "Reading", "Writing", "Searching",
                "Executing", "Fetching", "Improvising",
                "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_BUSY_RAW    = ("esc to interrupt",)   # checked against raw pane (before noise filter)
_UI_NOISE    = ("bypass permissions", "shift+tab", "? for shortcuts",
                "enter to confirm", "Claude Code", "Sonnet", "Opus",
                "Welcome to", "▐▛", "▝▜", "▘▘",
                "Syntax theme", "ctrl+t to disable", "╌",
                "tmux focus", "focus-events", "3rd-party platform",
                "new task? /clear", "ctrl+t to hide tasks", "ctrl+t to show tasks",
                "⏵⏵", "accept edits on", "esc to interrupt", "to save", "tokens")

def _is_ui_noise(line: str) -> bool:
    s = line.strip()
    if any(n in line for n in _UI_NOISE):
        return True
    # Claude Code separator lines (all ─ or ━ chars)
    if s and all(c in "─━═─ " for c in s):
        return True
    return False
# Patterns that mean the session needs human intervention
_BLOCKED_SIGNS = [
    ("login", "Login code"),           # Claude auth code prompt
    ("trust", "Trust this folder"),    # new workdir trust prompt
    ("trust", "Do you trust"),
    ("login", "claude.ai/login"),
    ("login", "Authentication required"),
    ("prompt", "Press Enter to continue"),
    ("prompt", "Continue? (y/n)"),
    ("prompt", "proceed? [y/N]"),
    ("prompt", "[y/N]"),
    ("prompt", "(Y/n)"),
]


def tmux_set(session: str, key: str, value: str):
    subprocess.run(["tmux", "set-option", "-t", session, "-p", f"@{key}", value],
                   capture_output=True)


def tmux_get(session: str, key: str) -> str:
    r = subprocess.run(["tmux", "display-message", "-t", session, "-p", f"#{{@{key}}}"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def pane_state(pane: str) -> str:
    """Return 'busy', 'blocked', 'waiting', or 'idle' based on pane content."""
    real = [l for l in pane.splitlines()
            if l.strip() and not _is_ui_noise(l)]
    tail = "\n".join(real[-8:]) if real else ""
    last = real[-1].strip() if real else ""

    # Blocked: needs human action (login, trust prompt, y/n)
    for _, sign in _BLOCKED_SIGNS:
        if sign.lower() in pane.lower():
            return "blocked"

    # Idle prompt check FIRST — ❯ at the end of real content means Claude is
    # waiting for input, regardless of "esc to interrupt" in the status bar
    # (Claude Code always shows that bar even when idle at the ❯ prompt)
    if last.startswith("❯") and not last.strip("❯ "):
        content = [l for l in real if not l.strip().startswith("❯")]
        last_content_lines = content[-3:] if content else []
        if any("?" in l for l in last_content_lines):
            return "waiting"
        return "idle"

    # Busy: "esc to interrupt" in raw pane — reliable active-execution signal
    # (only reached if ❯ idle prompt is NOT the last real line)
    if any(sign in pane for sign in _BUSY_RAW):
        return "busy"

    # Busy: braille spinner chars and tool/thinking indicators in recent output
    if any(sign in tail for sign in _BUSY_SIGNS):
        return "busy"

    return "idle"


def all_worker_sessions() -> list[str]:
    """Return all registered worker sessions from workers.yaml."""
    try:
        with open(ORCHMUX / "workers.yaml") as f:
            data = yaml.safe_load(f)
        sessions = []
        for cfg in data.get("workers", {}).values():
            sessions.extend(cfg.get("sessions", []))
        return sessions
    except Exception:
        return []


# ── helpers ────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"{datetime.now().strftime('%H:%M:%S')} [watcher] {msg}", flush=True)


def orchmux_post(path: str, body: dict) -> dict:
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{SERVER}{path}", data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5, context=_SSL_CTX) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def capture_pane(session: str, lines: int = 80) -> str:
    r = subprocess.run(
        ["tmux", "capture-pane", "-t", session, "-p", "-S", f"-{lines}"],
        capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def session_exists(session: str) -> bool:
    return subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True).returncode == 0


def pending_tasks() -> list[dict]:
    """Return all queue YAMLs with status=pending."""
    tasks = []
    for f in QUEUE_DIR.glob("*.yaml"):
        try:
            with open(f) as fp:
                q = yaml.safe_load(fp)
            if q and q.get("status") == "pending":
                q["_queue_file"] = str(f)
                q["_dispatched_ts"] = _parse_ts(q.get("dispatched_at", ""))
                tasks.append(q)
        except Exception as e:
            log(f"WARN: could not parse {f.name}: {e}")
    return tasks


def _parse_ts(s: str) -> float:
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(s.rstrip("Z"))
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return time.time()


def extract_done_result(output: str) -> str | None:
    """Return result text if [DONE] found, else None."""
    if "[DONE]" not in output:
        return None
    # Skip [DONE] embedded in task prompt instructions ("write exactly: [DONE]")
    # Find the last standalone [DONE] — one NOT preceded by "write exactly" on the same line
    idx = -1
    search_from = len(output)
    while True:
        pos = output.rfind("[DONE]", 0, search_from)
        if pos == -1:
            break
        line_start = output.rfind("\n", 0, pos) + 1
        line_prefix = output[line_start:pos].lower()
        if "write exactly" not in line_prefix:
            idx = pos
            break
        search_from = pos  # skip this one, look earlier
    if idx == -1:
        return None
    before = output[:idx].rstrip()
    after = output[idx + 6:].strip()
    before_line = before.split("\n")[-1].strip() if before else ""
    result = before_line or after
    return result or output[max(0, idx - 200):idx].strip()


def last_nonempty_line(text: str) -> str:
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    return lines[-1] if lines else ""


def is_question(output: str) -> bool:
    last = last_nonempty_line(output)
    # Ignore Claude Code UI lines
    skip = {"bypass permissions on (shift+tab to cycle)", "esc to interrupt",
            "? for shortcuts", "enter to confirm", "· /effort"}
    if any(s in last.lower() for s in skip):
        return False
    return last.endswith("?")


# ── main loop ──────────────────────────────────────────────────────────────

def process_task(task: dict):
    session   = task.get("session", "")
    task_id   = task.get("task_id", "")
    domain    = task.get("domain", "?")
    dispatch_ts = task.get("_dispatched_ts", time.time())

    if not session or not session_exists(session):
        return

    output = capture_pane(session, lines=120)
    if not output.strip():
        return

    # Hard wall-clock timeout — fires regardless of output changes
    elapsed = time.time() - dispatch_ts
    if elapsed > TIMEOUT_SEC:
        last = last_nonempty_line(output)
        log(f"TIMEOUT {session} ({elapsed/60:.0f}min) — auto-completing")
        result = orchmux_post("/complete", {
            "session": session, "task_id": task_id,
            "result": f"[timeout after {elapsed/60:.0f}min] {last[:300]}",
            "success": False
        })
        if result.get("status") in ("ok", "already completed") or result.get("note"):
            _last_seen[session] = output
        return

    # Deduplicate — only act if output changed since last check
    prev = _last_seen.get(session, "")
    if output == prev:
        return

    _last_seen[session] = output

    # [DONE] detection — only if pane changed since last check (not old residual output)
    result_text = extract_done_result(output)
    if result_text is not None:
        baseline = task.get("pane_baseline", "")
        baseline_done_ct = baseline.count("[DONE]") if baseline else 0
        # Guard 1: no new [DONE] since task was dispatched
        guard1 = output.count("[DONE]") <= baseline_done_ct
        # Guard 2: task just dispatched and Claude hasn't shown activity yet
        guard2 = (time.time() - dispatch_ts < 20 and not any(s in output for s in (
                  "esc to interrupt", "Thinking", "Running", "Reading",
                  "Slithering", "Analysing", "Writing", "Executing")))
        if not guard1 and not guard2:
            log(f"[DONE] detected in {session} — completing task {task_id}")
            res = orchmux_post("/complete", {
                "session": session, "task_id": task_id,
                "result": result_text[:1000], "success": True
            })
            if res.get("status") in ("ok",) or res.get("note"):
                # Keep current output as baseline so next task doesn't re-trigger on same [DONE]
                _last_seen[session] = output
            return

    # Question detection
    if is_question(output):
        last = last_nonempty_line(output)
        log(f"Question detected in {session}: {last[:80]}")
        orchmux_post("/notify", {
            "message": f"❓ [{domain}] [{session}] asks:\n{last[:300]}",
            "session": session,
            "channels": ["telegram"]
        })
        # Don't re-send same question — mark as seen
        _last_seen[session] = output
        return


_alerted_blocked: set[str] = set()  # sessions we already notified as blocked
_auth_stuck_count: dict[str, int] = {}  # how many polls a session has been auth-stuck
_startup_polls: dict[str, int] = {}    # polls spent at startup screen — nudge after 3
_AUTH_SIGNS = ("OAuth error", "Invalid code", "Paste code here",
               "Browser didn't open", "Press Enter to retry",
               "API Error: 401", "authentication_error", "Invalid authentication",
               "Please run /login", "Invalid API key", "expired")
_STARTUP_SIGNS = ("Syntax theme", "╌╌╌╌", "ctrl+t to disable")


def _domain_for_session(session: str) -> str:
    try:
        with open(ORCHMUX / "workers.yaml") as f:
            data = yaml.safe_load(f)
        for domain, cfg in data.get("workers", {}).items():
            if session in cfg.get("sessions", []):
                return domain
    except Exception:
        pass
    return "research"


def _get_latest_session_id(session: str) -> str | None:
    """Find the most recently modified Claude session ID for this worker."""
    proj_dir = (Path.home() / ".claude" / "projects" /
                f"-home-claude-orchmux-worker-workdirs-{session}")
    if not proj_dir.exists():
        return None
    files = sorted(proj_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    return files[0].stem if files else None


def _save_session_id(session: str, session_id: str):
    (SESSION_ID_DIR / f"{session}.txt").write_text(session_id)


def _load_session_id(session: str) -> str | None:
    f = SESSION_ID_DIR / f"{session}.txt"
    return f.read_text().strip() if f.exists() else None


def _graceful_exit(session: str, timeout: float = 6.0):
    """Ask Claude to exit gracefully; falls back to kill if it doesn't comply."""
    # Cancel any in-progress action first
    subprocess.run(["tmux", "send-keys", "-t", session, "Escape", ""],
                   capture_output=True)
    time.sleep(0.3)
    subprocess.run(["tmux", "send-keys", "-t", session, "/exit", "Enter"],
                   capture_output=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        pane = capture_pane(session, 5)
        lines = [l.strip() for l in pane.splitlines() if l.strip()]
        last = lines[-1] if lines else ""
        # Shell prompt means Claude exited cleanly
        if last.startswith("$") or last.startswith("#") or last == "":
            return
    # Didn't exit cleanly in time — kill will follow


def _relaunch_worker(session: str):
    """Gracefully exit Claude, save session ID, then resume it on restart."""
    domain = _domain_for_session(session)
    work_dir = ORCHMUX / "worker-workdirs" / session
    work_dir.mkdir(parents=True, exist_ok=True)

    # Save current session ID BEFORE doing anything destructive
    current_id = _get_latest_session_id(session)
    if current_id:
        _save_session_id(session, current_id)
        log(f"[heal] saved session ID for {session}: {current_id}")

    # Try graceful exit first so Claude can finalise its state
    if session_exists(session):
        _graceful_exit(session)

    claude_src = ORCHMUX / "worker" / domain / "CLAUDE.md"
    if claude_src.exists():
        import shutil
        shutil.copy2(str(claude_src), str(work_dir / "CLAUDE.md"))

    subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
    time.sleep(0.5)
    subprocess.run([
        "tmux", "new-session", "-d", "-s", session, "-c", str(work_dir),
        "-e", f"ORCHMUX_SESSION={session}",
        "-e", f"ORCHMUX_WORKER_ID={session}",
        "-e", f"ORCHMUX_DOMAIN={domain}",
        "-e", f"ORCHMUX_QUEUE={QUEUE_DIR}/{session}.yaml",
        "-e", f"ORCHMUX_RESULTS={ORCHMUX}/results/{session}.yaml",
    ], capture_output=True)

    resume_id = _load_session_id(session)
    if resume_id:
        cmd = f"claude --resume {resume_id} --dangerously-skip-permissions"
        log(f"[heal] resuming {session} with prior session {resume_id}")
    else:
        cmd = "claude --dangerously-skip-permissions"
        log(f"[heal] fresh start for {session} (no prior session ID)")

    subprocess.run(["tmux", "send-keys", "-t", session, cmd, "Enter"],
                   capture_output=True)
    _auth_stuck_count.pop(session, None)
    _alerted_blocked.discard(session)


def _notify_supervisor_direct(session: str, event: str, detail: str):
    """Tell the server to queue a supervisor update for direct (non-dispatched) tasks."""
    if event == "started":
        msg = f"[orchmux] 🔄 {session} started direct task: {detail}"
    else:
        msg = f"[orchmux] ✅ {session} finished direct task: {detail}"
    # POST to /notify with channels=[] — server queues it for supervisor only
    orchmux_post("/supervisor-update", {"message": msg})


def sync_worker_status():
    """Sync @status for all workers. Catches direct tasks + blocked states + auto-heals."""
    for session in all_worker_sessions():
        if not session_exists(session):
            log(f"[heal] {session} missing — relaunching")
            _relaunch_worker(session)
            continue

        pane    = capture_pane(session, lines=30)
        if not pane.strip():
            continue

        # Auto-heal: auth stuck (OAuth loop)
        if any(sign in pane for sign in _AUTH_SIGNS):
            count = _auth_stuck_count.get(session, 0) + 1
            _auth_stuck_count[session] = count
            if count >= 3:  # stuck for 3 polls (~15s) → relaunch
                log(f"[heal] {session} auth-stuck for {count} polls — relaunching")
                orchmux_post("/notify", {
                    "message": f"🔄 [{session}] was auth-stuck — auto-relaunched",
                    "channels": ["telegram"]
                })
                _relaunch_worker(session)
            continue
        else:
            _auth_stuck_count.pop(session, None)

        # Auto-nudge: startup animation stuck (╌╌╌ / Syntax theme) → send Enter
        if any(sign in pane for sign in _STARTUP_SIGNS):
            count = _startup_polls.get(session, 0) + 1
            _startup_polls[session] = count
            if count >= 2:  # stuck 2 polls (~10s) → nudge
                subprocess.run(["tmux", "send-keys", "-t", session, "", "Enter"],
                               capture_output=True)
                log(f"[nudge] {session} startup-stuck — sent Enter")
                _startup_polls.pop(session, None)
            continue
        else:
            _startup_polls.pop(session, None)

        current = tmux_get(session, "status")
        state   = pane_state(pane)

        if state == "blocked" and session not in _alerted_blocked:
            tmux_set(session, "status", "blocked")
            # Extract the blocking line for context
            hint = next((l.strip() for l in reversed(pane.splitlines())
                         if l.strip() and not _is_ui_noise(l)), "")[:80]
            tmux_set(session, "current_task", f"BLOCKED: {hint}")
            log(f"[blocked] {session} needs attention: {hint}")
            orchmux_post("/notify", {
                "message": f"⚠️ [{session}] is BLOCKED and needs input:\n{hint}",
                "session": session,
                "channels": ["telegram"]
            })
            _alerted_blocked.add(session)

        elif state == "waiting" and current not in ("waiting",):
            content = [l.strip() for l in pane.splitlines()
                       if l.strip() and not _is_ui_noise(l)
                       and not l.strip().startswith("❯")]
            question = next((l for l in reversed(content) if "?" in l), "")[:120]
            tmux_set(session, "status", "waiting")
            tmux_set(session, "current_task", f"Q: {question}")
            _alerted_blocked.discard(session)
            log(f"[waiting] {session} asked: {question}")
            _notify_supervisor_direct(session, "waiting", question)
            orchmux_post("/notify", {
                "message": f"❓ [{session}] is waiting for your input:\n{question}",
                "session": session,
                "channels": ["telegram"]
            })

        elif state == "busy" and current not in ("busy", "blocked"):
            tmux_set(session, "status", "busy")
            real_lines = [l.strip() for l in pane.splitlines()
                          if l.strip() and not _is_ui_noise(l)
                          and not l.strip().startswith("❯")]
            task_hint = (real_lines[-1] if real_lines else "")[:60]
            tmux_set(session, "current_task", task_hint)
            tmux_set(session, "started_at", str(int(time.time())))
            _alerted_blocked.discard(session)
            log(f"[direct] {session} → busy: {task_hint}")
            # Don't push "started" to supervisor — visible in monitor, not actionable

        elif state == "idle" and current in ("busy", "blocked", "waiting"):
            queue_file = QUEUE_DIR / f"{session}.yaml"
            if not queue_file.exists() or _queue_status(queue_file) != "pending":
                tmux_set(session, "status", "idle")
                tmux_set(session, "current_task", "")
                _alerted_blocked.discard(session)
                log(f"[direct] {session} → idle")
                # Don't push "done" to supervisor — causes feedback loops


def _queue_status(path: Path) -> str:
    try:
        with open(path) as f:
            return yaml.safe_load(f).get("status", "")
    except Exception:
        return ""


_INFRA_SESSIONS = {
    "orchmux-server": {
        "cwd": str(ORCHMUX / "server"),
        "cmd": (
            "while true; do"
            " env ORCHMUX_BIND_HOST=$ORCHMUX_BIND_HOST"
            " TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN"
            " TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID"
            f" {ORCHMUX}/.venv/bin/uvicorn server:app"
            f"  --host ${{ORCHMUX_BIND_HOST:-127.0.0.1}} --port 9889"
            f"  --ssl-certfile {ORCHMUX}/server/cert.pem"
            f"  --ssl-keyfile  {ORCHMUX}/server/key.pem"
            " 2>&1 | tee /tmp/orchmux-server.log;"
            " echo '[orchmux-server] crashed — restarting in 3s...'; sleep 3; done"
        ),
    },
    "orchmux-supervisor": {
        "cwd": str(ORCHMUX / "supervisor"),
        "cmd": "claude --dangerously-skip-permissions",
    },
    "orchmux-telegram": {
        "cwd": str(ORCHMUX),
        "cmd": (
            f"while true; do {ORCHMUX}/.venv/bin/python -u {ORCHMUX}/telegram_bot.py"
            f" 2>&1 | tee -a {ORCHMUX}/logs/orchmux-telegram.log;"
            " echo '[orchmux-telegram] crashed — restarting in 3s...'; sleep 3; done"
        ),
    },
    "orchmux-approver": {
        "cwd": str(Path.home() / ".claude" / "hooks"),
        "cmd": (
            "while true; do /usr/bin/python3 -u"
            f" {Path.home()}/.claude/hooks/telegram_approver.py"
            f" 2>&1 | tee -a {ORCHMUX}/logs/orchmux-approver.log;"
            " echo '[orchmux-approver] crashed — restarting in 3s...'; sleep 3; done"
        ),
    },
    "orchmux-monitor": {
        "cwd": str(ORCHMUX),
        "cmd": f"while true; do bash {ORCHMUX}/monitor-table.sh; sleep 2; done",
    },
}

_infra_dead_count: dict[str, int] = {}
_server_hung_count: int = 0


def _server_is_responsive() -> bool:
    """Return True if the server responds to /health within 4s."""
    try:
        req = urllib.request.Request(f"{SERVER}/health")
        with urllib.request.urlopen(req, timeout=4, context=_SSL_CTX) as r:
            return r.status == 200
    except Exception:
        return False


def _kill_hung_server():
    """Send SIGKILL to the uvicorn process inside the orchmux-server session."""
    r = subprocess.run(
        ["tmux", "capture-pane", "-t", "orchmux-server", "-p", "-S", "-2"],
        capture_output=True, text=True)
    # Extract PID via pgrep — kill the python process in that session
    r2 = subprocess.run(
        ["pgrep", "-f", "server/server.py"],
        capture_output=True, text=True)
    for pid in r2.stdout.strip().splitlines():
        try:
            subprocess.run(["kill", "-9", pid], check=True)
            log(f"[heal-infra] killed hung server pid={pid}")
        except Exception:
            pass


def sync_infra_health():
    """Monitor infra sessions and restart any that are dead or hung."""
    # Load env so relaunched server gets Telegram + bind host
    env_file = Path.home() / ".claude" / "hooks" / ".env"
    env_extra = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env_extra[k.strip()] = v.strip()

    bind = subprocess.run(
        ["ip", "addr", "show", "tailscale0"],
        capture_output=True, text=True)
    import re as _re
    m = _re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", bind.stdout)
    bind_host = m.group(1) if m else "127.0.0.1"

    for name, cfg in _INFRA_SESSIONS.items():
        if session_exists(name):
            _infra_dead_count.pop(name, None)
            continue
        count = _infra_dead_count.get(name, 0) + 1
        _infra_dead_count[name] = count
        if count < 2:  # confirm dead for 2 polls before acting
            continue
        log(f"[heal-infra] {name} dead — relaunching")
        env = {**os.environ, **env_extra, "ORCHMUX_BIND_HOST": bind_host}
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "-c", cfg["cwd"]],
            capture_output=True, env=env)
        subprocess.run(
            ["tmux", "send-keys", "-t", name, cfg["cmd"], "Enter"],
            capture_output=True)
        orchmux_post("/notify", {
            "message": f"🔄 [{name}] was dead — auto-relaunched by watcher",
            "channels": ["telegram"]
        })
        _infra_dead_count.pop(name, None)

    # ── Hung-server check ──────────────────────────────────────────────────
    # The session can be alive but uvicorn deadlocked (accepts TCP, returns nothing).
    # Detect this by probing /health with a short timeout.
    global _server_hung_count
    if session_exists("orchmux-server"):
        if _server_is_responsive():
            _server_hung_count = 0
        else:
            _server_hung_count += 1
            log(f"[heal-infra] server unresponsive (count={_server_hung_count})")
            if _server_hung_count >= 2:  # unresponsive for 2 checks (~60s) → kill
                log("[heal-infra] server hung — killing process, session will auto-restart")
                _kill_hung_server()
                _server_hung_count = 0
                orchmux_post("/notify", {
                    "message": "🔄 [orchmux-server] was hung (no HTTP response) — killed & restarting",
                    "channels": ["telegram"]
                })


_infra_check_tick = 0


def _seed_last_seen():
    """Populate _last_seen with current pane state on startup.
    Prevents false [DONE] triggers from residual output in already-idle panes."""
    for task in pending_tasks():
        session = task.get("session", "")
        if session and session_exists(session):
            output = capture_pane(session, lines=120)
            if output.strip():
                _last_seen[session] = output
                log(f"[direct] {session} → seeded baseline ({len(output)} chars)")


def main():
    global _infra_check_tick
    log("started — polling every 5s")
    _seed_last_seen()
    while True:
        try:
            for task in pending_tasks():
                process_task(task)
            sync_worker_status()
            _infra_check_tick += 1
            if _infra_check_tick % 6 == 0:  # every 30s
                sync_infra_health()
        except Exception as e:
            log(f"error: {e}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
