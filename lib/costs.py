"""
lib/costs.py — Per-task cost/duration tracking for orchmux.

Completed task metrics are written to:
    ~/orchmux/costs/YYYY-MM.jsonl

One JSON line per task, written with fcntl.flock for thread safety.
"""

import fcntl
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Union

ORCHMUX = Path.home() / "orchmux"
COSTS_DIR = ORCHMUX / "costs"


def _costs_path(ts: datetime) -> Path:
    COSTS_DIR.mkdir(parents=True, exist_ok=True)
    month = ts.strftime("%Y-%m")
    return COSTS_DIR / f"{month}.jsonl"


def cost_record(
    task_id: str,
    domain: str,
    session: str,
    model: str,
    duration_s: float,
    result_len: int,
    success: bool,
) -> None:
    """Append one cost record to ~/orchmux/costs/YYYY-MM.jsonl.

    Thread-safe via fcntl.flock exclusive lock.
    """
    now = datetime.now(timezone.utc)
    record = {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "task_id": task_id,
        "domain": domain,
        "session": session,
        "model": model,
        "duration_s": duration_s,
        "result_len": result_len,
        "success": success,
    }
    path = _costs_path(now)
    with path.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(record) + "\n")
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _iter_recent_records(days: int):
    """Yield cost records from the last `days` days across relevant month files."""
    COSTS_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    # Collect the set of YYYY-MM strings we need to check
    months_needed: set[str] = set()
    cursor = cutoff
    end = datetime.now(timezone.utc)
    while cursor <= end:
        months_needed.add(cursor.strftime("%Y-%m"))
        # advance by ~1 month
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)
    months_needed.add(end.strftime("%Y-%m"))

    for month in sorted(months_needed):
        path = COSTS_DIR / f"{month}.jsonl"
        if not path.exists():
            continue
        with path.open("r") as fh:
            fcntl.flock(fh, fcntl.LOCK_SH)
            try:
                lines = fh.readlines()
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Filter to cutoff window
            ts_str = record.get("ts", "")
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
            if ts >= cutoff:
                yield record


def cost_summary(days: int = 7) -> dict:
    """Return per-domain aggregates for the last `days` days.

    Returns:
        {
          domain: {
            "count": int,
            "avg_duration_s": float,
            "total_duration_s": float,
            "models": {"claude": int, "codex": int, ...}
          }
        }
    """
    # domain -> accumulator
    agg: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "total_duration_s": 0.0, "models": defaultdict(int)}
    )
    for record in _iter_recent_records(days):
        domain = record.get("domain") or "unknown"
        model = record.get("model") or "unknown"
        duration = record.get("duration_s") or 0.0
        agg[domain]["count"] += 1
        agg[domain]["total_duration_s"] += duration
        agg[domain]["models"][model] += 1

    result = {}
    for domain, data in sorted(agg.items()):
        count = data["count"]
        total = data["total_duration_s"]
        result[domain] = {
            "count": count,
            "avg_duration_s": round(total / count, 1) if count else 0.0,
            "total_duration_s": round(total, 1),
            "models": dict(data["models"]),
        }
    return result


def cost_summary_text(days: int = 7) -> str:
    """Return a human-readable table of cost/duration stats for the last `days` days."""
    summary = cost_summary(days=days)
    if not summary:
        return f"No cost records found for the last {days} days."

    lines = [f"Task stats — last {days} days", ""]
    header = f"{'Domain':<20} {'Tasks':>6} {'Avg dur':>9} {'Total dur':>10}  Models"
    lines.append(header)
    lines.append("-" * len(header))

    for domain, data in summary.items():
        avg = _fmt_duration(data["avg_duration_s"])
        total = _fmt_duration(data["total_duration_s"])
        models_str = "  ".join(
            f"{m}:{n}" for m, n in sorted(data["models"].items())
        )
        lines.append(
            f"{domain:<20} {data['count']:>6} {avg:>9} {total:>10}  {models_str}"
        )

    total_tasks = sum(d["count"] for d in summary.values())
    total_dur = sum(d["total_duration_s"] for d in summary.values())
    lines.append("-" * len(header))
    lines.append(
        f"{'TOTAL':<20} {total_tasks:>6} {'':>9} {_fmt_duration(total_dur):>10}"
    )
    return "\n".join(lines)


def _fmt_duration(seconds: float) -> str:
    """Format seconds as Xm Ys or Xs."""
    seconds = int(seconds)
    if seconds >= 60:
        m, s = divmod(seconds, 60)
        return f"{m}m {s:02d}s"
    return f"{seconds}s"
