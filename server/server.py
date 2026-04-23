#!/usr/bin/env python3
"""
orchmux MCP server — localhost:9889
Dispatches tasks to tmux workers with queue + spawn logic.
"""

import asyncio
import json
import os
import random
import threading
import subprocess
import time
import yaml
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).parent.parent
WORKERS_CONFIG = ROOT / "workers.yaml"
QUEUE_DIR = ROOT / "queue"
RESULTS_DIR = ROOT / "results"
LOGS_DIR = ROOT / "logs"
MEMORY_FILE = ROOT / "memory.md"
BIND_HOST = os.environ.get("ORCHMUX_BIND_HOST", "127.0.0.1")
_CERT = Path(__file__).parent / "cert.pem"
_KEY  = Path(__file__).parent / "key.pem"
_SCHEME = "https" if (_CERT.exists() and _KEY.exists()) else "http"

for d in [QUEUE_DIR, RESULTS_DIR, LOGS_DIR]:
    d.mkdir(exist_ok=True)


def load_config():
    with open(WORKERS_CONFIG) as f:
        return yaml.safe_load(f)


# In-memory state
worker_status: dict[str, str] = {}
task_queue: dict[str, list] = defaultdict(list)
active_temp_workers: dict[str, dict] = {}
task_registry: dict[str, dict] = {}
_questions: list[dict] = []        # pending/answered questions from workers
_completed: list[dict] = []        # last 10 completed tasks
_drain_lock = threading.Lock()     # prevents concurrent queue drains
_supervisor_inbox: list[str] = []  # completions queued for supervisor
_notified_task_ids: set[str] = set()  # task_ids already queued for supervisor

# ── Persistent queue + dispatch history ───────────────────────────────────
_QUEUE_FILE   = QUEUE_DIR / "_pending_queue.json"
_HISTORY_FILE = QUEUE_DIR / "_dispatch_history.json"
_META_FILE    = QUEUE_DIR / "_worker_meta.json"
_TODO_FILE    = QUEUE_DIR / "_todos.json"

def _load_worker_meta() -> dict:
    if _META_FILE.exists():
        try: return json.loads(_META_FILE.read_text())
        except Exception: pass
    return {}

def _save_worker_meta(meta: dict):
    try: _META_FILE.write_text(json.dumps(meta, indent=2))
    except Exception: pass

def _save_queue():
    try:
        with open(_QUEUE_FILE, "w") as f:
            json.dump(dict(task_queue), f)
    except Exception:
        pass

def _load_queue():
    if _QUEUE_FILE.exists():
        try:
            data = json.loads(_QUEUE_FILE.read_text())
            for domain, items in data.items():
                task_queue[domain].extend(items)
            total = sum(len(v) for v in task_queue.values())
            if total:
                log(f"[startup] restored {total} queued tasks from disk")
        except Exception:
            pass

def _append_history(task: str, domain: str, session: str):
    history = []
    if _HISTORY_FILE.exists():
        try:
            history = json.loads(_HISTORY_FILE.read_text())
        except Exception:
            pass
    entry = {"task": task, "domain": domain, "session": session,
             "at": datetime.utcnow().strftime("%m-%d %H:%M")}
    # Deduplicate by task text — move to front if already exists
    history = [h for h in history if h["task"] != task]
    history.insert(0, entry)
    history = history[:30]  # keep last 30
    try:
        with open(_HISTORY_FILE, "w") as f:
            json.dump(history, f)
    except Exception:
        pass

app = FastAPI(title="orchmux", version="0.1.0")
_load_queue()

# Serve clean redesign static files at /clean (jsx + html)
_CLEAN_DIR = ROOT / "clean"
if _CLEAN_DIR.exists():
    app.mount("/clean", StaticFiles(directory=str(_CLEAN_DIR)), name="clean-static")


# ── Models ─────────────────────────────────────────────────────────────────
class DispatchRequest(BaseModel):
    domain: str = ""
    task: str
    context: Optional[str] = None
    priority: str = "normal"
    session: Optional[str] = None   # if set, dispatch directly to this session
    force: bool = False              # bypass busy check for direct dispatch

class CompleteRequest(BaseModel):
    session: str
    task_id: str
    result: str
    success: bool = True

class NotifyRequest(BaseModel):
    message: str
    session: Optional[str] = None
    channels: list[str] = ["telegram"]


# ── tmux helpers ───────────────────────────────────────────────────────────
def tmux(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux"] + args, capture_output=True, text=True)

def session_exists(session: str) -> bool:
    return tmux(["has-session", "-t", session]).returncode == 0

def get_opt(session: str, key: str) -> str:
    # Read from pane scope (where set_opt writes)
    r = tmux(["show-options", "-t", session, "-pv", f"@{key}"])
    return r.stdout.strip()

def set_opt(session: str, key: str, value: str):
    tmux(["set-option", "-t", session, "-p", f"@{key}", value])

def send_keys(session: str, text: str):
    tmux(["send-keys", "-t", session, text])
    time.sleep(0.3)  # let Claude Code accept the full input before Enter
    tmux(["send-keys", "-t", session, "Enter"])

def log(msg: str):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} {msg}"
    print(line)
    log_file = LOGS_DIR / f"{datetime.utcnow().strftime('%Y-%m-%d')}.log"
    with open(log_file, "a") as f:
        f.write(line + "\n")


# ── Worker selection ───────────────────────────────────────────────────────
def find_idle_worker(domain_cfg: dict) -> Optional[str]:
    for session in domain_cfg.get("sessions", []):
        if not session_exists(session):
            continue
        if get_opt(session, "status") in ("idle", ""):
            return session
    return None

def queue_depth(domain: str) -> int:
    return len(task_queue[domain])

def make_task_id(domain: str) -> str:
    return f"{domain}-{int(time.time() * 1000)}-{random.randint(100, 999)}"


# ── Wait for Claude Code prompt ────────────────────────────────────────────
_BUSY_MARKERS = ("esc to interrupt",)   # checked against raw pane (reliable active signal)
_NOISE_MARKERS = ("bypass permissions", "shift+tab", "⏵⏵", "Claude Code", "Sonnet", "Opus")

async def _wait_for_prompt(session: str, timeout: int = 30):
    """Wait until Claude Code's ❯ is the last non-noise line and Claude isn't running."""
    for _ in range(timeout * 2):
        r = tmux(["capture-pane", "-t", session, "-p", "-S", "-8"])
        raw = r.stdout
        # If esc to interrupt is present, Claude is definitely running
        if any(m in raw for m in _BUSY_MARKERS):
            await asyncio.sleep(0.5)
            continue
        # Filter status bar / UI noise before checking for prompt
        lines = [l for l in raw.splitlines()
                 if l.strip() and not any(n in l for n in _NOISE_MARKERS)
                 and not all(c in "─━═ " for c in l.strip())]
        last = lines[-1].strip() if lines else ""
        if last.startswith("❯"):
            await asyncio.sleep(0.3)  # brief settle
            return
        await asyncio.sleep(0.5)
    log(f"[warn] {session} prompt wait timed out, sending anyway")


async def _send_task(session: str, text: str, task_id: str = "") -> bool:
    """Type text then send Enter. Returns True if sent, False if aborted."""
    await _wait_for_prompt(session)
    # Abort if task was already completed while we were waiting for the prompt
    if task_id and task_registry.get(task_id, {}).get("status") == "done":
        log(f"[abort] {task_id} already completed before keys sent — skipping")
        if task_id in task_registry:
            task_registry[task_id]["status"] = "aborted"
        return False
    # load-buffer reads from stdin — no arg length limit (set-buffer fails on long text)
    # paste twice: Claude Code shows "paste again to expand" for large pastes
    import subprocess as _sp
    _sp.run(["tmux", "load-buffer", "-"], input=text, text=True, capture_output=True)
    tmux(["paste-buffer", "-t", session])
    if len(text) > 400:
        await asyncio.sleep(0.9)
        _sp.run(["tmux", "load-buffer", "-"], input=text, text=True, capture_output=True)
        tmux(["paste-buffer", "-t", session])
    await asyncio.sleep(0.5)
    tmux(["send-keys", "-t", session, "Enter"])
    log(f"[keys] sent to {session}")
    return True


# ── Smart context injection ────────────────────────────────────────────────
# ── Service credential snippets — loaded from gitignored service-context.yaml ─
def _load_svc_snippets() -> dict[str, str]:
    """Load per-service context snippets from server/service-context.yaml.
    File is gitignored so credentials never land in the public repo.
    Returns empty dict if file is absent (safe default for fresh installs)."""
    cfg = Path(__file__).parent / "service-context.yaml"
    if not cfg.exists():
        return {}
    try:
        with open(cfg) as f:
            data = yaml.safe_load(f)
        return {k: str(v).strip() for k, v in (data or {}).items() if v}
    except Exception:
        return {}

_SVC_SNIPPETS: dict[str, str] = _load_svc_snippets()

# keyword triggers → snippet keys
_SVC_RULES: list[tuple[list[str], list[str]]] = [
    (["metabase", "q191", "q192", "q192", "q192", "q192", "dashboard", "card/",
      "data query", "fetch data", "bdr", "drr", "demand", "inventory data",
      "snowflake query", ],
     ["metabase"]),
    (["snowflake", "snowsql"],
     ["snowflake"]),
    (["amazon", "sp-api", "spapi", "seller central", "buy box", "asin",
      "listing", "vendor central", "marketplace", "fba"],
     ["amazon"]),
    (["zoho", "zohobooks", "zohoapis", "invoice", "bill", "zoho books"],
     ["zoho"]),
    (["slack", "channel", "notify", " dm ", "broadcast", "message to", "slack_"],
     ["slack"]),
    (["gws", "google doc", "google sheet", "gdoc", "gsheet", "docs.google",
      "drive.google", "google workspace", "sheets", "batchupdate"],
     ["gws"]),
    (["git", "commit", "branch", "pull request", " pr ", "merge", "push", "diff"],
     ["git"]),
    (["postgres", " sql ", "psql", "migration", "select ", "pg_", "database"],
     ["postgres"]),
    (["deploy", "staging", "production", "uvicorn", "restart server", "promote"],
     ["deploy"]),
    (["aws", " ec2 ", " s3 ", " rds ", " eks ", "cloudwatch", "kubectl"],
     ["aws"]),
    (["nvidia", "nim", "minimaxai", "build.nvidia", "nvapi", "nvidia api"],
     ["nvidia"]),
]


def _smart_context(task_text: str, memory_content: str) -> str:
    """Return only memory sections + service credential snippets relevant to this task."""
    task_lo = task_text.lower()

    # ── Part 1: inject hard-coded service credential snippets ────────────────
    svc_parts: list[str] = []
    seen_snippets: set[str] = set()
    for task_kws, snippet_keys in _SVC_RULES:
        if any(kw in task_lo for kw in task_kws):
            for key in snippet_keys:
                if key not in seen_snippets and key in _SVC_SNIPPETS:
                    svc_parts.append(_SVC_SNIPPETS[key])
                    seen_snippets.add(key)

    # ── Part 2: pull matching sections from live memory.md ───────────────────
    mem_parts: list[str] = []
    if memory_content.strip():
        lines = memory_content.splitlines()
        section_starts = [i for i, l in enumerate(lines) if l.startswith("## ")]
        if section_starts:
            sections = []
            for j, start in enumerate(section_starts):
                end = section_starts[j + 1] if j + 1 < len(section_starts) else len(lines)
                header = lines[start][3:].lower()
                body = "\n".join(lines[start:end]).strip()
                sections.append((header, body))

            preamble = "\n".join(lines[: section_starts[0]]).strip()
            MEM_RULES = [
                (["slack", "channel", "notify"], ["slack", "tool"]),
                (["gws", "google doc", "google sheet", "gdoc"], ["gws", "google"]),
                (["git", "commit", "branch", " pr "], ["git", "deploy"]),
                (["postgres", " sql ", "psql"], ["postgres", "database"]),
                (["deploy", "staging", "production"], ["deploy", "infra"]),
                (["aws", " ec2 ", " rds ", " eks "], ["aws", "infra"]),
                (["metabase", "snowflake", "data query", "fetch data"], ["metabase", "data"]),
                (["amazon", "sp-api", "seller", "asin"], ["amazon", "spapi"]),
                (["zoho", "invoice", "bill"], ["zoho"]),
            ]
            included: set[int] = set()
            for task_kws, sec_hints in MEM_RULES:
                if any(kw in task_lo for kw in task_kws):
                    for i, (header, _) in enumerate(sections):
                        if any(hint in header for hint in sec_hints):
                            included.add(i)
            if included:
                if preamble:
                    mem_parts.append(preamble)
                mem_parts += [sections[i][1] for i in sorted(included)]

    all_parts = svc_parts + mem_parts
    return "\n\n".join(all_parts) if all_parts else ""


# ── Dispatch to a session ──────────────────────────────────────────────────
async def dispatch_to(session: str, task_id: str, task: str,
                      context: Optional[str], domain: str):
    # Capture pane state BEFORE dispatch — watcher uses this to skip stale [DONE]
    pre_pane = tmux(["capture-pane", "-t", session, "-p", "-S", "-80"]).stdout
    payload = {
        "task_id": task_id, "domain": domain, "task": task,
        "context": context or "", "status": "pending",
        "session": session,
        "dispatched_at": datetime.utcnow().isoformat(),
        "report_to": f"{_SCHEME}://{BIND_HOST}:9889/complete",
        "pane_baseline": pre_pane[-2000:] if pre_pane else "",  # last 2KB of pre-dispatch output
    }
    task_file = QUEUE_DIR / f"{session}.yaml"
    with open(task_file, "w") as f:
        yaml.dump(payload, f, default_flow_style=False)

    set_opt(session, "status", "busy")
    set_opt(session, "current_task", task[:80])
    set_opt(session, "task_id", task_id)
    set_opt(session, "started_at", str(int(time.time())))
    worker_status[session] = "busy"

    task_registry[task_id] = {
        "task_id": task_id, "domain": domain, "session": session,
        "task": task, "status": "running",
        "dispatched_at": datetime.utcnow().isoformat()
    }

    # Escape lines starting with / so Claude Code doesn't interpret them as slash commands
    safe_task = "\n".join(
        (" " + line if line.startswith("/") else line)
        for line in task.splitlines()
    )
    memory = MEMORY_FILE.read_text().strip() if MEMORY_FILE.exists() else ""
    relevant = _smart_context(task, memory)
    memory_block = f"[GLOBAL CONTEXT — applies to this task]\n{relevant}\n\n" if relevant else ""
    prompt = (
        f"{memory_block}TASK ({task_id}): {safe_task}\n\n"
        f"When finished, write a one-sentence summary of what you completed, then on the very next line write exactly: [DONE]"
    )
    # Send keys in background — don't block the HTTP response
    async def _bg_send():
        sent = await _send_task(session, prompt, task_id=task_id)
        if sent:
            log(f"[dispatch] {task_id} → {session}")
            _append_history(task, domain, session)
    asyncio.create_task(_bg_send())


# ── Spawn temp worker ──────────────────────────────────────────────────────
def spawn_temp(session_name: str, domain: str, model: str) -> bool:
    config = load_config()
    spawn_cfg = config.get("spawn_config", {}).get(model, {})
    command = spawn_cfg.get("command", "claude --dangerously-skip-permissions")

    # Use worker-workdirs for cwd (CLAUDE.md auto-discovered); no CLAUDE_CONFIG_DIR (avoids re-login)
    work_dir = ROOT / "worker-workdirs" / session_name
    if not work_dir.exists():
        work_dir.mkdir(parents=True)
        worker_md = ROOT / "worker" / domain / "CLAUDE.md"
        if worker_md.exists():
            import shutil
            shutil.copy2(str(worker_md), str(work_dir / "CLAUDE.md"))

    try:
        tmux(["new-session", "-d", "-s", session_name,
              "-c", str(work_dir),
              "-e", f"ORCHMUX_SESSION={session_name}",
              "-e", f"ORCHMUX_WORKER_ID={session_name}",
              "-e", f"ORCHMUX_DOMAIN={domain}",
              "-e", f"ORCHMUX_QUEUE={QUEUE_DIR}/{session_name}.yaml",
              "-e", f"ORCHMUX_RESULTS={RESULTS_DIR}/{session_name}.yaml"])

        send_keys(session_name, command)
        set_opt(session_name, "worker_id", session_name)
        set_opt(session_name, "worker_domain", domain)
        set_opt(session_name, "worker_type", "temp")
        set_opt(session_name, "status", "idle")
        log(f"[spawn] temp worker {session_name} for {domain}")
        return True
    except Exception as e:
        log(f"[spawn] FAILED {session_name}: {e}")
        return False


# ── /dispatch ──────────────────────────────────────────────────────────────
@app.post("/dispatch")
async def dispatch(req: DispatchRequest):
    config = load_config()
    workers_cfg = config.get("workers", {})

    # Direct-to-session dispatch — resolve domain automatically if not provided
    if req.session:
        if not session_exists(req.session):
            raise HTTPException(404, f"Session {req.session} not found")
        # Auto-resolve domain from config if caller didn't provide one
        if not req.domain:
            for d, dcfg in workers_cfg.items():
                if req.session in dcfg.get("sessions", []):
                    req.domain = d
                    break
            if not req.domain:
                req.domain = req.session  # fallback: use session name as domain
        # Check if worker is busy — block unless force=True
        current_status = worker_status.get(req.session, "idle")
        if current_status == "busy" and not req.force:
            current_task = get_opt(req.session, "current_task") or "unknown task"
            raise HTTPException(409, f"busy: {req.session} is working on '{current_task[:60]}'")
        task_id = make_task_id(req.domain)
        await dispatch_to(req.session, task_id, req.task, req.context, req.domain)
        return {"task_id": task_id, "session": req.session,
                "status": "dispatched", "worker_type": "direct" if not req.force else "force"}

    domain_cfg = workers_cfg.get(req.domain)
    if not domain_cfg:
        raise HTTPException(404, f"Unknown domain: {req.domain}")

    task_id = make_task_id(req.domain)
    strategy = domain_cfg.get("queue_strategy", "queue")
    model = domain_cfg.get("model", "claude")

    protected = config.get("_protected", {})
    for s in domain_cfg.get("sessions", []):
        if s in protected:
            raise HTTPException(400, f"Session {s} is protected")

    idle = find_idle_worker(domain_cfg)

    # Happy path
    if idle:
        await dispatch_to(idle, task_id, req.task, req.context, req.domain)
        return {"task_id": task_id, "session": idle,
                "status": "dispatched", "worker_type": "persistent"}

    # All busy
    if strategy == "spawn":
        temp = f"{req.domain}-temp-{int(time.time())}"
        if spawn_temp(temp, req.domain, model):
            await asyncio.sleep(3)
            await dispatch_to(temp, task_id, req.task, req.context, req.domain)
            ttl = domain_cfg.get("temp_ttl_minutes", 20) * 60
            active_temp_workers[temp] = {
                "domain": req.domain, "started_at": time.time(),
                "task_id": task_id, "ttl": ttl
            }
            return {"task_id": task_id, "session": temp,
                    "status": "dispatched", "worker_type": "temp"}
        raise HTTPException(500, "Failed to spawn temp worker")

    elif strategy == "queue":
        task_queue[req.domain].append({
            "task_id": task_id, "task": req.task,
            "context": req.context, "queued_at": time.time(),
            "priority": req.priority
        })
        task_registry[task_id] = {
            "task_id": task_id, "domain": req.domain, "task": req.task,
            "status": "queued", "queued_at": datetime.utcnow().isoformat()
        }
        _save_queue()
        _append_history(req.task, req.domain, "queued")
        log(f"[queue] {task_id} depth={queue_depth(req.domain)}")
        return {"task_id": task_id, "status": "queued",
                "queue_depth": queue_depth(req.domain)}

    elif strategy == "queue_then_spawn":
        max_depth = domain_cfg.get("max_queue_depth", 2)
        spawn_ok = domain_cfg.get("spawn_allowed", False)

        if queue_depth(req.domain) < max_depth:
            task_queue[req.domain].append({
                "task_id": task_id, "task": req.task,
                "context": req.context, "queued_at": time.time(),
                "priority": req.priority
            })
            task_registry[task_id] = {
                "task_id": task_id, "domain": req.domain, "task": req.task,
                "status": "queued", "queued_at": datetime.utcnow().isoformat()
            }
            _save_queue()
            _append_history(req.task, req.domain, "queued")
            return {"task_id": task_id, "status": "queued",
                    "queue_depth": queue_depth(req.domain)}
        elif spawn_ok:
            temp = f"{req.domain}-temp-{int(time.time())}"
            if spawn_temp(temp, req.domain, model):
                await asyncio.sleep(3)
                await dispatch_to(temp, task_id, req.task, req.context, req.domain)
                ttl = domain_cfg.get("temp_ttl_minutes", 20) * 60
                active_temp_workers[temp] = {
                    "domain": req.domain, "started_at": time.time(),
                    "task_id": task_id, "ttl": ttl
                }
                return {"task_id": task_id, "session": temp,
                        "status": "dispatched", "worker_type": "temp"}
        raise HTTPException(503, f"All {req.domain} workers busy, queue full")

    raise HTTPException(400, f"Unknown strategy: {strategy}")


# ── /complete ──────────────────────────────────────────────────────────────
@app.post("/complete")
async def complete(req: CompleteRequest):
    # Mark done FIRST (synchronous, no await) — eliminates race with _send_task abort check
    already_done = task_registry.get(req.task_id, {}).get("status") == "done"
    if not already_done:
        if req.task_id not in task_registry:
            task_registry[req.task_id] = {"task_id": req.task_id}
        task_registry[req.task_id].update({"status": "done", "result": req.result})

    result_file = RESULTS_DIR / f"{req.session}.yaml"
    with open(result_file, "w") as f:
        yaml.dump({
            "task_id": req.task_id, "session": req.session,
            "result": req.result, "success": req.success,
            "completed_at": datetime.utcnow().isoformat()
        }, f)

    # Update queue YAML status so worker-stop hook sees it as done
    queue_file = QUEUE_DIR / f"{req.session}.yaml"
    if queue_file.exists():
        try:
            with open(queue_file) as f:
                q = yaml.safe_load(f) or {}
            if q.get("task_id") == req.task_id:
                q["status"] = "done"
                with open(queue_file, "w") as f:
                    yaml.dump(q, f, default_flow_style=False)
        except Exception:
            pass

    if not already_done:
        _completed.insert(0, {
            "task_id": req.task_id,
            "session": req.session,
            "domain": task_registry.get(req.task_id, {}).get("domain", "?"),
            "task": task_registry.get(req.task_id, {}).get("task", "")[:60],
            "result": req.result[:120],
            "success": req.success,
            "completed_at": datetime.utcnow().strftime("%H:%M:%S")
        })
        if len(_completed) > 10:
            _completed.pop()

    if already_done:
        # Still reset worker_status in case server restarted with stale memory
        if worker_status.get(req.session) == "busy":
            worker_status[req.session] = "idle"
            set_opt(req.session, "status", "idle")
            set_opt(req.session, "current_task", "")
        return {"status": "ok", "note": "already completed"}

    set_opt(req.session, "status", "idle")
    set_opt(req.session, "current_task", "")
    set_opt(req.session, "task_id", "")
    worker_status[req.session] = "idle"
    log(f"[complete] {req.task_id} ← {req.session} ({'ok' if req.success else 'FAIL'})")

    # Drain queue — lock prevents concurrent /complete calls double-popping
    domain = task_registry.get(req.task_id, {}).get("domain")
    next_task = None
    if domain:
        with _drain_lock:
            # Skip any queued entries that match the completing task (self-dispatch guard)
            while task_queue[domain] and task_queue[domain][0]["task_id"] == req.task_id:
                task_queue[domain].pop(0)
            if task_queue[domain]:
                next_task = task_queue[domain].pop(0)
                _save_queue()
    if next_task:
        log(f"[drain] {next_task['task_id']} → {req.session}")
        await dispatch_to(req.session, next_task["task_id"], next_task["task"],
                          next_task.get("context"), domain)

    await _notify_completion(req.task_id, req.result, req.success)

    if req.session in active_temp_workers:
        ttl = active_temp_workers[req.session]["ttl"]
        asyncio.create_task(_cleanup_temp(req.session, ttl))

    return {"status": "ok"}


