#!/usr/bin/env python3
"""
orchmux worker-stop hook
Fires after every Claude Code worker turn.
Handles [DONE] completion signaling and question routing to supervisor.
"""

import json
import os
import sys
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError
import urllib.parse

ORCHMUX_SERVER = "http://localhost:9889"
TIMEOUT = 5
QUEUE_DIR = Path.home() / "orchmux" / "queue"


def http_post(url: str, data: dict) -> bool:
    payload = json.dumps(data).encode("utf-8")
    req = Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status < 400
    except (URLError, Exception):
        return False


def last_non_empty_line(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    for line in reversed(lines):
        if line:
            return line
    return ""


def main() -> None:
    # Only run inside orchmux worker sessions
    session = os.environ.get("ORCHMUX_SESSION", "").strip()
    if not session:
        sys.exit(0)

    # Parse stdin JSON
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)

    last_message = (data.get("last_assistant_message") or "").strip()

    # Read the queue YAML for this session
    queue_file = QUEUE_DIR / f"{session}.yaml"
    if not queue_file.exists():
        sys.exit(0)

    try:
        import yaml
        with open(queue_file, "r") as f:
            task = yaml.safe_load(f)
    except Exception:
        sys.exit(0)

    if not task or task.get("status") != "pending":
        sys.exit(0)

    task_id = task.get("task_id", "unknown")
    domain = task.get("domain", "worker")

    # --- [DONE] detection ---
    if "[DONE]" in last_message:
        idx = last_message.index("[DONE]") + len("[DONE]")
        result = last_message[idx:].strip() or last_message

        http_post(f"{ORCHMUX_SERVER}/complete", {
            "session": session,
            "task_id": task_id,
            "result": result,
            "success": True,
        })
        print(f"-> orchmux: task {task_id} auto-completed")
        sys.exit(0)

    # --- Question detection ---
    last_line = last_non_empty_line(last_message)
    if last_line.endswith("?"):
        snippet = last_message[:300]
        http_post(f"{ORCHMUX_SERVER}/notify", {
            "message": f"❓ [{domain}] [{session}] asks: {snippet}",
            "channels": ["telegram"],
        })
        print("-> orchmux: question sent to supervisor")
        sys.exit(0)

    # Default: mid-task, do nothing
    sys.exit(0)


try:
    main()
except Exception:
    # Never crash or block Claude
    sys.exit(0)
