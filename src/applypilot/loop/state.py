"""Persistent state for the autonomous apply reliability loop.

State lives in $APPLYPILOT_DIR/loop-state.json. Atomic write via temp file
+ os.replace. Each iteration of ralph-loop reads on entry, writes on exit.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from applypilot import config

SCHEMA_VERSION = 1
STATE_FILENAME = "loop-state.json"
ATTEMPT_WINDOW = 10


def state_path() -> Path:
    return config.APP_DIR / STATE_FILENAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def default_state() -> dict[str, Any]:
    """Fresh loop state — used on first iteration or when state file is missing."""
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": str(uuid.uuid4()),
        "pid": os.getpid(),
        "started_at": _now_iso(),
        "iteration": 0,
        "streak": 0,
        "last_processed_review_ts": None,
        "attempts": [],
        "signature_counts": {},
        "cooldown_remaining": 0,
        "last_patch": None,
        "patch_budgets": {},
        "class_blocklist": [],
        "rollback_log": [],
    }


def save_atomic(state: dict[str, Any]) -> None:
    """Atomic write via temp file + os.replace. Survives interruption."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".loop-state-",
        suffix=".json.tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_or_init() -> dict[str, Any]:
    """Read state file; create with defaults if absent."""
    path = state_path()
    if not path.exists():
        s = default_state()
        save_atomic(s)
        return s
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def is_pid_alive(pid: int) -> bool:
    """Cross-platform check whether a PID corresponds to a running process."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def verify_lock(state: dict[str, Any]) -> str:
    """Return one of: "owned", "conflict", "stale".

    "owned"    — current process matches state.pid (we hold the lock)
    "conflict" — foreign session_id AND foreign PID still alive
    "stale"    — foreign session_id but foreign PID is dead (safe to take over)
    """
    own_pid = os.getpid()
    if state.get("pid") == own_pid:
        return "owned"
    foreign_pid = state.get("pid", 0)
    if is_pid_alive(foreign_pid):
        return "conflict"
    return "stale"


def rolling_pass_rate(attempts: list[dict[str, Any]]) -> float:
    """Fraction of attempts whose status is exactly 'applied'.

    Verifier-confirmed only — 'needs_review:applied' or similar does not count.
    """
    if not attempts:
        return 0.0
    hits = sum(1 for a in attempts if a.get("status") == "applied")
    return hits / len(attempts)


def roll_attempts(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only the most recent ATTEMPT_WINDOW entries."""
    if len(attempts) <= ATTEMPT_WINDOW:
        return list(attempts)
    return list(attempts[-ATTEMPT_WINDOW:])


def streak_from_attempts(attempts: list[dict[str, Any]]) -> int:
    """Number of trailing 'applied' entries before the first non-applied."""
    count = 0
    for a in reversed(attempts):
        if a.get("status") == "applied":
            count += 1
        else:
            break
    return count
