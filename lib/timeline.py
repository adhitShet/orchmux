"""
lib/timeline.py — Structured per-worker event log for orchmux.

Each worker session gets its own JSONL file at:
    ~/orchmux/timeline/{session}.jsonl

One JSON line per event, written with fcntl.flock for thread safety.
"""

import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ORCHMUX = Path.home() / "orchmux"
TIMELINE_DIR = ORCHMUX / "timeline"

_VALID_EVENTS = frozenset({"dispatched", "completed", "relaunched", "blocked", "timeout"})


def _timeline_path(session: str) -> Path:
    TIMELINE_DIR.mkdir(parents=True, exist_ok=True)
    return TIMELINE_DIR / f"{session}.jsonl"


def timeline_write(
    session: str,
    event: str,
    *,
    task_id: str = "",
    domain: str = "",
    model: str = "",
    duration_s: Optional[float] = None,
    result_summary: str = "",
    success: bool = True,
) -> None:
    """Append one event line to ~/orchmux/timeline/{session}.jsonl.

    Thread-safe via fcntl.flock exclusive lock.
    """
    record = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": event,
        "task_id": task_id,
        "session": session,
        "domain": domain,
        "model": model,
        "duration_s": duration_s,
        "result_summary": result_summary,
        "success": success,
    }
    path = _timeline_path(session)
    with path.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(record) + "\n")
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def timeline_read(session: str, limit: int = 20) -> list[dict]:
    """Return the last `limit` events for the given session, oldest-first."""
    path = _timeline_path(session)
    if not path.exists():
        return []
    lines: list[str] = []
    with path.open("r") as fh:
        fcntl.flock(fh, fcntl.LOCK_SH)
        try:
            lines = fh.readlines()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    tail = lines[-limit:] if len(lines) > limit else lines
    records = []
    for line in tail:
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def timeline_recent(limit: int = 50) -> list[dict]:
    """Return the most recent `limit` events across ALL session files, sorted by ts desc."""
    TIMELINE_DIR.mkdir(parents=True, exist_ok=True)
    all_records: list[dict] = []
    for path in TIMELINE_DIR.glob("*.jsonl"):
        with path.open("r") as fh:
            fcntl.flock(fh, fcntl.LOCK_SH)
            try:
                lines = fh.readlines()
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        for line in lines:
            line = line.strip()
            if line:
                try:
                    all_records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    # Sort descending by ts string (ISO-8601 sorts lexicographically)
    all_records.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return all_records[:limit]