# ── /status ────────────────────────────────────────────────────────────────
@app.get("/status")
async def status():
    config = load_config()
    out = {}
    for domain, cfg in config.get("workers", {}).items():
        if domain.startswith("_"):
            continue
        workers_list = []
        for session in cfg.get("sessions", []):
            exists = session_exists(session)
            st = (get_opt(session, "status") or "idle") if exists else "offline"
            entry = {
                "session": session, "exists": exists,
                "status": st,
                "current_task": get_opt(session, "current_task"),
                "worker_type": "persistent"
            }
            started = get_opt(session, "started_at")
            if started:
                entry["elapsed_seconds"] = int(time.time()) - int(started)
            if exists and st.lower() == "busy":
                r = tmux(["capture-pane", "-t", session, "-p", "-S", "-80"])
                skip = {"?", ">", "$", "│", "╭", "╰", "●", "◎", "·", "⠋", "⠙", "⠹", "⠸"}
                lines = [l.strip() for l in r.stdout.split("\n")
                         if l.strip() and l.strip()[0] not in skip
                         and not l.strip().startswith(("claude", "Claude", "✓", "✗"))]
                if lines:
                    entry["pane_progress"] = lines[-1][:70]
            workers_list.append(entry)

        for temp, info in active_temp_workers.items():
            if info["domain"] == domain:
                workers_list.append({
                    "session": temp, "exists": session_exists(temp),
                    "status": get_opt(temp, "status") or "idle",
                    "current_task": get_opt(temp, "current_task"),
                    "worker_type": "temp"
                })

        out[domain] = {
            "workers": workers_list,
            "queue_depth": queue_depth(domain),
            "strategy": cfg.get("queue_strategy", "queue"),
            "model": cfg.get("model", "claude")
        }
    return out


@app.get("/queue")
async def queue_view():
    return {d: t for d, t in task_queue.items() if t}


@app.get("/dispatch-history")
async def dispatch_history(token: str = ""):
    if not _check_token(token):
        from fastapi.responses import Response
        return Response(status_code=403)
    if _HISTORY_FILE.exists():
        try:
            return json.loads(_HISTORY_FILE.read_text())
        except Exception:
            pass
    return []


@app.get("/task/{task_id}")
async def get_task(task_id: str):
    if task_id not in task_registry:
        raise HTTPException(404, f"Task {task_id} not found")
    return task_registry[task_id]


@app.post("/notify")
async def notify(req: NotifyRequest):
    q_id = f"q-{int(time.time()*1000)}"
    _questions.append({
        "id": q_id,
        "message": req.message,
        "session": req.session or "",
        "asked_at": datetime.utcnow().strftime("%H:%M:%S"),
        "answered": False,
        "answer": ""
    })
    if len(_questions) > 50:
        _questions.pop(0)
    await _send_notification(req.message, req.channels)
    return {"status": "sent", "question_id": q_id}


@app.post("/answer/{q_id}")
async def answer_question(q_id: str, body: dict):
    for i, q in enumerate(_questions):
        if q["id"] == q_id:
            answer = body.get("answer", "")
            session = q.get("session", "")
            # Send reply to the worker's tmux session
            if answer and session and session_exists(session):
                send_keys(session, answer)
            # Remove from list so it never shows up again
            _questions.pop(i)
            return {"status": "ok", "sent_to": session or None}
    raise HTTPException(404, f"Question {q_id} not found")


@app.delete("/questions/{q_id}")
async def dismiss_question(q_id: str, token: str = ""):
    global _questions
    _questions = [q for q in _questions if q["id"] != q_id]
    return {"status": "dismissed"}


@app.get("/questions")
async def get_questions():
    # Only return pending (unanswered) questions; answered are removed on answer
    pending = [q for q in _questions if not q.get("answered")]
    return {"pending": pending, "answered": []}


@app.get("/completed")
async def get_completed():
    if not _completed:
        _load_results_from_disk()
    return _completed[:8]


def _load_results_from_disk():
    """Populate _completed from result files on disk (used after restart)."""
    loaded = []
    for f in sorted(RESULTS_DIR.glob("*.yaml"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
        try:
            d = yaml.safe_load(f.read_text())
            if d and d.get("task_id"):
                loaded.append({
                    "task_id":     d.get("task_id", ""),
                    "session":     d.get("session", f.stem),
                    "domain":      d.get("task_id", "").split("-")[0] if d.get("task_id") else "",
                    "result":      (d.get("result") or "")[:300],
                    "success":     d.get("success", True),
                    "completed_at": d.get("completed_at", ""),
                })
        except Exception:
            pass
    _completed.extend(loaded)


class SupervisorUpdateRequest(BaseModel):
    message: str

@app.post("/supervisor-update")
async def supervisor_update(req: SupervisorUpdateRequest):
    """Queue a message for the supervisor inbox (no Telegram)."""
    _supervisor_inbox.append(req.message)
    return {"status": "queued"}


@app.get("/health")
async def health():
    return {"status": "ok", "workers": len(worker_status),
            "queued": sum(len(v) for v in task_queue.values()),
            "temp_workers": len(active_temp_workers)}


_INFRA_SESSIONS = [
    ("orchmux-server",     "server",     "uvicorn / FastAPI"),
    ("orchmux-supervisor", "supervisor", "task supervisor"),
    ("orchmux-watcher",    "watcher",    "completion watcher"),
    ("orchmux-telegram",   "telegram",   "telegram bot"),
    ("orchmux-monitor",    "monitor",    "dashboard monitor"),
]

@app.get("/infra-status")
async def infra_status(token: str = ""):
    if not _check_token(token):
        from fastapi.responses import Response
        return Response(status_code=403)
    result = []
    for session, name, description in _INFRA_SESSIONS:
        exists = session_exists(session)
        pane = ""
        if exists:
            r = tmux(["capture-pane", "-t", session, "-p", "-S", "-5"])
            lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
            pane = lines[-1][:120] if lines else ""
        result.append({
            "session": session,
            "name": name,
            "description": description,
            "up": exists,
            "last_line": pane,
        })
    return result


VAULT_ROOT = Path.home() / "obsidian-vault"
VAULT_NAME = "obsidian-vault"  # must match the vault name in Obsidian app

@app.get("/vault/ls")
async def vault_ls(path: str = "", token: str = ""):
    if not _check_token(token):
        from fastapi.responses import Response
        return Response(status_code=403)
    base = (VAULT_ROOT / path).resolve()
    if not str(base).startswith(str(VAULT_ROOT)):
        raise HTTPException(400, "Path outside vault")
    if not base.exists():
        raise HTTPException(404, "Not found")
    entries = []
    for p in sorted(base.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if p.name.startswith('.') or p.name == '_Attachments' or p.suffix in ('.png','.jpg','.jpeg','.gif','.svg','.webp','.pdf','.excalidraw'):
            continue
        entries.append({
            "name": p.name,
            "path": str(p.relative_to(VAULT_ROOT)),
            "is_dir": p.is_dir(),
            "mtime": int(p.stat().st_mtime),
        })
    return entries

@app.get("/vault/read")
async def vault_read(path: str, token: str = ""):
    if not _check_token(token):
        from fastapi.responses import Response
        return Response(status_code=403)
    target = (VAULT_ROOT / path).resolve()
    if not str(target).startswith(str(VAULT_ROOT)):
        raise HTTPException(400, "Path outside vault")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "File not found")
    return {"content": target.read_text(errors='replace'), "path": path, "name": target.name}


@app.post("/vault/write")
async def vault_write(req: Request, token: str = ""):
    if not _check_token(token):
        from fastapi.responses import Response
        return Response(status_code=403)
    body = await req.json()
    path, content = body.get("path",""), body.get("content","")
    target = (VAULT_ROOT / path).resolve()
    if not str(target).startswith(str(VAULT_ROOT)):
        raise HTTPException(400, "Path outside vault")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return {"ok": True, "path": path, "bytes": len(content)}


@app.post("/vault/export-doc")
async def vault_export_doc(req: Request, token: str = ""):
    """Export a vault file to Google Docs via gws CLI. Returns the doc URL."""
    if not _check_token(token):
        from fastapi.responses import Response
        return Response(status_code=403)
    body = await req.json()
    path = body.get("path", "")
    target = (VAULT_ROOT / path).resolve()
    if not str(target).startswith(str(VAULT_ROOT)):
        raise HTTPException(400, "Path outside vault")
    if not target.exists():
        raise HTTPException(404, "File not found")
    title = target.stem
    content = target.read_text()
    try:
        # Create blank doc
        create_r = subprocess.run(
            ["gws", "docs", "documents", "create", "--json", json.dumps({"title": title})],
            capture_output=True, text=True, timeout=30
        )
        if create_r.returncode != 0:
            raise HTTPException(500, "gws create failed: " + (create_r.stderr or create_r.stdout)[:300])
        doc_data = json.loads(create_r.stdout)
        doc_id = doc_data.get("documentId")
        if not doc_id:
            raise HTTPException(500, "No documentId in response")
        # Append content as plain text
        write_r = subprocess.run(
            ["gws", "docs", "+write", "--document", doc_id, "--text", content],
            capture_output=True, text=True, timeout=30
        )
        if write_r.returncode != 0:
            raise HTTPException(500, "gws write failed: " + (write_r.stderr or write_r.stdout)[:300])
        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
        return {"ok": True, "url": doc_url, "doc_id": doc_id, "title": title}
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "gws timed out")
    except FileNotFoundError:
        raise HTTPException(500, "gws CLI not found")
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"gws response parse error: {e}")


@app.get("/todos")
async def get_todos(token: str = ""):
    if not _check_token(token):
        from fastapi.responses import Response
        return Response(status_code=403)
    if _TODO_FILE.exists():
        try: return json.loads(_TODO_FILE.read_text())
        except Exception: pass
    return []

@app.post("/todos")
async def save_todos(req: Request, token: str = ""):
    if not _check_token(token):
        from fastapi.responses import Response
        return Response(status_code=403)
    items = await req.json()
    _TODO_FILE.write_text(json.dumps(items))
    return {"ok": True}


@app.get("/vault/sessions")
async def vault_sessions(worker: str = "", token: str = ""):
    """Return session notes that belong to a given orchmux worker session."""
    if not _check_token(token):
        from fastapi.responses import Response
        return Response(status_code=403)
    sessions_dir = VAULT_ROOT / "AI-Systems" / "Claude-Logs" / "Sessions"
    if not sessions_dir.exists():
        return []
    results = []
    for f in sorted(sessions_dir.glob("*.md"), reverse=True)[:60]:
        try:
            text = f.read_text(errors='replace')
        except Exception:
            continue
        rel = str(f.relative_to(VAULT_ROOT))
        # Always include if worker name appears in filename or frontmatter cwd
        name_match = worker and worker in f.name
        cwd_match  = worker and f"/{worker}" in text[:400]
        topic_match = worker and worker in text[:400]
        if worker and not (name_match or cwd_match or topic_match):
            continue
        # Snippet: first non-frontmatter, non-empty line
        lines = text.split('\n')
        snippet = ''
        in_fm = lines[0].strip() == '---'
        fm_closed = not in_fm
        for line in lines[1:]:
            if in_fm and line.strip() == '---':
                fm_closed = True; in_fm = False; continue
            if in_fm:
                continue
            if fm_closed and line.strip():
                snippet = line.strip().lstrip('#').strip()[:120]
                break
        results.append({"path": rel, "name": f.name, "snippet": snippet, "mtime": int(f.stat().st_mtime)})
    return results


@app.get("/memory")
async def get_memory(token: str = ""):
    if not _check_token(token):
        from fastapi.responses import Response
        return Response(status_code=403)
    return {"content": MEMORY_FILE.read_text() if MEMORY_FILE.exists() else ""}


@app.post("/memory")
async def save_memory(req: Request, token: str = ""):
    if not _check_token(token):
        from fastapi.responses import Response
        return Response(status_code=403)
    body = await req.json()
    content = body.get("content", "")
    MEMORY_FILE.write_text(content)
    return {"ok": True, "bytes": len(content)}


_DASHBOARD_TOKEN = os.environ.get("ORCHMUX_DASHBOARD_TOKEN", "")


def _check_token(token: str = "") -> bool:
    if not _DASHBOARD_TOKEN:
        return True  # no token configured → allow (localhost only)
    return token == _DASHBOARD_TOKEN


def _clean_result(raw: str) -> str:
    """Extract [DONE] summary if present; otherwise strip tmux pane noise."""
    if not raw:
        return ""
    # Prefer [DONE] summary line
    for line in reversed(raw.splitlines()):
        stripped = line.strip()
        if stripped.startswith("[DONE]"):
            return stripped
    # Strip tmux UI noise
    noise = ("bypass permissions", "shift+tab", "⏵⏵", "ctrl+t", "esc to interrupt",
             "? for shortcuts", "[timeout after", "✢", "✽", "✻", "✺", "⏵", "Brewing",
             "Whirring", "Fermenting", "Baking", "Brewing")
    lines = [l for l in raw.splitlines()
             if l.strip()
             and not any(n in l for n in noise)
             and not all(c in "─━═ │┃►◄" for c in l.strip())
             and len(l.strip()) > 2]
    cleaned = "\n".join(lines).strip()
    # If result is suspiciously short or looks like yaml internals, discard
    if len(cleaned) < 20 or cleaned.startswith("task_id:") or cleaned.startswith("session:"):
        return ""
    return cleaned


@app.get("/results")
async def all_results(token: str = "", limit: int = 50):
    if not _check_token(token):
        from fastapi.responses import Response
        return Response(status_code=403)
    # Build index of queue YAMLs for task text + domain lookup
    queue_index = {}
    for f in QUEUE_DIR.glob("*.yaml"):
        try:
            q = yaml.safe_load(f.read_text()) or {}
            if q.get("task_id"):
                queue_index[q["task_id"]] = q
            # Also index by session name (fallback)
            if q.get("session"):
                queue_index.setdefault(q["session"], q)
        except Exception:
            pass

    items = []
    for f in sorted(RESULTS_DIR.glob("*.yaml"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            r = yaml.safe_load(f.read_text()) or {}
            task_id = r.get("task_id", "")
            session = r.get("session", f.stem)
            q = queue_index.get(task_id) or queue_index.get(session) or {}
            raw_result = r.get("result") or ""
            cleaned = _clean_result(raw_result)
            items.append({
                "session": session,
                "domain": q.get("domain", ""),
                "task": (q.get("task") or ""),
                "result": cleaned,
                "result_raw": raw_result[:2000],
                "completed_at": r.get("completed_at", ""),
                "success": r.get("success", True),
            })
        except Exception:
            pass
        if len(items) >= limit:
            break
    return items


_pipe_pane_active: set = set()
LOG_MAX_LINES = 5000

def ensure_pipe_pane(session: str):
    """Enable tmux pipe-pane logging once per session lifetime."""
    if session in _pipe_pane_active:
        return
    log_file = LOGS_DIR / f"{session}.log"
    r = subprocess.run(
        ["tmux", "pipe-pane", "-t", session, "-o", f"cat >> {log_file}"],
        capture_output=True
    )
    if r.returncode == 0:
        _pipe_pane_active.add(session)


@app.get("/pane/{session}")
async def pane_output(session: str, token: str = "", lines: int = 60):
    if not _check_token(token):
        from fastapi.responses import Response
        return Response(status_code=403)
    r = tmux(["capture-pane", "-t", session, "-p", "-S", f"-{lines}"])
    if r.returncode != 0:
        return {"session": session, "output": "", "exists": False}
    ensure_pipe_pane(session)
    return {"session": session, "output": r.stdout, "exists": True}


@app.get("/pane-log/{session}")
async def pane_log(session: str, token: str = "", skip: int = 0, lines: int = 100):
    if not _check_token(token):
        from fastapi.responses import Response
        return Response(status_code=403)
    log_file = LOGS_DIR / f"{session}.log"
    if not log_file.exists():
        return {"lines": [], "total": 0, "has_more": False}
    all_lines = log_file.read_text(errors="replace").splitlines()
    total = len(all_lines)
    # Rotate: keep last LOG_MAX_LINES
    if total > LOG_MAX_LINES:
        all_lines = all_lines[-LOG_MAX_LINES:]
        log_file.write_text("\n".join(all_lines) + "\n")
        total = LOG_MAX_LINES
    # Paginate from end: skip=0 → last N, skip=100 → 100-200 from end
    end = total - skip
    if end <= 0:
        return {"lines": [], "total": total, "has_more": False}
    start = max(0, end - lines)
    chunk = all_lines[start:end]
    return {"lines": chunk, "total": total, "has_more": start > 0, "skip_next": skip + len(chunk)}


class SpawnRequest(BaseModel):
    session: str
    domain: str
    model: str = "claude"

@app.post("/spawn-worker")
async def spawn_worker_endpoint(req: SpawnRequest, token: str = ""):
    if not _check_token(token):
        raise HTTPException(403, "Unauthorized")
    config = load_config()
    protected = set(config.get("_protected", {}).keys())
    if req.session in protected or req.session.startswith("orchmux-"):
        raise HTTPException(400, f"Session name '{req.session}' is reserved")
    if session_exists(req.session):
        raise HTTPException(409, f"Session '{req.session}' already exists")
    if req.domain not in config.get("workers", {}):
        raise HTTPException(404, f"Unknown domain: {req.domain}")

    ok = spawn_temp(req.session, req.domain, req.model)
    if not ok:
        raise HTTPException(500, f"Failed to spawn session '{req.session}'")

    # Mark as persistent (not temp — no TTL)
    set_opt(req.session, "worker_type", "persistent")
    worker_status[req.session] = "idle"

    # Append to workers.yaml so it survives restarts
    with open(WORKERS_CONFIG) as f:
        raw = f.read()
    sessions_line = f"sessions: [{', '.join(config['workers'][req.domain].get('sessions', []))}]"
    new_sessions = config['workers'][req.domain].get('sessions', []) + [req.session]
    new_line = f"sessions: [{', '.join(new_sessions)}]"
    updated = raw.replace(sessions_line, new_line, 1)
    with open(WORKERS_CONFIG, "w") as f:
        f.write(updated)

    log(f"[spawn-worker] spawned persistent {req.session} for {req.domain} (model={req.model})")
    return {"status": "spawned", "session": req.session, "domain": req.domain, "model": req.model}


class AttachRequest(BaseModel):
    session: str
    domain: str

@app.post("/attach-worker")
async def attach_worker_endpoint(req: AttachRequest, token: str = ""):
    if not _check_token(token):
        raise HTTPException(403, "Unauthorized")
    config = load_config()
    if not session_exists(req.session):
        raise HTTPException(404, f"tmux session '{req.session}' not found")
    if req.domain not in config.get("workers", {}):
        raise HTTPException(404, f"Unknown domain: {req.domain}")
    already = any(req.session in cfg.get("sessions", [])
                  for cfg in config["workers"].values())
    if already:
        raise HTTPException(409, f"Session '{req.session}' already registered")

    set_opt(req.session, "worker_type", "persistent")
    set_opt(req.session, "worker_domain", req.domain)
    set_opt(req.session, "status", "idle")
    worker_status[req.session] = "idle"

    with open(WORKERS_CONFIG) as f:
        raw = f.read()
    sessions = config['workers'][req.domain].get('sessions', [])
    old_line = f"sessions: [{', '.join(sessions)}]"
    new_line = f"sessions: [{', '.join(sessions + [req.session])}]"
    with open(WORKERS_CONFIG, "w") as f:
        f.write(raw.replace(old_line, new_line, 1))

    log(f"[attach-worker] registered existing session {req.session} → {req.domain}")
    return {"status": "attached", "session": req.session, "domain": req.domain}


@app.get("/tmux-sessions")
async def tmux_sessions(token: str = ""):
    if not _check_token(token):
        from fastapi.responses import Response
        return Response(status_code=403)
    config = load_config()
    registered = set()
    for cfg in config.get("workers", {}).values():
        registered.update(cfg.get("sessions", []))
    protected_keys = set(config.get("_protected", {}).keys())
    r = tmux(["list-sessions", "-F", "#{session_name}"])
    all_sessions = [s.strip() for s in r.stdout.splitlines() if s.strip()]
    available = [
        {"name": s, "protected": s in protected_keys}
        for s in all_sessions
        if s not in registered and not s.startswith("orchmux-")
    ]
    # Also include protected sessions that exist as tmux sessions but aren't in workers
    for s in protected_keys:
        if session_exists(s) and s not in registered and not any(x["name"] == s for x in available):
            available.append({"name": s, "protected": True})
    return {"available": available, "registered": sorted(registered)}


@app.get("/session-notes/{session}")
async def session_notes(session: str, token: str = ""):
    if not _check_token(token):
        from fastapi.responses import Response
        return Response(status_code=403)
    obsidian = Path.home() / "obsidian-vault" / "AI-Systems" / "Claude-Logs" / "Sessions"
    matches = []
    if obsidian.exists():
        for f in sorted(obsidian.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                content = f.read_text(errors="ignore")
                # Match by session name in filename or first 500 chars of content
                if session.lower() in f.stem.lower() or session.lower() in content[:500].lower():
                    matches.append({
                        "file": f.name,
                        "preview": content[:600],
                        "full": content[:4000],
                    })
                    if len(matches) >= 3:
                        break
            except Exception:
                pass
    return {"session": session, "notes": matches}


@app.delete("/session/{name}")
async def kill_session_endpoint(name: str, token: str = ""):
    if not _check_token(token):
        raise HTTPException(403, "Unauthorized")
    config = load_config()
    protected = set(config.get("_protected", {}).keys())
    if name in protected or name.startswith("orchmux-"):
        raise HTTPException(400, f"Session '{name}' is protected and cannot be killed")
    if not session_exists(name):
        raise HTTPException(404, f"Session '{name}' not found")

    tmux(["kill-session", "-t", name])
    worker_status.pop(name, None)

    # Remove from workers.yaml sessions lists
    with open(WORKERS_CONFIG) as f:
        raw = f.read()
    for domain, cfg in config.get("workers", {}).items():
        sessions = cfg.get("sessions", [])
        if name in sessions:
            old_line = f"sessions: [{', '.join(sessions)}]"
            new_sessions = [s for s in sessions if s != name]
            new_line = f"sessions: [{', '.join(new_sessions)}]"
            raw = raw.replace(old_line, new_line, 1)
    with open(WORKERS_CONFIG, "w") as f:
        f.write(raw)

    # Clean up queue YAML if pending
    q_file = QUEUE_DIR / f"{name}.yaml"
    if q_file.exists():
        q_file.unlink()

    log(f"[kill-session] killed {name}")
    return {"status": "killed", "session": name}


class AddDomainRequest(BaseModel):
    domain: str
    sessions: list[str] = []
    model: str = "claude"
    handles: list[str] = []
    spawn_allowed: bool = True
    token: str = ""

@app.get("/domains")
async def get_domains(token: str = ""):
    config = load_config()
    return [k for k in config.get("workers", {}).keys() if not k.startswith("_")]

@app.get("/task/{task_id}")
async def get_task(task_id: str, token: str = ""):
    entry = task_registry.get(task_id)
    if not entry:
        raise HTTPException(404, f"Task {task_id} not found")
    return entry

@app.get("/session-domains")
async def session_domains(token: str = ""):
    """Return a mapping of session_name → domain for all configured workers."""
    config = load_config()
    mapping = {}
    for domain, dcfg in config.get("workers", {}).items():
        if domain.startswith("_"):
            continue
        for s in dcfg.get("sessions", []):
            mapping[s] = domain
    return mapping

@app.post("/restart")
async def restart_server():
    """Re-exec the server process in-place (picks up code changes, clears state)."""
    import sys, threading
    def _do_restart():
        time.sleep(0.3)  # let response flush
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=_do_restart, daemon=True).start()
    return {"status": "restarting"}

@app.post("/add-domain")
async def add_domain(req: AddDomainRequest):
    if not _check_token(req.token):
        raise HTTPException(403, "Unauthorized")
    name = req.domain.strip().lower().replace(" ", "_").replace("-", "_")
    if not name or not name.isidentifier():
        raise HTTPException(400, "Invalid domain name")
    config = load_config()
    if name in config.get("workers", {}):
        raise HTTPException(400, f"Domain '{name}' already exists")
    # Append new domain block to workers.yaml
    with open(WORKERS_CONFIG, "a") as f:
        handles_str = ", ".join(req.handles) if req.handles else name
        sessions_str = ", ".join(req.sessions) if req.sessions else ""
        f.write(f"\n  {name}:\n")
        f.write(f"    sessions: [{sessions_str}]\n")
        f.write(f"    model: {req.model}\n")
        f.write(f"    handles: [{handles_str}]\n")
        f.write(f"    queue_strategy: queue\n")
        f.write(f"    spawn_allowed: {'true' if req.spawn_allowed else 'false'}\n")
    log(f"[add-domain] created domain '{name}' with sessions {req.sessions}")
    return {"status": "created", "domain": name}


_AUTH_PANE_SIGNS = ("OAuth error", "Invalid code", "Paste code here",
                    "Browser didn't open", "Press Enter to retry",
                    "API Error: 401", "authentication_error", "Invalid authentication",
                    "Please run /login", "Invalid API key")
_NOISE_PANE = ("bypass permissions", "shift+tab", "⏵⏵", "Claude Code",
               "Sonnet", "Opus", "Welcome to", "Syntax theme",
               "ctrl+t to disable", "? for shortcuts", "╌",
               "tmux focus", "focus-events", "3rd-party platform")


@app.get("/worker-details")
async def worker_details(token: str = ""):
    if not _check_token(token):
        from fastapi.responses import Response
        return Response(status_code=403)
    config = load_config()
    result = {}
    for domain, cfg in config.get("workers", {}).items():
        if domain.startswith("_"):
            continue
        for session in cfg.get("sessions", []):
            # Auth status from pane
            pane_r = tmux(["capture-pane", "-t", session, "-p", "-S", "-10"])
            pane = pane_r.stdout if pane_r.returncode == 0 else ""
            if not session_exists(session):
                auth = "missing"
            elif any(s in pane for s in _AUTH_PANE_SIGNS):
                auth = "auth_error"
            else:
                real = [l for l in pane.splitlines()
                        if l.strip() and not any(n in l for n in _NOISE_PANE)
                        and not all(c in "─━═ " for c in l.strip())]
                last = real[-1].strip() if real else ""
                auth = "ok" if last.startswith("❯") else "loading"

            # Last task from queue file
            qf = QUEUE_DIR / f"{session}.yaml"
            last_task = ""
            last_task_status = ""
            last_task_time = ""
            if qf.exists():
                try:
                    q = yaml.safe_load(qf.read_text())
                    if q:
                        last_task = (q.get("task") or "")[:120]
                        last_task_status = q.get("status", "")
                        last_task_time = q.get("dispatched_at", "")
                except Exception:
                    pass

            result[session] = {
                "auth": auth,
                "last_task": last_task,
                "last_task_status": last_task_status,
                "last_task_time": last_task_time,
                "domain": domain,
            }
    # Merge in display names / roles / slack_target from meta
    meta = _load_worker_meta()
    for session, m in meta.items():
        if session in result:
            result[session]["display_name"] = m.get("display_name", "")
            result[session]["role"] = m.get("role", "")
            result[session]["slack_target"] = m.get("slack_target", "")
    return result


class WorkerMetaRequest(BaseModel):
    session: str
    display_name: str = ""
    role: str = ""
    slack_target: str = ""   # Slack channel ID or user ID

@app.get("/worker-meta")
async def get_worker_meta(token: str = ""):
    return _load_worker_meta()

@app.post("/worker-meta")
async def set_worker_meta(req: WorkerMetaRequest):
    meta = _load_worker_meta()
    entry = meta.get(req.session, {})
    if req.display_name != "": entry["display_name"] = req.display_name
    if req.role != "":         entry["role"] = req.role
    if req.slack_target != "": entry["slack_target"] = req.slack_target
    meta[req.session] = entry
    _save_worker_meta(meta)
    return {"status": "saved"}


@app.post("/slack-send")
async def slack_send(req: Request, token: str = ""):
    """Send a Slack message to a worker's configured Slack target."""
    if not _check_token(token):
        from fastapi.responses import Response
        return Response(status_code=403)
    body   = await req.json()
    target  = body.get("target", "")   # channel or user ID
    message = body.get("message", "")
    if not target or not message:
        raise HTTPException(400, "target and message required")
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot_token:
        raise HTTPException(503, "SLACK_BOT_TOKEN not set")
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", "https://slack.com/api/chat.postMessage",
         "-H", f"Authorization: Bearer {bot_token}",
         "-H", "Content-Type: application/json",
         "--data", json.dumps({"channel": target, "text": message})],
        capture_output=True, text=True, timeout=15
    )
    try:
        resp = json.loads(result.stdout)
        if resp.get("ok"):
            return {"ok": True, "ts": resp.get("ts")}
        return {"ok": False, "error": resp.get("error", "unknown")}
    except Exception:
        raise HTTPException(500, "Slack API error")


