"""
lib/model_health.py — Model failure tracking and fallback selection for orchmux.

Tracks per-model failure counts and cooldown windows in:
    ~/orchmux/health/model_health.json

Cooldown schedule:
    0 failures  → no cooldown
    1 failure   → 5 min
    2 failures  → 15 min
    3+ failures → 60 min

Uses fcntl.flock for safe concurrent access.
"""

import fcntl
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ORCHMUX = Path.home() / "orchmux"
HEALTH_DIR = ORCHMUX / "health"
HEALTH_FILE = HEALTH_DIR / "model_health.json"

# Cooldown minutes indexed by failure count (index 3 covers 3+)
_COOLDOWN_MINUTES = [0, 5, 15, 60]


def _cooldown_minutes(failures: int) -> int:
    if failures <= 0:
        return 0
    idx = min(failures, len(_COOLDOWN_MINUTES) - 1)
    return _COOLDOWN_MINUTES[idx]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _default_entry() -> dict:
    return {"failures": 0, "last_failure": None, "cooldown_until": None}


def _load_state() -> dict:
    """Load model health state from disk. Returns empty dict if file missing."""
    if not HEALTH_FILE.exists():
        return {}
    try:
        with HEALTH_FILE.open("r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    """Atomically write state to disk using fcntl locking."""
    HEALTH_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = HEALTH_FILE.with_suffix(".lock")

    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            tmp_path = HEALTH_FILE.with_suffix(".tmp")
            with tmp_path.open("w") as f:
                json.dump(state, f, indent=2)
                f.write("\n")
            tmp_path.replace(HEALTH_FILE)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def _read_locked() -> dict:
    """Read state under an exclusive lock to prevent torn reads during writes."""
    HEALTH_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = HEALTH_FILE.with_suffix(".lock")

    with open(lock_path, "a+") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_SH)
        try:
            return _load_state()
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def record_failure(model: str) -> None:
    """
    Increment failure count for model and set cooldown_until accordingly.
    """
    HEALTH_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = HEALTH_FILE.with_suffix(".lock")

    with open(lock_path, "a+") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            state = _load_state()
            entry = state.get(model, _default_entry())

            new_failures = entry["failures"] + 1
            minutes = _cooldown_minutes(new_failures)
            now = datetime.now(timezone.utc)
            cooldown_until = (
                (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")
                if minutes > 0
                else None
            )

            state[model] = {
                "failures": new_failures,
                "last_failure": now.isoformat(timespec="seconds"),
                "cooldown_until": cooldown_until,
            }

            tmp_path = HEALTH_FILE.with_suffix(".tmp")
            with tmp_path.open("w") as f:
                json.dump(state, f, indent=2)
                f.write("\n")
            tmp_path.replace(HEALTH_FILE)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def record_success(model: str) -> None:
    """
    Reset failure count to 0 and clear cooldown for model.
    """
    HEALTH_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = HEALTH_FILE.with_suffix(".lock")

    with open(lock_path, "a+") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            state = _load_state()
            state[model] = _default_entry()

            tmp_path = HEALTH_FILE.with_suffix(".tmp")
            with tmp_path.open("w") as f:
                json.dump(state, f, indent=2)
                f.write("\n")
            tmp_path.replace(HEALTH_FILE)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def is_healthy(model: str) -> bool:
    """
    Return True if model has no active cooldown.

    A model with zero recorded failures, or whose cooldown has expired,
    is considered healthy.
    """
    state = _read_locked()
    entry = state.get(model, _default_entry())
    cooldown_until = _parse_iso(entry.get("cooldown_until"))

    if cooldown_until is None:
        return True

    return datetime.now(timezone.utc) >= cooldown_until


def _load_workers_yaml(workers_yaml_path: Path) -> dict:
    """
    Minimal YAML parser for workers.yaml — extracts top-level worker entries
    and their model/fallback fields. Returns dict[domain -> {model, fallback}].
    """
    if not workers_yaml_path.exists():
        return {}

    workers: dict = {}
    in_workers_block = False
    current_domain: Optional[str] = None

    with workers_yaml_path.open() as f:
        for raw_line in f:
            line = raw_line.rstrip()
            stripped = line.lstrip()

            if not stripped or stripped.startswith("#"):
                continue

            indent = len(line) - len(line.lstrip())

            if indent == 0:
                if stripped == "workers:":
                    in_workers_block = True
                    current_domain = None
                elif stripped.endswith(":") and not stripped.startswith("_"):
                    in_workers_block = False
                    current_domain = None
                continue

            if not in_workers_block:
                continue

            if indent == 2 and stripped.endswith(":") and not stripped.startswith("-"):
                current_domain = stripped[:-1]
                workers[current_domain] = {}
            elif indent == 4 and current_domain is not None and ":" in stripped:
                key, _, value = stripped.partition(":")
                key = key.strip()
                value = value.strip()
                if key in ("model", "fallback"):
                    workers[current_domain][key] = value

    return workers


def get_fallback(model: str, workers_yaml_path: Path) -> Optional[str]:
    """
    Find the fallback model for the given model.

    Reads workers.yaml to find the domain that uses this model.
    If that domain has a `fallback` field, returns it.
    Otherwise returns "claude" if model is not "claude", else None.

    Args:
        model: the model name to find a fallback for (e.g. "codex")
        workers_yaml_path: path to workers.yaml

    Returns:
        fallback model name string, or None if no fallback is available
    """
    workers = _load_workers_yaml(workers_yaml_path)

    # Find the first domain that uses this model
    for _domain, spec in workers.items():
        if spec.get("model") == model:
            fallback = spec.get("fallback")
            if fallback:
                return fallback
            break

    # Default: fall back to claude unless we already are claude
    if model != "claude":
        return "claude"

    return None


def health_summary() -> str:
    """
    Return a one-line human-readable status for all tracked models.

    Example: "claude ✓  codex ✗ (cooldown 23min)"
    """
    state = _read_locked()

    if not state:
        return "no model health data"

    now = datetime.now(timezone.utc)
    parts: list[str] = []

    for model, entry in sorted(state.items()):
        cooldown_until = _parse_iso(entry.get("cooldown_until"))

        if cooldown_until is None or now >= cooldown_until:
            parts.append(f"{model} ✓")
        else:
            remaining = cooldown_until - now
            mins_left = int(remaining.total_seconds() / 60) + 1
            parts.append(f"{model} ✗ (cooldown {mins_left}min)")

    return "  ".join(parts)
