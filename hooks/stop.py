#!/usr/bin/env python3
"""
orchmux Stop hook — auto-dispatch fallback.
Fires after every Claude turn. If we're in the orchmux supervisor
and Claude didn't already dispatch, we dispatch on its behalf.
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

ORCHMUX_ROOT = Path.home() / "orchmux"
SERVER = "http://localhost:9889"

# Short messages that aren't tasks
SKIP_PATTERNS = [
    r"^(yes|no|ok|okay|sure|thanks|thank you|got it|done|good|great|cool|perfect)\.?$",
    r"^(y|n|k)$",
]

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


def read_transcript(path: str) -> list:
    lines = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        lines.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return lines


def get_last_user_message(transcript: list) -> str:
    for entry in reversed(transcript):
        if entry.get("type") == "user":
            content = entry.get("message", {}).get("content", "")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
                return " ".join(parts).strip()
    return ""


def already_dispatched(transcript: list) -> bool:
    """Check if Claude already called /dispatch this turn."""
    for entry in reversed(transcript):
        if entry.get("type") != "assistant":
            continue
        content = entry.get("message", {}).get("content", [])
        if not isinstance(content, list):
            break
        for block in content:
            if not isinstance(block, dict):
                continue
            # Tool call to Bash with dispatch URL
            if block.get("type") == "tool_use" and block.get("name") == "Bash":
                cmd = block.get("input", {}).get("command", "")
                if "9889/dispatch" in cmd:
                    return True
            # Claude responded with ROUTE: format
            if block.get("type") == "text":
                text = block.get("text", "")
                if re.search(r"ROUTE:\w+\|", text):
                    return True
        break  # only check last assistant message
    return False


def is_supervisor_session(cwd: str) -> bool:
    """Only run dispatch logic when we're in the orchmux supervisor."""
    if not cwd:
        return False
    # Workers set ORCHMUX_SESSION — if so, this is a worker, not supervisor
    if os.environ.get("ORCHMUX_SESSION"):
        return False
    cwd_path = Path(cwd)
    if (cwd_path / ".orchmux_supervisor").exists():
        return True
    # Must be exactly the supervisor dir, not worker-workdirs
    if cwd_path.name == "supervisor" and "orchmux" in cwd_path.parts:
        return True
    return False


def is_task_message(msg: str) -> bool:
    """Filter out short acknowledgments that aren't tasks."""
    if len(msg.strip()) < 8:
        return False
    # Ignore orchmux status updates injected by the flusher — not user tasks
    if msg.strip().startswith("[orchmux]"):
        return False
    for pattern in SKIP_PATTERNS:
        if re.match(pattern, msg.strip().lower()):
            return False
    return True


def match_domain(msg: str) -> str:
    msg_lower = msg.lower()
    scores = {}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in msg_lower)
        if score > 0:
            scores[domain] = score
    if scores:
        return max(scores, key=scores.get)
    return "research"  # default fallback


def dispatch(domain: str, task: str, context: str = "") -> dict:
    payload = json.dumps({
        "domain": domain,
        "task": task,
        "context": context,
        "priority": "normal"
    }).encode()
    req = urllib.request.Request(
        f"{SERVER}/dispatch",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError:
        return {}  # server not running — silent fail


def server_running() -> bool:
    try:
        urllib.request.urlopen(f"{SERVER}/health", timeout=1)
        return True
    except Exception:
        return False


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    cwd = data.get("cwd", "")
    transcript_path = data.get("transcript_path", "")
    last_msg = data.get("last_assistant_message", "")

    # Only act in supervisor context
    if not is_supervisor_session(cwd):
        sys.exit(0)

    # Only act if server is up
    if not server_running():
        sys.exit(0)

    transcript = read_transcript(transcript_path)

    # Skip if Claude already dispatched
    if already_dispatched(transcript):
        sys.exit(0)

    # Get last user message
    user_task = get_last_user_message(transcript)
    if not user_task or not is_task_message(user_task):
        sys.exit(0)

    # Determine domain and dispatch
    domain = match_domain(user_task)
    result = dispatch(domain, user_task, context=last_msg[:500])

    if result.get("task_id"):
        task_id = result["task_id"]
        session = result.get("session", "queued")
        wtype = result.get("worker_type", "")
        status = result.get("status", "")
        print(f"\n→ orchmux: dispatched to {domain} worker "
              f"[{session}] ({wtype}) — task {task_id}")
    else:
        print(f"\n→ orchmux: fallback dispatch failed for domain={domain}")


if __name__ == "__main__":
    main()
