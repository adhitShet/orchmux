#!/usr/bin/env python3
"""
orchmux Telegram bot — mirrors the dashboard in chat.

Commands
────────
/w  [domain]        workers table (auth + status + last task)
/p  <session>       terminal snapshot (last 30 lines)
/i                  infra status (server/watcher/telegram/supervisor/monitor)
/q                  pending queue
/hist               recent dispatch history (last 10)
/d  <session> <task> dispatch directly to a session
/s                  server health
/h                  help

Free-text → auto-routed dispatch (keyword domain matching)
"""

import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# ── Config ──────────────────────────────────────────────────────────────────
ENV_FILE = Path.home() / ".claude/hooks/.env"
for line in ENV_FILE.read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

if not TOKEN:
    sys.exit("TELEGRAM_BOT_TOKEN not set")

TG = f"https://api.telegram.org/bot{TOKEN}"

def _server_url() -> str:
    from pathlib import Path as _Path
    cert = _Path(__file__).parent / "server" / "cert.pem"
    scheme = "https" if cert.exists() else "http"
    bind = os.environ.get("ORCHMUX_BIND_HOST", "")
    if bind:
        return f"{scheme}://{bind}:9889"
    r = subprocess.run(["ip", "addr", "show", "tailscale0"], capture_output=True, text=True)
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", r.stdout)
    return f"{scheme}://{m.group(1)}:9889" if m else f"{scheme}://127.0.0.1:9889"

SERVER = _server_url()

# ── Domain routing ───────────────────────────────────────────────────────────
# Customize these keywords to match your own domains and worker handles.
DOMAIN_KEYWORDS = {
    "engineering": ["bug fix", "deploy", "code review", "pull request", "pr review",
                    "diff", "merge", "api", "backend", "frontend", "database", "staging",
                    "hotfix", "rollback", "build fail", "ci fail"],
    "support":     ["support", "ticket", "customer", "issue", "help desk",
                    "escalation", "complaint", "refund", "billing"],
    "data":        ["data", "analytics", "pipeline", "report", "metrics",
                    "dashboard", "query", "anomaly", "data audit"],
    "notifications": ["notify", "alert", "broadcast", "scheduled report",
                      "send message", "ping team"],
    "research":    ["research", "search for", "find out", "look up", "investigate",
                    "analyze", "summarize", "compare"],
}