@app.get("/dashboard")
async def dashboard(token: str = "", ui: str = ""):
    if not _check_token(token):
        return HTMLResponse(content="<h2>403 Forbidden</h2><p>Add ?token=YOUR_TOKEN to the URL.</p>", status_code=403)
    if ui == "clean":
        html_path = _CLEAN_DIR / "orchmux-clean.html"
        if html_path.exists():
            return HTMLResponse(content=html_path.read_text())
    return HTMLResponse(content=_build_dashboard(token))


@app.get("/dashboard/clean")
async def clean_dashboard(token: str = ""):
    if not _check_token(token):
        return HTMLResponse(content="<h2>403 Forbidden</h2><p>Add ?token=YOUR_TOKEN to the URL.</p>", status_code=403)
    html_path = _CLEAN_DIR / "orchmux-clean.html"
    if not html_path.exists():
        raise HTTPException(404, "Clean UI not found — run the integration step first")
    return HTMLResponse(content=html_path.read_text())


def _build_dashboard(token: str = "") -> str:  # noqa: E501
    q = f"?token={token}" if token else ""
    qa = f"&token={token}" if token else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>orchmux</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#f5f5f5;color:#1a1a1a;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',sans-serif;font-size:13px;padding:12px;line-height:1.5}}
h1{{font-size:16px;font-weight:700;color:#000;letter-spacing:-.01em;margin-bottom:2px}}
.sub{{color:#999;font-size:11px;margin-bottom:14px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.full{{grid-column:1/-1}}
@media(max-width:640px){{
  body{{padding:8px}}
  .grid{{grid-template-columns:1fr;gap:8px}}
  .full{{grid-column:1}}
  #term-grid{{grid-template-columns:1fr 1fr!important;gap:6px!important}}
  .term-cell{{min-height:180px;max-height:260px}}
  .term-cell-out{{font-size:9px!important;padding:6px 7px!important;line-height:1.4}}
  .term-cell-hdr{{padding:4px 7px;gap:5px}}
  .term-cell-hdr span:first-child+span{{font-size:9px!important}}
}}
.panel{{background:#fff;border:1px solid #e8e8e8;border-radius:10px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,.04)}}
.pt{{font-size:10px;font-weight:600;color:#aaa;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px}}
table{{width:100%;border-collapse:collapse}}
th{{font-size:10px;color:#bbb;text-transform:uppercase;padding:0 10px 7px 0;text-align:left;font-weight:600}}
td{{padding:6px 10px 6px 0;border-top:1px solid #f0f0f0;vertical-align:middle;font-size:12px}}
.tbl-wrap{{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}}
@media(max-width:640px){{
  .col-hide{{display:none}}
  td,th{{padding:5px 6px 5px 0;font-size:11px}}
}}
.b{{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600}}
.idle{{background:#f0faf0;color:#2e7d32;border:1px solid #c8e6c9}}
.busy{{background:#fff8e1;color:#e65100;border:1px solid #ffe082}}
.waiting{{background:#e8eaf6;color:#3949ab;border:1px solid #c5cae9}}
.blocked{{background:#fce4ec;color:#c62828;border:1px solid #f48fb1}}
.auth{{background:#fce4ec;color:#ad1457;border:1px solid #f48fb1}}
.missing{{background:#f5f5f5;color:#bbb;border:1px solid #e0e0e0}}
.pulse{{width:6px;height:6px;border-radius:50%;display:inline-block;flex-shrink:0}}
.p-idle{{background:#4caf50}}
.p-busy{{background:#ff9800;animation:blink .8s infinite}}
.p-wait{{background:#5c6bc0;animation:blink 1.2s infinite}}
.p-block{{background:#e53935;animation:blink .4s infinite}}
.p-auth{{background:#d81b60;animation:blink .4s infinite}}
.p-miss{{background:#ccc}}
@keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
.dim{{color:#aaa;font-size:11px}}
.task{{color:#888;font-size:11px;max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.alert-row{{display:flex;align-items:flex-start;gap:10px;padding:7px 0;border-top:1px solid #f0f0f0}}
.alert-row:first-child{{border-top:none;padding-top:0}}
.alert-icon{{font-size:14px;flex-shrink:0;margin-top:1px}}
.alert-body{{flex:1;min-width:0}}
.alert-title{{font-size:12px;color:#333;margin-bottom:2px;font-weight:500}}
.alert-detail{{font-size:11px;color:#999;line-height:1.4}}
.card{{padding:8px 0;border-top:1px solid #f0f0f0;overflow:hidden;max-width:100%}}
.card:first-child{{border-top:none;padding-top:0}}
.card-head{{display:flex;align-items:center;gap:8px;margin-bottom:3px;font-size:11px;color:#aaa;flex-wrap:wrap;min-width:0}}
.card-body{{font-size:11px;color:#777;line-height:1.5;overflow:hidden;word-break:break-word}}
.md-result{{max-width:100%;overflow-x:hidden;word-break:break-word}}
.md-result h1,.md-result h2,.md-result h3{{font-size:13px;font-weight:600;color:#333;margin:8px 0 4px}}
.md-result p{{margin:4px 0;color:#555}}
.md-result code{{background:#f0f0f0;padding:1px 4px;border-radius:3px;font-family:monospace;font-size:10.5px;word-break:break-all}}
.md-result pre{{background:#f0f0f0;padding:8px;border-radius:4px;overflow-x:auto;font-size:10px;line-height:1.4;max-width:100%}}
.md-result ul,.md-result ol{{margin:4px 0;padding-left:16px;color:#555}}
.md-result li{{margin:2px 0}}
.md-result strong{{color:#333}}
.md-result hr{{border:none;border-top:1px solid #eee;margin:6px 0}}
.md-result table{{border-collapse:collapse;width:100%;font-size:10.5px;display:block;overflow-x:auto;-webkit-overflow-scrolling:touch}}
.md-result td,.md-result th{{border:1px solid #e0e0e0;padding:3px 6px;white-space:nowrap}}
.md-result th{{background:#f5f5f5;font-weight:600}}
.infra-row{{display:flex;align-items:center;gap:10px;padding:5px 0;border-top:1px solid #f0f0f0}}
.infra-row:first-child{{border-top:none;padding-top:0}}
.dot{{width:8px;height:8px;border-radius:50%;flex-shrink:0;background:#4caf50}}
.dot.dead{{background:#e53935;animation:blink .4s infinite}}
.infra-bar{{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:14px;padding:9px 14px;background:#fff;border:1px solid #e8e8e8;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.04)}}
.infra-chip{{display:inline-flex;align-items:center;gap:5px;font-size:11px;color:#aaa;font-weight:500}}
.infra-chip.alive{{color:#2e7d32}}
.infra-chip.dead{{color:#c62828;font-weight:700}}
.infra-chip .cdot{{width:7px;height:7px;border-radius:50%;background:#ddd;flex-shrink:0}}
.infra-chip.alive .cdot{{background:#4caf50}}
.infra-chip.dead .cdot{{background:#e53935;animation:blink .5s infinite}}
.stat{{display:inline-block;background:#f5f5f5;border:1px solid #e8e8e8;border-radius:4px;padding:2px 9px;font-size:11px;color:#999;margin-right:5px}}
.stat span{{color:#333;font-weight:600}}
.ts{{color:#ccc;font-size:10px}}
.empty{{color:#ccc;font-size:11px;padding:4px 0}}
.ans-row{{margin-top:6px;display:flex;gap:6px}}
.ans-row input{{flex:1;background:#fff;border:1px solid #e0e0e0;border-radius:6px;color:#333;padding:5px 10px;font-size:12px;font-family:inherit;outline:none}}
.ans-row input:focus{{border-color:#999}}
.ans-row button{{background:#f0faf0;border:1px solid #c8e6c9;color:#2e7d32;border-radius:6px;padding:5px 12px;cursor:pointer;font-size:11px;font-weight:600}}
.tag{{display:inline-block;padding:1px 5px;border-radius:3px;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-left:4px}}
.tag-heal{{background:#e3f2fd;color:#1565c0;border:1px solid #bbdefb}}
.tag-auth{{background:#fce4ec;color:#ad1457;border:1px solid #f8bbd9}}
.tag-stuck{{background:#fff8e1;color:#e65100;border:1px solid #ffe082}}
.tag-dead{{background:#fce4ec;color:#c62828;border:1px solid #f48fb1}}
.section-divider{{height:1px;background:#f0f0f0;margin:4px 0}}
.skill-item{{padding:7px 12px;cursor:pointer;display:flex;align-items:baseline;gap:8px;border-bottom:1px solid #f5f5f5}}
.skill-item:last-child{{border-bottom:none}}
.skill-item:hover,.skill-item.active{{background:#f0f7ff}}
.skill-name{{color:#1a73e8;font-size:12px;font-weight:600}}
.skill-cat{{font-size:9px;color:#bbb;text-transform:uppercase;letter-spacing:.06em;margin-left:auto}}
.worker-row{{cursor:pointer}}
.worker-row:hover td{{background:#fafafa}}
.pane-wrap{{display:none;background:#1e1e1e;border:1px solid #ddd;border-radius:6px;margin:4px 0 4px 0;overflow:hidden}}
.pane-wrap.open{{display:block}}
.pane-head{{display:flex;align-items:center;gap:8px;padding:6px 12px;background:#2a2a2a;border-bottom:1px solid #333;font-size:10px;color:#999;font-weight:500}}
.pane-head span{{color:#bbb}}
.pane-close{{margin-left:auto;cursor:pointer;color:#666;font-size:13px;padding:0 4px;line-height:1}}
.pane-close:hover{{color:#ccc}}
.pane-body{{padding:10px 12px;font-size:11.5px;font-family:'SF Mono','Fira Code',monospace;color:#a8cc88;white-space:pre;overflow-x:auto;max-height:500px;overflow-y:auto;line-height:1.5;user-select:text;background:#1e1e1e;-webkit-overflow-scrolling:touch}}
.dispatch-row{{display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap}}
@media(max-width:640px){{
  html{{overflow-x:hidden}}
  body{{padding:8px;width:100%;box-sizing:border-box}}
  /* tabs: full-width, tall enough to tap */
  .tab-btn{{flex:1;padding:10px 4px!important;font-size:11px!important;min-height:40px;text-align:center}}
  /* dispatch */
  .dispatch-row{{flex-direction:column;align-items:stretch}}
  .dispatch-row>*{{flex:none!important;width:100%!important;min-width:0!important}}
  .dispatch-row select,.dispatch-row input{{width:100%}}
  .pane-head{{flex-wrap:wrap;gap:4px}}
  /* workers table: show all cols, scroll horizontally, hide kill col (manage from desktop) */
  .col-hide{{display:table-cell!important}}
  .col-kill{{display:none!important}}
  .panel{{max-width:100%;box-sizing:border-box;overflow:hidden}}
  .tbl-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch;max-width:100%}}
  .tbl-wrap table{{table-layout:auto;min-width:480px;width:max-content}}
  .task{{white-space:nowrap;max-width:130px;overflow:hidden;text-overflow:ellipsis}}
  /* manage form full-width inputs */
  #sp-name,#sp-domain,#sp-model,#at-name,#at-domain{{width:100%!important;box-sizing:border-box}}
  #mtab-spawn,#mtab-attach{{flex:1;min-height:36px;font-size:11px!important}}
  /* results cards: readable on phone */
  #rl .card{{padding:10px 0}}
  /* terminal pane */
  .pane-wrap{{max-width:100%;overflow:hidden}}
  .pane-body{{
    font-size:10px;
    max-height:260px;
    white-space:pre-wrap;
    word-break:break-all;
    overflow-x:hidden;
    overflow-y:auto;
    width:100%;
  }}
}}
.desktop-only{{display:none}}
@media(min-width:768px){{.desktop-only{{display:inline-block}}}}
.term-cell{{background:#1e1e1e;border:1px solid #333;border-radius:8px;overflow:hidden;display:flex;flex-direction:column;min-height:240px;max-height:380px}}
.term-cell-hdr{{display:flex;align-items:center;gap:8px;padding:6px 10px;background:#2a2a2a;border-bottom:1px solid #333;flex-shrink:0}}
.term-cell-out{{flex:1;overflow-y:auto;padding:8px 10px;font-size:11px;font-family:'SF Mono','Fira Code',monospace;color:#a8cc88;white-space:pre-wrap;word-break:break-all;line-height:1.45}}
.pane-modern{{background:#fafafa;color:#1a1a1a;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',sans-serif;white-space:normal;word-break:normal}}
.pane-modern h1{{font-size:18px;font-weight:700;color:#111;margin:18px 0 6px;padding-bottom:6px;border-bottom:2px solid #f0f0f0;line-height:1.3}}
.pane-modern h2{{font-size:15px;font-weight:700;color:#1a1a1a;margin:14px 0 5px;padding-bottom:3px;border-bottom:1px solid #f0f0f0}}
.pane-modern h3{{font-size:13px;font-weight:600;color:#333;margin:11px 0 4px;text-transform:uppercase;letter-spacing:.04em}}
.pane-modern h4,.pane-modern h5{{font-size:12px;font-weight:600;color:#555;margin:9px 0 3px}}
.pane-modern p{{margin:0 0 9px;color:#2a2a2a;line-height:1.7}}
.log-history-chunk{{opacity:.88;border-bottom:1px dashed #eee;margin-bottom:10px;padding-bottom:10px}}
.session-start-marker{{text-align:center;color:#bbb;font-size:10px;padding:10px 0 6px;letter-spacing:.05em;user-select:none}}
.log-load-indicator{{text-align:center;padding:8px;font-size:10px;color:#aaa;font-style:italic;user-select:none}}
.task-dispatch-banner{{background:#f3f0ff;border-left:3px solid #7c4dff;border-radius:0 8px 8px 0;padding:8px 12px 8px 12px;margin-bottom:14px;font-size:11px}}
.task-dispatch-banner .tdb-header{{display:flex;align-items:center;gap:6px;margin-bottom:4px}}
.task-dispatch-banner .tdb-label{{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#7c4dff;background:#ede7ff;border-radius:4px;padding:1px 6px}}
.task-dispatch-banner .tdb-domain{{font-size:9px;color:#9e9e9e;background:#f0f0f0;border-radius:4px;padding:1px 6px}}
.task-dispatch-banner .tdb-time{{font-size:9px;color:#bbb;margin-left:auto}}
.task-dispatch-banner .tdb-text{{color:#333;line-height:1.55;white-space:pre-wrap;word-break:break-word;font-size:11.5px}}
.pane-modern .plain-block{{white-space:pre-wrap;margin:0 0 6px;line-height:1.6;color:#2a2a2a;word-break:break-word;font-family:inherit}}
.pane-modern ul,.pane-modern ol{{padding-left:22px;margin:0 0 9px;color:#2a2a2a;line-height:1.75}}
.pane-modern li{{margin-bottom:3px}}
.pane-modern li p{{margin:0}}
.pane-modern strong{{color:#111;font-weight:600}}
.pane-modern em{{color:#555}}
.pane-modern code{{background:#f0f0f0;padding:1px 6px;border-radius:4px;font-family:'SF Mono','Fira Code',monospace;font-size:11px;color:#d63384;border:1px solid #e8e8e8}}
.pane-modern pre{{background:#f6f8fa;border:1px solid #e1e4e8;border-radius:8px;padding:14px;overflow-x:auto;position:relative;margin:10px 0}}
.pane-modern pre code{{background:none;padding:0;font-size:11px;line-height:1.6;color:#24292e;border:none}}
.pane-modern blockquote{{background:#f8f9ff;border-left:3px solid #4285f4;border-radius:0 6px 6px 0;margin:8px 0;padding:8px 14px;color:#444}}
.pane-modern blockquote p{{margin:0;color:#444}}
.pane-modern hr{{border:none;border-top:1px solid #eee;margin:14px 0}}
.pane-modern table{{border-collapse:collapse;width:100%;font-size:12px;margin:10px 0;border-radius:6px;overflow:hidden;border:1px solid #e0e0e0}}
.pane-modern th{{background:#f5f5f5;font-weight:600;padding:7px 12px;border-bottom:2px solid #e0e0e0;border-right:1px solid #e0e0e0;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.03em;color:#555}}
.pane-modern td{{padding:6px 12px;border-bottom:1px solid #f0f0f0;border-right:1px solid #f0f0f0;vertical-align:top}}
.pane-modern tr:last-child td{{border-bottom:none}}
.pane-modern tr:nth-child(even){{background:#fafafa}}
.pane-modern input[type=checkbox]{{margin-right:5px;cursor:default}}
.pane-modern details{{background:#f9f9f9;border:1px solid #e8e8e8;border-radius:6px;margin:6px 0;padding:0}}
.pane-modern summary{{padding:7px 12px;cursor:pointer;font-weight:600;font-size:12px;color:#333;list-style:none}}
.pane-modern summary::-webkit-details-marker{{display:none}}
.pane-modern summary::before{{content:'▶ ';font-size:9px;color:#999}}
.pane-modern details[open] summary::before{{content:'▼ '}}
.pane-modern details>*:not(summary){{padding:0 12px 10px}}
.pane-ts-footer{{padding:4px 12px;font-size:10px;color:#888;background:#eaeaea;border-top:1px solid #d8d8d8;text-align:center;flex-shrink:0;font-family:-apple-system,sans-serif;letter-spacing:.02em}}
.pane-ts-footer.dark{{background:#252525;border-top:1px solid #333;color:#555}}
.pane-copy{{position:absolute;top:6px;right:8px;background:#fff;border:1px solid #ddd;border-radius:4px;padding:2px 8px;font-size:10px;cursor:pointer;color:#888;font-family:inherit;line-height:1.4}}
.pane-copy:hover{{color:#333;border-color:#bbb}}
.tbl-wrap{{position:relative;margin:8px 0;overflow-x:auto}}
.tbl-copy{{position:absolute;top:4px;right:4px;background:#f5f5f5;border:1px solid #ddd;border-radius:4px;padding:2px 8px;font-size:10px;cursor:pointer;color:#888;font-family:inherit;line-height:1.4;z-index:2;opacity:0;transition:opacity .15s}}
.tbl-wrap:hover .tbl-copy{{opacity:1}}
.new-out-pill{{position:absolute;bottom:14px;left:50%;transform:translateX(-50%);background:#1a73e8;color:#fff;border-radius:20px;padding:6px 16px;font-size:11px;font-weight:600;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,0.25);display:none;z-index:10;white-space:nowrap;user-select:none;transition:opacity .15s}}
.new-out-pill:hover{{background:#1557b0}}
#pane-modal.fullscreen>div{{height:100vh!important;border-radius:0!important;max-width:100%!important}}
</style>
<script>
// Custom markdown renderer — no CDN, handles Claude's output directly
// Detects blocks by line content, not blank-line position
function _mdInl(s){{
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\\*\\*([^*\\n]+)\\*\\*/g,'<strong>$1</strong>')
    .replace(/\\*([^*\\n]+)\\*/g,'<em>$1</em>')
    .replace(/`([^`\\n]+)`/g,'<code>$1</code>')
    .replace(/~~([^~\\n]+)~~/g,'<del>$1</del>')
    .replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g,'<a href="$2" target="_blank">$1</a>');
}}
function mdParse(raw){{
  const lines=raw.replace(/\\r/g,'').split('\\n');
  const isTR=l=>{{const t=l.trim();return t.length>2&&t[0]==='|'&&t[t.length-1]==='|';}};
  const isSep=l=>{{const t=l.trim();return t.length>1&&t.includes('|')&&!t.replace(/[|:\\- ]/g,'').length;}};
  const isLI=l=>/^([-*+]|\\d+\\.) /.test(l.trim());
  const splitCells=r=>r.trim().slice(1,-1).split('|').map(c=>c.trim());
  const BOX_CELL='│';
  const isBoxRow=l=>{{const t=l.trim();return t.includes(BOX_CELL)&&t.split(BOX_CELL).some(c=>c.trim().length>0);}};
  const boxCells=r=>r.split(BOX_CELL).slice(1,-1).map(c=>c.trim());
  const html=[];let i=0;
  while(i<lines.length){{
    const line=lines[i],t=line.trim();
    if(!t){{i++;continue;}}
    // ── Code fence ──
    if(t.startsWith('```')){{
      const lang=t.slice(3).trim();i++;
      const cl=[];
      while(i<lines.length&&!lines[i].trim().startsWith('```')){{cl.push(lines[i]);i++;}}
      i++;
      const code=cl.join('\\n').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      html.push('<pre><code'+(lang?' class="lang-'+lang+'"':'')+'>'+code+'</code></pre>');
      continue;
    }}
    // ── Heading ──
    const hm=t.match(/^(#{{1,6}}) (.+)$/);
    if(hm){{html.push('<h'+hm[1].length+'>'+_mdInl(hm[2])+'</h'+hm[1].length+'>');i++;continue;}}
    // ── Box-drawing table (Claude Code native output: │ as cell separator) ──
    if(isBoxRow(line)){{
      const rows=[];
      while(i<lines.length&&(isBoxRow(lines[i])||!lines[i].trim())){{
        if(lines[i].trim())rows.push(lines[i]);i++;
      }}
      let th='<table>';
      if(rows.length>=1){{
        th+='<thead><tr>'+boxCells(rows[0]).map(c=>'<th>'+_mdInl(c)+'</th>').join('')+'</tr></thead>';
        if(rows.length>1)th+='<tbody>'+rows.slice(1).map(r=>'<tr>'+boxCells(r).map(c=>'<td>'+_mdInl(c)+'</td>').join('')+'</tr>').join('')+'</tbody>';
      }}
      html.push(th+'</table>');continue;
    }}
    // ── Markdown table — collect consecutive table rows (skip blank lines within) ──
    if(isTR(line)){{
      const rows=[];
      while(i<lines.length&&(isTR(lines[i])||!lines[i].trim())){{
        if(lines[i].trim())rows.push(lines[i].trim());i++;
      }}
      const sepIdx=rows.findIndex(r=>isSep(r));
      const dataRows=rows.filter(r=>!isSep(r));
      let th='<table>';
      if(dataRows.length>=1){{
        th+='<thead><tr>'+splitCells(dataRows[0]).map(c=>'<th>'+_mdInl(c)+'</th>').join('')+'</tr></thead>';
        if(dataRows.length>1)th+='<tbody>'+dataRows.slice(1).map(r=>'<tr>'+splitCells(r).map(c=>'<td>'+_mdInl(c)+'</td>').join('')+'</tr>').join('')+'</tbody>';
      }}
      html.push(th+'</table>');continue;
    }}
    // ── List ──
    if(isLI(line)){{
      const items=[];
      while(i<lines.length&&isLI(lines[i])){{
        let item=lines[i].trim().replace(/^([-*+]|\\d+\\.) /,'');
        item=item.replace(/^\\[ \\] /,'<input type="checkbox" disabled> ');
        item=item.replace(/^\\[x\\] /i,'<input type="checkbox" checked disabled> ');
        items.push(_mdInl(item));i++;
      }}
      const tag=/^\\d/.test(line.trim())?'ol':'ul';
      html.push('<'+tag+'>'+items.map(it=>'<li>'+it+'</li>').join('')+'</'+tag+'>');
      continue;
    }}
    // ── Blockquote ──
    if(t.startsWith('>')){{
      const bq=[];
      while(i<lines.length&&lines[i].trim().startsWith('>')){{bq.push(lines[i].trim().slice(1).trim());i++;}}
      html.push('<blockquote><p>'+_mdInl(bq.join(' '))+'</p></blockquote>');
      continue;
    }}
    // ── Details/summary ──
    if(t.startsWith('<details')||t.startsWith('<summary')){{
      const bl=[];
      while(i<lines.length&&lines[i].trim()){{bl.push(lines[i]);i++;}}
      html.push(bl.join('\\n'));continue;
    }}
    // ── Plain text block — pre-wrap preserves indentation and column structure ──
    const pl=[];
    while(i<lines.length&&lines[i].trim()&&!lines[i].trim().startsWith('```')
          &&!lines[i].trim().match(/^#{{1,6}} /)&&!isTR(lines[i])&&!isBoxRow(lines[i])
          &&!isLI(lines[i])&&!lines[i].trim().startsWith('>')){{
      pl.push(lines[i]);i++;
    }}
    if(pl.length)html.push('<div class="plain-block">'+pl.map(l=>_mdInl(l)).join('\\n')+'</div>');
  }}
  return html.join('\\n');
}}
</script>
</head>
<body>

<!-- Notes modal -->
<!-- Terminal pane modal -->
<div id="pane-modal" style="display:none;position:fixed;inset:0;z-index:9998;align-items:flex-end;justify-content:center;background:rgba(0,0,0,0.55);box-sizing:border-box" onclick="if(event.target===this)closePaneModal()">
  <div style="background:#1e1e1e;border-radius:12px 12px 0 0;width:100%;max-width:900px;box-shadow:0 -4px 24px rgba(0,0,0,0.4);display:flex;flex-direction:column;height:75vh;box-sizing:border-box">
    <div style="display:flex;align-items:center;gap:8px;padding:8px 12px;background:#2a2a2a;border-bottom:1px solid #333;border-radius:12px 12px 0 0;flex-shrink:0">
      <span style="font-size:11px;color:#999">&#x1F4BB;</span>
      <span id="pane-modal-title" style="font-size:12px;color:#bbb;font-family:'SF Mono','Fira Code',monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1"></span>
      <span id="pane-modal-ts" style="font-size:10px;color:#555"></span>
      <button id="pane-modal-refresh" onclick="refreshPaneBtn(_paneModalSession,this)" style="background:#333;border:1px solid #444;color:#aaa;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:10px;font-family:inherit">&#x21BA;</button>
      <button id="pane-modal-live" onclick="toggleLiveModal()" style="background:#1a4a1a;border:1px solid #2a6a2a;color:#6c6;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:10px;font-family:inherit">&#x23F5; Live</button>
      <button onclick="openPaneDispatch()" style="background:#1a3a6a;border:1px solid #2a5aa0;color:#7ab;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:10px;font-family:inherit">&#x25B6; Task</button>
      <button onclick="pinToGrid()" id="pane-grid-btn" title="Pin to Terminals grid" style="background:#2a1a4a;border:1px solid #5a2a9a;color:#b8a;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:10px;font-family:inherit">&#x229E; Grid</button>
      <button onclick="_setPaneFontSize(-1)" title="Decrease font size" style="background:#333;border:1px solid #444;color:#aaa;border-radius:4px;padding:2px 7px;cursor:pointer;font-size:10px;font-family:inherit">A-</button>
      <button onclick="_setPaneFontSize(1)" title="Increase font size" style="background:#333;border:1px solid #444;color:#aaa;border-radius:4px;padding:2px 7px;cursor:pointer;font-size:10px;font-family:inherit">A+</button>
      <button id="pane-mode-btn" onclick="_togglePaneMode()" style="background:#333;border:1px solid #444;color:#aaa;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:10px;font-family:inherit">&#x2600; Modern</button>
      <button id="pane-fs-btn" onclick="_togglePaneFS()" title="Toggle fullscreen" style="background:#333;border:1px solid #444;color:#aaa;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:10px;font-family:inherit">&#x26F6;</button>
      <button onclick="closePaneModal()" style="background:none;border:none;font-size:20px;color:#666;cursor:pointer;padding:0 4px;line-height:1;flex-shrink:0">&times;</button>
    </div>
    <div style="display:flex;align-items:center;justify-content:space-between;padding:3px 10px 4px;background:#242424;border-bottom:1px solid #2e2e2e;flex-shrink:0">
      <button onclick="navPane(-1)" style="background:none;border:none;color:#666;font-size:18px;cursor:pointer;padding:0 6px;line-height:1;user-select:none" title="Previous worker (←)">&#x2039;</button>
      <span id="pane-nav-label" style="font-size:10px;color:#555;letter-spacing:.04em"></span>
      <button onclick="navPane(1)" style="background:none;border:none;color:#666;font-size:18px;cursor:pointer;padding:0 6px;line-height:1;user-select:none" title="Next worker (→)">&#x203a;</button>
    </div>
    <div style="position:relative;flex:1;overflow:hidden;display:flex;flex-direction:column">
      <div id="pane-modal-body" style="padding:12px 14px;font-size:11px;font-family:'SF Mono','Fira Code',monospace;color:#a8cc88;white-space:pre-wrap;word-break:break-all;overflow-y:auto;flex:1;-webkit-overflow-scrolling:touch;line-height:1.5;user-select:text;-webkit-user-select:text"></div>
      <div id="new-out-pill" class="new-out-pill" onclick="scrollPaneToBottom()">&#x2193; new output</div>
    </div>
    <div id="pane-ts-footer" class="pane-ts-footer dark"></div>
  </div>
</div>

<!-- Notes modal -->
<div id="notes-modal" style="display:none;position:fixed;inset:0;z-index:9999;align-items:flex-end;justify-content:center;background:rgba(0,0,0,0.55);box-sizing:border-box" onclick="if(event.target===this)closeNotesModal()">
  <div style="background:#1e1e1e;border-radius:12px 12px 0 0;width:100%;max-width:900px;box-shadow:0 -4px 24px rgba(0,0,0,0.4);display:flex;flex-direction:column;height:80vh;box-sizing:border-box">
    <div style="display:flex;align-items:center;gap:8px;padding:8px 12px;background:#2a2a2a;border-bottom:1px solid #333;border-radius:12px 12px 0 0;flex-shrink:0">
      <span style="font-size:11px;color:#999">&#x1F4D3;</span>
      <span id="notes-modal-title" style="font-size:12px;color:#bbb;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1"></span>
      <button onclick="closeNotesModal()" style="background:none;border:none;font-size:20px;color:#666;cursor:pointer;padding:0 4px;line-height:1;flex-shrink:0">&times;</button>
    </div>
    <div id="notes-modal-body" style="padding:16px;overflow-y:auto;flex:1;-webkit-overflow-scrolling:touch;color:#ccc;font-size:12px;line-height:1.7"></div>
  </div>
</div>

<!-- Result full-output modal -->
<div id="result-modal" style="display:none;position:fixed;inset:0;z-index:9997;align-items:flex-end;justify-content:center;background:rgba(0,0,0,0.55);box-sizing:border-box" onclick="if(event.target===this)closeResultModal()">
  <div style="background:#1e1e1e;border-radius:12px 12px 0 0;width:100%;max-width:900px;box-shadow:0 -4px 24px rgba(0,0,0,0.4);display:flex;flex-direction:column;height:80vh;box-sizing:border-box">
    <div style="display:flex;align-items:center;gap:8px;padding:8px 12px;background:#2a2a2a;border-bottom:1px solid #333;border-radius:12px 12px 0 0;flex-shrink:0">
      <span style="font-size:11px;color:#999">&#x2705;</span>
      <span id="result-modal-title" style="font-size:12px;color:#bbb;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1"></span>
      <span id="result-modal-ts" style="font-size:10px;color:#555;white-space:nowrap"></span>
      <button onclick="closeResultModal()" style="background:none;border:none;font-size:20px;color:#666;cursor:pointer;padding:0 4px;line-height:1;flex-shrink:0">&times;</button>
    </div>
    <div id="result-modal-body" class="md-result" style="padding:16px;overflow-y:auto;flex:1;-webkit-overflow-scrolling:touch;color:#ccc;font-size:12px;line-height:1.7"></div>
  </div>
</div>

<div id="dispatch-modal" style="display:none;position:fixed;inset:0;z-index:10000;align-items:flex-end;justify-content:center;background:rgba(0,0,0,0.55);box-sizing:border-box" onclick="if(event.target===this)closeDispatchModal()">
  <div style="background:#1e1e1e;border-radius:12px 12px 0 0;width:100%;max-width:900px;box-shadow:0 -4px 24px rgba(0,0,0,0.5);display:flex;flex-direction:column;max-height:60vh;box-sizing:border-box">
    <div style="display:flex;align-items:center;gap:8px;padding:10px 14px;background:#252525;border-bottom:1px solid #333;border-radius:12px 12px 0 0;flex-shrink:0">
      <span style="font-size:12px;color:#888">&#x25B6;</span>
      <span style="font-size:13px;color:#eee;font-weight:600;flex:1">Dispatch Task</span>
      <span id="dm-route-badge" style="font-size:10px;background:#1a3a1a;border:1px solid #2a5a2a;color:#6c6;border-radius:12px;padding:2px 10px">routing to: research</span>
      <button onclick="closeDispatchModal()" style="background:none;border:none;font-size:20px;color:#555;cursor:pointer;padding:0 4px;line-height:1;margin-left:4px">&times;</button>
    </div>
    <div style="padding:12px 14px;display:flex;flex-direction:column;gap:10px;overflow-y:auto;flex:1;-webkit-overflow-scrolling:touch">
      <div style="display:flex;gap:8px">
        <div style="flex:1">
          <div style="font-size:9px;color:#666;margin-bottom:4px;text-transform:uppercase;letter-spacing:.06em">Worker</div>
          <select id="dm-worker" style="width:100%;background:#2a2a2a;border:1px solid #3a3a3a;border-radius:6px;color:#ccc;padding:7px 10px;font-size:12px;font-family:inherit;outline:none" onchange="dmWorkerChanged()">
            <option value="">— any idle worker —</option>
          </select>
        </div>
        <div style="flex:0 0 auto">
          <div style="font-size:9px;color:#666;margin-bottom:4px;text-transform:uppercase;letter-spacing:.06em">Domain override</div>
          <select id="dm-domain" style="background:#2a2a2a;border:1px solid #3a3a3a;border-radius:6px;color:#888;padding:7px 10px;font-size:12px;font-family:inherit;outline:none">
            <option value="cx">cx</option>
            <option value="research" selected>research</option>
            <option value="finance">finance</option>
            <option value="amazon">amazon</option>
            <option value="firmware">firmware</option>
            <option value="data">data</option>
            <option value="pr_review">pr_review</option>
            <option value="wacli">wacli</option>
            <option value="legal">legal</option>
          </select>
        </div>
      </div>
      <div style="flex:1;display:flex;flex-direction:column;position:relative">
        <textarea id="dm-task" placeholder="Describe the task…  type / for skills  ·  ⌘↵ to send" autocomplete="off" style="flex:1;min-height:120px;width:100%;background:#242424;border:1px solid #3a3a3a;border-radius:8px;color:#ddd;padding:10px 12px;font-size:13px;font-family:-apple-system,sans-serif;outline:none;resize:none;line-height:1.6;box-sizing:border-box" oninput="dmTaskInput(this.value);skillDropFor('dm-task','dm-skill-drop')" onkeydown="if((event.metaKey||event.ctrlKey)&&event.key==='Enter')dispatchModal();else skillKeyNavFor(event,'dm-task','dm-skill-drop')"></textarea>
        <div id="dm-skill-drop" style="display:none;position:absolute;top:100%;left:0;right:0;background:#2a2a2a;border:1px solid #555;border-radius:0 0 8px 8px;max-height:180px;overflow-y:auto;z-index:10001;box-shadow:0 4px 12px rgba(0,0,0,.4)"></div>
      </div>
      <div style="display:flex;gap:8px;flex-shrink:0;align-items:center">
        <button onclick="dispatchModal()" style="flex:1;background:#1a73e8;border:none;color:#fff;border-radius:8px;padding:12px;font-size:14px;font-family:inherit;font-weight:600;cursor:pointer">&#x25B6; Dispatch</button>
        <button id="dm-force-btn" onclick="dispatchModal(true)" style="background:#2a2a2a;border:1px solid #444;color:#e07050;border-radius:8px;padding:12px 16px;font-size:12px;font-family:inherit;cursor:pointer;white-space:nowrap">&#x26A1; Force</button>
      </div>
      <div id="dm-msg" style="font-size:11px;color:#aaa;text-align:center;min-height:16px;padding-bottom:2px"></div>
    </div>
  </div>
</div>

<!-- Worker meta edit modal -->
<div id="meta-modal" style="display:none;position:fixed;inset:0;z-index:10001;align-items:flex-end;justify-content:center;background:rgba(0,0,0,0.55);box-sizing:border-box" onclick="if(event.target===this)closeMetaModal()">
  <div style="background:#1e1e1e;border-radius:12px 12px 0 0;width:100%;max-width:900px;box-shadow:0 -4px 24px rgba(0,0,0,0.4);display:flex;flex-direction:column;box-sizing:border-box">
    <div style="display:flex;align-items:center;gap:8px;padding:10px 14px;background:#2a2a2a;border-bottom:1px solid #333;border-radius:12px 12px 0 0;flex-shrink:0">
      <span style="font-size:13px">&#x270E;</span>
      <span id="meta-modal-title" style="font-size:13px;color:#eee;font-weight:600;flex:1">Edit Worker</span>
      <button onclick="closeMetaModal()" style="background:none;border:none;font-size:22px;color:#666;cursor:pointer;padding:0 4px;line-height:1">&times;</button>
    </div>
    <div style="padding:16px;display:flex;flex-direction:column;gap:12px">
      <div>
        <div style="font-size:10px;color:#888;margin-bottom:6px;text-transform:uppercase;letter-spacing:.05em">Display Name <span style="color:#555;text-transform:none;font-size:10px">(shown under session ID)</span></div>
        <input id="meta-name" placeholder="e.g. CX Bug Hunter, Finance Analyst…" style="width:100%;background:#2a2a2a;border:1px solid #444;border-radius:6px;color:#ddd;padding:9px 10px;font-size:13px;font-family:inherit;outline:none;box-sizing:border-box">
      </div>
      <div>
        <div style="font-size:10px;color:#888;margin-bottom:6px;text-transform:uppercase;letter-spacing:.05em">Role / How to use <span style="color:#555;text-transform:none;font-size:10px">(supervisor context)</span></div>
        <textarea id="meta-role" placeholder="e.g. Handles CX bot bugs and PR reviews. Prefers step-by-step tasks. Always run /cx-monitor first." style="width:100%;background:#2a2a2a;border:1px solid #444;border-radius:6px;color:#ddd;padding:10px;font-size:12px;font-family:inherit;outline:none;resize:none;line-height:1.6;box-sizing:border-box;min-height:100px"></textarea>
      </div>
      <button onclick="saveMeta()" style="background:#1a73e8;border:none;color:#fff;border-radius:8px;padding:11px;font-size:13px;font-family:inherit;font-weight:600;cursor:pointer">Save</button>
      <div id="meta-msg" style="font-size:11px;color:#aaa;text-align:center;min-height:16px"></div>
    </div>
  </div>
</div>

<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:6px">
  <h1>&#x2B21; orchmux</h1>
  <span class="stat">workers <span id="sw">&#8212;</span></span>
  <span class="stat">queued <span id="sq">&#8212;</span></span>
  <span class="ts" id="ts">loading&hellip;</span>
</div>
<div class="infra-bar" id="infra-bar">
  <span style="font-size:10px;font-weight:600;color:#bbb;text-transform:uppercase;letter-spacing:.06em;margin-right:4px">Infra</span>
  <span id="ib-server" class="infra-chip"><span class="cdot"></span>server</span>
  <span id="ib-supervisor" class="infra-chip"><span class="cdot"></span>supervisor</span>
  <span id="ib-watcher" class="infra-chip"><span class="cdot"></span>watcher</span>
  <span id="ib-telegram" class="infra-chip"><span class="cdot"></span>telegram</span>
  <span id="ib-monitor" class="infra-chip"><span class="cdot"></span>monitor</span>
  <span style="margin-left:auto;font-size:10px;color:#ddd" id="infra-ts"></span>
</div>
<div class="grid">

  <div class="panel full">
    <div class="pt" onclick="openDispatchModal()" style="display:flex;align-items:center;justify-content:space-between;cursor:pointer">&#x1F4AC; Dispatch Task <span style="font-size:10px;color:#aaa;font-weight:400">tap to open ↗</span></div>
    <div class="dispatch-row">
      <div style="flex:0 0 180px">
        <div style="font-size:10px;color:#aaa;margin-bottom:4px">WORKER</div>
        <select id="d-worker" style="width:100%;background:#fff;border:1px solid #e0e0e0;border-radius:6px;color:#333;padding:5px 8px;font-size:12px;font-family:inherit;outline:none" onchange="workerChanged()">
          <option value="">— any idle worker —</option>
        </select>
      </div>
      <div style="flex:0 0 120px">
        <div style="font-size:10px;color:#aaa;margin-bottom:4px">DOMAIN</div>
        <select id="d-domain" style="width:100%;background:#fff;border:1px solid #e0e0e0;border-radius:6px;color:#333;padding:5px 8px;font-size:12px;font-family:inherit;outline:none">
          <option value="cx">cx</option>
          <option value="research">research</option>
          <option value="data">data</option>
          <option value="pr_review">pr_review</option>
          <option value="wacli">wacli</option>
          <option value="legal">legal</option>
        </select>
      </div>
      <div style="flex:1;min-width:240px;position:relative">
        <div style="font-size:10px;color:#aaa;margin-bottom:4px">TASK &nbsp;<span style="color:#ccc">type / for skills &amp; commands</span></div>
        <input id="d-task" placeholder="Describe the task… or /skill-name" autocomplete="off" style="width:100%;background:#fff;border:1px solid #e0e0e0;border-radius:6px;color:#333;padding:5px 10px;font-size:12px;font-family:inherit;outline:none">
        <div id="skill-drop" style="display:none;position:absolute;top:100%;left:0;right:0;background:#fff;border:1px solid #e0e0e0;border-radius:0 0 8px 8px;max-height:240px;overflow-y:auto;z-index:999;margin-top:-1px;box-shadow:0 4px 12px rgba(0,0,0,.1)"></div>
        <div id="hist-drop" style="display:none;position:absolute;top:100%;left:0;right:0;background:#fff;border:1px solid #e0e0e0;border-radius:0 0 8px 8px;max-height:280px;overflow-y:auto;z-index:1000;margin-top:-1px;box-shadow:0 4px 12px rgba(0,0,0,.1)"></div>
      </div>
      <button id="hist-btn" onclick="toggleHistDrop()" title="Recent tasks" style="background:#fff;border:1px solid #e0e0e0;color:#555;border-radius:6px;padding:7px 10px;cursor:pointer;font-size:13px;font-family:inherit;white-space:nowrap">&#x1F4CB;</button>
      <button onclick="dispatch()" style="background:#1a73e8;border:none;color:#fff;border-radius:6px;padding:7px 18px;cursor:pointer;font-size:12px;font-family:inherit;font-weight:600;white-space:nowrap">&#x25B6; Dispatch</button>
      <button onclick="dispatch(true)" style="background:#e65100;border:none;color:#fff;border-radius:6px;padding:7px 14px;cursor:pointer;font-size:12px;font-family:inherit;font-weight:600;white-space:nowrap">&#x26A1; Force</button>
      <div id="d-msg" style="font-size:11px;color:#999;align-self:center"></div>
    </div>
  </div>

  <!-- Tab bar -->
  <div class="full" style="display:flex;gap:4px;border-bottom:2px solid #f0f0f0;margin-bottom:-4px">
    <button class="tab-btn active" id="tbtn-workers" onclick="switchTab('workers')" style="background:none;border:none;border-bottom:2px solid #1a73e8;margin-bottom:-2px;padding:6px 14px;font-size:12px;font-weight:600;color:#1a73e8;cursor:pointer;font-family:inherit">Workers</button>
    <button class="tab-btn" id="tbtn-results" onclick="switchTab('results')" style="background:none;border:none;border-bottom:2px solid transparent;margin-bottom:-2px;padding:6px 14px;font-size:12px;font-weight:600;color:#aaa;cursor:pointer;font-family:inherit">Results <span id="res-badge" style="display:none;background:#1a73e8;color:#fff;border-radius:10px;padding:1px 6px;font-size:9px;margin-left:3px">0</span></button>
    <button class="tab-btn desktop-only" id="tbtn-terminals" onclick="switchTab('terminals')" style="background:none;border:none;border-bottom:2px solid transparent;margin-bottom:-2px;padding:6px 14px;font-size:12px;font-weight:600;color:#aaa;cursor:pointer;font-family:inherit">Terminals <span id="term-badge" style="display:none;background:#e65100;color:#fff;border-radius:10px;padding:1px 6px;font-size:9px;margin-left:3px">0</span></button>
    <button class="tab-btn" id="tbtn-manage" onclick="switchTab('manage')" style="background:none;border:none;border-bottom:2px solid transparent;margin-bottom:-2px;padding:6px 14px;font-size:12px;font-weight:600;color:#aaa;cursor:pointer;font-family:inherit">Manage</button>
    <div style="flex:1"></div>
    <a href="/dashboard/clean{q}" style="display:flex;align-items:center;gap:5px;padding:4px 12px;margin-bottom:-2px;border:1px solid #e8e8e8;border-bottom:none;border-radius:6px 6px 0 0;background:#fffdf5;color:#b08a2a;font-size:11px;font-weight:600;text-decoration:none;white-space:nowrap">✦ Clean UI</a>
  </div>

  <!-- Workers tab -->
  <div id="tab-workers" class="full">
    <div class="panel full">
      <div class="pt">Workers</div>
      <div class="tbl-wrap"><table>
        <thead><tr><th></th><th>Session</th><th class="col-hide">Domain</th><th>Status</th><th class="col-hide">Auth</th><th>Last Task</th><th class="col-kill" style="width:28px"></th></tr></thead>
        <tbody id="wb"></tbody>
      </table></div>
    </div>
    <div class="panel full">
      <div class="pt">Alerts &amp; Issues</div>
      <div id="alerts"><div class="empty">All clear</div></div>
    </div>
    <div class="grid" style="margin-top:0">
      <div class="panel">
        <div class="pt">Pending Questions</div>
        <div id="ql"><div class="empty">None</div></div>
      </div>
      <div class="panel">
        <div class="pt">Recent Completions</div>
        <div id="cl"><div class="empty">None yet</div></div>
      </div>
    </div>
  </div>

  <!-- Results tab -->
  <div id="tab-results" class="full" style="display:none;overflow:hidden">
    <div class="panel full" style="max-height:75vh;overflow-y:auto;overflow-x:hidden;word-break:break-word">
      <div class="pt" style="position:sticky;top:0;background:#fff;padding-bottom:8px;z-index:1;display:flex;align-items:center;gap:8px">
        Task Outputs
        <span id="res-count" style="color:#aaa;font-size:10px"></span>
        <button onclick="refreshResults()" style="margin-left:auto;background:none;border:1px solid #e0e0e0;border-radius:4px;padding:2px 8px;font-size:10px;cursor:pointer;color:#888">↻ Refresh</button>
      </div>
      <div id="rl"><div class="empty">Loading…</div></div>
    </div>
  </div>

  <!-- Terminals tab (desktop only) -->
  <div id="tab-terminals" class="full" style="display:none;flex-direction:column;gap:0">
    <div style="display:flex;align-items:center;gap:10px;padding:8px 0 6px;flex-shrink:0;position:relative">
      <span style="font-size:11px;color:#aaa">Active workers — live output</span>
      <button onclick="togglePinPicker()" id="pin-picker-btn" title="Pin a terminal to the grid" style="background:#1a3a1a;border:1px solid #2a6a2a;color:#6c6;border-radius:5px;padding:2px 9px;cursor:pointer;font-size:11px;font-family:inherit">+ Pin</button>
      <!-- Pin picker dropdown -->
      <div id="pin-picker" style="display:none;position:absolute;top:32px;left:0;background:#1e1e1e;border:1px solid #333;border-radius:8px;z-index:200;min-width:220px;box-shadow:0 4px 16px rgba(0,0,0,0.4);max-height:260px;overflow-y:auto">
        <div style="padding:6px 10px;font-size:10px;color:#666;border-bottom:1px solid #2a2a2a">Click to pin terminal</div>
        <div id="pin-picker-list"></div>
      </div>
      <label style="margin-left:auto;font-size:11px;color:#888;display:flex;align-items:center;gap:5px;cursor:pointer">
        <input type="checkbox" id="term-idle-toggle" onchange="renderTermGrid()" style="cursor:pointer"> show idle
      </label>
    </div>
    <div id="term-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,440px),1fr));gap:10px;overflow-y:auto;flex:1"></div>
    <div id="term-empty" style="display:none;padding:40px;text-align:center;color:#bbb;font-size:13px">No active workers right now</div>
  </div>

  <!-- Manage tab -->
  <div id="tab-manage" class="full" style="display:none">
    <div class="grid" style="margin-top:0">
      <div class="panel">
        <div style="display:flex;gap:0;margin-bottom:12px;border-bottom:1px solid #f0f0f0">
          <button id="mtab-spawn" onclick="mgTab('spawn')" style="background:none;border:none;border-bottom:2px solid #1a73e8;padding:4px 12px 8px;font-size:11px;font-weight:600;color:#1a73e8;cursor:pointer;font-family:inherit">&#x2795; Spawn New</button>
          <button id="mtab-attach" onclick="mgTab('attach')" style="background:none;border:none;border-bottom:2px solid transparent;padding:4px 12px 8px;font-size:11px;font-weight:600;color:#aaa;cursor:pointer;font-family:inherit">&#x1F517; Attach Existing</button>
          <button id="mtab-domain" onclick="mgTab('domain')" style="background:none;border:none;border-bottom:2px solid transparent;padding:4px 12px 8px;font-size:11px;font-weight:600;color:#aaa;cursor:pointer;font-family:inherit">&#x1F4C1; Add Domain</button>
          <button id="mtab-server" onclick="mgTab('server')" style="background:none;border:none;border-bottom:2px solid transparent;padding:4px 12px 8px;font-size:11px;font-weight:600;color:#aaa;cursor:pointer;font-family:inherit">&#x2699;&#xFE0F; Server</button>
        </div>
        <!-- Spawn form -->
        <div id="mg-spawn" style="display:flex;flex-direction:column;gap:10px">
          <div style="font-size:10px;color:#888;line-height:1.5">Creates a new tmux session, starts the model CLI inside it, and registers it as a worker.</div>
          <div>
            <div style="font-size:10px;color:#aaa;margin-bottom:4px">SESSION NAME</div>
            <input id="sp-name" placeholder="e.g. cx-bot-fix-6" style="width:100%;box-sizing:border-box;background:#fff;border:1px solid #e0e0e0;border-radius:6px;color:#333;padding:6px 10px;font-size:12px;font-family:inherit;outline:none">
          </div>
          <div>
            <div style="font-size:10px;color:#aaa;margin-bottom:4px">DOMAIN</div>
            <select id="sp-domain" style="width:100%;background:#fff;border:1px solid #e0e0e0;border-radius:6px;color:#333;padding:6px 8px;font-size:12px;font-family:inherit;outline:none">
              <option value="">— loading… —</option>
            </select>
          </div>
          <div>
            <div style="font-size:10px;color:#aaa;margin-bottom:4px">MODEL</div>
            <select id="sp-model" style="width:100%;background:#fff;border:1px solid #e0e0e0;border-radius:6px;color:#333;padding:6px 8px;font-size:12px;font-family:inherit;outline:none">
              <option value="claude">claude</option>
              <option value="codex">codex</option>
              <option value="kimi">kimi</option>
            </select>
          </div>
          <button onclick="spawnWorker()" style="background:#1a73e8;border:none;color:#fff;border-radius:6px;padding:8px;font-size:12px;font-family:inherit;font-weight:600;cursor:pointer">&#x25B6; Spawn Worker</button>
          <div id="sp-msg" style="font-size:11px;color:#999;min-height:16px"></div>
        </div>
        <!-- Attach form -->
        <div id="mg-attach" style="display:none;flex-direction:column;gap:10px">
          <div style="font-size:10px;color:#888;line-height:1.5">Register an existing tmux session as a worker without touching it.</div>
          <div>
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
              <span style="font-size:10px;color:#aaa">TMUX SESSION</span>
              <button onclick="loadAvailableSessions()" style="background:none;border:none;color:#1a73e8;font-size:10px;cursor:pointer;padding:0">↻ refresh</button>
            </div>
            <select id="at-name" style="width:100%;box-sizing:border-box;background:#fff;border:1px solid #e0e0e0;border-radius:6px;color:#333;padding:6px 8px;font-size:12px;font-family:inherit;outline:none">
              <option value="">— loading… —</option>
            </select>
          </div>
          <div>
            <div style="font-size:10px;color:#aaa;margin-bottom:4px">DOMAIN</div>
            <select id="at-domain" style="width:100%;box-sizing:border-box;background:#fff;border:1px solid #e0e0e0;border-radius:6px;color:#333;padding:6px 8px;font-size:12px;font-family:inherit;outline:none">
              <option value="">— loading… —</option>
            </select>
          </div>
          <button onclick="attachWorker()" style="background:#5a7a9a;border:none;color:#fff;border-radius:6px;padding:8px;font-size:12px;font-family:inherit;font-weight:600;cursor:pointer">&#x1F517; Attach Session</button>
          <div id="at-msg" style="font-size:11px;color:#999;min-height:16px"></div>
        </div>
        <!-- Add Domain form -->
        <div id="mg-domain" style="display:none;flex-direction:column;gap:10px">
          <div style="font-size:10px;color:#888;line-height:1.5">Create a new domain in workers.yaml. Workers assigned to this domain will only receive tasks dispatched to it.</div>
          <div>
            <div style="font-size:10px;color:#aaa;margin-bottom:4px">DOMAIN NAME</div>
            <input id="nd-name" placeholder="e.g. orchm" style="width:100%;box-sizing:border-box;background:#fff;border:1px solid #e0e0e0;border-radius:6px;color:#333;padding:6px 10px;font-size:12px;font-family:inherit;outline:none">
          </div>
          <div>
            <div style="font-size:10px;color:#aaa;margin-bottom:4px">SESSIONS (comma-separated, optional)</div>
            <input id="nd-sessions" placeholder="e.g. orchM" style="width:100%;box-sizing:border-box;background:#fff;border:1px solid #e0e0e0;border-radius:6px;color:#333;padding:6px 10px;font-size:12px;font-family:inherit;outline:none">
          </div>
          <div>
            <div style="font-size:10px;color:#aaa;margin-bottom:4px">KEYWORD HANDLES (comma-separated — what text triggers routing to this domain)</div>
            <input id="nd-handles" placeholder="e.g. orchm, orchestrate, meta" style="width:100%;box-sizing:border-box;background:#fff;border:1px solid #e0e0e0;border-radius:6px;color:#333;padding:6px 10px;font-size:12px;font-family:inherit;outline:none">
          </div>
          <div>
            <div style="font-size:10px;color:#aaa;margin-bottom:4px">MODEL</div>
            <select id="nd-model" style="width:100%;background:#fff;border:1px solid #e0e0e0;border-radius:6px;color:#333;padding:6px 8px;font-size:12px;font-family:inherit;outline:none">
              <option value="claude">claude</option>
              <option value="codex">codex</option>
              <option value="kimi">kimi</option>
            </select>
          </div>
          <button onclick="addDomain()" style="background:#388e3c;border:none;color:#fff;border-radius:6px;padding:8px;font-size:12px;font-family:inherit;font-weight:600;cursor:pointer">&#x1F4C1; Create Domain</button>
          <div id="nd-msg" style="font-size:11px;color:#999;min-height:16px"></div>
        </div>
        <!-- Server panel -->
        <div id="mg-server" style="display:none;flex-direction:column;gap:14px">
          <div style="font-size:10px;color:#888;line-height:1.5">Re-exec the server process in-place. Picks up code changes and clears in-memory state. Workers keep running — only the API restarts.</div>
          <button onclick="restartServer()" style="background:#c62828;border:none;color:#fff;border-radius:6px;padding:10px;font-size:13px;font-family:inherit;font-weight:600;cursor:pointer">&#x1F504; Restart Server</button>
          <div id="restart-msg" style="font-size:11px;color:#999;min-height:16px"></div>
          <div style="font-size:10px;color:#aaa;line-height:1.5">Auto-heal: watcher watchdog also restarts the server automatically if <code>/health</code> fails twice in a row (~60s recovery).</div>
        </div>
      </div>
      <div class="panel">
        <div class="pt">Infrastructure</div>
        <div id="il"></div>
      </div>
    </div>
  </div>

</div>
<script>
const B=window.location.origin;
const TQ='{q}';
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const INFRA=['orchmux-server','orchmux-supervisor','orchmux-watcher','orchmux-telegram','orchmux-monitor'];
const AUTH_SIGNS=['OAuth error','Invalid code','Paste code here',"Browser didn't open",'Press Enter to retry'];
const _alerts=[];

async function get(p){{try{{const r=await fetch(B+p+TQ);return r.ok?r.json():null}}catch{{return null}}}}

function pulseClass(s){{
  if(s==='busy')return 'p-busy';
  if(s==='waiting')return 'p-wait';
  if(s==='blocked'||s==='auth')return 'p-block';
  if(s==='missing')return 'p-miss';
  return 'p-idle';
}}

// ── skill autocomplete ────────────────────────────────────────────────────
const SKILLS=[
  // Deploy & Git
  {{n:'deploy',c:'deploy'}},{{n:'deploy-broadcast',c:'deploy'}},
  {{n:'hotfix-cycle',c:'deploy'}},{{n:'git-workflow',c:'code'}},
  // Code
  {{n:'fastapi-patterns',c:'code'}},{{n:'react-patterns',c:'code'}},
  {{n:'postgresql',c:'code'}},{{n:'pgvector',c:'code'}},{{n:'code-audit',c:'code'}},
  // Data
  {{n:'metabase',c:'data'}},{{n:'snowflake',c:'data'}},{{n:'data-audit',c:'data'}},
  {{n:'revenue-report',c:'data'}},{{n:'analytics',c:'data'}},
  // Ops
  {{n:'error-digest',c:'ops'}},{{n:'slack-thread-triage',c:'ops'}},
  {{n:'notion-action-extractor',c:'ops'}},
  // Docs & Content
  {{n:'google-workspace',c:'docs'}},{{n:'google-docs-formatting',c:'docs'}},
  {{n:'pptx-design',c:'docs'}},
  // Workflow commands
  {{n:'standup',c:'cmd'}},{{n:'triage',c:'cmd'}},{{n:'weekly',c:'cmd'}},
  {{n:'architect',c:'cmd'}},{{n:'debug',c:'cmd'}},{{n:'brainstorm',c:'cmd'}},
  {{n:'payments',c:'cmd'}},{{n:'inventory',c:'cmd'}},{{n:'pr-review-fix',c:'cmd'}},
];

let _skillIdx=-1;
let _skillFiltered=[];
let _skillActiveInput='';  // which input is driving the dropdown

function skillDropFor(inpId,dropId){{
  const inp=document.getElementById(inpId);
  const drop=document.getElementById(dropId);
  if(!inp||!drop)return;
  _skillActiveInput=inpId;
  const val=inp.value||inp.textContent||'';
  const slash=val.lastIndexOf('/');
  if(slash===-1){{drop.style.display='none';return;}}
  const query=val.slice(slash+1).toLowerCase();
  _skillFiltered=SKILLS.filter(s=>s.n.includes(query)||s.c.includes(query));
  _skillIdx=-1;
  if(!_skillFiltered.length){{drop.style.display='none';return;}}
  const dark=inpId==='dm-task';
  drop.innerHTML=_skillFiltered.map((s,i)=>
    `<div class="skill-item" data-i="${{i}}" onmousedown="pickSkillFor(${{i}},'${{inpId}}','${{dropId}}')">
      <span class="skill-name" style="${{dark?'color:#7ab':''}}"">/${{esc(s.n)}}</span>
      <span class="skill-cat">${{esc(s.c)}}</span>
    </div>`).join('');
  drop.style.display='block';
}}
function skillDrop(){{skillDropFor('d-task','skill-drop');}}

function pickSkillFor(i,inpId,dropId){{
  const s=_skillFiltered[i];
  if(!s)return;
  const inp=document.getElementById(inpId);
  const drop=document.getElementById(dropId);
  const slash=inp.value.lastIndexOf('/');
  inp.value=inp.value.slice(0,slash)+'/'+s.n+' ';
  if(drop)drop.style.display='none';
  inp.focus();
}}
function pickSkill(i){{pickSkillFor(i,'d-task','skill-drop');}}

function skillKeyNavFor(e,inpId,dropId){{
  const drop=document.getElementById(dropId);
  if(!drop||drop.style.display==='none')return;
  if(e.key==='ArrowDown'){{
    e.preventDefault();
    _skillIdx=Math.min(_skillIdx+1,_skillFiltered.length-1);
  }}else if(e.key==='ArrowUp'){{
    e.preventDefault();
    _skillIdx=Math.max(_skillIdx-1,-1);
  }}else if(e.key==='Enter'){{
    e.preventDefault();
    if(_skillIdx>=0)pickSkillFor(_skillIdx,inpId,dropId);
    return;
  }}else if(e.key==='Escape'){{
    drop.style.display='none';_skillIdx=-1;return;
  }}
  drop.querySelectorAll('.skill-item').forEach((el,i)=>
    el.classList.toggle('active',i===_skillIdx));
  if(_skillIdx>=0){{const el=drop.querySelector('.skill-item.active');if(el)el.scrollIntoView({{block:'nearest'}});}}
}}

// ── dispatch history ──────────────────────────────────────────────────────
let _history=[];
let _sessionLastTask={{}};  // session → {{task, domain, at}}

async function loadHistory(){{
  const h=await get('/dispatch-history');
  if(h&&Array.isArray(h)){{
    _history=h;
    // Build per-session last-task index (first entry per session = most recent)
    _sessionLastTask={{}};
    for(const d of h){{
      const s=d.session;
      if(s&&s!=='queued'&&!_sessionLastTask[s])_sessionLastTask[s]=d;
    }}
  }}
}}

function _renderHistDrop(){{
  const drop=document.getElementById('hist-drop');
  if(!_history.length){{
    drop.innerHTML='<div style="padding:10px 12px;font-size:11px;color:#999">No history yet</div>';
  }}else{{
    drop.innerHTML=_history.map((h,i)=>
      `<div class="skill-item" onclick="pickHistory(${{i}})" style="padding:8px 12px;cursor:pointer;border-bottom:1px solid #f0f0f0">
        <div style="font-size:11px;color:#333;line-height:1.4">${{esc(h.task.substring(0,120))}}${{h.task.length>120?'…':''}}</div>
        <div style="font-size:10px;color:#999;margin-top:2px">${{esc(h.domain)}}${{h.session&&h.session!=='queued'?' → '+esc(h.session):''}} &nbsp;·&nbsp; ${{esc(h.at)}}</div>
      </div>`).join('');
  }}
  drop.style.display='block';
}}

async function toggleHistDrop(){{
  const drop=document.getElementById('hist-drop');
  if(drop.style.display!=='none'){{drop.style.display='none';return;}}
  // Always fetch fresh — don't rely on stale in-memory list
  await loadHistory();
  _renderHistDrop();
}}

function pickHistory(i){{
  const h=_history[i];
  if(!h)return;
  document.getElementById('d-task').value=h.task;
  document.getElementById('d-domain').value=h.domain||'cx';
  // Pre-select the worker if it still exists
  const wsel=document.getElementById('d-worker');
  if(h.session&&h.session!=='queued'){{
    for(let j=0;j<wsel.options.length;j++){{
      if(wsel.options[j].value===h.session){{wsel.value=h.session;break;}}
    }}
  }}
  document.getElementById('hist-drop').style.display='none';
  document.getElementById('d-task').focus();
}}

document.addEventListener('DOMContentLoaded',()=>{{
  const inp=document.getElementById('d-task');
  inp.addEventListener('input',skillDrop);
  inp.addEventListener('keydown',e=>{{
    const drop=document.getElementById('skill-drop');
    if(drop&&drop.style.display!=='none'){{skillKeyNavFor(e,'d-task','skill-drop');return;}}
    if(e.key==='Enter'){{e.preventDefault();dispatch();}}
  }});
  document.addEventListener('click',e=>{{
    if(!e.target.closest('#d-task')&&!e.target.closest('#skill-drop'))
      document.getElementById('skill-drop').style.display='none';
    if(!e.target.closest('#dm-task')&&!e.target.closest('#dm-skill-drop'))
      {{const d=document.getElementById('dm-skill-drop');if(d)d.style.display='none';}}
    if(!e.target.closest('#hist-drop')&&!e.target.closest('#hist-btn'))
      document.getElementById('hist-drop').style.display='none';
    if(!e.target.closest('#pin-picker')&&!e.target.closest('#pin-picker-btn'))
      {{const p=document.getElementById('pin-picker');if(p)p.style.display='none';}}
  }});
}});

// ── per-component refresh ─────────────────────────────────────────────────
const _openPanes   = new Set();
const _paneLive    = new Set();   // sessions in live-append mode
const _paneLinesCt = {{}};         // session → last line count seen
let   _liveTicker  = null;        // single global ticker — no per-pane intervals

let _paneModalSession='';
let _paneMode=localStorage.getItem('paneMode')||'modern';
let _paneFontSize=parseInt(localStorage.getItem('paneFontSize')||'11');
let _paneNewLineCt=0;
let _paneNavList=[];
let _paneLogOffset={{}};   // session → lines already loaded from end
let _paneLogDone={{}};     // session → true when beginning of log reached
let _paneLogLoading=false;
let _touchStartX=0;

function stripAnsi(s){{
  // Strip all CSI escape sequences and OSC sequences
  return s.replace(/\\x1b\\[[0-9;?]*[a-zA-Z~]/g,'')
          .replace(/\\x1b\\][^\\x07\\x1b]*(\\x07|\\x1b\\\\)/g,'')
          .replace(/\\x1b[^\\[\\]]/g,'')
          .replace(/\\r/g,'');
}}
function cleanTermOutput(s){{
  // Strip terminal noise — mdParse handles all block detection directly
  const lines=s.split('\\n');
  const out=[];let blanks=0;
  for(const line of lines){{
    const t=line.trim();
    if(t.length>0&&/^[─-╿\\s]+$/.test(t))continue;
    if(/^[-─━=╌]{{4,}}$/.test(t))continue;
    if(/^\\[\\?[0-9]+[hl]/.test(t))continue;
    if(/^\\x1b/.test(t))continue;
    if(/^[❯>$#]\\s*$/.test(t))continue;
    if(t===''){{blanks++;if(blanks<=1)out.push('');continue;}}
    blanks=0;out.push(line);
  }}
  while(out.length&&out[out.length-1].trim()==='')out.pop();
  return out.join('\\n');
}}
function _applyPaneStyle(){{
  const body=document.getElementById('pane-modal-body');
  if(!body)return;
  body.style.fontSize=_paneFontSize+'px';
  const isModern=_paneMode==='modern';
  if(isModern){{
    body.classList.add('pane-modern');
    body.style.color='';
    body.style.fontFamily='';
    body.style.whiteSpace='normal';
    body.style.wordBreak='normal';
    body.style.background='#fff';
  }}else{{
    body.classList.remove('pane-modern');
    body.style.color='#a8cc88';
    body.style.fontFamily="'SF Mono','Fira Code',monospace";
    body.style.whiteSpace='pre-wrap';
    body.style.wordBreak='break-all';
    body.style.background='#1e1e1e';
  }}
  const btn=document.getElementById('pane-mode-btn');
  if(btn)btn.textContent=isModern?'⬛ Term':'☀ Modern';
}}
function _setPaneFontSize(delta){{
  _paneFontSize=Math.max(9,Math.min(18,_paneFontSize+delta));
  localStorage.setItem('paneFontSize',_paneFontSize);
  _applyPaneStyle();
}}
function _togglePaneMode(){{
  _paneMode=_paneMode==='terminal'?'modern':'terminal';
  localStorage.setItem('paneMode',_paneMode);
  _paneNewLineCt=0;
  if(_paneModalSession)refreshPaneModal(_paneModalSession);
}}
function scrollPaneToBottom(){{
  const body=document.getElementById('pane-modal-body');
  if(body)body.scrollTop=body.scrollHeight;
  const pill=document.getElementById('new-out-pill');
  if(pill)pill.style.display='none';
}}
function _togglePaneFS(){{
  const m=document.getElementById('pane-modal');
  const btn=document.getElementById('pane-fs-btn');
  if(m.classList.toggle('fullscreen')){{btn.textContent='⊡';btn.title='Exit fullscreen';}}
  else{{btn.textContent='⛶';btn.title='Fullscreen';}}
}}
function _updatePaneNav(){{
  const el=document.getElementById('pane-nav-label');
  if(!el||!_paneModalSession)return;
  const idx=_paneNavList.indexOf(_paneModalSession);
  el.textContent=idx>=0?`${{idx+1}} / ${{_paneNavList.length}}`:'';
}}
function navPane(dir){{
  if(!_paneNavList.length)return;
  const idx=_paneNavList.indexOf(_paneModalSession);
  const next=_paneNavList[(idx+dir+_paneNavList.length)%_paneNavList.length];
  if(next)openPaneModal(next);
}}
document.addEventListener('keydown',e=>{{
  if(!_paneModalSession)return;
  if(e.key==='ArrowLeft')navPane(-1);
  if(e.key==='ArrowRight')navPane(1);
}});

// Preload pane data for adjacent workers so nav feels instant
async function _preloadAdjacentPanes(session){{
  const idx=_paneNavList.indexOf(session);
  if(idx<0||_paneNavList.length<2)return;
  const toLoad=[];
  const prev=_paneNavList[(idx-1+_paneNavList.length)%_paneNavList.length];
  const next=_paneNavList[(idx+1)%_paneNavList.length];
  if(prev&&prev!==session)toLoad.push(prev);
  if(next&&next!==session&&next!==prev)toLoad.push(next);
  for(const s of toLoad){{
    const cached=_paneCache[s];
    if(cached&&Date.now()-cached.ts<5000)continue;  // fresh enough
    get('/pane/'+encodeURIComponent(s)+'?lines=200').then(d=>{{
      if(d&&d.output!==undefined)_paneCache[s]={{output:d.output,ts:Date.now()}};
    }});
  }}
}}

async function loadMorePaneLines(session){{
  if(_paneLogLoading||_paneLogDone[session])return;
  const body=document.getElementById('pane-modal-body');
  if(!body)return;
  _paneLogLoading=true;
  // Show loading indicator at top
  let ind=document.getElementById('log-load-ind');
  if(!ind){{ind=document.createElement('div');ind.id='log-load-ind';ind.className='log-load-indicator';ind.textContent='Loading earlier output…';body.insertBefore(ind,body.firstChild);}}
  const skip=_paneLogOffset[session]||200;  // start above the ~200 lines already shown via tmux
  const d=await get('/pane-log/'+encodeURIComponent(session)+'?skip='+skip+'&lines=150');
  ind=document.getElementById('log-load-ind');if(ind)ind.remove();
  if(!d||!d.lines||!d.lines.length){{_paneLogDone[session]=true;_paneLogLoading=false;return;}}
  const prevH=body.scrollHeight,prevT=body.scrollTop;
  const chunk=document.createElement('div');chunk.className='log-history-chunk';
  const clean=cleanTermOutput(stripAnsi(d.lines.join('\\n')));
  if(_paneMode==='modern')chunk.innerHTML=mdParse(clean);
  else{{chunk.style.cssText='white-space:pre-wrap;font-family:monospace;font-size:11px;color:#a8cc88';chunk.textContent=clean;}}
  if(!d.has_more){{
    const m=document.createElement('div');m.className='session-start-marker';m.textContent='── session start ──';
    body.insertBefore(m,body.firstChild);
  }}
  body.insertBefore(chunk,body.firstChild);
  // Restore scroll position so view doesn't jump
  body.scrollTop=prevT+(body.scrollHeight-prevH);
  _paneLogOffset[session]=d.skip_next;
  if(!d.has_more)_paneLogDone[session]=true;
  _paneLogLoading=false;
}}

function _setupPaneScrollLog(session){{
  const body=document.getElementById('pane-modal-body');
  if(!body)return;
  const prev=body.onscroll;
  body.onscroll=()=>{{
    if(prev)prev();
    if(body.scrollTop<80&&!_paneLogLoading&&!_paneLogDone[session])loadMorePaneLines(session);
  }};
}}

function togglePane(session){{
  if(_paneModalSession===session){{
    closePaneModal();
  }}else{{
    openPaneModal(session);
  }}
}}
function openPaneModal(session){{
  _paneModalSession=session;
  _paneNewLineCt=0;
  // Reset scroll-log state for this session
  delete _paneLogOffset[session];
  delete _paneLogDone[session];
  _paneLogLoading=false;
  _openPanes.add(session);
  document.getElementById('pane-modal').style.display='flex';
  document.getElementById('pane-modal-title').textContent=session;
  document.getElementById('pane-modal-ts').textContent='';
  const gb=document.getElementById('pane-grid-btn');
  if(gb){{gb.textContent='⊞ Grid';gb.style.background='#2a1a4a';gb.style.borderColor='#5a2a9a';gb.style.color='#b8a';}}
  _updatePaneNav();
  const pill=document.getElementById('new-out-pill');
  if(pill)pill.style.display='none';
  const body=document.getElementById('pane-modal-body');
  // Show from cache immediately if available, else show Loading
  const cached=_paneCache[session];
  if(cached&&cached.output){{}};
  body.innerHTML='<span style="color:#888;font-size:11px">Loading…</span>';
  _applyPaneStyle();
  // If cached, render it immediately then fetch fresh
  if(cached&&cached.output){{
    setTimeout(()=>refreshPaneModalFromCache(session,cached.output),0);
  }}
  refreshPaneModal(session);
  _preloadAdjacentPanes(session);
  // Auto-enable Live in Modern mode
  if(_paneMode==='modern'&&!_paneLive.has(session)){{
    _paneLive.add(session);
    document.getElementById('pane-modal-live').textContent='⏹ Stop';
    if(!_liveTicker)_liveTicker=setInterval(()=>{{
      for(const ls of _paneLive)if(ls===_paneModalSession)refreshPaneModal(ls);
    }},3000);
  }}
}}

function refreshPaneModalFromCache(session,raw){{
  if(_paneModalSession!==session)return;
  const body=document.getElementById('pane-modal-body');
  if(!body||body.textContent!=='Loading…')return;
  const clean=cleanTermOutput(stripAnsi(raw));
  body.innerHTML=mdParse(clean);
  _applyPaneStyle();
  requestAnimationFrame(()=>body.scrollTop=body.scrollHeight);
  const footer=document.getElementById('pane-ts-footer');
  if(footer){{footer.textContent='Cached '+new Date().toLocaleTimeString();footer.className='pane-ts-footer '+(_paneMode==='modern'?'':'dark');}}
}}
function closePaneModal(){{
  const m=document.getElementById('pane-modal');
  m.classList.remove('fullscreen');
  const btn=document.getElementById('pane-fs-btn');
  if(btn){{btn.textContent='⛶';btn.title='Fullscreen';}}
  m.style.display='none';
  if(_paneModalSession){{
    _openPanes.delete(_paneModalSession);
    _stopLive(_paneModalSession);
    _paneModalSession='';
  }}
}}
async function refreshPaneModal(session){{
  if(!session)return;
  const d=await get('/pane/'+encodeURIComponent(session)+'?lines=200');
  const body=document.getElementById('pane-modal-body');
  const pill=document.getElementById('new-out-pill');
  if(!d||!d.exists){{body.textContent='Session not found';return;}}
  if(_paneModalSession!==session)return;  // navigated away while fetching
  const raw=d.output||'';
  _paneCache[session]={{output:raw,ts:Date.now()}};
  const newLineCt=raw.split('\\n').length;
  const wasEmpty=body.textContent.length<10;
  const savedScrollTop=body.scrollTop;
  // Don't clobber active text selection — skip this refresh cycle
  const _sel=window.getSelection();
  if(_sel&&_sel.toString().length>0&&body.contains(_sel.anchorNode))return;
  if(_paneMode==='modern'){{
    const clean=cleanTermOutput(stripAnsi(raw));
    body.innerHTML=mdParse(clean);
    // Dispatched task banner — shows the last task sent to this session
    const _lt=_sessionLastTask[session];
    if(_lt){{
      const bn=document.createElement('div');bn.className='task-dispatch-banner';
      bn.innerHTML='<div class="tdb-header"><span class="tdb-label">&#x25B6; Task</span>'
        +'<span class="tdb-domain">'+esc(_lt.domain||'')+'</span>'
        +'<span class="tdb-time">'+esc(_lt.at||'')+'</span></div>'
        +'<div class="tdb-text">'+esc(_lt.task||'').replace(/\\n/g,'<br>')+'</div>';
      body.insertBefore(bn,body.firstChild);
    }}
    // Copy buttons for code blocks
    body.querySelectorAll('pre').forEach(pre=>{{
      const btn=document.createElement('button');
      btn.className='pane-copy';btn.textContent='Copy';
      btn.onclick=()=>{{navigator.clipboard.writeText(pre.querySelector('code')?.textContent||pre.textContent);btn.textContent='✓';setTimeout(()=>btn.textContent='Copy',1500);}};
      pre.appendChild(btn);
    }});
    // Copy buttons for tables (TSV format for pasting into spreadsheets)
    body.querySelectorAll('table').forEach(tbl=>{{
      const wrap=document.createElement('div');wrap.className='tbl-wrap';
      tbl.parentNode.insertBefore(wrap,tbl);wrap.appendChild(tbl);
      const btn=document.createElement('button');btn.className='tbl-copy';btn.textContent='⎘ Copy';
      btn.onclick=e=>{{
        e.stopPropagation();
        const tsv=[...tbl.querySelectorAll('tr')].map(r=>[...r.querySelectorAll('th,td')].map(c=>c.textContent.trim()).join('\\t')).join('\\n');
        navigator.clipboard.writeText(tsv);
        btn.textContent='✓ Copied';setTimeout(()=>btn.textContent='⎘ Copy',1500);
      }};
      wrap.appendChild(btn);
    }});
  }}else{{
    body.innerHTML=raw.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }}
  const tsStr='Updated '+new Date().toLocaleTimeString();
  document.getElementById('pane-modal-ts').textContent='';
  const footer=document.getElementById('pane-ts-footer');
  if(footer){{footer.textContent=tsStr;footer.className='pane-ts-footer '+(_paneMode==='modern'?'':'dark');}}
  _applyPaneStyle();
  if(wasEmpty){{
    // First load — scroll to bottom so latest output is visible
    requestAnimationFrame(()=>body.scrollTop=body.scrollHeight);
    if(pill)pill.style.display='none';
  }}else{{
    // Live update — lock scroll position; new content appends below, user scrolls down when ready
    body.scrollTop=savedScrollTop;
    if(newLineCt>_paneNewLineCt&&_paneNewLineCt>0){{
      const added=newLineCt-_paneNewLineCt;
      if(pill){{pill.style.display='block';pill.textContent='↓ '+added+' new line'+(added!==1?'s':'');}}
    }}
  }}
  _paneNewLineCt=newLineCt;
  if(wasEmpty)_setupPaneScrollLog(session);
  body.onscroll=()=>{{
    if(body.scrollHeight-body.scrollTop-body.clientHeight<4&&pill)pill.style.display='none';
    if(body.scrollTop<80&&!_paneLogLoading&&!_paneLogDone[session])loadMorePaneLines(session);
  }};
}}
function toggleLiveModal(){{
  if(!_paneModalSession)return;
  const s=_paneModalSession;
  if(_paneLive.has(s)){{
    _stopLive(s);
    document.getElementById('pane-modal-live').textContent='⏵ Live';
  }}else{{
    _paneLive.add(s);
    document.getElementById('pane-modal-live').textContent='⏹ Stop';
    if(!_liveTicker)_liveTicker=setInterval(()=>{{
      for(const ls of _paneLive)if(ls===_paneModalSession)refreshPaneModal(ls);
    }},3000);
  }}
}}

function _stopLive(session){{
  _paneLive.delete(session);
  if(_paneLive.size===0&&_liveTicker){{
    clearInterval(_liveTicker);
    _liveTicker=null;
  }}
  const btn=document.getElementById('pl-'+session);
  if(btn)btn.textContent='⏵ Live';
}}

function toggleLive(session){{
  if(_paneLive.has(session)){{
    _stopLive(session);
  }}else{{
    delete _paneLinesCt[session];   // force full load on first tick
    _paneLive.add(session);
    _livePane(session);             // immediate first fetch
    if(!_liveTicker){{              // one global ticker for ALL live panes
      _liveTicker=setInterval(async()=>{{
        for(const s of _paneLive)await _livePane(s);
      }},2500);
    }}
    const btn=document.getElementById('pl-'+session);
    if(btn)btn.textContent='⏹ Stop';
  }}
}}

async function _livePane(session){{
  const d=await get('/pane/'+session+(TQ||'?')+'&lines=300');
  const el=document.getElementById('pane-body-'+session);
  if(!el||!d)return;
  const raw=d.output||'';
  const newLines=raw.split('\\n');
  const oldCt=_paneLinesCt[session];
  const atBottom=el.scrollHeight-el.scrollTop-el.clientHeight<4;
  if(oldCt==null||newLines.length<oldCt-5){{
    // First load or terminal was cleared — full replace
    el.textContent=raw;
    _paneLinesCt[session]=newLines.length;
  }}else if(newLines.length>oldCt){{
    // Append only the new tail as a separate text node — never touches existing nodes
    // so scroll position is preserved
    const added=newLines.slice(oldCt).join('\\n');
    if(added.trim())el.appendChild(document.createTextNode(added));
    _paneLinesCt[session]=newLines.length;
  }}
  if(atBottom)requestAnimationFrame(()=>{{el.scrollTop=el.scrollHeight;}});
  const ts=document.getElementById('pane-ts-'+session);
  if(ts)ts.textContent=new Date().toLocaleTimeString();
}}

function refreshPaneBtn(session,btn){{
  if(btn){{btn.textContent='…';btn.disabled=true;}}
  refreshPaneModal(session).then(()=>{{
    if(btn){{btn.textContent='↺';btn.disabled=false;}}
  }});
}}

async function refreshPane(session){{ return refreshPaneModal(session); }}

async function refreshWorkers(){{
  const [status,wdet]=await Promise.all([
    get('/status'),
    get('/worker-details'+(TQ||'?'))
  ]);
  if(!status)return;
  const alerts=[];
  const tbody=document.getElementById('wb');
  // Flatten all workers, attach domain + det, sort by last dispatch desc
  const allWorkers=[];
  for(const[domain,cfg]of Object.entries(status)){{
    for(const w of cfg.workers||[])allWorkers.push({{...w,_domain:domain,_det:(wdet&&wdet[w.session])||{{}}}});
  }}
  allWorkers.sort((a,b)=>{{
    const ta=a._det.last_task_time||'';
    const tb=b._det.last_task_time||'';
    if(ta&&tb)return tb.localeCompare(ta);
    if(ta)return -1;
    if(tb)return 1;
    return a.session.localeCompare(b.session);
  }});
  _paneNavList=allWorkers.map(w=>w.session);
  _updatePaneNav();
  let rows='';
  for(const w of allWorkers){{
    const domain=w._domain;
    const det=w._det;
    let s=w.exists?w.status:'missing';
      let alertTag='';
      let authBadge='<span style="color:#ddd">—</span>';
      if(det.auth==='ok')authBadge='<span style="color:#2e7d32;font-size:11px;font-weight:500">&#x2713; authed</span>';
      else if(det.auth==='auth_error')authBadge='<span style="color:#c62828;font-size:11px;font-weight:600">&#x26A0; OAuth</span>';
      else if(det.auth==='loading')authBadge='<span style="color:#e65100;font-size:11px">&#x22EF; loading</span>';
      else if(det.auth==='missing')authBadge='<span style="color:#bbb;font-size:11px">&#x2715; missing</span>';
      let lastTaskCell='<span style="color:#ddd">—</span>';
      if(det.last_task){{
        const stBadge=det.last_task_status==='done'?'<span style="color:#2e7d32;font-size:9px;font-weight:600">done</span>':
                      det.last_task_status==='pending'?'<span style="color:#e65100;font-size:9px;font-weight:600">running</span>':
                      '<span style="color:#aaa;font-size:9px">'+esc(det.last_task_status||'')+'</span>';
        const ts=det.last_task_time?det.last_task_time.substring(11,16)+' UTC':'';
        lastTaskCell=`${{stBadge}} <span style="color:#555;font-size:11px" title="${{esc(det.last_task)}}">${{esc(det.last_task.substring(0,60))}}</span><span style="color:#ccc;font-size:9px;margin-left:4px">${{esc(ts)}}</span>`;
      }}
      if(det.auth==='auth_error'||(w.current_task&&AUTH_SIGNS.some(a=>w.current_task.includes(a)))){{
        s='auth'; alertTag='<span class="tag tag-auth">auth stuck</span>';
        alerts.push({{icon:'&#x1F512;',title:`${{esc(w.session)}} — auth stuck`,detail:'OAuth loop. Watchdog auto-relaunching.',type:'auth'}});
      }}
      if(s==='blocked'){{
        alertTag='<span class="tag tag-dead">needs input</span>';
        alerts.push({{icon:'&#x26A0;&#xFE0F;',title:`${{esc(w.session)}} — blocked`,detail:esc(w.current_task||''),type:'block'}});
      }}
      if(s==='missing'){{
        alertTag='<span class="tag tag-dead">missing</span>';
        alerts.push({{icon:'&#x1F534;',title:`${{esc(w.session)}} — session dead`,detail:'Watchdog relaunching...',type:'dead'}});
      }}
      const displayName=det.display_name?`<span style="font-size:10px;color:#1a73e8;display:block;line-height:1.2">${{esc(det.display_name)}}</span>`:'';
      const roleHint=det.role?`<span style="font-size:9px;color:#aaa;display:block;line-height:1.3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:110px">${{esc(det.role.substring(0,40))}}</span>`:'';
      rows+=`<tr class="worker-row" onclick="togglePane('${{esc(w.session)}}')">
        <td><span class="pulse ${{pulseClass(s)}}"></span></td>
        <td style="color:#333;font-size:12px;font-weight:500;max-width:110px;word-break:break-all">${{esc(w.session)}}${{displayName}}${{roleHint}}</td>
        <td class="dim col-hide">${{esc(domain)}}</td>
        <td><span class="b ${{s}}">${{esc(s)}}</span>${{alertTag}}</td>
        <td class="col-hide">${{authBadge}}</td>
        <td class="task" style="max-width:200px">${{lastTaskCell}}</td>
        <td class="col-kill" onclick="event.stopPropagation()" style="padding:0 4px;text-align:right;white-space:nowrap">
          <span onclick="openMetaModal('${{esc(w.session)}}','${{esc(det.display_name||'')}}','${{esc(det.role||'')}}')" title="Edit worker name/role" style="cursor:pointer;color:#ddd;font-size:12px;padding:2px 4px;border-radius:3px;user-select:none;margin-right:2px" onmouseover="this.style.color='#1a73e8'" onmouseout="this.style.color='#ddd'">&#x270E;</span>
          <span data-kill="${{esc(w.session)}}" onclick="killSession('${{esc(w.session)}}')" title="Kill session" style="cursor:pointer;color:#ddd;font-size:13px;padding:2px 4px;border-radius:3px;user-select:none" onmouseover="this.style.color='#e53935'" onmouseout="this.style.color='#ddd'">&#x2715;</span>
        </td>
      </tr>`;
  }}
  tbody.innerHTML=rows||'<tr><td colspan="7" class="empty">No workers</td></tr>';
  // Update terminals badge with active worker count
  {{const busy=Object.values(status).flatMap(c=>c.workers||[]).filter(w=>w.exists&&(w.status==='busy'||w.status==='waiting'||w.status==='blocked')).length;
  const b=document.getElementById('term-badge');if(b){{b.textContent=busy;b.style.display=busy>0?'':'none';}}}}
  // Browser notifications: fire when busy → idle (task done)
  if(_notifSeeded){{
    for(const[domain,cfg]of Object.entries(status)){{
      for(const w of cfg.workers||[]){{
        const prev=_prevWorkerStatus[w.session];
        const cur=w.exists?w.status:'missing';
        if(prev==='busy'&&(cur==='idle'||cur==='waiting')){{
          const det=wdet&&wdet[w.session]||{{}};
          _workerDoneNotif(w.session,det.display_name,det.last_task);
        }}
        _prevWorkerStatus[w.session]=cur;
      }}
    }}
  }}else{{
    // Seed on first poll — don't notify for already-finished workers
    for(const[domain,cfg]of Object.entries(status)){{
      for(const w of cfg.workers||[]){{
        _prevWorkerStatus[w.session]=w.exists?w.status:'missing';
      }}
    }}
    _notifSeeded=true;
  }}
  // If modal is open for a session that got removed, close it
  if(_paneModalSession&&!_openPanes.has(_paneModalSession))closePaneModal();
  document.getElementById('alerts').innerHTML=alerts.length?alerts.map(a=>
    `<div class="alert-row"><div class="alert-icon">${{a.icon}}</div>
     <div class="alert-body"><div class="alert-title">${{a.title}}</div>
     <div class="alert-detail">${{a.detail}}</div></div></div>`).join('')
    :'<div class="empty">All clear &#x2713;</div>';
}}

async function refreshHealth(){{
  const h=await get('/health');
  if(!h)return;
  document.getElementById('sw').textContent=h.workers;
  document.getElementById('sq').textContent=h.queued;
  document.getElementById('st').textContent=h.temp_workers;
  document.getElementById('ts').textContent='updated '+new Date().toLocaleTimeString();
}}

async function refreshInfra(){{
  const raw=await get('/infra-status'+(TQ||'?'));
  // Normalise: endpoint returns an array; convert to dict keyed by session name
  const infra={{}};
  if(Array.isArray(raw)){{
    raw.forEach(s=>{{ infra[s.session]=s; }});
  }} else if(raw){{
    Object.assign(infra,raw);
  }}
  // Header bar — always update even if infra fetch fails (show red)
  const map={{'orchmux-server':'server','orchmux-supervisor':'supervisor',
              'orchmux-watcher':'watcher','orchmux-telegram':'telegram','orchmux-monitor':'monitor'}};
  for(const[key,label]of Object.entries(map)){{
    const el=document.getElementById('ib-'+label);
    if(!el)continue;
    const svc=infra[key];
    const alive=svc&&(svc.up!==undefined?svc.up:!!svc);
    el.className='infra-chip '+(alive?'alive':'dead');
    el.innerHTML=`<span class="cdot"></span>${{esc(label)}}${{alive?'':' &#x26A0;'}}`;
  }}
  const ts=document.getElementById('infra-ts');
  if(ts)ts.textContent=new Date().toLocaleTimeString();
  if(!raw)return;
  // Bottom panel — show last_line if available
  document.getElementById('il').innerHTML=INFRA.map(n=>{{
    const svc=infra[n];
    const alive=svc&&(svc.up!==undefined?svc.up:!!svc);
    const lastLine=svc&&svc.last_line?`<div class="dim" style="margin-top:2px;font-size:10px;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${{esc(svc.last_line)}}</div>`:'';
    return `<div class="infra-row"><div class="dot${{alive?'':' dead'}}"></div>
     <div style="flex:1"><div style="color:#555">${{esc(svc&&svc.name||n)}}</div>${{lastLine}}</div>
     <div class="dim">${{alive?'&#x2713; running':'&#x2715; dead'}}</div></div>`;
  }}).join('');
}}

async function refreshResults(){{
  const d=await get('/results'+(TQ||'?')+'&limit=50');
  if(!d)return;
  document.getElementById('res-count').textContent=`${{d.length}} tasks`;
  // Badge: show count of new results since last Results tab visit
  if(_activeTab!=='results'){{
    const badge=document.getElementById('res-badge');
    const newCount=d.length-(_resSeenCount||0);
    if(badge&&newCount>0){{badge.textContent=newCount;badge.style.display='inline';}}
  }}else{{
    _resSeenCount=d.length;
  }}
  const _rdata={{}};
  document.getElementById('rl').innerHTML=d.length?d.map((r,i)=>{{
    const icon=r.success!==false?'&#x2705;':'&#x274C;';
    const ts=(r.completed_at||'').substring(0,16).replace('T',' ');
    const uid='rc'+i;
    const resultFull=r.result||'';
    _rdata[uid]=resultFull;
    const hasMore=resultFull.length>400;
    const resultHtml=resultFull?mdParse(resultFull):'<span style="color:#bbb;font-style:italic">completed — no summary captured</span>';
    const shortHtml=resultFull.length>400?mdParse(resultFull.substring(0,400)+'…'):resultHtml;
    return `<div class="card" style="padding:10px 0">
      <div class="card-head" style="gap:6px;margin-bottom:6px">
        <span>${{icon}}</span>
        <code style="font-size:11px;color:#1a73e8;background:#f0f7ff;padding:1px 6px;border-radius:3px">${{esc(r.session||r.domain||'?')}}</code>
        <span style="font-size:10px;color:#aaa">[${{esc(r.domain||'')}}]</span>
        <span style="color:#2a2a2a;font-size:10px;margin-left:auto;white-space:nowrap">${{esc(ts)}}</span>
      </div>
      <div style="font-size:11px;color:#555;margin-bottom:6px;line-height:1.4;font-style:italic">${{esc(r.task||'')}}</div>
      <div class="card-body md-result" style="color:#333;line-height:1.6;background:#fafafa;padding:8px;border-radius:4px;font-size:11.5px">${{shortHtml}}</div>
      <div style="margin-top:4px;display:flex;gap:4px;flex-wrap:wrap">
        ${{hasMore?`<button onclick="openResultModal('${{esc(r.session)}}','${{esc(ts)}}',_rdata['${{uid}}'])" style="background:none;border:none;color:#1a73e8;font-size:10px;cursor:pointer;padding:2px 0">&#x25BC; Full output</button>`:''}}
        <button onclick="loadNotes('${{esc(r.session)}}')" style="background:none;border:none;color:#888;font-size:10px;cursor:pointer;padding:2px 0;margin-left:${{hasMore?'8':'0'}}px">&#x1F4D3; Notes</button>
      </div>
      </div>`;
  }}).join(''):'<div class="empty">No completed tasks yet</div>';
}}
function openResultModal(session,ts,fullText){{
  document.getElementById('result-modal-title').textContent=session;
  document.getElementById('result-modal-ts').textContent=ts;
  const body=document.getElementById('result-modal-body');
  body.innerHTML=mdParse(fullText);
  document.getElementById('result-modal').style.display='flex';
}}
function closeResultModal(){{
  document.getElementById('result-modal').style.display='none';
}}

let _sessionDomainMap={{}};
async function openDispatchModal(){{
  const modal=document.getElementById('dispatch-modal');
  modal.style.display='flex';
  // Sync worker dropdown
  const w=document.getElementById('d-worker');
  const dmw=document.getElementById('dm-worker');
  if(w&&dmw)dmw.innerHTML=w.innerHTML;
  // Load session→domain map and domains in parallel
  const [sdMap]=await Promise.all([get('/session-domains'),loadDomains()]);
  if(sdMap)_sessionDomainMap=sdMap;
  // Set domain to match currently selected worker, or fall back to first domain
  const dmd=document.getElementById('dm-domain');
  const selWorker=dmw?dmw.value:'';
  const resolvedDomain=_sessionDomainMap[selWorker]||document.getElementById('d-domain')?.value||'';
  if(dmd&&resolvedDomain)dmd.value=resolvedDomain;
  setTimeout(()=>document.getElementById('dm-task').focus(),100);
}}
function closeDispatchModal(){{
  document.getElementById('dispatch-modal').style.display='none';
  document.getElementById('dm-msg').textContent='';
}}

let _metaSession='';
function openMetaModal(session,name,role){{
  _metaSession=session;
  document.getElementById('meta-modal-title').textContent='Edit: '+session;
  document.getElementById('meta-name').value=name||'';
  document.getElementById('meta-role').value=role||'';
  document.getElementById('meta-msg').textContent='';
  document.getElementById('meta-modal').style.display='flex';
  setTimeout(()=>document.getElementById('meta-name').focus(),100);
}}
function closeMetaModal(){{
  document.getElementById('meta-modal').style.display='none';
}}
async function saveMeta(){{
  const name=document.getElementById('meta-name').value.trim();
  const role=document.getElementById('meta-role').value.trim();
  const msg=document.getElementById('meta-msg');
  msg.style.color='#aaa'; msg.textContent='Saving…';
  try{{
    const r=await fetch(B+'/worker-meta'+(TQ||'?'),{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{session:_metaSession,display_name:name,role}})}});
    if(r.ok){{
      msg.style.color='#66bb6a'; msg.textContent='✅ Saved';
      setTimeout(closeMetaModal,800);
      refreshWorkers();
    }}else{{
      msg.style.color='#e57373'; msg.textContent='⚠️ Save failed';
    }}
  }}catch(e){{
    msg.style.color='#e57373'; msg.textContent='⚠️ Network error';
  }}
}}
const _DKW={{
  support:['support ticket','customer','escalation','refund','bug report'],
  finance:['revenue','receivable','metabase','snowflake','payment','finance','invoice','margin','reconcil'],
  research:['research','look up','find out','summarize','competitive','analysis'],
  security:['security','vulnerability','audit','breach','2fa','compliance'],
  legal:['legal','patent','litigation','contract','compliance'],
  pr_review:['pr review','pull request','code review','diff','merge'],
  data:['analytics','data audit','pipeline','report','query','snowflake','metabase'],
  research:['research','search for','find out','look up','investigate'],
}};
function _detectDomain(txt){{
  const ml=txt.toLowerCase();let best='research',bv=0;
  for(const [d,kws] of Object.entries(_DKW)){{const sc=kws.filter(k=>ml.includes(k)).length;if(sc>bv){{bv=sc;best=d;}}}}
  return bv>0?best:'research';
}}
function dmTaskInput(val){{
  const domain=_detectDomain(val);
  const dmd=document.getElementById('dm-domain');
  const badge=document.getElementById('dm-route-badge');
  if(dmd)dmd.value=domain;
  if(badge)badge.textContent='routing to: '+domain;
}}
function dmWorkerChanged(){{
  const w=document.getElementById('dm-worker').value;
  // Auto-set domain to match selected worker
  const domain=_sessionDomainMap[w];
  const dmd=document.getElementById('dm-domain');
  const badge=document.getElementById('dm-route-badge');
  if(dmd&&domain){{dmd.value=domain;if(badge)badge.textContent='routing to: '+domain;}}
  const dw=document.getElementById('d-worker');
  if(dw)dw.value=w;
}}
async function openPaneDispatch(){{
  const session=_paneModalSession;
  await openDispatchModal();
  // Pre-select the session this pane belongs to
  const dmw=document.getElementById('dm-worker');
  if(dmw&&session){{
    dmw.value=session;
    dmWorkerChanged();
    setTimeout(()=>document.getElementById('dm-task').focus(),150);
  }}
}}
async function dispatchModal(force){{
  const task=document.getElementById('dm-task').value.trim();
  const worker=document.getElementById('dm-worker').value;
  const domain=document.getElementById('dm-domain').value;
  const msg=document.getElementById('dm-msg');
  const fpBtn=document.getElementById('dm-force-btn');
  if(!task){{msg.style.color='#e57373';msg.textContent='Enter a task first';return;}}
  msg.style.color='#aaa';
  msg.textContent='Dispatching…';
  const sep=TQ?TQ+'&':'?';
  const body={{domain,task,priority:'normal'}};
  if(worker)body.session=worker;
  if(force)body.force=true;
  try{{
    const r=await fetch(B+'/dispatch'+sep+'domain='+encodeURIComponent(domain),{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});
    const d=await r.json().catch(()=>({{}}));
    if(d.task_id){{
      msg.style.color='#66bb6a';
      msg.textContent='✅ Dispatched → '+(d.session||'queued');
      document.getElementById('dm-task').value='';
      // Poll once after 3s to confirm task actually landed (wasn't aborted)
      setTimeout(async()=>{{
        try{{
          const s=await get('/task/'+d.task_id);
          if(s&&s.status==='aborted'){{
            msg.style.color='#e57373';
            msg.textContent='⚠️ Task was aborted — try Force Push.';
            return;
          }}
        }}catch(e){{}}
        closeDispatchModal();
      }},2500);
    }}else if(d.detail&&d.detail.includes('busy')){{
      msg.style.color='#ffb74d';
      const busyDetail=d.detail.replace(/\s*—\s*use force push.*$/i,'').replace(/^busy:\s*/i,'');
      msg.textContent='⚠️ Worker busy: '+busyDetail+' — use Force Push to override';
    }}else{{
      msg.style.color='#e57373';
      msg.textContent='⚠️ '+(d.detail||'Dispatch failed');
    }}
  }}catch(e){{
    msg.style.color='#e57373';
    msg.textContent='⚠️ Network error';
  }}
}}
async function loadNotes(session){{
  const modal=document.getElementById('notes-modal');
  const body=document.getElementById('notes-modal-body');
  const title=document.getElementById('notes-modal-title');
  title.textContent='Notes: '+session;
  body.innerHTML='<span style="color:#bbb">Loading\u2026</span>';
  modal.style.display='flex';
  const d=await get('/session-notes/'+encodeURIComponent(session));
  if(!d||!d.notes||!d.notes.length){{
    body.innerHTML='<span style="color:#bbb">No Obsidian notes found for '+esc(session)+'</span>';
    return;
  }}
  body.innerHTML=d.notes.map(n=>{{
    const noteHtml=mdParse(n.full);
    return `<div style="margin-bottom:12px">
      <div style="font-size:11px;color:#7986cb;font-weight:600;margin-bottom:6px">${{esc(n.file)}}</div>
      <div style="font-size:12px;line-height:1.7;color:#ccc">${{noteHtml}}</div>
    </div>`;
  }}).join('<hr style="border:none;border-top:1px solid #333;margin:12px 0">');
}}
function closeNotesModal(){{
  document.getElementById('notes-modal').style.display='none';
}}

async function refreshQueues(){{
  const [qs,done]=await Promise.all([get('/questions'),get('/completed')]);
  if(qs){{
    const pending=qs.pending||[];
    document.getElementById('ql').innerHTML=pending.length?pending.map(q=>
      `<div class="card" id="qcard-${{esc(q.id)}}">
        <div class="card-head">
          <span>[</span><span>${{esc(q.session||'?')}}</span><span>]</span>
          <span style="margin-left:auto;font-size:10px;color:#888">${{esc(q.asked_at||'')}}</span>
          <span onclick="dismissQ('${{esc(q.id)}}')" style="cursor:pointer;color:#bbb;padding:0 4px;font-size:12px;margin-left:6px" title="Dismiss">✕</span>
        </div>
        <div class="card-body">${{esc((q.message||'').substring(0,400))}}</div>
        <div class="ans-row">
          <input id="a${{esc(q.id)}}" placeholder="Reply to ${{esc(q.session||'worker')}}…" onkeydown="if(event.key==='Enter')ans('${{esc(q.id)}}')">
          <button onclick="ans('${{esc(q.id)}}')">&#x25B6; Send</button>
        </div>
      </div>`).join(''):'<div class="empty">None</div>';
  }}
  if(done){{
    document.getElementById('cl').innerHTML=done.length?done.slice(0,8).map(c=>
      `<div class="card">
        <div class="card-head"><span>${{c.success?'&#x2705;':'&#x274C;'}}</span>
        <span>[</span><span style="color:#666">${{esc(c.domain||'?')}}</span><span>]</span>
        <span style="color:#555">${{esc(c.session||'')}}</span>
        <span style="color:#2a2a2a;font-size:10px;margin-left:auto">${{esc(c.completed_at||'')}}</span></div>
        <div class="card-body">${{esc((c.result||'').substring(0,200))}}</div>
      </div>`).join(''):'<div class="empty">None yet</div>';
  }}
}}

// ── Tabs ──────────────────────────────────────────────────────────────────
let _activeTab='workers';
let _resSeenCount=0;
const _prevWorkerStatus={{}};  // session → last known status (for done notifications)
let _notifSeeded=false;        // first poll seeds state without notifying
function _requestNotifPerm(){{
  if('Notification' in window && Notification.permission==='default')
    Notification.requestPermission();
}}
function _workerDoneNotif(session,displayName,task){{
  if(!('Notification' in window)||Notification.permission!=='granted')return;
  const title='✅ '+(displayName||session)+' done';
  const body=task?task.substring(0,80):'Task completed';
  const n=new Notification(title,{{body,icon:'',tag:'orchmux-'+session,renotify:true}});
  n.onclick=()=>{{window.focus();n.close();}};
}}
function switchTab(t){{
  ['workers','results','terminals','manage'].forEach(id=>{{
    const panel=document.getElementById('tab-'+id);
    const btn=document.getElementById('tbtn-'+id);
    if(panel)panel.style.display=id===t?(id==='terminals'?'flex':''):'none';
    if(btn){{
      btn.style.borderBottomColor=id===t?'#1a73e8':'transparent';
      btn.style.color=id===t?'#1a73e8':'#aaa';
    }}
  }});
  _activeTab=t;
  if(t==='results'){{
    const badge=document.getElementById('res-badge');
    if(badge)badge.style.display='none';
    _resSeenCount=_resSeenCount||0;
    refreshResults();
  }}
  if(t==='manage')loadDomains();
  if(t==='terminals'){{startTermGrid();}}else{{stopTermGrid();}}
}}

// ── Terminals tab ────────────────────────────────────────────────────────────
let _termTicker=null;
let _termWorkers={{}};  // session → {{status, displayName, domain}}
let _pinnedSessions=new Set(JSON.parse(localStorage.getItem('pinnedSessions')||'[]'));
let _allKnownSessions={{}};  // session → {{domain, displayName}} — for pin picker
let _paneCache={{}};  // session → {{output, ts}} — preloaded pane data

function _savePins(){{localStorage.setItem('pinnedSessions',JSON.stringify([..._pinnedSessions]));}}

function togglePinPicker(){{
  const picker=document.getElementById('pin-picker');
  if(picker.style.display==='none'){{
    _buildPinPicker();picker.style.display='';
  }}else{{picker.style.display='none';}}
}}

function _buildPinPicker(){{
  const list=document.getElementById('pin-picker-list');
  if(!list)return;
  const sessions=Object.entries(_allKnownSessions)
    .filter(([s])=>!_pinnedSessions.has(s))
    .sort((a,b)=>a[0].localeCompare(b[0]));
  if(!sessions.length){{list.innerHTML='<div style="padding:8px 12px;font-size:11px;color:#666">All sessions already pinned</div>';return;}}
  list.innerHTML=sessions.map(([s,info])=>`
    <div onclick="pinSession('${{esc(s)}}')" style="padding:7px 12px;cursor:pointer;display:flex;align-items:center;gap:8px;border-bottom:1px solid #2a2a2a" onmouseover="this.style.background='#2a2a2a'" onmouseout="this.style.background=''">
      <span style="font-size:11px;color:#ccc;flex:1">${{esc(s)}}</span>
      <span style="font-size:9px;color:#555">${{esc(info.domain||'')}}</span>
    </div>`).join('');
}}

function pinSession(session){{
  _pinnedSessions.add(session);_savePins();
  document.getElementById('pin-picker').style.display='none';
  renderTermGrid();
}}
function pinToGrid(){{
  if(!_paneModalSession)return;
  _pinnedSessions.add(_paneModalSession);_savePins();
  // Update button to confirm
  const btn=document.getElementById('pane-grid-btn');
  if(btn){{btn.textContent='✓ Grid';btn.style.background='#1a4a1a';btn.style.borderColor='#2a6a2a';btn.style.color='#6c6';}}
  // Switch to terminals tab so user sees it was added
  switchTab('terminals');
}}

function unpinSession(session){{
  _pinnedSessions.delete(session);_savePins();
  document.getElementById('tc-'+session)?.remove();
  const grid=document.getElementById('term-grid');
  const empty=document.getElementById('term-empty');
  const hasCells=grid&&grid.querySelectorAll('.term-cell').length>0;
  if(grid)grid.style.display=hasCells?'':'none';
  if(empty)empty.style.display=hasCells?'none':'';
}}

async function renderTermGrid(){{
  const showIdle=document.getElementById('term-idle-toggle')?.checked;
  const status=await get('/status');
  const wdet=await get('/worker-details'+(TQ||'?'));
  if(!status)return;
  // Build known sessions map for pin picker
  for(const[domain,cfg]of Object.entries(status))
    for(const w of cfg.workers||[])
      _allKnownSessions[w.session]={{domain,displayName:(wdet&&wdet[w.session]?.display_name)||''}};
  // Collect workers to show
  const toShow=[];
  const seenSessions=new Set();
  for(const[domain,cfg]of Object.entries(status)){{
    for(const w of cfg.workers||[]){{
      const det=wdet&&wdet[w.session]||{{}};
      const s=w.exists?w.status:'missing';
      seenSessions.add(w.session);
      if(showIdle||s==='busy'||s==='waiting'||s==='blocked'||_pinnedSessions.has(w.session))
        toShow.push({{session:w.session,status:s,domain,displayName:det.display_name||'',task:w.current_task||'',pinned:_pinnedSessions.has(w.session)}});
    }}
  }}
  // Include pinned sessions not in any domain (might be temp/unknown)
  for(const ps of _pinnedSessions){{
    if(!seenSessions.has(ps))toShow.push({{session:ps,status:'missing',domain:'?',displayName:'',task:'',pinned:true}});
  }}
  const grid=document.getElementById('term-grid');
  const empty=document.getElementById('term-empty');
  // Update badge
  const busy=toShow.filter(w=>w.status==='busy'||w.status==='waiting'||w.status==='blocked').length;
  const badge=document.getElementById('term-badge');
  if(badge){{badge.textContent=busy;badge.style.display=busy>0?'':'none';}}
  if(!toShow.length){{grid.style.display='none';empty.style.display='';return;}}
  grid.style.display='';empty.style.display='none';
  // Add/remove cells, update content
  const existing=new Set([...grid.querySelectorAll('.term-cell')].map(el=>el.dataset.session));
  const needed=new Set(toShow.map(w=>w.session));
  // Remove stale (not needed AND not pinned)
  for(const s of existing)if(!needed.has(s)&&!_pinnedSessions.has(s))document.getElementById('tc-'+s)?.remove();
  // Add new
  for(const w of toShow){{
    if(!document.getElementById('tc-'+w.session)){{
      const cell=document.createElement('div');
      cell.className='term-cell';cell.dataset.session=w.session;cell.id='tc-'+w.session;
      const statusColor=w.status==='busy'?'#ff9800':w.status==='blocked'?'#f44336':w.status==='waiting'?'#29b6f6':w.status==='missing'?'#555':'#4caf50';
      const pinBtn=w.pinned?`<button onclick="unpinSession('${{esc(w.session)}}')" title="Unpin" style="background:none;border:none;color:#555;cursor:pointer;font-size:11px;padding:0 3px;line-height:1">✕</button>`:'';
      cell.innerHTML=`<div class="term-cell-hdr">
        <span style="width:7px;height:7px;border-radius:50%;background:${{statusColor}};flex-shrink:0"></span>
        <span style="font-size:11px;color:#ddd;font-weight:600;flex:1">${{esc(w.displayName||w.session)}}</span>
        <span style="font-size:9px;color:#666">${{esc(w.domain)}}</span>
        ${{pinBtn}}
        <button onclick="togglePane('${{esc(w.session)}}')" style="background:#333;border:1px solid #444;color:#aaa;border-radius:3px;padding:1px 7px;cursor:pointer;font-size:9px;font-family:inherit">&#x26F6;</button>
        <button onclick="openPaneDispatchFor('${{esc(w.session)}}')" style="background:#1a3a6a;border:1px solid #2a5aa0;color:#7ab;border-radius:3px;padding:1px 7px;cursor:pointer;font-size:9px;font-family:inherit">&#x25B6; Task</button>
      </div>
      <div class="term-cell-out pane-modern" id="tco-${{w.session}}" style="background:#fafafa;color:#1a1a1a;font-family:-apple-system,sans-serif;padding:12px 14px;white-space:normal;word-break:normal;font-size:12px;line-height:1.6">Loading…</div>
      <div id="tcts-${{w.session}}" class="pane-ts-footer"></div>`;
      grid.appendChild(cell);
    }}
  }}
  // Refresh pane output — use cache for non-visible cells, fresh fetch for visible
  await Promise.all(toShow.map(async w=>{{
    const out=document.getElementById('tco-'+w.session);
    if(!out)return;
    // Use cache if available and cell already has content
    const cached=_paneCache[w.session];
    if(cached&&out.textContent!=='Loading…'){{
      // Update from cache immediately, then fetch fresh in background
      _applyPaneCellOutput(w.session,cached.output);
    }}
    const d=await get('/pane/'+encodeURIComponent(w.session)+'?lines=60');
    if(!d||!d.exists){{out.textContent='Session not found';return;}}
    _paneCache[w.session]={{output:d.output||'',ts:Date.now()}};
    _applyPaneCellOutput(w.session,d.output||'');
  }}));
}}

function _applyPaneCellOutput(session,raw){{
  const out=document.getElementById('tco-'+session);
  if(!out)return;
  const clean=cleanTermOutput(stripAnsi(raw));
  const atBottom=out.scrollHeight-out.scrollTop-out.clientHeight<4;
  out.innerHTML=mdParse(clean);
  if(atBottom)out.scrollTop=out.scrollHeight;
  const tsEl=document.getElementById('tcts-'+session);
  if(tsEl)tsEl.textContent='Updated '+new Date().toLocaleTimeString();
}}

function startTermGrid(){{
  renderTermGrid();
  if(!_termTicker)_termTicker=setInterval(renderTermGrid,3000);
}}
function stopTermGrid(){{
  if(_termTicker){{clearInterval(_termTicker);_termTicker=null;}}
}}
function openPaneDispatchFor(session){{
  _paneModalSession=session;
  openPaneDispatch();
}}

// ── Kill session — inline type-to-confirm ────────────────────────────────
function killSession(name){{
  // Inject an inline confirm row directly under the worker row
  const existing=document.getElementById('kill-confirm-'+name);
  if(existing){{existing.remove();return;}}
  // Find the kill-btn td for this session
  const span=document.querySelector(`[data-kill="${{name}}"]`);
  if(!span)return;
  const row=span.closest('tr');
  const conf=document.createElement('tr');
  conf.id='kill-confirm-'+name;
  conf.innerHTML=`<td colspan="7" style="padding:6px 10px;background:#fff5f5;border-top:1px solid #fcc">
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <span style="font-size:11px;color:#c62828">Type <b>${{name}}</b> to confirm kill:</span>
      <input id="kill-input-${{name}}" placeholder="${{name}}" style="flex:1;min-width:120px;border:1px solid #fcc;border-radius:4px;padding:4px 8px;font-size:11px;font-family:monospace;outline:none" oninput="killCheck('${{name}}',this.value)">
      <button id="kill-go-${{name}}" disabled onclick="killConfirmed('${{name}}')" style="background:#e53935;border:none;color:#fff;border-radius:4px;padding:4px 12px;font-size:11px;cursor:not-allowed;opacity:.4">Kill</button>
      <button onclick="document.getElementById('kill-confirm-${{name}}').remove()" style="background:none;border:1px solid #ddd;border-radius:4px;padding:4px 10px;font-size:11px;cursor:pointer;color:#888">Cancel</button>
    </div>
  </td>`;
  row.after(conf);
  document.getElementById('kill-input-'+name).focus();
}}
function killCheck(name,val){{
  const btn=document.getElementById('kill-go-'+name);
  if(!btn)return;
  const match=val.trim()===name;
  btn.disabled=!match;
  btn.style.cursor=match?'pointer':'not-allowed';
  btn.style.opacity=match?'1':'.4';
}}
async function killConfirmed(name){{
  const r=await fetch(B+'/session/'+encodeURIComponent(name)+(TQ||'?'),{{method:'DELETE'}});
  const conf=document.getElementById('kill-confirm-'+name);
  if(conf)conf.remove();
  if(r.ok){{
    refreshWorkers();
  }}else{{
    const d=await r.json().catch(()=>({{}}));
    alert('Error: '+(d.detail||r.status));
  }}
}}

// ── Manage sub-tabs ───────────────────────────────────────────────────────
async function loadDomains(){{
  const d=await get('/domains');
  if(!d||!d.length)return;
  const opts=d.map(x=>`<option value="${{esc(x)}}">${{esc(x)}}</option>`).join('');
  ['sp-domain','at-domain','d-domain','dm-domain'].forEach(id=>{{
    const el=document.getElementById(id);
    if(el)el.innerHTML=opts;
  }});
}}

function mgTab(t){{
  ['spawn','attach','domain','server'].forEach(id=>{{
    const p=document.getElementById('mg-'+id);
    const b=document.getElementById('mtab-'+id);
    if(p)p.style.display=id===t?'flex':'none';
    if(b){{b.style.borderBottomColor=id===t?'#1a73e8':'transparent';b.style.color=id===t?'#1a73e8':'#aaa';}}
  }});
  if(t==='attach')loadAvailableSessions();
}}

async function restartServer(){{
  const btn=document.querySelector('#mg-server button');
  const msg=document.getElementById('restart-msg');
  if(btn)btn.disabled=true;
  msg.style.color='#999';
  msg.textContent='Sending restart signal…';
  try{{
    await fetch(B+'/restart',{{method:'POST'}});
  }}catch(e){{}}
  msg.textContent='Restarting… reconnecting in 4s';
  setTimeout(async()=>{{
    for(let i=0;i<10;i++){{
      try{{
        const r=await fetch(B+'/health');
        if(r.ok){{
          msg.style.color='#388e3c';
          msg.textContent='✅ Server back online';
          if(btn)btn.disabled=false;
          return;
        }}
      }}catch(e){{}}
      await new Promise(r=>setTimeout(r,1000));
    }}
    msg.style.color='#c62828';
    msg.textContent='⚠️ Server may still be restarting — refresh page';
    if(btn)btn.disabled=false;
  }},4000);
}}

async function loadAvailableSessions(){{
  const sel=document.getElementById('at-name');
  if(!sel)return;
  const d=await get('/tmux-sessions');
  if(!d){{sel.innerHTML='<option value="">— could not load —</option>';return;}}
  const sessions=d.available||[];
  if(!sessions.length){{
    sel.innerHTML='<option value="">— all sessions already registered —</option>';
    return;
  }}
  sel.innerHTML=sessions.map(s=>`<option value="${{esc(s.name)}}">${{esc(s.name)}}${{s.protected?' ⚠ protected':''}}</option>`).join('');
}}

// ── Attach existing session ────────────────────────────────────────────────
async function attachWorker(){{
  const name=document.getElementById('at-name').value.trim();
  const domain=document.getElementById('at-domain').value;
  const msg=document.getElementById('at-msg');
  if(!name){{msg.textContent='Session name required';msg.style.color='#e53935';return;}}
  msg.textContent='Attaching…';msg.style.color='#999';
  const r=await fetch(B+'/attach-worker'+(TQ||'?'),{{
    method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{session:name,domain}})
  }});
  const d=await r.json().catch(()=>({{}}));
  if(r.ok){{
    msg.textContent='✓ Attached '+name+' → '+domain;msg.style.color='#2e7d32';
    document.getElementById('at-name').value='';
    setTimeout(()=>{{refreshWorkers();refreshWorkerDropdown();}},1000);
  }}else{{
    msg.textContent='Error: '+(d.detail||r.status);msg.style.color='#e53935';
  }}
}}

// ── Spawn worker ───────────────────────────────────────────────────────────
async function spawnWorker(){{
  const name=document.getElementById('sp-name').value.trim();
  const domain=document.getElementById('sp-domain').value;
  const model=document.getElementById('sp-model').value;
  const msg=document.getElementById('sp-msg');
  if(!name){{msg.textContent='Session name required';msg.style.color='#e53935';return;}}
  if(!/^[\w-]+$/.test(name)){{msg.textContent='Name: letters, numbers, hyphens only';msg.style.color='#e53935';return;}}
  msg.textContent='Spawning…';msg.style.color='#999';
  const r=await fetch(B+'/spawn-worker'+(TQ||'?'),{{
    method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{session:name,domain,model}})
  }});
  const d=await r.json().catch(()=>({{}}));
  if(r.ok){{
    msg.textContent='✓ Spawned '+name+' ('+model+')';msg.style.color='#2e7d32';
    document.getElementById('sp-name').value='';
    setTimeout(()=>{{refreshWorkers();refreshWorkerDropdown();}},1500);
  }}else{{
    msg.textContent='Error: '+(d.detail||r.status);msg.style.color='#e53935';
  }}
}}

// ── Add domain ────────────────────────────────────────────────────────────
async function addDomain(){{
  const name=document.getElementById('nd-name').value.trim();
  const sessions=document.getElementById('nd-sessions').value.trim().split(',').map(s=>s.trim()).filter(Boolean);
  const handles=document.getElementById('nd-handles').value.trim().split(',').map(s=>s.trim()).filter(Boolean);
  const model=document.getElementById('nd-model').value;
  const msg=document.getElementById('nd-msg');
  if(!name){{msg.textContent='Domain name required';msg.style.color='#e53935';return;}}
  msg.textContent='Creating…';msg.style.color='#999';
  const r=await fetch(B+'/add-domain'+(TQ||'?'),{{
    method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{domain:name,sessions,handles,model,spawn_allowed:true}})
  }});
  const d=await r.json().catch(()=>({{}}));
  if(r.ok){{
    msg.textContent='✓ Domain '+d.domain+' created';msg.style.color='#2e7d32';
    document.getElementById('nd-name').value='';
    document.getElementById('nd-sessions').value='';
    document.getElementById('nd-handles').value='';
    loadDomains();
    setTimeout(()=>refreshWorkers(),1000);
  }}else{{
    msg.textContent='Error: '+(d.detail||r.status);msg.style.color='#e53935';
  }}
}}

// When a specific worker is picked, auto-set the matching domain
function workerChanged(){{
  const sel=document.getElementById('d-worker');
  const opt=sel.options[sel.selectedIndex];
  if(opt&&opt.dataset.domain){{
    document.getElementById('d-domain').value=opt.dataset.domain;
  }}
}}

// Populate worker dropdown from /status
async function refreshWorkerDropdown(){{
  const st=await get('/status');
  if(!st)return;
  const sel=document.getElementById('d-worker');
  const prev=sel.value;
  // Remove all except first "any" option
  while(sel.options.length>1)sel.remove(1);
  Object.entries(st).forEach(([domain,info])=>{{
    (info.workers||[]).forEach(w=>{{
      const opt=document.createElement('option');
      opt.value=w.session;
      opt.dataset.domain=domain;
      const badge=w.status==='idle'?'✓':w.status==='busy'?'⏳':'–';
      opt.textContent=badge+' '+w.session+' ['+domain+']';
      if(w.status!=='idle')opt.style.color='#999';
      sel.appendChild(opt);
    }});
  }});
  // Restore previous selection if still valid
  if(prev){{
    for(let i=0;i<sel.options.length;i++){{
      if(sel.options[i].value===prev){{sel.value=prev;break;}}
    }}
  }}
}}

async function dispatch(force){{
  const domain=document.getElementById('d-domain').value;
  const worker=document.getElementById('d-worker').value;
  const task=document.getElementById('d-task').value.trim();
  const msg=document.getElementById('d-msg');
  if(!task){{msg.textContent='Enter a task first.';return;}}
  msg.textContent='Dispatching…';
  try{{
    const body={{domain,task,context:''}};
    if(worker)body.session=worker;
    if(force)body.force=true;
    const sep=TQ?TQ+'&':'?';
    const r=await fetch(B+'/dispatch'+sep+'domain='+encodeURIComponent(domain),{{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify(body)
    }});
    const d=await r.json();
    if(d.task_id){{msg.style.color='#4caf50';msg.textContent='→ '+(d.session||domain)+': '+d.task_id;document.getElementById('d-task').value='';}}
    else if(d.detail&&d.detail.includes('busy')){{msg.style.color='#ff9800';msg.textContent='⚠️ Busy — use ⚡ Force';}}
    else{{msg.style.color='#f44336';msg.textContent='Error: '+(d.detail||JSON.stringify(d));}}
  }}catch(e){{msg.style.color='#f44336';msg.textContent='Network error';}}
  setTimeout(()=>{{msg.textContent='';msg.style.color='#555';}},4000);
  refresh();
}}

async function ans(qid){{
  const el=document.getElementById('a'+qid);
  if(!el||!el.value.trim())return;
  const answer=el.value.trim();
  el.value='';
  const r=await fetch(B+'/answer/'+qid+(TQ||''),{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{answer}})}});
  const d=await r.json().catch(()=>({{}}));
  // Remove card immediately from DOM (no waiting for next poll)
  const card=document.getElementById('qcard-'+qid);
  if(card)card.remove();
  if(d.sent_to)console.log('Reply sent to',d.sent_to);
}}

async function dismissQ(qid){{
  await fetch(B+'/questions/'+qid+(TQ||''),{{method:'DELETE'}});
  const card=document.getElementById('qcard-'+qid);
  if(card)card.remove();
}}

// Boot: stagger initial loads so they don't all hit at once
refreshHealth();refreshWorkers();refreshInfra();refreshQueues();refreshResults();refreshWorkerDropdown();loadHistory();
_requestNotifPerm();
setInterval(refreshHealth,        3000);
setInterval(refreshWorkers,       4000);
setInterval(refreshInfra,         3000);
setInterval(refreshQueues,        5000);
setInterval(refreshResults,       8000);
setInterval(refreshWorkerDropdown,6000);
setInterval(loadHistory,         10000);
</script>
</body>
</html>"""


# ── Notifications ──────────────────────────────────────────────────────────
async def _notify_completion(task_id: str, result: str, success: bool):
    task = task_registry.get(task_id, {})
    domain  = task.get("domain", "?")
    session = task.get("session", "?")
    icon    = "✅" if success else "❌"
    msg = f"{icon} [{domain}] {result[:200]}"
    await _send_notification(msg, ["telegram", "ntfy"])
    asyncio.create_task(_notify_supervisor(task_id, session, domain, result[:300], success))


async def _notify_supervisor(task_id: str, session: str, domain: str, result: str, success: bool):
    """Queue completion for supervisor — flushed when supervisor is next idle."""
    if task_id in _notified_task_ids:
        return
    _notified_task_ids.add(task_id)
    icon = "✅" if success else "❌"
    _supervisor_inbox.append(
        f"{icon} [{domain}] {session}: {result[:200]}"
    )


async def _supervisor_flusher():
    """Background loop: flush inbox to supervisor whenever it's at idle prompt."""
    sup = "orchmux-supervisor"
    _noise = ("bypass permissions", "shift+tab", "⏵⏵", "Claude Code", "Sonnet", "Opus")
    last_flush = 0.0

    while True:
        await asyncio.sleep(5)
        if not _supervisor_inbox:
            continue
        # Cooldown: don't flood supervisor — wait at least 30s between flushes
        if time.time() - last_flush < 30:
            continue
        if not session_exists(sup):
            continue
        r = tmux(["capture-pane", "-t", sup, "-p", "-S", "-8"])
        lines = [l for l in r.stdout.splitlines()
                 if l.strip() and not any(n in l for n in _noise)
                 and not all(c in "─━═ " for c in l.strip())]
        last = lines[-1].strip() if lines else ""
        if not last.startswith("❯"):
            continue
        # Batch all pending messages into one single-line delivery (deduplicated).
        # Single line is critical — tmux send-keys treats \n as Enter, which would
        # submit each completion line as a separate command via the stop hook.
        msgs = list(dict.fromkeys(_supervisor_inbox))
        _supervisor_inbox.clear()
        full = "[orchmux] " + " | ".join(msgs)
        tmux(["send-keys", "-t", sup, full])
        await asyncio.sleep(0.5)
        tmux(["send-keys", "-t", sup, "Enter"])
        last_flush = time.time()
        log(f"[supervisor] flushed {len(msgs)} msg(s)")


async def _send_notification(message: str, channels: list[str]):
    for ch in channels:
        if ch == "telegram":
            asyncio.create_task(_telegram(message))
        elif ch == "ntfy":
            asyncio.create_task(_ntfy(message))


async def _telegram(msg: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    payload = json.dumps({"chat_id": chat_id, "text": msg})
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-X", "POST",
        f"https://api.telegram.org/bot{token}/sendMessage",
        "-H", "Content-Type: application/json",
        "-d", payload,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()


async def _ntfy(msg: str):
    topic = os.environ.get("NTFY_TOPIC", "orchmux")
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-d", msg, f"https://ntfy.sh/{topic}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()


# ── Temp worker cleanup ────────────────────────────────────────────────────
async def _cleanup_temp(session: str, delay: int):
    await asyncio.sleep(delay)
    if session in active_temp_workers:
        tmux(["kill-session", "-t", session])
        del active_temp_workers[session]
        log(f"[cleanup] {session} killed after TTL")


# ── Startup reconciliation ────────────────────────────────────────────────
@app.on_event("startup")
async def _on_startup():
    config = load_config()
    reset_count = 0
    for domain, cfg in config.get("workers", {}).items():
        if domain.startswith("_"):
            continue
        for session in cfg.get("sessions", []):
            if not session_exists(session):
                continue
            # Reset any stale "busy" from a previous server run
            if get_opt(session, "status") == "busy":
                set_opt(session, "status", "idle")
                set_opt(session, "current_task", "")
                set_opt(session, "task_id", "")
                worker_status[session] = "idle"
                reset_count += 1
    if reset_count:
        log(f"[startup] reset {reset_count} stale-busy workers")
    asyncio.create_task(_drainer())
    asyncio.create_task(_supervisor_flusher())
    asyncio.create_task(_watchdog())
    # Start HTTP→HTTPS redirect if TLS cert is present
    if _CERT.exists() and _KEY.exists():
        t = threading.Thread(target=_run_http_redirect, args=(BIND_HOST,), daemon=True)
        t.start()


# ── Queue drainer (background) ─────────────────────────────────────────────


async def _drainer():
    while True:
        await asyncio.sleep(10)
        config = load_config()
        for domain, tasks in list(task_queue.items()):
            if not tasks:
                continue
            cfg = config.get("workers", {}).get(domain)
            if not cfg:
                continue
            idle = find_idle_worker(cfg)
            if idle:
                with _drain_lock:
                    if not task_queue[domain]:
                        continue
                    next_task = task_queue[domain].pop(0)
                log(f"[drainer] {next_task['task_id']} → {idle}")
                await dispatch_to(idle, next_task["task_id"], next_task["task"],
                                  next_task.get("context"), domain)


# ── Watchdog (background) ──────────────────────────────────────────────────

_AUTH_SIGNS    = ("OAuth error", "Invalid code", "Paste code here",
                  "Browser didn't open", "Press Enter to retry")
_auth_stuck_ct: dict[str, int] = {}

def _relaunch_worker(session: str, domain: str):
    """Kill and relaunch a persistent worker using global ~/.claude auth."""
    work_dir = ROOT / "worker-workdirs" / session
    work_dir.mkdir(parents=True, exist_ok=True)
    import shutil
    claude_src = ROOT / "worker" / domain / "CLAUDE.md"
    if claude_src.exists():
        shutil.copy2(str(claude_src), str(work_dir / "CLAUDE.md"))
    tmux(["kill-session", "-t", session])
    time.sleep(0.5)
    r = subprocess.run([
        "tmux", "new-session", "-d", "-s", session, "-c", str(work_dir),
        "-e", f"ORCHMUX_SESSION={session}",
        "-e", f"ORCHMUX_WORKER_ID={session}",
        "-e", f"ORCHMUX_DOMAIN={domain}",
        "-e", f"ORCHMUX_QUEUE={QUEUE_DIR}/{session}.yaml",
        "-e", f"ORCHMUX_RESULTS={RESULTS_DIR}/{session}.yaml",
    ], capture_output=True)
    if r.returncode == 0:
        time.sleep(0.3)
        tmux(["send-keys", "-t", session, "claude --dangerously-skip-permissions"])
        time.sleep(0.3)
        tmux(["send-keys", "-t", session, "Enter"])
        log(f"[watchdog] relaunched worker {session} (domain={domain})")
    _auth_stuck_ct.pop(session, None)

def _relaunch_infra(name: str, cmd: str, cwd: str = None):
    """Restart an orchmux infra session (watcher, supervisor, monitor, telegram)."""
    tmux(["kill-session", "-t", name])
    time.sleep(0.5)
    args = ["tmux", "new-session", "-d", "-s", name]
    if cwd:
        args += ["-c", cwd]
    args.append(cmd)
    r = subprocess.run(args, capture_output=True)
    if r.returncode == 0:
        log(f"[watchdog] restarted infra session {name}")
    else:
        log(f"[watchdog] FAILED to restart {name}: {r.stderr.decode()[:100]}")

async def _watchdog():
    """
    Runs every 30s. Checks:
    1. All persistent workers — missing or auth-stuck → relaunch
    2. orchmux-watcher — dead → restart
    3. orchmux-supervisor — dead → restart
    4. orchmux-telegram — dead → restart
    5. Pending queue tasks whose worker is idle → re-send task (recovers send_keys race)
    """
    await asyncio.sleep(30)  # let everything settle on startup
    while True:
        try:
            await _watchdog_tick()
        except Exception as e:
            log(f"[watchdog] error: {e}")
        await asyncio.sleep(30)

async def _watchdog_tick():
    config   = load_config()
    orchmux  = ROOT

    # ── 1. Persistent workers ──────────────────────────────────────────────
    for domain, cfg in config.get("workers", {}).items():
        if domain.startswith("_"):
            continue
        for session in cfg.get("sessions", []):
            if not session_exists(session):
                log(f"[watchdog] {session} missing — relaunching")
                await asyncio.get_event_loop().run_in_executor(
                    None, _relaunch_worker, session, domain)
                continue

            # auth-stuck check
            r = tmux(["capture-pane", "-t", session, "-p", "-S", "-10"])
            pane = r.stdout
            if any(sign in pane for sign in _AUTH_SIGNS):
                ct = _auth_stuck_ct.get(session, 0) + 1
                _auth_stuck_ct[session] = ct
                if ct >= 2:
                    log(f"[watchdog] {session} auth-stuck ({ct} checks) — relaunching")
                    await asyncio.get_event_loop().run_in_executor(
                        None, _relaunch_worker, session, domain)
            else:
                _auth_stuck_ct.pop(session, None)

    # ── 2. Infra sessions ──────────────────────────────────────────────────
    venv_py  = str(orchmux / ".venv" / "bin" / "python")
    sup_cfg  = "/tmp/orchmux-supervisor"

    infra = [
        ("orchmux-watcher",
         f"while true; do {venv_py} {orchmux}/watcher.py 2>&1; echo '[watcher] restarting...'; sleep 3; done",
         str(orchmux)),
        ("orchmux-supervisor",
         f"env CLAUDE_CONFIG_DIR={sup_cfg} claude --dangerously-skip-permissions",
         str(orchmux / "supervisor")),
        ("orchmux-telegram",
         f"while true; do {venv_py} {orchmux}/telegram_bot.py; echo '[telegram] restarting...'; sleep 3; done",
         str(orchmux)),
    ]
    for name, cmd, cwd in infra:
        if not session_exists(name):
            log(f"[watchdog] {name} dead — restarting")
            await asyncio.get_event_loop().run_in_executor(
                None, _relaunch_infra, name, cmd, cwd)

    # ── 3. Stuck pending tasks (send_keys race recovery) ──────────────────
    QUEUE_DIR.mkdir(exist_ok=True)
    for qf in QUEUE_DIR.glob("*.yaml"):
        try:
            with open(qf) as f:
                task = yaml.safe_load(f)
            if not task or task.get("status") != "pending":
                continue
            session = task.get("session", "")
            if not session or not session_exists(session):
                continue
            # Only recover if worker is at idle prompt (task text never executed)
            pane = tmux(["capture-pane", "-t", session, "-p", "-S", "-5"]).stdout
            at_prompt = any(
                l.strip().startswith("❯") and not l.strip("❯ ")
                for l in pane.splitlines() if l.strip()
            )
            if not at_prompt:
                continue
            dispatched_at = task.get("dispatched_at", "")
            # Only recover tasks dispatched >60s ago (give normal flow time to work)
            try:
                from datetime import timezone
                dt = datetime.fromisoformat(dispatched_at.rstrip("Z"))
                age = time.time() - dt.replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                age = 0
            if age < 60:
                continue
            task_text = task.get("task", "")
            task_id   = task.get("task_id", "")
            log(f"[watchdog] recovering stuck task {task_id} on {session} (age {age:.0f}s)")
            await asyncio.get_event_loop().run_in_executor(None, lambda s=session, t=task_text: (
                tmux(["send-keys", "-t", s, t]),
                time.sleep(0.3),
                tmux(["send-keys", "-t", s, "Enter"])
            ))
        except Exception as e:
            log(f"[watchdog] queue scan error {qf.name}: {e}")


def _run_http_redirect(host: str, http_port: int = 9888, https_port: int = 9889):
    """Tiny HTTP server that 301-redirects all requests to HTTPS."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            target = f"https://{host}:{https_port}{self.path}"
            self.send_response(301)
            self.send_header("Location", target)
            self.end_headers()
        do_POST = do_HEAD = do_PUT = do_DELETE = do_GET
        def log_message(self, *_): pass  # silence access log

    try:
        srv = HTTPServer((host, http_port), _Redirect)
    except OSError as e:
        print(f"[http-redirect] port {http_port} already in use, skipping: {e}")
        return
    print(f"[http-redirect] {host}:{http_port} → https:{https_port}")
    srv.serve_forever()


if __name__ == "__main__":
    import uvicorn
    tailscale_ip = os.environ.get("ORCHMUX_BIND_HOST", "127.0.0.1")
    cert = Path(__file__).parent / "cert.pem"
    key  = Path(__file__).parent / "key.pem"
    ssl_kwargs = {}
    if cert.exists() and key.exists():
        ssl_kwargs = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
        # Start HTTP→HTTPS redirect on port 9888 in background thread
        t = threading.Thread(target=_run_http_redirect, args=(tailscale_ip,), daemon=True)
        t.start()

    # Bind to Tailscale IP only (UFW blocks public ports)
    uvicorn.run(app, host=tailscale_ip, port=9889, log_level="info", **ssl_kwargs)
