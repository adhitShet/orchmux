"""
orchmux notify — channel abstraction for Telegram + Slack

Routing rules:
  urgent     → Telegram only     (blocked, auth-stuck, infra-dead)
  completion → Slack only        (#orchmux-results / C09AQ35HG6P)
  question   → Telegram only     (worker asking for input)
  info       → Slack only        (status updates)

Usage:
  from lib.notify import notify, notify_telegram, notify_slack

  notify("task done", priority="completion", session="firmware")
  notify_telegram("urgent: server down")
  notify_slack("deploy finished", channel="C09AQ35HG6P")
"""

import json
import os
import threading
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

# ── Config loading ─────────────────────────────────────────────────────────────

_ENV_FILE = Path.home() / ".claude/hooks/.env"
_env_loaded = False


def _load_env() -> None:
    global _env_loaded
    if _env_loaded:
        return
    try:
        for line in _ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass
    _env_loaded = True


def _token(key: str) -> str:
    _load_env()
    return os.environ.get(key, "")


# ── Priority routing ───────────────────────────────────────────────────────────

# Maps priority → list of channels to send to
_ROUTING: dict[str, list[str]] = {
    "urgent":     ["telegram"],
    "completion": ["slack"],
    "question":   ["telegram"],
    "info":       ["slack"],
}

_DEFAULT_SLACK_CHANNEL = "C09AQ35HG6P"


# ── Telegram ───────────────────────────────────────────────────────────────────

def notify_telegram(message: str) -> None:
    """Send message to Telegram chat. Swallows all errors."""
    token = _token("TELEGRAM_BOT_TOKEN")
    chat_id = _token("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


# ── Slack ──────────────────────────────────────────────────────────────────────

def notify_slack(message: str, channel: str = _DEFAULT_SLACK_CHANNEL) -> None:
    """Send message to Slack channel. Swallows all errors."""
    token = _token("SLACK_BOT_TOKEN")
    if not token:
        return
    url = "https://slack.com/api/chat.postMessage"
    payload = json.dumps({
        "channel": channel,
        "text": message,
    }).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


# ── Main entry point ───────────────────────────────────────────────────────────

def notify(
    message: str,
    priority: str = "info",
    session: str = "",
) -> None:
    """
    Route message to correct channel(s) based on priority.
    Non-blocking: dispatches in a background thread.
    Never raises — all errors are swallowed.

    Args:
        message:  Text to send.
        priority: One of "urgent", "completion", "question", "info".
                  Unknown priorities fall back to "info" routing.
        session:  Optional worker session name, prepended to message as context.
    """
    channels = _ROUTING.get(priority, _ROUTING["info"])
    full_message = f"[{session}] {message}" if session else message

    def _send() -> None:
        try:
            for ch in channels:
                if ch == "telegram":
                    notify_telegram(full_message)
                elif ch == "slack":
                    notify_slack(full_message)
        except Exception:
            pass

    t = threading.Thread(target=_send, daemon=True)
    t.start()
