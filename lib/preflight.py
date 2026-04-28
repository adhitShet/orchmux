"""
lib/preflight.py — Pre-dispatch skill readiness check for orchmux.

Before a task is dispatched to a worker, verifies the domain's required
binaries and environment variables exist.

Requirements are read from ~/orchmux/skills/registry.yaml.
Env vars are sourced from ~/.claude/hooks/.env AND os.environ.
"""

import os
import shutil
from pathlib import Path
from typing import Optional

ORCHMUX = Path.home() / "orchmux"
REGISTRY_PATH = ORCHMUX / "skills" / "registry.yaml"
HOOKS_ENV_PATH = Path.home() / ".claude" / "hooks" / ".env"


def _load_registry() -> dict:
    """Parse registry.yaml using stdlib only (no PyYAML dependency)."""
    if not REGISTRY_PATH.exists():
        return {}

    registry: dict = {}
    current_domain: Optional[str] = None
    current_section: Optional[str] = None

    with REGISTRY_PATH.open() as f:
        for raw_line in f:
            line = raw_line.rstrip()

            # Skip comments and blank lines
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue

            indent = len(line) - len(line.lstrip())

            if indent == 0:
                # Top-level key: domain name
                if line.endswith(":"):
                    current_domain = line[:-1].strip()
                    registry[current_domain] = {"bins": [], "env": []}
                    current_section = None
            elif indent == 2 and current_domain is not None:
                # Section key: bins or env
                if ":" in stripped:
                    key, _, rest = stripped.partition(":")
                    key = key.strip()
                    rest = rest.strip()
                    if key in ("bins", "env"):
                        current_section = key
                        # Inline list like: bins: [git, gh, west]
                        if rest.startswith("[") and rest.endswith("]"):
                            items = [
                                item.strip()
                                for item in rest[1:-1].split(",")
                                if item.strip()
                            ]
                            registry[current_domain][key] = items
                            current_section = None
                        elif rest == "[]":
                            registry[current_domain][key] = []
                            current_section = None
            elif indent >= 4 and current_domain is not None and current_section is not None:
                # List item: - value
                if stripped.startswith("- "):
                    value = stripped[2:].strip()
                    registry[current_domain][current_section].append(value)

    return registry


def _load_hooks_env() -> dict[str, str]:
    """Load key=value pairs from ~/.claude/hooks/.env, ignoring comments."""
    env: dict[str, str] = {}
    if not HOOKS_ENV_PATH.exists():
        return env

    with HOOKS_ENV_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Strip surrounding quotes if present
                if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                    value = value[1:-1]
                if key:
                    env[key] = value

    return env


def _env_present(var: str, hooks_env: dict[str, str]) -> bool:
    """Return True if var is non-empty in hooks_env or os.environ."""
    value_hooks = hooks_env.get(var, "")
    value_os = os.environ.get(var, "")
    return bool(value_hooks) or bool(value_os)


def preflight_check(domain: str) -> tuple[bool, list[str]]:
    """
    Check that all required binaries and env vars for a domain are present.

    Returns:
        (ok, issues) where issues is a list of human-readable failure strings
        such as "missing binary: gh" or "missing env: SLACK_BOT_TOKEN".
        ok is True only when issues is empty.
    """
    registry = _load_registry()
    issues: list[str] = []

    if domain not in registry:
        # Unknown domain — no requirements, passes by default
        return True, []

    spec = registry[domain]
    hooks_env = _load_hooks_env()

    for binary in spec.get("bins", []):
        if shutil.which(binary) is None:
            issues.append(f"missing binary: {binary}")

    for var in spec.get("env", []):
        if not _env_present(var, hooks_env):
            issues.append(f"missing env: {var}")

    return (len(issues) == 0), issues


def preflight_summary(domain: str) -> str:
    """
    Return a one-line human-readable summary of preflight status.

    Examples:
        "✓ firmware ready"
        "✗ firmware: missing binary: west, missing env: GITHUB_TOKEN"
    """
    ok, issues = preflight_check(domain)
    if ok:
        return f"✓ {domain} ready"
    return f"✗ {domain}: {', '.join(issues)}"
