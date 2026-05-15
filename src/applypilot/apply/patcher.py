"""Tier 2 patch driver — spawns a short-scoped Claude Code subprocess to
fill ONLY the unresolved fields in a skill, then returns control.

This is intentionally a thin shell. It reuses the launcher's existing
subprocess machinery (Claude Code CLI + Playwright MCP + Gmail MCP) but
with a different prompt and a different RESULT regex (PATCHED, not APPLIED).

The launcher calls this in the apply flow when Tier 1's `replay_skill()`
returns `needs_patch`:

    if replay_result.status == "needs_patch":
        patch_result = run_patch(
            job=job, port=port, worker_id=worker_id,
            apply_url=apply_url, unresolved=replay_result.unresolved,
            profile=profile, tailored_resume=resume_text,
            model=model, dry_run=dry_run, patch_timeout_s=180,
        )
        if patch_result.status == "patched":
            submit_result = replay.submit_only(skill, page)
            ...

For v1 the patcher does NOT itself manage Chrome — it assumes Chrome is
already up on `port` (the launcher provisioned it for Tier 1). This keeps
Phase 3 testable without spinning up the full apply machinery.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from applypilot.apply.skill_schema import UnresolvedField
from applypilot.apply.prompt_patch import build_patch_prompt

log = logging.getLogger(__name__)

# Result statuses
PATCH_PATCHED = "patched"
PATCH_FAILED_DRIFT = "failed:patch_form_drift"
PATCH_FAILED_UNINTERPRETABLE = "failed:patch_uninterpretable"
PATCH_FAILED_TIMEOUT = "failed:patch_timeout"
PATCH_FAILED_NO_RESULT = "failed:patch_no_result_line"
PATCH_FAILED_OTHER = "failed:patch_other"


@dataclass
class PatchResult:
    status: str
    duration_ms: int
    output: str = ""
    error: str | None = None


# Match RESULT:PATCHED or RESULT:FAILED:patch_<reason>
_RESULT_PATCHED = re.compile(r"\bRESULT:PATCHED\b", re.IGNORECASE)
_RESULT_FAILED = re.compile(r"\bRESULT:FAILED:(patch_[A-Za-z0-9_]+)", re.IGNORECASE)


def run_patch(
    *,
    job: dict,
    apply_url: str,
    unresolved: Iterable[UnresolvedField],
    profile: dict,
    tailored_resume: str = "",
    model: str = "claude-haiku-4-5-20251001",
    dry_run: bool = False,
    patch_timeout_s: int = 180,
    claude_bin: str | None = None,
    mcp_config_path: str | Path | None = None,
    workdir: str | Path | None = None,
    extra_argv: list[str] | None = None,
    _spawn_fn=None,
) -> PatchResult:
    """Run the Tier 2 patch subprocess and parse the result.

    Args mirror the launcher's run_job() where it makes sense, but with the
    scope narrowed to "fill these specific fields, exit".

    `_spawn_fn` is for testing: pass a callable(argv, stdin, timeout) that
    returns (returncode, stdout). When None, uses subprocess.Popen.
    """
    unresolved_list = list(unresolved)
    if not unresolved_list:
        # No work to do — caller shouldn't have called us. Treat as patched.
        return PatchResult(status=PATCH_PATCHED, duration_ms=0,
                           output="(no unresolved fields)")

    prompt = build_patch_prompt(
        job=job, apply_url=apply_url, unresolved=unresolved_list,
        profile=profile, tailored_resume=tailored_resume,
    )

    if dry_run:
        log.info("run_patch: dry_run, returning PATCHED without spawning subprocess")
        return PatchResult(status=PATCH_PATCHED, duration_ms=0,
                           output="(dry_run)")

    argv = _build_argv(
        claude_bin=claude_bin, model=model,
        mcp_config_path=mcp_config_path,
        extra_argv=extra_argv,
    )

    started = time.monotonic()
    spawn = _spawn_fn or _spawn_subprocess
    try:
        returncode, stdout = spawn(argv=argv, stdin_text=prompt,
                                    timeout_s=patch_timeout_s,
                                    workdir=workdir)
    except _SpawnTimeout as e:
        return PatchResult(
            status=PATCH_FAILED_TIMEOUT,
            duration_ms=int((time.monotonic() - started) * 1000),
            output=e.partial_output,
            error="patch subprocess hit patch_timeout_s wall",
        )
    except Exception as e:
        return PatchResult(
            status=PATCH_FAILED_OTHER,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(e).__name__}: {e}",
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    status, error = _classify_output(stdout, returncode)
    return PatchResult(status=status, duration_ms=duration_ms,
                       output=stdout, error=error)


# ---------------------------------------------------------------------------
# Result classification
# ---------------------------------------------------------------------------

def _classify_output(stdout: str, returncode: int) -> tuple[str, str | None]:
    """Walk stdout looking for the patch result code. Stream-json aware:
    if the line is JSON with an assistant message, look inside its content
    blocks for text. Plain-text lines also accepted (Claude Code may emit
    both).
    """
    # Collect candidate text fragments
    text_chunks: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            text_chunks.append(line)
            continue
        if isinstance(obj, dict) and obj.get("type") == "assistant":
            for block in obj.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    t = block.get("text") or ""
                    if t:
                        text_chunks.append(t)
        elif isinstance(obj, dict) and obj.get("type") == "result":
            r = obj.get("result")
            if r:
                text_chunks.append(str(r))
    full = "\n".join(text_chunks)

    # Latest line wins: reverse-search
    for line in reversed(full.splitlines()):
        clean = line.strip().strip("`*").replace("**", "").replace("__", "")
        if _RESULT_PATCHED.search(clean):
            return PATCH_PATCHED, None
        m = _RESULT_FAILED.search(clean)
        if m:
            reason = m.group(1).lower()
            if reason == "patch_form_drift":
                return PATCH_FAILED_DRIFT, None
            if reason == "patch_uninterpretable":
                return PATCH_FAILED_UNINTERPRETABLE, None
            return f"failed:{reason}", None

    # No RESULT line at all
    if returncode and returncode != 0:
        return PATCH_FAILED_OTHER, f"subprocess exited with code {returncode}"
    return PATCH_FAILED_NO_RESULT, "model emitted no RESULT:PATCHED line"


# ---------------------------------------------------------------------------
# Subprocess machinery
# ---------------------------------------------------------------------------

class _SpawnTimeout(Exception):
    def __init__(self, partial_output: str) -> None:
        super().__init__("spawn timeout")
        self.partial_output = partial_output


def _build_argv(
    *,
    claude_bin: str | None,
    model: str,
    mcp_config_path: str | Path | None,
    extra_argv: list[str] | None,
) -> list[str]:
    if claude_bin is None:
        from applypilot import config
        claude_bin = config.find_claude_binary()
    if not claude_bin:
        raise RuntimeError("could not find claude binary for Tier 2 patch")
    argv = [
        claude_bin,
        "--model", model,
        "-p",
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        "--output-format", "stream-json",
        "--verbose",
        "-",  # read stdin
    ]
    if mcp_config_path is not None:
        argv[2:2] = ["--mcp-config", str(mcp_config_path), "--strict-mcp-config"]
    if extra_argv:
        argv[2:2] = list(extra_argv)
    return argv


def _spawn_subprocess(
    *,
    argv: list[str],
    stdin_text: str,
    timeout_s: int,
    workdir: str | Path | None,
) -> tuple[int, str]:
    """Default spawn: subprocess.Popen with timeout."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(workdir) if workdir else None,
    )
    try:
        stdout, _ = proc.communicate(input=stdin_text, timeout=timeout_s)
        return proc.returncode, stdout or ""
    except subprocess.TimeoutExpired as e:
        proc.kill()
        partial = (e.output or "") if hasattr(e, "output") else ""
        try:
            partial, _ = proc.communicate(timeout=2)
        except Exception:
            pass
        raise _SpawnTimeout(partial or "")
