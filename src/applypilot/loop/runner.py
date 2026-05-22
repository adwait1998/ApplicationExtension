"""Subprocess wrapper for `applypilot apply --limit 1` with brick detection.

apply_one() invokes one live (or dry-run-flagged) apply, measures
launch-to-exit elapsed time, and returns a structured dict the driver
prompt uses to update state. Brick-detection logic is delegated to
safety.brick_detected for testability.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any

from applypilot.loop import safety


DEFAULT_APPLY_ARGS: tuple[str, ...] = (
    "--workers", "1",
    "--limit", "1",
    "--model", "claude-haiku-4-5-20251001",
    "--headless",
    "--no-live",
    "--job-timeout", "720",
    "--max-transient-retries", "1",
)


def _monotonic_elapsed(start: float | None = None) -> float:
    """Return seconds since `start`, or 0.0 if start is None."""
    if start is None:
        return 0.0
    return time.monotonic() - start


def apply_one(extra_args: tuple[str, ...] = ()) -> dict[str, Any]:
    """Run one live apply and report whether it succeeded fast or bricked.

    Returns a dict with keys:
      - status: "completed" | "brick_detected"
      - returncode: int
      - brick: bool
      - elapsed_s: float
      - stdout: str (last 4000 chars)
      - stderr: str (last 4000 chars)
    """
    os.environ.setdefault("APPLYPILOT_VISUAL_TRACE", "1")
    cmd: list[str] = [
        sys.executable, "-m", "applypilot", "apply",
        *DEFAULT_APPLY_ARGS, *extra_args,
    ]
    start = time.monotonic()
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    elapsed = _monotonic_elapsed(start)
    brick = safety.brick_detected(completed.returncode, elapsed)
    return {
        "status": "brick_detected" if brick else "completed",
        "returncode": completed.returncode,
        "brick": brick,
        "elapsed_s": elapsed,
        "stdout": (completed.stdout or "")[-4000:],
        "stderr": (completed.stderr or "")[-4000:],
    }