def match_domain(msg: str) -> str:
    ml = msg.lower()
    scores = {d: sum(1 for kw in kws if kw in ml) for d, kws in DOMAIN_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "research"


# ── Telegram helpers ─────────────────────────────────────────────────────────
def tg_post(method, **data):
    payload = json.dumps(data).encode()
    req = urllib.request.Request(f"{TG}/{method}", data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[tg] {method} error: {e}")
        return {}

def tg_get(method, **params):
    url = f"{TG}/{method}?" + "&".join(
        f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    try:
        with urllib.request.urlopen(url, timeout=35) as r:
            return json.loads(r.read())
    except Exception:
        return {}

def send(text: str, chat_id: str = CHAT_ID, parse_mode: str = "HTML"):
    # Telegram HTML: only <b> <i> <code> <pre> <a> allowed
    return tg_post("sendMessage", chat_id=chat_id, text=text,
                   parse_mode=parse_mode, disable_web_page_preview=True)


# ── orchmux API helpers ───────────────────────────────────────────────────────
def api(path: str, method: str = "GET", body=None, timeout: int = 6):
    req = urllib.request.Request(
        f"{SERVER}{path}",
        data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json"} if body else {},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            return json.loads(r.read())
    except urllib.error.URLError:
        return None


# ── Formatters ───────────────────────────────────────────────────────────────

STATUS_ICON = {
    "idle":    "⚪",
    "busy":    "🟠",
    "waiting": "🔵",
    "blocked": "🔴",
    "auth":    "🔐",
    "missing": "💀",
}
AUTH_ICON = {
    "ok":         "✅",
    "loading":    "⏳",
    "auth_error": "🔐",
    "missing":    "—",
}

def fmt_workers(domain_filter: str = "") -> str:
    status = api("/status")
    wdet   = api("/worker-details")
    if not status:
        return "⚠️ orchmux server offline"

    lines = ["<b>Workers</b>  " + datetime.now().strftime("%H:%M:%S"), ""]
    any_shown = False
    for domain, cfg in status.items():
        if domain_filter and domain != domain_filter:
            continue
        for w in cfg.get("workers", []):
            any_shown = True
            s        = w.get("status") or "idle"
            icon     = STATUS_ICON.get(s, "⚪")
            session  = w.get("session", "")
            det      = (wdet or {}).get(session, {})
            auth     = AUTH_ICON.get(det.get("auth", ""), "—")
            last     = (det.get("last_task") or "")[:50]
            lst_st   = det.get("last_task_status", "")
            elapsed  = ""
            el_s     = w.get("elapsed_seconds")
            if el_s:
                m, s2 = divmod(int(el_s), 60)
                elapsed = f" {m}m{s2:02d}s" if m else f" {s2}s"

            lines.append(f"{icon} <code>{session}</code>  {auth}{elapsed}")
            if last:
                st_tag = "✓" if lst_st == "done" else "⏳" if lst_st == "pending" else ""
                lines.append(f"   {st_tag} <i>{last}</i>")
    if not any_shown:
        lines.append("No workers found.")
    return "\n".join(lines)


def fmt_pane(session: str) -> str:
    data = api(f"/pane/{urllib.parse.quote(session)}?lines=35")
    if data is None:
        return "⚠️ Server offline"
    if not data.get("exists"):
        return f"❌ Session <code>{session}</code> not found"

    raw = (data.get("output") or "").strip()
    # Keep last 30 non-empty lines
    lines = [l for l in raw.splitlines() if l.strip()][-30:]
    trimmed = "\n".join(lines)
    if not trimmed:
        return f"📟 <code>{session}</code> — empty pane"
    # Escape HTML inside <pre>
    escaped = trimmed.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"📟 <code>{session}</code>\n<pre>{escaped}</pre>"


def fmt_infra() -> str:
    data = api("/infra-status")
    if data is None:
        return "⚠️ Server offline"
    icons = {True: "✅", False: "❌"}
    lines = ["<b>Infra</b>"]
    for name, alive in data.items():
        lines.append(f"  {icons[bool(alive)]} {name}")
    return "\n".join(lines)


def fmt_queue() -> str:
    data = api("/queue")
    if data is None:
        return "⚠️ Server offline"
    if not data:
        return "✅ Queue empty"
    lines = ["<b>Pending queue</b>"]
    for domain, tasks in data.items():
        for t in tasks:
            lines.append(f"  <code>[{domain}]</code> {(t.get('task') or '')[:60]}")
    return "\n".join(lines)


def fmt_history() -> str:
    data = api("/dispatch-history")
    if data is None:
        return "⚠️ Server offline"
    if not data:
        return "No dispatch history yet."
    lines = ["<b>Recent dispatches</b>"]
    for h in data[:10]:
        session = h.get("session", "")
        sess_str = f" → <code>{session}</code>" if session and session != "queued" else ""
        lines.append(
            f"  <code>[{h.get('domain','?')}]</code>{sess_str}  {h.get('at','')}\n"
            f"  <i>{(h.get('task') or '')[:80]}</i>"
        )
    return "\n".join(lines)


def do_dispatch(task: str, domain: str, session: str = "") -> str:
    body = {"domain": domain, "task": task, "priority": "normal"}
    if session:
        body["session"] = session
    result = api("/dispatch", method="POST", body=body)
    if result and result.get("task_id"):
        tid     = result["task_id"]
        worker  = result.get("session", "queued")
        status  = result.get("status", "")
        wtype   = result.get("worker_type", "")
        icon    = "⚡" if wtype == "temp" else "🚀" if status == "dispatched" else "📋"
        return (f"{icon} Dispatched\n"
                f"<code>{tid}</code>\n"
                f"domain: {domain}  worker: <code>{worker}</code>")
    return f"⚠️ Dispatch failed (domain={domain})"


# ── Help text ─────────────────────────────────────────────────────────────────
HELP = """\
<b>orchmux commands</b>

<b>View</b>
/w [domain]   — workers (status · auth · last task)
/p &lt;session&gt;  — terminal snapshot
/i            — infra health
/q            — pending queue
/hist         — last 10 dispatches
/s            — server health

<b>Dispatch</b>
/d &lt;session&gt; &lt;task&gt;  — send task to specific worker
/d &lt;task&gt;            — auto-route by domain keyword

<b>Free text</b>  — auto-routed as a task (same as /d)

<b>Examples</b>
<code>/p eng-worker-1</code>
<code>/w engineering</code>
<code>/d eng-worker-2 run tests and report results</code>
"""


# ── Command handler ───────────────────────────────────────────────────────────
def handle(msg: dict):
    text    = msg.get("text", "").strip()
    chat_id = str(msg.get("chat", {}).get("id", CHAT_ID))
    if not text:
        return

    # /w [domain]
    if re.match(r"^/w\b", text):
        parts  = text.split(None, 1)
        domain = parts[1].strip() if len(parts) > 1 else ""
        send(fmt_workers(domain), chat_id=chat_id)
        return

    # /p <session>
    if re.match(r"^/p\b", text):
        parts = text.split(None, 1)
        if len(parts) < 2:
            send("Usage: /p <session>", chat_id=chat_id)
            return
        send(fmt_pane(parts[1].strip()), chat_id=chat_id)
        return

    # /i — infra
    if re.match(r"^/i\b", text):
        send(fmt_infra(), chat_id=chat_id)
        return

    # /q — queue
    if re.match(r"^/q\b", text):
        send(fmt_queue(), chat_id=chat_id)
        return

    # /hist — history
    if re.match(r"^/hist\b", text):
        send(fmt_history(), chat_id=chat_id)
        return

    # /s — status
    if re.match(r"^/s\b", text):
        h = api("/health")
        if h:
            send(f"✅ orchmux online\n"
                 f"workers: {h['workers']}  queued: {h['queued']}  temp: {h['temp_workers']}",
                 chat_id=chat_id)
        else:
            send("⚠️ orchmux server offline", chat_id=chat_id)
        return

    # /h or /help
    if re.match(r"^/(h|help)\b", text):
        send(HELP, chat_id=chat_id)
        return

    # /d <session> <task>  OR  /d <task>  (session = word with no spaces)
    if re.match(r"^/d\b", text):
        rest = text[2:].strip()
        if not rest:
            send("Usage: /d [session] <task>", chat_id=chat_id)
            return
        # Check if first word looks like a session name (contains hyphen or matches known pattern)
        parts   = rest.split(None, 1)
        session = ""
        task    = rest
        if len(parts) == 2 and re.match(r"^[\w-]+-\d+$", parts[0]):
            # looks like eng-worker-1, support-worker-2, etc.
            session = parts[0]
            task    = parts[1]
        domain = match_domain(task)
        send(do_dispatch(task, domain, session), chat_id=chat_id)
        return

    # Legacy aliases
    if re.match(r"^/(monitor|m)\b", text):
        send(fmt_workers(), chat_id=chat_id)
        return
    if re.match(r"^/(queue)\b", text):
        send(fmt_queue(), chat_id=chat_id)
        return
    if re.match(r"^/(status)\b", text):
        h = api("/health")
        send(f"✅ workers:{h['workers']}" if h else "⚠️ offline", chat_id=chat_id)
        return

    # Skip very short acks
    if len(text) < 8 or re.match(r"^(yes|no|ok|okay|sure|thanks|y|n|k)\.?$", text.lower()):
        return

    # Free text → auto-dispatch
    domain = match_domain(text)
    send(do_dispatch(text, domain), chat_id=chat_id)


# ── Poll loop ─────────────────────────────────────────────────────────────────
def main():
    print(f"[orchmux-bot] starting — server={SERVER}, chat={CHAT_ID}")
    offset = 0
    while True:
        try:
            resp = tg_get("getUpdates", offset=offset, timeout=30,
                          allowed_updates="message")
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                if msg:
                    handle(msg)
        except KeyboardInterrupt:
            print("\n[orchmux-bot] stopped")
            break
        except Exception as e:
            print(f"[orchmux-bot] error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
