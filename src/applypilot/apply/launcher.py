"""Apply orchestration: acquire jobs, spawn Claude Code sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Claude Code for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import hashlib
import json
import logging
import os
import platform
import queue
import random
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.database import get_connection
from applypilot.identity import parse_ats_url
from applypilot.submission_ledger import SubmissionLedger
from applypilot.apply import prompt as prompt_mod
from applypilot.apply.browser_stream import (
    BrowserObservation,
    BrowserStateStream,
    summarize_observation,
)
from applypilot.apply.prefill import prefill_application
from applypilot.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome,
    reset_worker_dir, cleanup_on_exit, _kill_process_tree,
    BASE_CDP_PORT,
)
from applypilot.apply.visual_trace import VisualTraceRecorder
from applypilot.apply.dashboard import (
    init_worker, update_state, add_event, get_state,
    render_full, get_totals, wait_for_change,
)

logger = logging.getLogger(__name__)

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# In --no-live mode the Rich dashboard isn't rendered, so the terminal looks
# idle while Sonnet works. main() flips this on so run_job mirrors tool calls
# to stdout (one line per call) for live progress visibility.
_print_tool_calls = False

# Track active Claude Code processes for skip (Ctrl+C) handling
_claude_procs: dict[int, subprocess.Popen] = {}
_claude_lock = threading.Lock()

# Jobs claimed during the current process. This prevents one bad URL from
# being retried by another worker in the same run after a timeout/no-result.
# Sites are also tracked so sequential workers do one pass across companies
# before returning to the same company, while still draining thin queues.
_run_seen_urls: set[str] = set()
_run_seen_sites: set[str] = set()
_run_seen_lock = threading.Lock()

_allowed_tools_supported: bool | None = None

RESOURCE_MIN_FREE_MB = 1536
RESOURCE_MAX_CPU_PERCENT = 92
RESOURCE_WAIT_TIMEOUT_S = 60
CLAUDE_LIMIT_PATTERNS: tuple[str, ...] = (
    "you've hit your limit",
    "you have hit your limit",
    "usage limit reached",
    "rate limit",
)
RESULT_JSON_PREFIX = "APPLYPILOT_RESULT_JSON:"
CHECKPOINT_PAGE_REACHED = "page_reached"
CHECKPOINT_RESUME_UPLOADED = "resume_uploaded"
CHECKPOINT_SUBMIT_ATTEMPTED = "submit_attempted"
CHECKPOINT_VERIFICATION_COMPLETE = "verification_complete"
TRANSIENT_FAILURE_PREFIXES: tuple[str, ...] = ("transient_",)

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

def _make_mcp_config(cdp_port: int, dry_run: bool = False,
                     broker_file: str | None = None,
                     job_identity: str | None = None) -> dict:
    """Build MCP config dict for a specific CDP port.

    `dry_run` is forwarded to the applypilot_stream server so final submits
    are refused SERVER-SIDE — the prompt-only dry-run guard failed live
    (Twilio, 2026-06-12: the model passed allow_submit=true during a dry run
    and a real application was submitted).

    `broker_file`/`job_identity` add a SECONDARY submit gate to the separate
    stream-MCP process: it refuses final submits unless a broker ticket is
    open. The PRIMARY always-on containment is the CDP network route in the
    launcher process (BrowserStateStream), which holds the live broker.
    """
    stream_args = [
        "-m",
        "applypilot.apply.stream_mcp_server",
        "--cdp-port",
        str(cdp_port),
    ]
    if dry_run:
        stream_args.append("--dry-run")
    if broker_file:
        stream_args += ["--broker-file", str(broker_file)]
    if job_identity:
        stream_args += ["--job-identity", str(job_identity)]
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    "@playwright/mcp@latest",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            },
            "applypilot_stream": {
                "command": sys.executable,
                "args": stream_args,
            },
            "gmail": {
                "command": "npx",
                "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
            },
        }
    }


def _claude_supports_allowed_tools(claude_bin: str) -> bool:
    """Return True when the installed Claude CLI supports --allowedTools."""
    global _allowed_tools_supported
    if _allowed_tools_supported is not None:
        return _allowed_tools_supported

    try:
        proc = subprocess.run(
            [claude_bin, "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        help_text = f"{proc.stdout}\n{proc.stderr}"
        _allowed_tools_supported = "--allowedTools" in help_text or "--allowed-tools" in help_text
    except Exception:
        _allowed_tools_supported = False
    return _allowed_tools_supported


def _allowed_tools_arg(
    *,
    allow_snapshot: bool = True,
    allow_raw_browser: bool = True,
    allow_navigate: bool = True,
) -> str:
    """Restrict Claude to the browser MCP and read/send-only Gmail tools."""
    tools = [
        "mcp__applypilot_stream__stream_latest",
        "mcp__applypilot_stream__stream_wait_for_change",
        "mcp__applypilot_stream__stream_execute",
        "mcp__playwright__browser_tabs",
        "mcp__gmail__search_emails",
        "mcp__gmail__read_email",
        "mcp__gmail__send_email",
    ]
    if allow_navigate:
        tools.insert(3, "mcp__playwright__browser_navigate")
    if allow_raw_browser:
        tools[5:5] = [
            "mcp__playwright__browser_click",
            "mcp__playwright__browser_type",
            "mcp__playwright__browser_fill_form",
            "mcp__playwright__browser_file_upload",
            "mcp__playwright__browser_evaluate",
            "mcp__playwright__browser_press_key",
            "mcp__playwright__browser_hover",
            "mcp__playwright__browser_select_option",
            "mcp__playwright__browser_console_messages",
            "mcp__playwright__browser_network_requests",
        ]
    if allow_snapshot:
        tools.insert(4, "mcp__playwright__browser_snapshot")
        tools.insert(5, "mcp__playwright__browser_take_screenshot")
    return ",".join(tools)


def _disallowed_tools_arg(
    *,
    allow_snapshot: bool = True,
    allow_raw_browser: bool = True,
    allow_navigate: bool = True,
) -> str:
    """Deny non-apply tools and hard-block snapshots during strict stream pass."""
    tools = [
        "Task", "WebFetch", "WebSearch", "TodoWrite", "Read", "Write", "Edit", "MultiEdit",
        "NotebookRead", "NotebookEdit", "Bash", "PowerShell", "Glob", "Grep", "LS",
        "mcp__playwright__browser_run_code_unsafe",
        "mcp__playwright__browser_wait_for",
        "mcp__gmail__draft_email", "mcp__gmail__modify_email",
        "mcp__gmail__delete_email", "mcp__gmail__download_attachment",
        "mcp__gmail__batch_modify_emails", "mcp__gmail__batch_delete_emails",
        "mcp__gmail__create_label", "mcp__gmail__update_label",
        "mcp__gmail__delete_label", "mcp__gmail__get_or_create_label",
        "mcp__gmail__list_email_labels", "mcp__gmail__create_filter",
        "mcp__gmail__list_filters", "mcp__gmail__get_filter",
        "mcp__gmail__delete_filter",
    ]
    if not allow_snapshot:
        tools.append("mcp__playwright__browser_snapshot")
        tools.append("mcp__playwright__browser_take_screenshot")
    if not allow_navigate:
        tools.append("mcp__playwright__browser_navigate")
    if not allow_raw_browser:
        tools.extend([
            "mcp__playwright__browser_click",
            "mcp__playwright__browser_type",
            "mcp__playwright__browser_fill_form",
            "mcp__playwright__browser_file_upload",
            "mcp__playwright__browser_evaluate",
            "mcp__playwright__browser_press_key",
            "mcp__playwright__browser_hover",
            "mcp__playwright__browser_select_option",
            "mcp__playwright__browser_console_messages",
            "mcp__playwright__browser_network_requests",
        ])
    return ",".join(tools)


def _is_claude_usage_limit(text: str) -> bool:
    """Detect Claude CLI account/usage-limit responses in plain or JSON text."""
    lower = text.lower()
    return any(pattern in lower for pattern in CLAUDE_LIMIT_PATTERNS)


def _needs_snapshot_fallback(output: str) -> bool:
    """Detect a strict stream pass blocked by unavailable fallback browser tools."""
    lower = (output or "").lower()
    blocked_tool_mentioned = any(
        tool in lower
        for tool in (
            "browser_snapshot",
            "browser_take_screenshot",
            "browser_click",
            "browser_type",
            "browser_fill_form",
            "browser_file_upload",
            "browser_evaluate",
            "browser_press_key",
            "browser_select_option",
        )
    )
    if not blocked_tool_mentioned:
        return False
    return any(
        marker in lower
        for marker in (
            "not available",
            "unavailable",
            "not allowed",
            "permission",
            "tool",
            "stream_snapshot_needed",
        )
    )


def _canonicalize_url(url: str) -> str:
    """Normalize URLs for idempotency hashing and duplicate checks."""
    if not url:
        return ""
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    query = ""
    if parsed.query:
        # Keep stable ATS identifiers that materially identify the posting.
        keep = []
        for part in parsed.query.split("&"):
            key = part.split("=", 1)[0].lower()
            if key in {"gh_jid", "gh_src", "job", "jobid", "id", "req_id"}:
                keep.append(part)
        query = "&".join(sorted(keep))
    normalized = parsed._replace(scheme=parsed.scheme.lower(), netloc=host, path=path, query=query, fragment="")
    return urlunparse(normalized)


_INVALID_URL_TEXT = {"", "none", "nan", "nat", "null"}


def _valid_http_url(value) -> str | None:
    """Return a usable HTTP(S) URL, ignoring common stringified nulls."""
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _INVALID_URL_TEXT:
        return None
    if not text.lower().startswith(("http://", "https://")):
        return None
    return text


def _effective_apply_url(job: dict) -> str | None:
    """Prefer a real direct apply URL, otherwise fall back to the listing URL."""
    return _valid_http_url(job.get("application_url")) or _valid_http_url(job.get("url"))


def _job_with_effective_apply_url(row) -> dict:
    """Convert a DB row to a job dict with application_url normalized."""
    job = dict(row)
    job["application_url"] = _effective_apply_url(job)
    return job


def _compute_idempotency_key(job: dict, profile: dict | None = None) -> str:
    """Build a stable idempotency key for this candidate+job submission intent."""
    apply_url = _effective_apply_url(job) or ""
    canonical = _canonicalize_url(apply_url)
    candidate = ""
    if profile:
        personal = profile.get("personal", {})
        candidate = (personal.get("email") or "").strip().lower()
    payload = {
        "url": canonical,
        "site": (job.get("site") or "").strip().lower(),
        "title": (job.get("title") or "").strip().lower(),
        "resume": (job.get("tailored_resume_path") or "").strip(),
        "candidate": candidate,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return digest


_REMOTE_LOCATION_RE = re.compile(
    r"\b(remote|work from home|work-from-home|wfh|distributed)\b",
    re.I,
)
_ONSITE_LOCATION_RE = re.compile(
    r"\b(on[- ]?site|hybrid|in[- ]office|office-based|based in|located in|relocat(?:e|ion))\b",
    re.I,
)


def _preapply_location_reject(job: dict, profile: dict, search_config: dict | None = None) -> str | None:
    """Return a rejection reason for clearly non-local onsite/hybrid roles.

    Conservative by design: remote wording wins, local accepted markers win,
    and empty/uncertain locations continue to the browser.
    """
    personal = profile.get("personal", {}) if isinstance(profile, dict) else {}
    if search_config is None:
        # Only fall back to disk config when the caller passed nothing.
        # An explicit {} means "no accept patterns" — previously `{}` was
        # falsy and silently loaded defaults whose markers neutered the gate.
        search_config = config.load_search_config() or {}
    location_cfg = search_config.get("location", {}) if isinstance(search_config, dict) else {}
    accepted = [str(personal.get("city") or "").strip().lower()]
    accepted.extend(str(x).strip().lower() for x in location_cfg.get("accept_patterns", []) or [])
    accepted.extend(["san francisco bay area", "bay area"])
    accepted = [x for x in dict.fromkeys(accepted) if x]

    title = str(job.get("title") or "")
    loc = str(job.get("location") or "")
    desc = str(job.get("full_description") or "")
    apply_url = _effective_apply_url(job) or ""
    combined = " ".join([title, loc, apply_url, desc[:5000]]).lower()

    if not (loc.strip() or apply_url.strip()):
        return None
    if _REMOTE_LOCATION_RE.search(combined) and not re.search(r"\b(up to|for up to)\s+\d+\s+weeks\b", combined):
        return None
    # Word-boundary match so short markers ("CA", "US", "SF") can't fire on
    # substrings of ordinary words ("appliCAtions", "joins US", ...), which
    # made the accept check pass for nearly every job description.
    if any(re.search(rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])", combined) for marker in accepted):
        return None

    concrete_city_state = bool(re.search(r"\b[A-Z][a-zA-Z .'-]+,\s*(?:[A-Z]{2}|[A-Za-z .'-]+)\b", loc))
    if _ONSITE_LOCATION_RE.search(combined) or concrete_city_state:
        return "not_eligible_location"
    return None


_WORKDAY_TENANT_RE = re.compile(r"https?://([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com", re.I)


def _workday_account_reject(job: dict, search_config: dict | None = None) -> str | None:
    """Park Workday jobs whose tenant has no registered account.

    Workday requires a pre-registered, email-verified account per tenant
    (manual step). Without one the LLM burns 350-430s + cost discovering a
    login wall (the 4 blocker_login_issue rows of 2026-05-20..22 were all
    exactly this). Tenants with working accounts are listed under
    `workday_accounts` in searches.yaml; unknown tenants are parked
    pre-spawn. No list configured → gate is a no-op (backward compatible).
    """
    url = _effective_apply_url(job) or ""
    m = _WORKDAY_TENANT_RE.match(url.strip().lower())
    if not m:
        return None
    if search_config is None:
        search_config = config.load_search_config() or {}
    raw = search_config.get("workday_accounts") if isinstance(search_config, dict) else None
    if not raw:
        return None
    tenants = {str(t).strip().lower() for t in raw if str(t).strip()}
    if m.group(1) in tenants:
        return None
    return "workday_account_required"


def _preapply_reject_reason(job: dict, profile: dict, search_config: dict | None = None) -> str | None:
    """All pre-spawn rejection checks, cheapest first. None = proceed."""
    return (_workday_account_reject(job, search_config)
            or _preapply_location_reject(job, profile, search_config))


def _read_job_row(url: str) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        """
        SELECT url, apply_status, applied_at, idempotency_key, checkpoint_json,
               submit_attempt_count, apply_attempts
        FROM jobs WHERE url = ?
        """,
        (url,),
    ).fetchone()
    return dict(row) if row else None


def _write_job_runtime_metadata(
    url: str,
    *,
    idempotency_key: str | None = None,
    checkpoint: dict | None = None,
    increment_submit_attempt: bool = False,
    apply_result_json: dict | None = None,
    verification_evidence: dict | None = None,
    verification_confidence: float | None = None,
    last_failure_class: str | None = None,
    skill_used: str | None = None,
    replay_duration_ms: int | None = None,
    patch_duration_ms: int | None = None,
) -> None:
    """Persist robustness metadata columns without changing status semantics."""
    conn = get_connection()
    sets: list[str] = []
    params: list = []

    if idempotency_key is not None:
        sets.append("idempotency_key = ?")
        params.append(idempotency_key)
    if checkpoint is not None:
        sets.append("checkpoint_json = ?")
        params.append(json.dumps(checkpoint, ensure_ascii=False))
    if increment_submit_attempt:
        sets.append("submit_attempt_count = COALESCE(submit_attempt_count, 0) + 1")
    if apply_result_json is not None:
        sets.append("apply_result_json = ?")
        params.append(json.dumps(apply_result_json, ensure_ascii=False))
    if verification_evidence is not None:
        sets.append("verification_evidence_json = ?")
        params.append(json.dumps(verification_evidence, ensure_ascii=False))
    if verification_confidence is not None:
        sets.append("verification_confidence = ?")
        params.append(float(verification_confidence))
    if last_failure_class is not None:
        sets.append("last_failure_class = ?")
        params.append(last_failure_class)
    if skill_used is not None:
        sets.append("skill_used = ?")
        params.append(skill_used)
    if replay_duration_ms is not None:
        sets.append("replay_duration_ms = ?")
        params.append(int(replay_duration_ms))
    if patch_duration_ms is not None:
        sets.append("patch_duration_ms = ?")
        params.append(int(patch_duration_ms))

    if not sets:
        return

    params.append(url)
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE url = ?", params)
    conn.commit()


def _checkpoint(stage: str, **extra) -> dict:
    cp = {
        "stage": stage,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        cp.update(extra)
    return cp


def _idempotency_already_applied(idempotency_key: str, current_url: str) -> bool:
    conn = get_connection()
    row = conn.execute(
        """
        SELECT 1
        FROM jobs
        WHERE idempotency_key = ?
          AND applied_at IS NOT NULL
          AND url != ?
        LIMIT 1
        """,
        (idempotency_key, current_url),
    ).fetchone()
    return bool(row)


def _extract_structured_result(output: str) -> dict | None:
    """Extract the final APPLYPILOT_RESULT_JSON payload if present."""
    for raw in reversed(output.splitlines()):
        line = raw.strip().strip("`")
        if RESULT_JSON_PREFIX not in line:
            continue
        payload = line.split(RESULT_JSON_PREFIX, 1)[1].strip()
        try:
            obj = json.loads(payload)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _sanitize_reason(reason: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", (reason or "unknown").strip().lower()).strip("_")
    return cleaned or "unknown"


def _result_from_structured(payload: dict) -> str | None:
    status = str(payload.get("status", "")).strip().lower()
    if status in {"applied", "expired", "captcha", "login_issue"}:
        return status
    if status == "failed":
        return f"failed:{_sanitize_reason(str(payload.get('reason', 'unknown')))}"
    return None


def _classify_failure_class(result: str, reason: str | None = None, hint: str | None = None) -> str:
    """Map status/reason to normalized failure classes for telemetry and policy."""
    if hint and re.fullmatch(r"(transient|validation|verification|blocker|policy)_[a-z0-9_.-]+", hint):
        return hint

    r = (reason or result or "").lower()
    if r.startswith("policy_"):
        return r
    if r in {
        "unsafe_permissions", "unsafe_verification", "not_a_job_application",
        "sso_required", "not_eligible_location", "not_eligible_salary",
        "not_eligible_work_auth",
    }:
        return f"policy_{r}"
    if r in {"captcha", "email_verification_required", "login_issue", "workday_account_required"}:
        return f"blocker_{r}"
    if r in {"candidate_profile_only", "apply_button_not_found"}:
        return f"blocker_{r}"
    if r in {"unverified_submission", "possible_duplicate_guard"}:
        return f"verification_{r}"
    if r in {"form_validation_error", "resume_upload_failed", "phone_country_validation"}:
        return f"validation_{r}"
    if r in {
        "timeout", "no_result_line", "browser_unavailable", "interrupted",
        "rate_limited", "stuck", "page_error", "stream_snapshot_needed",
    }:
        return f"transient_{r}"
    if result.startswith("needs_review:"):
        return f"verification_{r or 'needs_review'}"
    if result.startswith("failed:"):
        return f"validation_{r or 'failed'}"
    return "transient_unknown"


_DRY_RUN_SUBMIT_BLOCKER_JS = """
(() => {
  if (window.__applypilotDryRunBlocker) return;
  window.__applypilotDryRunBlocker = true;
  const isSubmitText = (t) => /submit (my )?application|^\\s*submit\\s*$/i.test((t || '').trim());
  document.addEventListener('submit', (e) => {
    e.preventDefault();
    e.stopImmediatePropagation();
  }, true);
  document.addEventListener('click', (e) => {
    const el = e.target && e.target.closest
      ? e.target.closest('button, input[type=submit], [role=button]') : null;
    if (!el) return;
    const label = el.innerText || el.value || el.getAttribute('aria-label') || '';
    if (isSubmitText(label)) {
      e.preventDefault();
      e.stopImmediatePropagation();
    }
  }, true);
  try {
    HTMLFormElement.prototype.submit = function () {};
    if (HTMLFormElement.prototype.requestSubmit) {
      HTMLFormElement.prototype.requestSubmit = function () {};
    }
  } catch (err) { /* best-effort */ }
})()
"""


def _inject_dry_run_submit_blocker(cdp_port: int) -> bool:
    """Best-effort DOM-level submit blocker for dry runs (defense in depth).

    Iter-17 incident: a dry-run submitted a real application because the only
    guard was prose in the prompt. The stream MCP server now refuses submits
    server-side; this blocker additionally neuters form submission inside the
    page itself, covering raw Playwright-MCP clicks. Installed as an init
    script (survives navigation) plus an immediate evaluate on open pages.
    Fail-OPEN: any error is logged and the dry run proceeds (the server-side
    guard remains).
    """
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
            for ctx in browser.contexts:
                try:
                    ctx.add_init_script(_DRY_RUN_SUBMIT_BLOCKER_JS)
                except Exception:
                    pass
                for page in ctx.pages:
                    try:
                        page.evaluate(_DRY_RUN_SUBMIT_BLOCKER_JS)
                    except Exception:
                        pass
            browser.close()  # CDP-connected: releases the socket only
        return True
    except Exception as e:  # noqa: BLE001 — never break the dry run
        logger.debug("dry-run submit blocker injection failed (non-fatal): %s", e)
        return False


def _classify_no_result(stop_requested: bool, default_class: str) -> tuple[str, str]:
    """Classify an agent run that ended without a RESULT line.

    Transcript evidence (Adapt/Fanatics 2026-05-21): runs killed by operator
    Ctrl+C end with cmd.exe's "Terminate batch job (Y/N)?" and no RESULT line.
    Those were logged as agent failures (transient_no_result_line /
    verification_missing_structured_result), polluting the removable-failure
    counts. When a stop was requested, the truthful class is "interrupted".
    """
    if stop_requested:
        return "transient_interrupted", "needs_review:interrupted"
    return default_class, "needs_review:no_result_line"


def _artifact_stem(job: dict, worker_id: int, suffix: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    site = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(job.get("site", "unknown")))[:30]
    return f"{ts}_w{worker_id}_{site}_{suffix}"


def _write_partial_transcript(job: dict, worker_id: int, text: str, suffix: str) -> Path:
    path = config.LOG_DIR / f"claude_{_artifact_stem(job, worker_id, suffix)}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _capture_browser_artifact(cdp_port: int, job: dict, worker_id: int, suffix: str) -> Path | None:
    """Best-effort screenshot from the active worker browser for fast triage."""
    path = config.LOG_DIR / f"browser_{_artifact_stem(job, worker_id, suffix)}.png"
    pw = None
    browser = None
    try:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{cdp_port}",
            timeout=3000,
        )
        pages = [p for ctx in browser.contexts for p in ctx.pages]
        page = pages[-1] if pages else None
        if page is None:
            return None
        page.screenshot(path=str(path), full_page=True, timeout=5000)
        return path
    except Exception as e:
        logger.debug("Failed to capture browser artifact: %s", e)
        return None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass


def _latest_stream_observation(browser_stream=None, *, refresh: bool = False) -> BrowserObservation | None:
    if browser_stream is None:
        return None
    try:
        if refresh and hasattr(browser_stream, "refresh_now"):
            obs = browser_stream.refresh_now(timeout_ms=3000)
        else:
            obs = browser_stream.latest()
    except Exception:
        logger.debug("browser stream latest failed", exc_info=True)
        return None
    if isinstance(obs, BrowserObservation) and not obs.error:
        return obs
    return None


def _is_voluntary_eeo_label(label: str) -> bool:
    low = re.sub(r"\s+", " ", (label or "").strip().lower())
    if not re.search(r"gender|race|ethnicity|hispanic|latino|veteran|disability|sexual orientation|transgender", low):
        return False
    return bool(re.search(r"voluntary|self-identif|equal employment|eeo|demographic|decline|prefer not|wish to answer", low))


def _greenhouse_ready_from_observation(obs: BrowserObservation) -> dict:
    missing: list[str] = []
    for label in obs.required_missing:
        text = str(label).replace("*", "").strip()
        low = text.lower()
        if not text:
            continue
        if "resume" in low or low == "cv" or "resume/cv" in low:
            continue
        if low == "country" or low.startswith("country "):
            continue
        if _is_voluntary_eeo_label(text):
            continue
        if text not in missing:
            missing.append(text)
    if not obs.resume_present and "Resume/CV" not in missing:
        missing.append("Resume/CV")
    submit_enabled = obs.submit_enabled
    return {
        "ready": bool(submit_enabled) and not missing,
        "missing": missing,
        "submit_enabled": bool(submit_enabled),
        "error": None,
        "source": "browser_stream",
    }


def _validation_from_observation(obs: BrowserObservation) -> dict:
    country_text = ""
    for control in obs.controls:
        hay = " ".join([control.label, control.selector, control.value]).lower()
        if "country" in hay:
            country_text = control.value.strip().lower()
            break
    phone_country_ok = not country_text or bool(re.search(r"(united states|\+1|\bus\b)", country_text, re.I))
    result = {
        "valid": False,
        "missing": list(obs.required_missing),
        "validation_errors": list(obs.validation_errors),
        "resume_present": bool(obs.resume_present),
        "phone_country_ok": phone_country_ok,
        "submit_enabled": bool(obs.submit_enabled),
        "url": obs.url,
        "error": None,
        "source": "browser_stream",
    }
    result["valid"] = (
        not result["missing"]
        and not result["validation_errors"]
        and result["resume_present"]
        and result["phone_country_ok"]
    )
    return result


def _check_greenhouse_submit_ready(cdp_port: int, browser_stream=None) -> dict:
    """Inspect the active Greenhouse form and report whether required fields are filled."""
    stream_obs = _latest_stream_observation(browser_stream, refresh=True)
    if stream_obs is not None:
        return _greenhouse_ready_from_observation(stream_obs)

    pw = None
    browser = None
    result = {"ready": False, "missing": [], "submit_enabled": False, "error": None}
    try:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{cdp_port}",
            timeout=3000,
        )
        pages = [p for ctx in browser.contexts for p in ctx.pages]
        page = pages[-1] if pages else None
        if page is None:
            result["error"] = "no_page"
            return result

        state = page.evaluate(
            """() => {
              const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
              const lower = s => norm(s).toLowerCase();
              const visible = el => {
                const box = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
              };
              const isVoluntaryEeo = label => {
                const labelText = lower(label.innerText || label.textContent);
                const eeoLabel = /gender|race|ethnicity|hispanic|latino|veteran|disability|sexual orientation|transgender/.test(labelText);
                if (!eeoLabel) return false;
                let node = label;
                for (let i = 0; node && i < 6; i += 1, node = node.parentElement) {
                  const text = lower(node.textContent);
                  if (/voluntary|self-identif|equal employment|eeo|demographic/.test(text)) return true;
                  if (/decline|prefer not|wish to answer|self-identif/.test(text)) {
                    return true;
                  }
                }
                return false;
              };
              const valueOf = el => {
                if (!el) return '';
                if (el.matches && el.matches('input, textarea, select')) {
                  if ((el.type || '').toLowerCase() === 'file') return el.files && el.files.length ? 'file' : '';
                  if (el.tagName === 'SELECT') return el.selectedOptions?.[0]?.textContent || el.value || '';
                  if (el.value) return el.value;
                  if (el.getAttribute('role') === 'combobox' || el.className?.includes('select__input')) {
                    const selectContainer = el.closest('.select__container, .select-shell, [class*="select__control"]');
                    const selected = selectContainer?.querySelector(
                      '[class*="singleValue"], [class*="multiValue"], [class*="selected"], [aria-selected="true"]'
                    );
                    return selected && norm(selected.textContent) ? norm(selected.textContent) : '';
                  }
                  return '';
                }
                const container = el.closest?.('div, fieldset, li') || el.parentElement || el;
                const control = container.querySelector('input:not([type=hidden]), textarea, select');
                if (control && control !== el) {
                  const controlValue = valueOf(control);
                  if (controlValue) return controlValue;
                }
                return norm(container.textContent);
              };
              const labels = Array.from(document.querySelectorAll('label')).filter(visible);
              const controlNear = label => {
                const forId = label.getAttribute('for');
                const byFor = forId ? document.getElementById(forId) : null;
                if (byFor) return byFor;
                const local = label.querySelector('input:not([type=hidden]), textarea, select');
                if (local) return local;
                let node = label.parentElement;
                for (let i = 0; node && i < 7; i += 1, node = node.parentElement) {
                  const control = node.querySelector?.('input:not([type=hidden]), textarea, select, [role="combobox"]');
                  if (control) return control;
                }
                return null;
              };
              const requiredLabels = labels.filter(label => {
                const text = norm(label.innerText || label.textContent);
                if (!text.includes('*')) return false;
                const t = lower(text);
                if (t.includes('resume') || t.includes('cv')) return false;
                if (t === 'country *' || t.startsWith('country ')) return false;
                if (isVoluntaryEeo(label)) return false;
                return true;
              });
              const missing = [];
              for (const label of requiredLabels) {
                const text = norm(label.innerText || label.textContent).replace(/\\*/g, '').trim();
                const target = controlNear(label);
                const val = target ? valueOf(target) : '';
                if (!norm(val) || /^select\\.?\\.?.*$/i.test(norm(val))) {
                  missing.push(text || 'required field');
                }
              }
              const fileInputs = Array.from(document.querySelectorAll('input[type=file]'));
              const pageText = `${document.body.innerText || ''} ${document.body.textContent || ''}`;
              const resumeUploaded = /resume\\.pdf|\\.pdf/i.test(pageText) ||
                fileInputs.some(el => el.files && el.files.length);
              if (!resumeUploaded) missing.push('Resume/CV');
              const submit = Array.from(document.querySelectorAll('button, input[type=submit]'))
                .find(el => /submit application|submit|apply/i.test(el.innerText || el.value || ''));
              const submitEnabled = !!submit && !submit.disabled && submit.getAttribute('aria-disabled') !== 'true';
              return {missing, submitEnabled};
            }"""
        )
        result["missing"] = state.get("missing", [])
        result["submit_enabled"] = bool(state.get("submitEnabled"))
        result["ready"] = result["submit_enabled"] and not result["missing"]
        return result
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass


def _validate_required_fields(cdp_port: int, browser_stream=None) -> dict:
    """Cross-ATS required field validator for pre/post submit checks."""
    stream_obs = _latest_stream_observation(browser_stream, refresh=True)
    if stream_obs is not None:
        return _validation_from_observation(stream_obs)

    pw = None
    browser = None
    result = {
        "valid": False,
        "missing": [],
        "validation_errors": [],
        "resume_present": False,
        "phone_country_ok": True,
        "submit_enabled": False,
        "url": "",
        "error": None,
    }
    try:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{cdp_port}",
            timeout=3000,
        )
        pages = [p for ctx in browser.contexts for p in ctx.pages]
        page = pages[-1] if pages else None
        if page is None:
            result["error"] = "no_page"
            return result

        state = page.evaluate(
            """() => {
              const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
              const visible = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const cs = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
              };

              const missing = [];
              const labels = Array.from(document.querySelectorAll('label')).filter(visible);
              for (const label of labels) {
                const text = norm(label.innerText || label.textContent);
                if (!text.includes('*')) continue;
                if (/resume|cv/i.test(text)) continue;
                const forId = label.getAttribute('for');
                let control = forId ? document.getElementById(forId) : null;
                if (!control) control = label.querySelector('input, textarea, select');
                if (!control) {
                  let node = label.parentElement;
                  for (let i = 0; node && i < 6; i += 1, node = node.parentElement) {
                    control = node.querySelector('input:not([type=hidden]), textarea, select, [role="combobox"]');
                    if (control) break;
                  }
                }
                const value = control ? (control.value || control.textContent || '').toString().trim() : '';
                if (!value || /^select\\.?$/i.test(value)) {
                  missing.push(text.replace('*', '').trim());
                }
              }

              const validationErrors = Array.from(document.querySelectorAll(
                '[aria-invalid="true"], .error, .errors, .field-error, [data-testid*="error" i], [role="alert"]'
              ))
                .filter(visible)
                .map(el => norm(el.innerText || el.textContent))
                .filter(Boolean);

              const resumePresent = /\\.pdf\\b|resume|curriculum vitae/i.test(document.body?.innerText || '') ||
                Array.from(document.querySelectorAll('input[type=file]')).some(el => el.files && el.files.length > 0);

              const countryInput = Array.from(document.querySelectorAll(
                'input[name*="country" i], input[id*="country" i], [aria-label*="country" i]'
              )).find(visible);
              const countryText = countryInput ? norm(countryInput.value || countryInput.textContent).toLowerCase() : '';
              const phoneCountryOk = !countryText || /(united states|\\+1|\\bus\\b)/i.test(countryText);

              const submit = Array.from(document.querySelectorAll('button, input[type=submit]'))
                .find(el => /submit application|submit|apply/i.test(el.innerText || el.value || ''));
              const submitEnabled = !!submit && !submit.disabled && submit.getAttribute('aria-disabled') !== 'true';

              return {
                missing,
                validationErrors,
                resumePresent,
                phoneCountryOk,
                submitEnabled,
                url: window.location.href,
              };
            }"""
        )
        result["missing"] = state.get("missing", []) or []
        result["validation_errors"] = state.get("validationErrors", []) or []
        result["resume_present"] = bool(state.get("resumePresent"))
        result["phone_country_ok"] = bool(state.get("phoneCountryOk", True))
        result["submit_enabled"] = bool(state.get("submitEnabled"))
        result["url"] = state.get("url", "")
        result["valid"] = (
            not result["missing"]
            and not result["validation_errors"]
            and result["resume_present"]
            and result["phone_country_ok"]
        )
        return result
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass


def _compute_verification_verdict(
    *,
    has_confirmation: bool,
    url_changed: bool,
    submit_gone: bool,
    submit_disabled: bool,
    no_validation_errors: bool,
    required_ok: bool,
    verify_threshold: float,
) -> tuple[float, bool]:
    """Pure decision core of the submission verifier. Extracted so it can be
    unit-tested without a live Chrome (it had 2 bugs in iter 11-12).

    Returns (confidence, verified).

    Weights: a persistent confirmation page is the strongest signal (0.55);
    the negative signals (url changed, submit button gone/disabled, no field
    errors, required fields valid) corroborate.

    The redirect-after-submit case (Instacart, Stripe, Lever, some Workday):
    the success page auto-redirects to the careers homepage in ~2-3s, so by
    the time the verifier reads the DOM `has_confirmation` is False. The
    combination url_changed + submit_gone + no_validation_errors is treated
    as strong evidence on its own (the agent already emitted RESULT:APPLIED
    before this runs, so it's corroborating, not the sole signal). We do NOT
    also require `required_ok` for this path — a redirected page has no form,
    so required-field validation necessarily fails there.
    """
    confidence = 0.0
    if has_confirmation:
        confidence += 0.55
    if url_changed:
        confidence += 0.15
    if submit_gone:
        confidence += 0.1
    elif submit_disabled:
        confidence += 0.05
    if no_validation_errors:
        confidence += 0.1
    if required_ok:
        confidence += 0.1
    confidence = max(0.0, min(1.0, confidence))

    strong_negative_signal_success = (
        url_changed and submit_gone and no_validation_errors
    )
    if strong_negative_signal_success:
        confidence = max(confidence, verify_threshold)

    verified = confidence >= verify_threshold and (
        has_confirmation or strong_negative_signal_success
    )
    return confidence, verified


# Per-document success/failure scan. Run in EVERY frame (Playwright reaches
# cross-origin children), then aggregated by _scan_frames_for_success.
_FRAME_SCAN_JS = r"""
() => {
  const text = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').trim();
  const lower = text.toLowerCase();
  const successPatterns = [
    'application received','application submitted','application complete',
    'thank you for applying','thanks for applying','thank you for your application',
    'thank you for your interest','successfully submitted','we received your application',
    'we have received your application','we will review your application',
    'our team is reviewing','your application has been submitted',
    'your application has been received','application has been sent','next steps',
    'we will be in touch','we got it from here',"we've got it from here",'we got it',
    'sign in to mygreenhouse',
  ];
  const hits = successPatterns.filter(p => lower.includes(p));
  const submit = Array.from(document.querySelectorAll('button, input[type=submit]'))
    .find(el => /submit application|submit my application/i.test(el.innerText || el.value || ''));
  const submitVisible = !!submit;
  const submitEnabled = !!submit && !submit.disabled && submit.getAttribute('aria-disabled') !== 'true';
  const validationErrors = Array.from(document.querySelectorAll(
    '[aria-invalid="true"], .error, .errors, .field-error, [data-testid*="error" i], [role="alert"]'
  )).map(el => (el.innerText || el.textContent || '').trim()).filter(Boolean);
  return {hits, submitVisible, submitEnabled, validationErrors, evidence: text.slice(0, 1200)};
}
"""


def _scan_frames_for_success(page) -> dict:
    """Aggregate the success/failure scan across the top page AND every
    child frame (Greenhouse/Lever embed the form cross-origin). Union the
    confirmation hits; submit/validation present if seen in ANY frame;
    evidence prefers a frame that has hits.

    Best-effort per frame — a detached/navigating frame is skipped, never
    fatal. Top-frame URL is always the reported url.
    """
    agg_hits: list[str] = []
    submit_visible = False
    submit_enabled = False
    validation_errors: list[str] = []
    evidence = ""
    evidence_from_hit_frame = False

    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    # Cap to keep it fast; real ATS pages have <~10 frames.
    for fr in frames[:12]:
        # No URL-based skipping: set_content pages are about:blank and
        # srcdoc embeds are about:srcdoc — filtering those drops the very
        # frames we need. A genuinely empty frame just returns empty hits
        # (harmless). Best-effort: a detached/navigating frame is skipped.
        try:
            st = fr.evaluate(_FRAME_SCAN_JS)
        except Exception:
            continue
        if not isinstance(st, dict):
            continue
        if st.get("hits"):
            for h in st["hits"]:
                if h not in agg_hits:
                    agg_hits.append(h)
        submit_visible = submit_visible or bool(st.get("submitVisible"))
        submit_enabled = submit_enabled or bool(st.get("submitEnabled"))
        if st.get("validationErrors"):
            validation_errors.extend(st["validationErrors"])
        ev = st.get("evidence") or ""
        # Prefer evidence from a frame that actually has a confirmation hit.
        if ev and (not evidence or (st.get("hits") and not evidence_from_hit_frame)):
            evidence = ev
            evidence_from_hit_frame = bool(st.get("hits"))

    try:
        top_url = page.url
    except Exception:
        top_url = ""
    return {
        "url": top_url,
        "hits": agg_hits,
        "submitVisible": submit_visible,
        "submitEnabled": submit_enabled,
        "validationErrors": validation_errors,
        "evidence": evidence,
    }


def _greenhouse_adapter_pass(cdp_port: int, profile: dict,
                             resume_pdf_path: str,
                             allow_submit: bool = False,
                             broker=None, identity_id: str | None = None) -> dict | None:
    """Connect to the live Chrome and run the deterministic Greenhouse
    adapter (zero Claude-Code LLM, self-healing locators, answer-cache for
    free-text). submit="auto" when allow_submit: the adapter submits
    deterministically ONLY if the form is fully satisfied (no unresolved
    custom Qs) — that path skips the LLM entirely (no timeout, ~$0). If
    anything custom remains, submit stays with the LLM. Fail-OPEN: any
    error → None (caller falls back to prefill+LLM). Returns
    {fields_filled, unresolved, submitted}."""
    pw = browser = None
    try:
        from playwright.sync_api import sync_playwright
        from applypilot.apply.adapters.greenhouse import fill_greenhouse
        from applypilot.apply.answer_cache import AnswerCache
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{cdp_port}", timeout=3000)
        pages = [p for ctx in browser.contexts for p in ctx.pages]
        page = pages[-1] if pages else None
        if page is None:
            return None
        ac = AnswerCache(profile, bank_path=config.APP_DIR / "answer_bank.json")
        # Gate deterministic submit on an open broker ticket. Even with
        # allow_submit True, do not let the adapter drive fill_greenhouse's
        # submit path unless a ticket is open for this identity — the network
        # route consumes it on the actual POST. broker=None keeps prior behavior.
        do_submit = allow_submit
        if broker is not None and identity_id and not broker.ticket_open(identity_id):
            do_submit = False
        res = fill_greenhouse(
            page, profile, resume_pdf_path,
            submit=("auto" if do_submit else False), answer_cache=ac)
        return {"fields_filled": res.fields_filled,
                "unresolved": res.unresolved,
                "submitted": bool(res.submitted)}
    except Exception as e:
        logger.debug("_greenhouse_adapter_pass failed-open: %s", e)
        return None
    finally:
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if pw is not None:
                pw.stop()
        except Exception:
            pass


def _verify_submission_success(cdp_port: int, verify_threshold: float = 0.75,
                               previous_url: str | None = None,
                               browser_stream=None) -> dict:
    """Weighted multi-signal submission verifier with confidence score."""
    pw = None
    browser = None
    result = {
        "verified": False,
        "confidence": 0.0,
        "signals": {},
        "evidence": "",
        "required_validation": None,
        "error": None,
    }
    try:
        required_state = _validate_required_fields(cdp_port, browser_stream=browser_stream)
        result["required_validation"] = required_state

        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{cdp_port}",
            timeout=3000,
        )
        pages = [p for ctx in browser.contexts for p in ctx.pages]
        page = pages[-1] if pages else None
        if page is None:
            result["error"] = "no_page"
            return result

        # Verifier v3 (iter-13): scan EVERY frame, not just the top window.
        # Greenhouse/Lever embed the application form in a cross-origin
        # iframe (e.g. boards.greenhouse.io/embed inside roblox.com careers).
        # After submit the "application received" message renders INSIDE that
        # iframe. Top-window document.body.innerText misses it (and JS can't
        # read a cross-origin iframe), so the verifier saw the careers
        # landing page → conf 0.2 → real submissions mis-flagged
        # needs_review. Playwright's frame API reaches cross-origin frames,
        # so aggregate the success scan across all of them.
        state = _scan_frames_for_success(page)

        current_url = state.get("url", "")
        previous_canonical = _canonicalize_url(previous_url or "")
        current_canonical = _canonicalize_url(current_url)

        has_confirmation = bool(state.get("hits"))
        url_changed = bool(previous_canonical and current_canonical and previous_canonical != current_canonical)
        submit_gone = not bool(state.get("submitVisible"))
        submit_disabled = bool(state.get("submitVisible")) and not bool(state.get("submitEnabled"))
        no_validation_errors = not bool(state.get("validationErrors"))
        required_ok = bool(required_state.get("valid")) if isinstance(required_state, dict) else False

        confidence, verified = _compute_verification_verdict(
            has_confirmation=has_confirmation,
            url_changed=url_changed,
            submit_gone=submit_gone,
            submit_disabled=submit_disabled,
            no_validation_errors=no_validation_errors,
            required_ok=required_ok,
            verify_threshold=verify_threshold,
        )

        result["confidence"] = confidence
        result["signals"] = {
            "has_confirmation_text": has_confirmation,
            "confirmation_hits": state.get("hits", []),
            "url_changed": url_changed,
            "submit_visible": bool(state.get("submitVisible")),
            "submit_enabled": bool(state.get("submitEnabled")),
            "validation_error_count": len(state.get("validationErrors", [])),
            "required_valid": required_ok,
        }
        result["verified"] = verified
        result["evidence"] = state.get("evidence", "")
        return result
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass


def _resolve_ledger_intent(ledger, identity_id, verification, job_meta) -> None:
    """Transition an open INTENT to CONFIRMED (verified) or FAILED (unverified).

    Called at every post-submit verify site. Idempotent by ledger design: the
    UPDATE only touches rows still in state='intent', so a second call is a
    no-op. No-op when there's no ledger/identity (legacy/test callers). Any
    submit path that returns WITHOUT reaching a verify site leaves the INTENT
    open on purpose — a dangling INTENT that the next run blocks on rather than
    blindly re-applying (the double-submit guard)."""
    if ledger is None or not identity_id:
        return
    try:
        if verification.get("verified"):
            ledger.confirm(identity_id, confidence=float(verification.get("confidence") or 0.0))
        else:
            reason = (job_meta.get("failure_class") if isinstance(job_meta, dict) else None) or "unverified"
            ledger.fail(identity_id, reason=reason)
    except Exception as e:  # never let ledger bookkeeping break the apply path
        logger.warning("submission ledger resolve failed (intent left open): %s", e)


def _extract_result_code(output: str) -> str | None:
    """Return the final anchored RESULT code, avoiding quoted/instructional mentions."""
    for line in reversed(output.splitlines()):
        # Strip surrounding whitespace + leading/trailing markdown bold/backticks,
        # then drop any markdown bold markers that survived inside the line
        # (Sonnet often emits `**RESULT:APPLIED** (dry run...)` which leaves a
        # mid-string `**` after .strip()).
        clean = line.strip().strip("`* ").replace("**", "").replace("__", "")
        match = re.fullmatch(r"RESULT:(APPLIED|EXPIRED|CAPTCHA|LOGIN_ISSUE)(?:\s*\([^)]*\))?", clean)
        if match:
            return match.group(1).lower()
        match = re.fullmatch(r"RESULT:FAILED:([A-Za-z0-9_.-]+)", clean)
        if match:
            return f"failed:{match.group(1)}"
    return None


def _infer_result_code_from_success_text(output: str) -> str | None:
    """Conservative fallback when Claude omits RESULT after a clear submit."""
    tail = (output or "")[-3000:].lower()
    success_markers = (
        "application has been successfully submitted",
        "application was successfully submitted",
        "has been successfully submitted",
        "successfully submitted with all required",
        "form is now locked",
    )
    if any(marker in tail for marker in success_markers):
        return "applied"
    return None


def reset_stale_in_progress(max_age_seconds: int) -> int:
    """Release in-progress jobs left by crashed/killed worker processes."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
    conn = get_connection()
    cursor = conn.execute(
        """
        UPDATE jobs
        SET apply_status = NULL, agent_id = NULL, apply_error = 'stale in_progress reset'
        WHERE apply_status = 'in_progress'
          AND (last_attempted_at IS NULL OR last_attempted_at < ?)
        """,
        (cutoff,),
    )
    conn.commit()
    return cursor.rowcount


def _available_memory_mb() -> int | None:
    """Best-effort available physical memory in MB without extra deps."""
    try:
        if platform.system() == "Windows":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullAvailPhys / (1024 * 1024))
            return None
        if hasattr(os, "sysconf"):
            pages = os.sysconf("SC_AVPHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return int((pages * page_size) / (1024 * 1024))
    except Exception:
        return None
    return None


def _cpu_percent() -> int | None:
    """Best-effort host CPU load percentage without adding psutil."""
    try:
        if platform.system() == "Windows":
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            value = proc.stdout.strip()
            return int(float(value)) if value else None
        if hasattr(os, "getloadavg"):
            load_1m = os.getloadavg()[0]
            cpus = os.cpu_count() or 1
            return int(min(100, (load_1m / cpus) * 100))
    except Exception:
        return None
    return None


def _resources_ok(
    min_free_mb: int = RESOURCE_MIN_FREE_MB,
    max_cpu_percent: int = RESOURCE_MAX_CPU_PERCENT,
) -> tuple[bool, str]:
    free_mb = _available_memory_mb()
    cpu = _cpu_percent()
    if free_mb is not None and free_mb < min_free_mb:
        return False, f"free memory {free_mb}MB < {min_free_mb}MB"
    if cpu is not None and cpu > max_cpu_percent:
        return False, f"CPU {cpu}% > {max_cpu_percent}%"
    return True, "ok"


def _wait_for_resources(worker_id: int) -> None:
    """Delay launching the next heavy browser+Claude pair under pressure."""
    deadline = time.monotonic() + RESOURCE_WAIT_TIMEOUT_S
    announced = False
    while not _stop_event.is_set():
        ok, reason = _resources_ok()
        if ok:
            if announced:
                add_event(f"[W{worker_id}] Resource guard cleared")
            return
        if not announced:
            add_event(f"[W{worker_id}] Waiting for resources: {reason}")
            announced = True
        if time.monotonic() >= deadline:
            add_event(f"[W{worker_id}] Resource guard timeout; launching anyway")
            return
        _stop_event.wait(timeout=5)


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def _fetch_apply_candidates(
    conn,
    min_score: int = 8,
    max_age_hours: int | None = None,
    site_contains: str | None = None,
    limit: int = 50,
) -> list:
    """Fetch apply-eligible candidates in deterministic queue order."""
    blocked_sites, blocked_patterns = _load_blocked()
    params: list = [min_score]
    age_clause = ""
    if max_age_hours and max_age_hours > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        age_clause = "AND discovered_at IS NOT NULL AND discovered_at >= ?"
        params.append(cutoff)
    site_clause = ""
    if blocked_sites:
        placeholders = ",".join("?" * len(blocked_sites))
        site_clause = f"AND site NOT IN ({placeholders})"
        params.extend(blocked_sites)
    site_contains_clause = ""
    if site_contains:
        site_contains_clause = "AND LOWER(site) LIKE ?"
        params.append(f"%{site_contains.lower()}%")
    url_clauses = ""
    if blocked_patterns:
        url_clauses = " ".join("AND url NOT LIKE ?" for _ in blocked_patterns)
        params.extend(blocked_patterns)

    return conn.execute(f"""
        SELECT url, title, site, application_url, tailored_resume_path,
               fit_score, location, full_description, cover_letter_path,
               discovered_at
        FROM (
            SELECT url, title, site, application_url, tailored_resume_path,
                   fit_score, location, full_description, cover_letter_path,
                   discovered_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY site ORDER BY discovered_at DESC, RANDOM()
                   ) AS site_idx
            FROM (
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path,
                       discovered_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY site, LOWER(TRIM(title))
                           ORDER BY
                             CASE WHEN LOWER(COALESCE(application_url, '')) LIKE '%greenhouse%'
                                    OR LOWER(COALESCE(url, '')) LIKE '%greenhouse%'
                                    OR LOWER(COALESCE(application_url, '')) LIKE '%lever.co%'
                                    OR LOWER(COALESCE(url, '')) LIKE '%lever.co%'
                                    OR LOWER(COALESCE(application_url, '')) LIKE '%ashby%'
                                    OR LOWER(COALESCE(url, '')) LIKE '%ashby%'
                                    OR LOWER(COALESCE(application_url, '')) LIKE '%myworkdayjobs%'
                                    OR LOWER(COALESCE(url, '')) LIKE '%myworkdayjobs%'
                                  THEN 0 ELSE 1 END,
                             discovered_at DESC
                       ) AS dup_rn
                FROM jobs
                WHERE (apply_status IS NULL OR apply_status = 'failed')
                  AND (apply_attempts IS NULL OR apply_attempts < ?)
                  AND fit_score >= ?
                  {age_clause}
                  AND site NOT IN (SELECT site FROM jobs WHERE apply_status = 'in_progress')
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs d
                      WHERE d.site = jobs.site
                        AND d.url != jobs.url
                        AND LOWER(TRIM(d.title)) = LOWER(TRIM(jobs.title))
                        AND (d.applied_at IS NOT NULL
                             OR d.apply_status IS NOT NULL)
                  )
                  {site_clause}
                  {site_contains_clause}
                  {url_clauses}
            )
            WHERE dup_rn = 1
        )
        ORDER BY fit_score DESC, site_idx ASC, discovered_at DESC, RANDOM()
        LIMIT ?
    """, [config.DEFAULTS["max_apply_attempts"]] + params + [limit]).fetchall()


def _first_applyable_from_rows(rows, *, mutate_manual: bool, use_run_seen: bool):
    """Pick the next applyable row from fetched candidates."""
    from applypilot.config import is_manual_ats

    row = None
    profile_for_gate = None
    search_config_for_gate = None
    if mutate_manual:
        try:
            profile_for_gate = config.load_profile()
            search_config_for_gate = config.load_search_config()
        except Exception:
            profile_for_gate = None

    def _is_manual(candidate) -> bool:
        apply_url = _effective_apply_url(dict(candidate))
        if not apply_url:
            if mutate_manual:
                conn = get_connection()
                conn.execute(
                    "UPDATE jobs SET apply_status = 'failed', apply_error = 'no apply URL', "
                    "apply_attempts = ? WHERE url = ?",
                    (config.DEFAULTS["max_apply_attempts"], candidate["url"]),
                )
                logger.info("Skipping job with no apply URL: %s", candidate["url"][:80])
            return True
        if not is_manual_ats(apply_url):
            return False
        if mutate_manual:
            conn = get_connection()
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                (candidate["url"],),
            )
            logger.info("Skipping manual ATS: %s", candidate["url"][:80])
        return True

    def _is_preapply_rejected(candidate) -> bool:
        if not mutate_manual or profile_for_gate is None:
            return False
        reason = _preapply_reject_reason(dict(candidate), profile_for_gate, search_config_for_gate)
        if not reason:
            return False
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE jobs SET apply_status = 'failed', apply_error = ?, apply_attempts = ?, "
                "last_failure_class = ? WHERE url = ?",
                (reason, config.DEFAULTS["max_apply_attempts"], _classify_failure_class(f"failed:{reason}", reason), candidate["url"]),
            )
        except Exception:
            conn.execute(
                "UPDATE jobs SET apply_status = 'failed', apply_error = ?, apply_attempts = ? WHERE url = ?",
                (reason, config.DEFAULTS["max_apply_attempts"], candidate["url"]),
            )
        logger.info("Skipping pre-apply ineligible location: %s", candidate["url"][:80])
        return True

    idx = 0
    while idx < len(rows) and not row:
        score = rows[idx]["fit_score"]
        group = []
        while idx < len(rows) and rows[idx]["fit_score"] == score:
            group.append(rows[idx])
            idx += 1

        deferred_seen_site = []
        for candidate in group:
            if use_run_seen:
                with _run_seen_lock:
                    if candidate["url"] in _run_seen_urls:
                        continue
                    if candidate["site"] in _run_seen_sites:
                        deferred_seen_site.append(candidate)
                        continue
            if _is_manual(candidate):
                continue
            if _is_preapply_rejected(candidate):
                continue
            row = candidate
            break

        if row:
            break

        for candidate in deferred_seen_site:
            if use_run_seen:
                with _run_seen_lock:
                    if candidate["url"] in _run_seen_urls:
                        continue
            if _is_manual(candidate):
                continue
            if _is_preapply_rejected(candidate):
                continue
            row = candidate
            break

    return row


def preview_apply_queue(
    limit: int = 10,
    min_score: int = 8,
    max_age_hours: int | None = 24,
    site_contains: str | None = None,
) -> list[dict]:
    """Return the top apply queue without acquiring or mutating jobs."""
    conn = get_connection()
    rows = list(_fetch_apply_candidates(
        conn,
        min_score=min_score,
        max_age_hours=max_age_hours,
        site_contains=site_contains,
        limit=max(limit * 5, 50),
    ))
    picked: list[dict] = []
    seen_urls: set[str] = set()
    seen_sites: set[str] = set()

    while rows and len(picked) < limit:
        with _run_seen_lock:
            saved_urls = set(_run_seen_urls)
            saved_sites = set(_run_seen_sites)
            _run_seen_urls.clear()
            _run_seen_urls.update(seen_urls)
            _run_seen_sites.clear()
            _run_seen_sites.update(seen_sites)
        try:
            row = _first_applyable_from_rows(rows, mutate_manual=False, use_run_seen=True)
        finally:
            with _run_seen_lock:
                _run_seen_urls.clear()
                _run_seen_urls.update(saved_urls)
                _run_seen_sites.clear()
                _run_seen_sites.update(saved_sites)

        if not row:
            break
        picked.append(_job_with_effective_apply_url(row))
        seen_urls.add(row["url"])
        seen_sites.add(row["site"])
        rows = [r for r in rows if r["url"] != row["url"]]

    return picked


def acquire_job(target_url: str | None = None, min_score: int = 8,
                worker_id: int = 0, max_age_hours: int | None = None,
                site_contains: str | None = None) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).
        max_age_hours: If set, only consider jobs whose discovered_at is
            within this many hours of now. Non-destructive freshness gate —
            stale jobs stay in the DB but are never selected for apply.
            None / 0 = no age filter (drain everything).

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        from applypilot.config import is_manual_ats

        if target_url:
            like = f"%{target_url.split('?')[0].rstrip('/')}%"
            rows = conn.execute("""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
                  AND (apply_status IS NULL OR apply_status != 'in_progress')
                LIMIT 10
            """, (target_url, target_url, like, like)).fetchall()
        else:
            blocked_sites, blocked_patterns = _load_blocked()
            params: list = [min_score]
            # Non-destructive freshness gate. Param order must stay:
            # fit_score>=? then discovered_at>=? (matches WHERE clause order).
            age_clause = ""
            if max_age_hours and max_age_hours > 0:
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
                age_clause = "AND discovered_at IS NOT NULL AND discovered_at >= ?"
                params.append(cutoff)
            site_clause = ""
            if blocked_sites:
                placeholders = ",".join("?" * len(blocked_sites))
                site_clause = f"AND site NOT IN ({placeholders})"
                params.extend(blocked_sites)
            site_contains_clause = ""
            if site_contains:
                site_contains_clause = "AND LOWER(site) LIKE ?"
                params.append(f"%{site_contains.lower()}%")
            url_clauses = ""
            if blocked_patterns:
                url_clauses = " ".join("AND url NOT LIKE ?" for _ in blocked_patterns)
                params.extend(blocked_patterns)
            # Fetch a batch so we can skip manual_ats jobs in one pass instead
            # of returning None and making the worker think the queue is empty.
            #
            # Round-robin across sites within each fit_score tier so we don't
            # stack 9 Stripe applies in a row. Greenhouse fraud-detection is
            # per-tenant; rapid same-company applies trigger email verification.
            # ROW_NUMBER() partitioned by site assigns each job an "Nth pick
            # from this site" index in newest-first order; the outer ORDER BY
            # then interleaves: round 1 of every site, round 2, etc.
            # Per-site lock: exclude any company already being applied to by
            # another worker right now. Prevents two workers from hitting the
            # same Greenhouse tenant in parallel (which triggers fraud-detection
            # email verification). Worker-N is locked out of every site whose
            # apply_status = 'in_progress' until that worker finishes.
            # Non-destructive de-dup: companies repost the same role under
            # multiple req IDs / locations (Amazon via indeed ~8x, PayPal
            # Workday, brex/databricks double-listings). Collapse to ONE row
            # per (site, normalized title), preferring a canonical-ATS
            # application_url (greenhouse/lever/ashby/workday) over aggregator
            # links, then the freshest. No DB rows are deleted — this is a
            # selection-time filter, same pattern as the freshness gate.
            rows = conn.execute(f"""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path,
                       discovered_at
                FROM (
                    SELECT url, title, site, application_url, tailored_resume_path,
                           fit_score, location, full_description, cover_letter_path,
                           discovered_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY site ORDER BY discovered_at DESC, RANDOM()
                           ) AS site_idx
                    FROM (
                        SELECT url, title, site, application_url, tailored_resume_path,
                               fit_score, location, full_description, cover_letter_path,
                               discovered_at,
                               ROW_NUMBER() OVER (
                                   PARTITION BY site, LOWER(TRIM(title))
                                   ORDER BY
                                     CASE WHEN LOWER(COALESCE(application_url, '')) LIKE '%greenhouse%'
                                            OR LOWER(COALESCE(url, '')) LIKE '%greenhouse%'
                                            OR LOWER(COALESCE(application_url, '')) LIKE '%lever.co%'
                                            OR LOWER(COALESCE(url, '')) LIKE '%lever.co%'
                                            OR LOWER(COALESCE(application_url, '')) LIKE '%ashby%'
                                            OR LOWER(COALESCE(url, '')) LIKE '%ashby%'
                                            OR LOWER(COALESCE(application_url, '')) LIKE '%myworkdayjobs%'
                                            OR LOWER(COALESCE(url, '')) LIKE '%myworkdayjobs%'
                                          THEN 0 ELSE 1 END,
                                     discovered_at DESC
                               ) AS dup_rn
                        FROM jobs
                        WHERE (apply_status IS NULL OR apply_status = 'failed')
                          AND (apply_attempts IS NULL OR apply_attempts < ?)
                          AND fit_score >= ?
                          {age_clause}
                          AND site NOT IN (SELECT site FROM jobs WHERE apply_status = 'in_progress')
                          -- Durable de-dup: one application per (company,
                          -- title). If ANY OTHER listing (different url) of
                          -- the same (site, normalized title) has already
                          -- been ATTEMPTED in any way — applied, in_progress,
                          -- needs_review, failed, manual — exclude this twin.
                          -- Two "Staff Product Designer @ brex" reqs is a
                          -- repost; applying to both looks spammy to the
                          -- recruiter regardless of outcome. The d.url !=
                          -- jobs.url guard means a job never excludes itself,
                          -- so a genuine single-listing retry still works.
                          AND NOT EXISTS (
                              SELECT 1 FROM jobs d
                              WHERE d.site = jobs.site
                                AND d.url != jobs.url
                                AND LOWER(TRIM(d.title)) = LOWER(TRIM(jobs.title))
                                AND (d.applied_at IS NOT NULL
                                     OR d.apply_status IS NOT NULL)
                          )
                          {site_clause}
                          {site_contains_clause}
                          {url_clauses}
                    )
                    WHERE dup_rn = 1
                )
                ORDER BY fit_score DESC, site_idx ASC, discovered_at DESC, RANDOM()
                LIMIT 50
            """, [config.DEFAULTS["max_apply_attempts"]] + params).fetchall()

        # Walk the batch score-tier by score-tier: prefer an unseen site within
        # the tier, then fall back to a seen site before dropping to a lower
        # score. This keeps fit score above company spread.
        row = None

        profile_for_gate = None
        search_config_for_gate = None
        try:
            profile_for_gate = config.load_profile()
            search_config_for_gate = config.load_search_config()
        except Exception:
            profile_for_gate = None

        def _mark_unapplyable_if_needed(candidate) -> bool:
            apply_url = _effective_apply_url(dict(candidate))
            if not apply_url:
                conn.execute(
                    "UPDATE jobs SET apply_status = 'failed', apply_error = 'no apply URL', "
                    "apply_attempts = ? WHERE url = ?",
                    (config.DEFAULTS["max_apply_attempts"], candidate["url"]),
                )
                logger.info("Skipping job with no apply URL: %s", candidate["url"][:80])
                return True
            if not is_manual_ats(apply_url):
                return False
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                (candidate["url"],),
            )
            logger.info("Skipping manual ATS: %s", candidate["url"][:80])
            return True

        def _mark_preapply_reject_if_needed(candidate) -> bool:
            if profile_for_gate is None:
                return False
            reason = _preapply_reject_reason(dict(candidate), profile_for_gate, search_config_for_gate)
            if not reason:
                return False
            failure_class = _classify_failure_class(f"failed:{reason}", reason)
            try:
                conn.execute(
                    "UPDATE jobs SET apply_status = 'failed', apply_error = ?, apply_attempts = ?, "
                    "last_failure_class = ? WHERE url = ?",
                    (reason, config.DEFAULTS["max_apply_attempts"], failure_class, candidate["url"]),
                )
            except Exception:
                conn.execute(
                    "UPDATE jobs SET apply_status = 'failed', apply_error = ?, apply_attempts = ? WHERE url = ?",
                    (reason, config.DEFAULTS["max_apply_attempts"], candidate["url"]),
                )
            logger.info("Skipping pre-apply ineligible location: %s", candidate["url"][:80])
            return True

        idx = 0
        while idx < len(rows) and not row:
            score = rows[idx]["fit_score"]
            group = []
            while idx < len(rows) and rows[idx]["fit_score"] == score:
                group.append(rows[idx])
                idx += 1

            deferred_seen_site = []
            for candidate in group:
                with _run_seen_lock:
                    if candidate["url"] in _run_seen_urls:
                        continue
                    if candidate["site"] in _run_seen_sites:
                        deferred_seen_site.append(candidate)
                        continue
                if _mark_unapplyable_if_needed(candidate):
                    continue
                if _mark_preapply_reject_if_needed(candidate):
                    continue
                row = candidate
                break

            if row:
                break

            for candidate in deferred_seen_site:
                with _run_seen_lock:
                    if candidate["url"] in _run_seen_urls:
                        continue
                if _mark_unapplyable_if_needed(candidate):
                    continue
                if _mark_preapply_reject_if_needed(candidate):
                    continue
                row = candidate
                break

        if not row:
            conn.commit()  # persist the manual marks
            return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?
            WHERE url = ?
        """, (f"worker-{worker_id}", now, row["url"]))
        with _run_seen_lock:
            _run_seen_urls.add(row["url"])
            _run_seen_sites.add(row["site"])
        conn.commit()

        return _job_with_effective_apply_url(row)
    except Exception:
        conn.rollback()
        raise


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (now, duration_ms, task_id, url))
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, task_id, url))
    conn.commit()


# Tail-friendly per-attempt log so a supervisor can `Get-Content -Wait` it.
def write_review_log(job: dict, status: str, model: str, duration_ms: int,
                     dry_run: bool, error: str | None = None,
                     worker_id: int = 0,
                     prefill_status: dict | None = None,
                     failure_class: str | None = None,
                     retry_count: int | None = None,
                     verification_confidence: float | None = None,
                     idempotency_key: str | None = None,
                     checkpoint_stage: str | None = None) -> None:
    """Append one JSON line per apply attempt to logs/review.jsonl."""
    config.ensure_dirs()
    path = config.LOG_DIR / "review.jsonl"
    # Telemetry hygiene: a successful attempt is never a failure. Stale
    # failure_class values (e.g. "transient_unknown" computed earlier in the
    # attempt loop) were being logged on applied / dry_run:applied rows,
    # polluting the (A)-removable failure counts in `applypilot report`.
    if status in {"applied", "dry_run:applied"} or status.endswith(":applied"):
        failure_class = None
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "worker_id": worker_id,
        "job_url": job.get("url"),
        "apply_url": _effective_apply_url(job),
        "title": job.get("title"),
        "site": job.get("site"),
        "fit_score": job.get("fit_score"),
        "tailored_resume_path": job.get("tailored_resume_path"),
        "cover_letter_path": job.get("cover_letter_path"),
        "model": model,
        "dry_run": dry_run,
        "status": status,
        "duration_ms": duration_ms,
        "error": error,
        "prefill_ats": prefill_status["ats"] if prefill_status else None,
        "prefill_fields_filled": prefill_status["fields_filled"] if prefill_status else None,
        "prefill_duration_ms": prefill_status["duration_ms"] if prefill_status else None,
        "prefill_error": prefill_status["error"] if prefill_status else None,
        "prefill_submit_ready": prefill_status.get("submit_ready") if prefill_status else None,
        "failure_class": failure_class,
        "retry_count": retry_count,
        "verification_confidence": verification_confidence,
        "idempotency_key": idempotency_key,
        "checkpoint_stage": checkpoint_stage,
    }

    # Reliability-v2 Phase A: structured cost/telemetry so $/apply, cache
    # hit-rate, and the (A)-removable vs (B)-irreducible failure split are
    # measurable from data instead of anecdote. Token/cost from run_job's
    # Claude Code result (job._run_meta.telemetry); tier_used from the
    # skill-playbook dispatcher (rides on prefill_status, like skill_used).
    telem = (job.get("_run_meta") or {}).get("telemetry") or {}
    row["input_tokens"] = telem.get("input_tokens")
    row["output_tokens"] = telem.get("output_tokens")
    row["cache_read"] = telem.get("cache_read")
    row["cache_create"] = telem.get("cache_create")
    row["cost_usd"] = telem.get("cost_usd")
    row["turns"] = telem.get("turns")
    if prefill_status:
        row["tier_used"] = prefill_status.get("tier_used")
        row["skill_used"] = prefill_status.get("skill_used")
        row["replay_duration_ms"] = prefill_status.get("replay_duration_ms")
        row["patch_duration_ms"] = prefill_status.get("patch_duration_ms")
    else:
        row["tier_used"] = None

    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("Failed to write review.jsonl")


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 8,
               model: str = "sonnet", worker_id: int = 0) -> Path | None:
    """Generate a prompt file and print the Claude CLI command for manual debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)
    if not job:
        return None

    # Read resume text â€” fall back to master resume if no tailored version
    resume_path = job.get("tailored_resume_path") or str(config.RESUME_PDF_PATH)
    txt_path = Path(resume_path).with_suffix(".txt")
    if not txt_path.exists():
        txt_path = config.RESUME_PATH
    job["tailored_resume_path"] = resume_path
    resume_text = ""
    if txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text)

    # Release the lock so the job stays available
    release_lock(job["url"])

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{job['title'][:30].replace(' ', '_')}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL
            WHERE url = ?
        """, (now, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL
            WHERE url = ?
        """, (reason or "manual", url))
    conn.commit()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status IN ('failed', 'needs_review', 'skipped')
    """)
    conn.commit()
    return cursor.rowcount


def reset_manual(site_contains: str | None = None, resolved_only: bool = False) -> int:
    """Reset jobs marked manual so they can be retried by the apply queue.

    Args:
        site_contains: Optional case-insensitive filter on the jobs.site value.
        resolved_only: If true, only reset manual jobs whose application_url is a
            usable non-LinkedIn outbound URL. This is useful after resolving
            LinkedIn listings to direct ATS links.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    params: list[object] = []
    site_clause = ""
    if site_contains:
        site_clause = "AND LOWER(site) LIKE ?"
        params.append(f"%{site_contains.lower()}%")

    resolved_clause = ""
    if resolved_only:
        resolved_clause = """
          AND application_url IS NOT NULL
          AND LOWER(TRIM(application_url)) NOT IN ('', 'none', 'null', 'nan', 'nat')
          AND (LOWER(TRIM(application_url)) LIKE 'http://%' OR LOWER(TRIM(application_url)) LIKE 'https://%')
          AND LOWER(TRIM(application_url)) NOT LIKE '%linkedin.com/jobs/view%'
        """

    cursor = conn.execute(f"""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status = 'manual'
          {site_clause}
          {resolved_clause}
    """, params)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False,
            job_timeout: int | None = None,
            verify_threshold: float | None = None,
            navigation_timeout: int | None = None,
            interaction_timeout: int | None = None,
            assert_timeout: int | None = None,
            escalation_mode: str = "pause",
            legacy_result_fallback: bool = True,
            retry_count: int = 0,
            recorder=None,
            browser_stream=None,
            visual_trace=None,
            broker=None,
            identity_id: str | None = None) -> tuple[str, int, dict | None]:
    """Spawn a Claude Code session for one job application.

    Returns:
        Tuple of (status_string, duration_ms, prefill_status). Status is one of:
        'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', 'needs_review:reason', or 'skipped'. prefill_status is the dict from
        prefill_application or None if pre-fill never ran.
    """
    run_started = time.time()
    if job_timeout is None:
        job_timeout = config.DEFAULTS["apply_timeout"]
    if verify_threshold is None:
        verify_threshold = float(config.DEFAULTS["verify_threshold"])
    # NOTE: max_transient_retries is owned by worker_loop's retry logic
    # (lines ~1913, 1921). It is NOT a run_job parameter — references here
    # were dead code from a refactor and caused NameError on every call.
    if navigation_timeout is None:
        navigation_timeout = int(config.DEFAULTS["navigation_timeout"])
    if interaction_timeout is None:
        interaction_timeout = int(config.DEFAULTS["interaction_timeout"])
    if assert_timeout is None:
        assert_timeout = int(config.DEFAULTS["assert_timeout"])
    if escalation_mode is None:
        escalation_mode = str(config.DEFAULTS["escalation_mode"])
    if legacy_result_fallback is None:
        legacy_result_fallback = bool(config.DEFAULTS["allow_legacy_result_fallback"])
    if verify_threshold is None:
        verify_threshold = float(config.DEFAULTS["verify_threshold"])
    if navigation_timeout is None:
        navigation_timeout = int(config.DEFAULTS["navigation_timeout"])
    if interaction_timeout is None:
        interaction_timeout = int(config.DEFAULTS["interaction_timeout"])
    if assert_timeout is None:
        assert_timeout = int(config.DEFAULTS["assert_timeout"])
    if escalation_mode not in {"pause", "skip"}:
        escalation_mode = str(config.DEFAULTS["escalation_mode"])

    job_meta = {
        "retry_count": retry_count,
        "failure_class": None,
        "verification_confidence": None,
        "verification_evidence": None,
        "idempotency_key": None,
        "checkpoint_stage": None,
        "apply_result_json": None,
    }
    job["_run_meta"] = job_meta

    profile = config.load_profile()
    apply_url = _effective_apply_url(job) or ""
    job["application_url"] = apply_url
    location_reject = _preapply_location_reject(job, profile)
    if location_reject:
        job_meta["failure_class"] = _classify_failure_class(f"failed:{location_reject}", location_reject)
        _write_job_runtime_metadata(
            job["url"],
            last_failure_class=job_meta["failure_class"],
            checkpoint=_checkpoint(CHECKPOINT_PAGE_REACHED, apply_url=_canonicalize_url(apply_url), preapply_location_gate=True),
        )
        add_event(f"[W{worker_id}] Pre-apply location reject: {location_reject}")
        update_state(worker_id, status="failed", last_action=location_reject)
        return f"failed:{location_reject}", int((time.time() - run_started) * 1000), None
    idempotency_key = _compute_idempotency_key(job, profile)
    job_meta["idempotency_key"] = idempotency_key
    _write_job_runtime_metadata(
        job["url"],
        idempotency_key=idempotency_key,
        checkpoint=_checkpoint(CHECKPOINT_PAGE_REACHED, apply_url=_canonicalize_url(apply_url)),
    )
    job_meta["checkpoint_stage"] = CHECKPOINT_PAGE_REACHED

    existing_row = _read_job_row(job["url"])
    checkpoint_stage = None
    if existing_row and existing_row.get("checkpoint_json"):
        try:
            checkpoint_stage = json.loads(existing_row["checkpoint_json"]).get("stage")
        except Exception:
            checkpoint_stage = None
    if checkpoint_stage in {CHECKPOINT_SUBMIT_ATTEMPTED, CHECKPOINT_VERIFICATION_COMPLETE}:
        job_meta["failure_class"] = "verification_possible_duplicate_guard"
        job_meta["checkpoint_stage"] = checkpoint_stage
        _write_job_runtime_metadata(
            job["url"],
            last_failure_class=job_meta["failure_class"],
        )
        return "needs_review:possible_duplicate_guard", int((time.time() - run_started) * 1000), None

    if _idempotency_already_applied(idempotency_key, job["url"]):
        job_meta["failure_class"] = "verification_possible_duplicate_guard"
        _write_job_runtime_metadata(
            job["url"],
            last_failure_class=job_meta["failure_class"],
            checkpoint=_checkpoint(CHECKPOINT_SUBMIT_ATTEMPTED, duplicate_guard=True),
        )
        job_meta["checkpoint_stage"] = CHECKPOINT_SUBMIT_ATTEMPTED
        return "needs_review:possible_duplicate_guard", int((time.time() - run_started) * 1000), None

    # SUBMIT-CRITICAL: Phase 3 watchdog must grant +15s grace here so a kill
    # can't strand a dangling INTENT.
    # Durable two-phase submission ledger (Task 10A). Runs whenever an identity
    # is known (identity_id is None only for legacy/test callers, which keep the
    # old behavior). Blocks re-apply across runs BEFORE any submit is attempted.
    ledger = None
    if identity_id:
        ledger = SubmissionLedger(get_connection())
        # (1) hard re-apply block across runs: prior CONFIRMED submission to
        #     this identity.
        if ledger.has_confirmed(identity_id):
            job_meta["failure_class"] = "verification_already_applied_identity"
            _write_job_runtime_metadata(job["url"], last_failure_class=job_meta["failure_class"])
            return "needs_review:already_applied_identity", int((time.time() - run_started) * 1000), None
        # (2) dangling-INTENT block: a prior run died mid-submit for this
        #     identity — never blindly re-apply over an unreconciled intent
        #     (the double-submit guard).
        if ledger.has_open_intent(identity_id):
            job_meta["failure_class"] = "verification_dangling_submission_intent"
            _write_job_runtime_metadata(job["url"], last_failure_class=job_meta["failure_class"])
            return "needs_review:dangling_submission_intent", int((time.time() - run_started) * 1000), None
        # (3) company cooldown: >=N confirmed applies to this board within window.
        _ref = parse_ats_url(_effective_apply_url(job) or "")
        if _ref and _ref.token:
            _since = (datetime.now(timezone.utc)
                      - timedelta(days=config.DEFAULTS["company_cooldown_days"])).isoformat()
            if ledger.confirmed_count_for_token(_ref.token, _since) >= config.DEFAULTS["company_cooldown_max"]:
                job_meta["failure_class"] = "verification_company_cooldown"
                _write_job_runtime_metadata(job["url"], last_failure_class=job_meta["failure_class"])
                return "needs_review:company_cooldown", int((time.time() - run_started) * 1000), None

    # Read resume text â€” fall back to master resume if no tailored version
    resume_path = job.get("tailored_resume_path") or str(config.RESUME_PDF_PATH)
    txt_path = Path(resume_path).with_suffix(".txt")
    if not txt_path.exists():
        txt_path = config.RESUME_PATH
    job["tailored_resume_path"] = resume_path
    resume_text = ""
    if txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    worker_dir = reset_worker_dir(worker_id)
    job["_upload_dir"] = str(worker_dir)

    # Submit broker: issue the one-shot ticket for THIS identity now that both
    # duplicate-guard short-circuits above have passed. In dry-run issue() is a
    # no-op (SubmitBroker never opens a ticket in dry-run) so the network route
    # fails closed and no submit POST can leave the browser. broker=None (e.g.
    # legacy/test callers) simply skips issue/gate — backward-compatible.
    broker_file = None
    if identity_id and ledger is not None and not dry_run:
        # INTENT before any submit. Gated on not-dry_run to mirror the broker
        # (dry-run never submits, so it must not strand an intent). A crash
        # between here and confirm/fail leaves a dangling INTENT that the NEXT
        # run blocks on (see block (2) above) — safe by design: better a
        # needs_review than a silent double-submit.
        ledger.record_intent(identity_id, worker_id=worker_id)
    if broker is not None and identity_id:
        try:
            broker.issue(identity_id)
            broker_file = str(getattr(broker, "path", "") or "") or None
        except Exception as e:
            logger.warning("submit broker issue failed (submits will fail closed): %s", e)

    # Write per-worker MCP config (dry_run forwarded → server-side submit block)
    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_config_path.write_text(
        json.dumps(_make_mcp_config(port, dry_run=dry_run,
                                    broker_file=broker_file, job_identity=identity_id)),
        encoding="utf-8",
    )

    # --- Deterministic pre-fill (Greenhouse v1) ---
    # Fills 4 standard fields + resume directly via CDP, BEFORE Claude spawns.
    # Saves ~30 LLM round-trips on Greenhouse pages. Must disconnect cleanly
    # before subprocess.Popen below, otherwise both Python Playwright and the
    # MCP Playwright server compete for the same CDP port.
    # Initialize first so any later return statement (incl. exception paths)
    # has the symbol bound, even if prefill_application itself throws.
    prefill_status: dict | None = None
    try:
        prefill_status = prefill_application(
            cdp_port=port,
            apply_url=apply_url,
            profile=profile,
            resume_pdf_path=str(Path(resume_path).with_suffix(".pdf")),
            timeout_s=30,
        )
    except Exception as e:
        logger.warning("prefill: unexpected error, proceeding without pre-fill: %s", e)
        prefill_status = {
            "ats": "error",
            "fields_filled": [],
            "error": f"{type(e).__name__}: {e}",
            "duration_ms": 0,
        }
    resolved_url = _valid_http_url(prefill_status.get("resolved_url") if prefill_status else None)
    if resolved_url and resolved_url != apply_url:
        apply_url = resolved_url
        job["application_url"] = resolved_url
        _write_job_runtime_metadata(
            job["url"],
            checkpoint=_checkpoint(
                CHECKPOINT_PAGE_REACHED,
                apply_url=_canonicalize_url(apply_url),
                resolved_by="prefill",
            ),
        )
        add_event(f"[W{worker_id}] Resolved apply URL: {resolved_url[:100]}")
    logger.info(
        "prefill: ats=%s filled=%s err=%s dur=%dms resolved=%s",
        prefill_status["ats"], prefill_status["fields_filled"],
        prefill_status["error"], prefill_status["duration_ms"],
        prefill_status.get("resolved_url"),
    )

    # Dry-run defense-in-depth (iter 17 incident): DOM-level submit blocker.
    # The stream MCP server already refuses submits server-side in dry-run,
    # but the raw Playwright MCP can still click buttons — block at the page.
    if dry_run:
        _inject_dry_run_submit_blocker(port)
    add_event(f"[W{worker_id}] Pre-filled {len(prefill_status['fields_filled'])} fields ({prefill_status['ats']})")
    if (
        prefill_status.get("ats") == "greenhouse"
        and prefill_status.get("error") == "no_form_detected"
        and not prefill_status.get("fields_filled")
    ):
        obs = _latest_stream_observation(browser_stream, refresh=True)
        page_text = (obs.page_text_sample if obs else "").lower()
        has_action_text = any(token in page_text for token in ("apply", "submit", "continue", "next"))
        if obs is not None and not obs.controls and not obs.submit_buttons and not has_action_text:
            duration_ms = int((time.time() - run_started) * 1000)
            reason = "apply_button_not_found"
            job_meta["failure_class"] = _classify_failure_class(f"failed:{reason}", reason)
            _write_job_runtime_metadata(
                job["url"],
                last_failure_class=job_meta["failure_class"],
                checkpoint=_checkpoint(CHECKPOINT_PAGE_REACHED, apply_url=_canonicalize_url(apply_url), no_form_no_apply=True),
            )
            add_event(f"[W{worker_id}] Greenhouse no form/apply controls; skipping LLM")
            update_state(worker_id, status="failed", last_action=reason)
            # Bailed before any submit POST — clear the open intent so this job
            # stays retryable (no CONFIRMED exists; not a dangling crash).
            _resolve_ledger_intent(ledger, identity_id, {"verified": False}, job_meta)
            return f"failed:{reason}", duration_ms, prefill_status
    if "resume" in (prefill_status.get("fields_filled") or []):
        _write_job_runtime_metadata(
            job["url"],
            checkpoint=_checkpoint(CHECKPOINT_RESUME_UPLOADED, ats=prefill_status.get("ats")),
        )
        job_meta["checkpoint_stage"] = CHECKPOINT_RESUME_UPLOADED

    if dry_run and prefill_status.get("ats") == "greenhouse" and not prefill_status.get("error"):
        time.sleep(1.5)
        ready_state = _check_greenhouse_submit_ready(port, browser_stream=browser_stream)
        filled = set(prefill_status.get("fields_filled") or [])
        normalized_missing = []
        for missing in ready_state.get("missing", []):
            key = str(missing).strip().lower()
            if key == "country" and "phone_country" in filled:
                continue
            if key in {"resume/cv", "resume", "cv"} and "resume" in filled:
                continue
            normalized_missing.append(missing)
        ready_state["missing"] = normalized_missing
        ready_state["ready"] = bool(ready_state.get("submit_enabled")) and not normalized_missing
        prefill_status["submit_ready"] = ready_state
        if ready_state.get("ready"):
            duration_ms = int((time.time() - run_started) * 1000)
            screenshot = _capture_browser_artifact(port, job, worker_id, "dry_run_ready")
            if screenshot:
                add_event(f"[W{worker_id}] Dry-run ready screenshot: {screenshot.name}")
            add_event(f"[W{worker_id}] Greenhouse dry-run submit-ready after prefill")
            return "applied", duration_ms, prefill_status
        if ready_state.get("missing"):
            add_event(f"[W{worker_id}] Prefill missing: {', '.join(ready_state['missing'][:4])}")

    # Reliability-v2 Phase E: automatability gate. BEFORE spending an LLM
    # apply, scan the loaded page for a hard blocker (visible CAPTCHA,
    # email-verification wall, SSO redirect, anti-bot interstitial). If
    # found, queue for a human and short-circuit — these are the
    # (B)-irreducible failures; paying Sonnet/Haiku to fail at a wall is
    # pure waste. Fail-OPEN: any scanner error → proceed normally.
    try:
        from applypilot.apply.automatability import automatability_gate
        _blocker = automatability_gate(
            port, job, queue_path=config.LOG_DIR / "human_queue.jsonl")
    except Exception:
        _blocker = None
    if _blocker:
        duration_ms = int((time.time() - run_started) * 1000)
        job_meta["failure_class"] = f"blocker_{_blocker}"
        _write_job_runtime_metadata(job["url"], last_failure_class=job_meta["failure_class"])
        add_event(f"[W{worker_id}] needs_human: {_blocker} (no LLM spent)")
        update_state(worker_id, status="needs_review",
                     last_action=f"needs_human: {_blocker}")
        # Bailed at a hard blocker before any submit POST — clear the open intent.
        _resolve_ledger_intent(ledger, identity_id, {"verified": False}, job_meta)
        return f"needs_review:needs_human_{_blocker}", duration_ms, prefill_status

    # Reliability-v2 Phase C: deterministic Greenhouse adapter pass.
    # When the v2 flag is on and prefill detected Greenhouse, run the
    # self-healing adapter (frame-aware, answer-cache for free-text) to
    # complete the FULL form with zero Claude-Code LLM. If it leaves NO
    # unresolved custom questions, it SUBMITS deterministically and we
    # skip the LLM entirely → no timeout, ~$0 (this is the (A)-timeout +
    # cost elimination). Otherwise it just front-loads fills and the LLM
    # finishes/submits. ADDITIVE + fail-OPEN: any error → existing
    # prefill+LLM flow, unchanged. Never hard-fails.
    try:
        from applypilot.apply.skill_runner import is_skill_flow_enabled
        if (is_skill_flow_enabled()
                and isinstance(prefill_status, dict)
                and prefill_status.get("ats") == "greenhouse"
                and not prefill_status.get("error")):
            adapter_res = _greenhouse_adapter_pass(
                port, profile, str(Path(resume_path).with_suffix(".pdf")),
                allow_submit=not dry_run, broker=broker, identity_id=identity_id)
            if adapter_res:
                ff = prefill_status.setdefault("fields_filled", [])
                for k in adapter_res.get("fields_filled", []):
                    if k not in ff:
                        ff.append(k)
                prefill_status["adapter"] = {
                    "fields_filled": adapter_res.get("fields_filled", []),
                    "unresolved": adapter_res.get("unresolved", []),
                    "submitted": adapter_res.get("submitted", False),
                }
                prefill_status["tier_used"] = (
                    "greenhouse_adapter_submit"
                    if adapter_res.get("submitted") else "greenhouse_adapter")
                add_event(
                    f"[W{worker_id}] Greenhouse adapter filled "
                    f"{len(adapter_res.get('fields_filled', []))} fields, "
                    f"{len(adapter_res.get('unresolved', []))} unresolved, "
                    f"submitted={adapter_res.get('submitted')}"
                )
                # Adapter submitted deterministically → the application is
                # SENT. Do NOT spawn the LLM (would double-submit). Verify
                # and return, mirroring the post-RESULT:APPLIED path.
                if adapter_res.get("submitted") and not dry_run:
                    _write_job_runtime_metadata(
                        job["url"], increment_submit_attempt=True,
                        checkpoint=_checkpoint(CHECKPOINT_SUBMIT_ATTEMPTED,
                                               source="greenhouse_adapter"))
                    job_meta["checkpoint_stage"] = CHECKPOINT_SUBMIT_ATTEMPTED
                    duration_ms = int((time.time() - run_started) * 1000)
                    try:
                        verification = _verify_submission_success(
                            port, verify_threshold=verify_threshold,
                            previous_url=apply_url,
                            browser_stream=browser_stream)
                    except Exception as ve:
                        logger.debug("adapter-submit verify error: %s", ve)
                        verification = {"verified": False,
                                        "error": f"verify_exc:{ve}"}
                    job_meta["verification_confidence"] = verification.get("confidence")
                    job_meta["verification_evidence"] = verification
                    _resolve_ledger_intent(ledger, identity_id, verification, job_meta)
                    if verification.get("verified"):
                        _write_job_runtime_metadata(
                            job["url"], apply_result_json={"status": "applied",
                            "via": "greenhouse_adapter"},
                            verification_evidence=verification,
                            verification_confidence=verification.get("confidence"),
                            checkpoint=_checkpoint(CHECKPOINT_VERIFICATION_COMPLETE))
                        add_event(f"[W{worker_id}] APPLIED via adapter (no LLM) "
                                  f"conf={verification.get('confidence')}")
                        update_state(worker_id, status="applied",
                                     last_action="adapter-submit applied")
                        return "applied", duration_ms, prefill_status
                    job_meta["failure_class"] = "verification_unverified_submission"
                    _write_job_runtime_metadata(
                        job["url"], verification_evidence=verification,
                        verification_confidence=verification.get("confidence"),
                        last_failure_class=job_meta["failure_class"])
                    add_event(f"[W{worker_id}] adapter submitted, UNVERIFIED")
                    update_state(worker_id, status="needs_review",
                                 last_action="adapter-submit unverified")
                    return "needs_review:unverified_submission", duration_ms, prefill_status
    except Exception as e:
        logger.debug("greenhouse adapter pass skipped: %s", e)

    # Build the prompt (after prefill so we can tell the agent what was already filled)
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
        prefill_status=prefill_status,
        browser_observation_summary=summarize_observation(
            _latest_stream_observation(browser_stream, refresh=True)
        ),
    )

    # Resolve claude binary via shared config helper (PATH + install glob)
    claude_bin = config.find_claude_binary()
    if not claude_bin:
        raise RuntimeError(
            "Could not find claude binary. Install Claude Code or add it to PATH."
        )

    # Build claude command
    cmd = [
        claude_bin,
        "--model", model,
        "-p",
        "--mcp-config", str(mcp_config_path),
        "--strict-mcp-config",
        "--tools", "",
        "--disable-slash-commands",
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        "--output-format", "stream-json",
        "--verbose", "-",
    ]
    allow_snapshot_first_pass = os.environ.get(
        "APPLYPILOT_ALLOW_SNAPSHOT_FIRST_PASS",
        "0",
    ).strip().lower() in {"1", "true", "yes", "on"} or retry_count > 0
    allow_raw_browser_fallback = os.environ.get(
        "APPLYPILOT_ALLOW_RAW_BROWSER_FIRST_PASS",
        "0",
    ).strip().lower() in {"1", "true", "yes", "on"} or retry_count > 0
    allow_navigate_first_pass = os.environ.get(
        "APPLYPILOT_ALLOW_NAVIGATE_AFTER_PREFILL",
        "0",
    ).strip().lower() in {"1", "true", "yes", "on"} or retry_count > 0 or not (prefill_status.get("fields_filled") or [])
    cmd[12:12] = [
        "--disallowedTools",
        _disallowed_tools_arg(
            allow_snapshot=allow_snapshot_first_pass,
            allow_raw_browser=allow_raw_browser_fallback,
            allow_navigate=allow_navigate_first_pass,
        ),
    ]
    if _claude_supports_allowed_tools(claude_bin):
        cmd[6:6] = [
            "--allowedTools",
            _allowed_tools_arg(
                allow_snapshot=allow_snapshot_first_pass,
                allow_raw_browser=allow_raw_browser_fallback,
                allow_navigate=allow_navigate_first_pass,
            ),
        ]

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env["APPLYPILOT_NAVIGATION_TIMEOUT_S"] = str(navigation_timeout)
    env["APPLYPILOT_INTERACTION_TIMEOUT_S"] = str(interaction_timeout)
    env["APPLYPILOT_ASSERT_TIMEOUT_S"] = str(assert_timeout)

    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {job.get('site', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {apply_url}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"Stream strict snapshot fallback allowed: {allow_snapshot_first_pass}\n"
        f"Stream strict raw browser fallback allowed: {allow_raw_browser_fallback}\n"
        f"Stream strict navigate allowed: {allow_navigate_first_pass}\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    stats: dict = {}
    proc = None
    strict_snapshot_violation = False

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(worker_dir),
        )
        with _claude_lock:
            _claude_procs[worker_id] = proc

        proc.stdin.write(agent_prompt)
        proc.stdin.close()

        text_parts: list[str] = []
        output_queue: queue.Queue[str | None] = queue.Queue()

        def _read_stdout() -> None:
            try:
                assert proc is not None
                assert proc.stdout is not None
                for raw in proc.stdout:
                    output_queue.put(raw)
            finally:
                output_queue.put(None)

        reader_thread = threading.Thread(
            target=_read_stdout,
            name=f"claude-reader-{worker_id}",
            daemon=True,
        )
        reader_thread.start()

        deadline = start + max(1, job_timeout)
        timed_out = False

        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(log_header)

            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    raw_line = output_queue.get(timeout=min(0.25, remaining))
                except queue.Empty:
                    continue

                if raw_line is None:
                    break

                line = raw_line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    msg_type = msg.get("type")
                    if msg_type == "assistant":
                        for block in msg.get("message", {}).get("content", []):
                            bt = block.get("type")
                            if bt == "text":
                                text_parts.append(block["text"])
                                lf.write(block["text"] + "\n")
                            elif bt == "tool_use":
                                raw_tool_name = block.get("name", "")
                                inp = block.get("input", {}) or {}
                                if (
                                    raw_tool_name in {
                                        "mcp__playwright__browser_snapshot",
                                        "mcp__playwright__browser_take_screenshot",
                                    }
                                    and not allow_snapshot_first_pass
                                ):
                                    strict_snapshot_violation = True
                                    text_parts.append(
                                        "RESULT:FAILED:stream_snapshot_needed"
                                    )
                                    lf.write(
                                        f"  >> {raw_tool_name.replace('mcp__playwright__', '')} BLOCKED: strict stream pass\n"
                                    )
                                    if proc is not None and proc.poll() is None:
                                        _kill_process_tree(proc.pid)
                                    break
                                # Phase 4: Tier 3 recorder side-channel. When a
                                # SkillRecorder is attached, observe every MCP
                                # browser_* call so a skill YAML can be written
                                # if this apply ends in 'applied'. Errors here
                                # must never break the apply.
                                if recorder is not None and raw_tool_name.startswith("mcp__playwright__"):
                                    try:
                                        recorder.observe_tool_use(
                                            raw_tool_name.replace("mcp__playwright__", ""),
                                            inp,
                                        )
                                    except Exception:
                                        logger.exception("recorder.observe_tool_use failed")
                                name = (
                                    raw_tool_name
                                    .replace("mcp__playwright__", "")
                                    .replace("mcp__gmail__", "gmail:")
                                )
                                if "url" in inp:
                                    desc = f"{name} {inp['url'][:60]}"
                                elif "ref" in inp:
                                    desc = f"{name} {inp.get('element', inp.get('text', ''))}"[:50]
                                elif "fields" in inp:
                                    desc = f"{name} ({len(inp['fields'])} fields)"
                                elif "paths" in inp:
                                    desc = f"{name} upload"
                                else:
                                    desc = name

                                if visual_trace is not None:
                                    try:
                                        visual_trace.mark_action(desc)
                                    except Exception:
                                        logger.debug("visual trace action mark failed", exc_info=True)
                                lf.write(f"  >> {desc}\n")
                                if _print_tool_calls:
                                    elapsed_s = int(time.time() - start)
                                    print(f"  [W{worker_id} {elapsed_s:3d}s] >> {desc}", flush=True)
                                ws = get_state(worker_id)
                                cur_actions = ws.actions if ws else 0
                                update_state(worker_id,
                                             actions=cur_actions + 1,
                                             last_action=desc[:35])
                        if strict_snapshot_violation:
                            break
                    elif msg_type == "result":
                        stats = {
                            "input_tokens": msg.get("usage", {}).get("input_tokens", 0),
                            "output_tokens": msg.get("usage", {}).get("output_tokens", 0),
                            "cache_read": msg.get("usage", {}).get("cache_read_input_tokens", 0),
                            "cache_create": msg.get("usage", {}).get("cache_creation_input_tokens", 0),
                            "cost_usd": msg.get("total_cost_usd", 0),
                            "turns": msg.get("num_turns", 0),
                        }
                        # Reliability-v2 Phase A: persist token/cost telemetry
                        # on the job_meta side-channel so write_review_log can
                        # record it regardless of which return path run_job
                        # takes. Previously these were computed then discarded
                        # (only the live dashboard saw them) — making cost /
                        # cache-hit-rate / $-per-apply unmeasurable historically.
                        job_meta["telemetry"] = dict(stats)
                        text_parts.append(msg.get("result", ""))
                except json.JSONDecodeError:
                    text_parts.append(line)
                    lf.write(line + "\n")

        if strict_snapshot_violation:
            duration_ms = int((time.time() - start) * 1000)
            elapsed = int(time.time() - start)
            job_meta["failure_class"] = "transient_stream_snapshot_needed"
            add_event(f"[W{worker_id}] Blocked first-pass browser_snapshot; retrying with fallback ({elapsed}s)")
            update_state(worker_id, status="needs_review",
                         last_action=f"blocked snapshot ({elapsed}s)")
            _write_job_runtime_metadata(
                job["url"],
                last_failure_class=job_meta["failure_class"],
            )
            if proc is not None and proc.poll() is None:
                _kill_process_tree(proc.pid)
            reader_thread.join(timeout=2)
            return "needs_review:stream_snapshot_needed", duration_ms, prefill_status

        if timed_out:
            output = "\n".join(text_parts)
            transcript = _write_partial_transcript(job, worker_id, output, "timeout_partial")
            screenshot = _capture_browser_artifact(port, job, worker_id, "timeout")
            duration_ms = int((time.time() - start) * 1000)
            elapsed = int(time.time() - start)
            job_meta["failure_class"] = "transient_timeout"
            add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
            add_event(f"[W{worker_id}] Artifacts: {transcript.name}")
            if screenshot:
                add_event(f"[W{worker_id}] Screenshot: {screenshot.name}")
            update_state(worker_id, status="needs_review", last_action=f"TIMEOUT ({elapsed}s)")
            _write_job_runtime_metadata(
                job["url"],
                last_failure_class=job_meta["failure_class"],
            )
            if proc.poll() is None:
                _kill_process_tree(proc.pid)
            reader_thread.join(timeout=2)
            return "needs_review:timeout", duration_ms, prefill_status

        proc.wait(timeout=30)
        reader_thread.join(timeout=2)
        returncode = proc.returncode
        proc = None

        if returncode and returncode < 0:
            return "skipped", int((time.time() - start) * 1000), prefill_status

        output = "\n".join(text_parts)
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = config.LOG_DIR / f"claude_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(output, encoding="utf-8")

        if stats:
            cost = stats.get("cost_usd", 0)
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)

        if _is_claude_usage_limit(output):
            add_event(f"[W{worker_id}] Claude usage limit reached; pausing run")
            update_state(worker_id, status="needs_review",
                         last_action="Claude usage limit")
            job_meta["failure_class"] = "transient_rate_limited"
            _write_job_runtime_metadata(
                job["url"],
                last_failure_class=job_meta["failure_class"],
            )
            return "rate_limited:claude_usage_limit", duration_ms, prefill_status

        structured_result = _extract_structured_result(output)
        if structured_result:
            job_meta["apply_result_json"] = structured_result
            job_meta["failure_class"] = _classify_failure_class(
                result="failed" if structured_result.get("status") == "failed" else str(structured_result.get("status", "")),
                reason=_sanitize_reason(str(structured_result.get("reason", ""))),
                hint=str(structured_result.get("failure_class", "")).strip().lower() or None,
            )
            cp_stage = str(structured_result.get("checkpoint_stage", "")).strip().lower()
            if cp_stage:
                job_meta["checkpoint_stage"] = cp_stage
            if bool(structured_result.get("submit_attempted")):
                _write_job_runtime_metadata(
                    job["url"],
                    increment_submit_attempt=True,
                    checkpoint=_checkpoint(CHECKPOINT_SUBMIT_ATTEMPTED, source="structured_result"),
                )
                job_meta["checkpoint_stage"] = CHECKPOINT_SUBMIT_ATTEMPTED

        result_code = _result_from_structured(structured_result) if structured_result else None
        if not result_code and legacy_result_fallback:
            result_code = _extract_result_code(output)
        if not result_code:
            result_code = _infer_result_code_from_success_text(output)
        if not result_code and not allow_snapshot_first_pass and _needs_snapshot_fallback(output):
            job_meta["failure_class"] = "transient_stream_snapshot_needed"
            _write_job_runtime_metadata(
                job["url"],
                apply_result_json={"raw_output_truncated": output[-1000:]},
                last_failure_class=job_meta["failure_class"],
            )
            add_event(f"[W{worker_id}] Stream strict pass needs snapshot fallback ({elapsed}s)")
            update_state(worker_id, status="needs_review",
                         last_action=f"stream snapshot fallback ({elapsed}s)")
            # Strict pass blocked on tooling before any submit — clear the open
            # intent so the snapshot-enabled retry isn't blocked as dangling.
            _resolve_ledger_intent(ledger, identity_id, {"verified": False}, job_meta)
            return "needs_review:stream_snapshot_needed", duration_ms, prefill_status
        elif not result_code:
            fc, status_code = _classify_no_result(
                _stop_event.is_set(), "verification_missing_structured_result")
            job_meta["failure_class"] = fc
            _write_job_runtime_metadata(
                job["url"],
                apply_result_json={"raw_output_truncated": output[-1000:]},
                last_failure_class=job_meta["failure_class"],
            )
            add_event(f"[W{worker_id}] Missing structured result ({elapsed}s)")
            update_state(worker_id, status="needs_review",
                         last_action=f"missing structured result ({elapsed}s)")
            return status_code, duration_ms, prefill_status

        if result_code in {"applied", "expired", "captcha", "login_issue"}:
            if result_code == "applied" and not dry_run:
                _write_job_runtime_metadata(
                    job["url"],
                    increment_submit_attempt=True,
                    checkpoint=_checkpoint(CHECKPOINT_SUBMIT_ATTEMPTED, retry_count=retry_count),
                )
                job_meta["checkpoint_stage"] = CHECKPOINT_SUBMIT_ATTEMPTED
                verification = _verify_submission_success(
                    port,
                    verify_threshold=verify_threshold,
                    previous_url=apply_url,
                    browser_stream=browser_stream,
                )
                job_meta["verification_confidence"] = verification.get("confidence")
                job_meta["verification_evidence"] = verification
                _resolve_ledger_intent(ledger, identity_id, verification, job_meta)
                if not verification.get("verified"):
                    verify_log = config.LOG_DIR / f"verify_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.json"
                    verify_log.write_text(json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8")
                    add_event(f"[W{worker_id}] UNVERIFIED APPLY ({elapsed}s): {job['title'][:30]}")
                    job_meta["failure_class"] = "verification_unverified_submission"
                    _write_job_runtime_metadata(
                        job["url"],
                        apply_result_json=structured_result,
                        verification_evidence=verification,
                        verification_confidence=verification.get("confidence"),
                        last_failure_class=job_meta["failure_class"],
                    )
                    if escalation_mode == "skip":
                        update_state(worker_id, status="failed",
                                     last_action=f"unverified apply ({elapsed}s)")
                        return "failed:unverified_submission", duration_ms, prefill_status
                    update_state(worker_id, status="needs_review",
                                 last_action=f"unverified apply ({elapsed}s)")
                    return "needs_review:unverified_submission", duration_ms, prefill_status

                _write_job_runtime_metadata(
                    job["url"],
                    apply_result_json=structured_result,
                    verification_evidence=verification,
                    verification_confidence=verification.get("confidence"),
                    checkpoint=_checkpoint(CHECKPOINT_VERIFICATION_COMPLETE, confidence=verification.get("confidence")),
                )
                job_meta["checkpoint_stage"] = CHECKPOINT_VERIFICATION_COMPLETE

            status_label = result_code.upper()
            add_event(f"[W{worker_id}] {status_label} ({elapsed}s): {job['title'][:30]}")
            update_state(worker_id, status=result_code,
                         last_action=f"{status_label} ({elapsed}s)")
            if result_code != "applied":
                job_meta["failure_class"] = _classify_failure_class(result_code, result_code)
                _write_job_runtime_metadata(
                    job["url"],
                    apply_result_json=structured_result,
                    last_failure_class=job_meta["failure_class"],
                )
                # expired/captcha/login_issue: no confirmed submit — clear the
                # open intent so the identity isn't blocked as dangling.
                _resolve_ledger_intent(ledger, identity_id, {"verified": False}, job_meta)
            return result_code, duration_ms, prefill_status

        if result_code and result_code.startswith("failed:"):
            reason = result_code.split(":", 1)[1] or "unknown"
            failure_class = _classify_failure_class(result_code, reason, job_meta.get("failure_class"))
            job_meta["failure_class"] = failure_class
            # Agent reported a clean FAILED (no confirmed submit) — clear the
            # open intent so the job stays retryable.
            _resolve_ledger_intent(ledger, identity_id, {"verified": False}, job_meta)
            PROMOTE_TO_STATUS = {"captcha", "expired", "login_issue"}
            if reason in PROMOTE_TO_STATUS:
                add_event(f"[W{worker_id}] {reason.upper()} ({elapsed}s): {job['title'][:30]}")
                update_state(worker_id, status=reason,
                             last_action=f"{reason.upper()} ({elapsed}s)")
                _write_job_runtime_metadata(
                    job["url"],
                    apply_result_json=structured_result,
                    last_failure_class=failure_class,
                )
                return reason, duration_ms, prefill_status
            add_event(f"[W{worker_id}] FAILED ({elapsed}s): {reason[:30]}")
            update_state(worker_id, status="failed",
                         last_action=f"FAILED: {reason[:25]}")
            _write_job_runtime_metadata(
                job["url"],
                apply_result_json=structured_result,
                last_failure_class=failure_class,
            )
            return result_code, duration_ms, prefill_status

        add_event(f"[W{worker_id}] NO RESULT ({elapsed}s)")
        update_state(worker_id, status="needs_review", last_action=f"no result ({elapsed}s)")
        fc, status_code = _classify_no_result(
            _stop_event.is_set(), "transient_no_result_line")
        job_meta["failure_class"] = fc
        _write_job_runtime_metadata(
            job["url"],
            apply_result_json=structured_result,
            last_failure_class=job_meta["failure_class"],
        )
        return status_code, duration_ms, prefill_status

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)
        job_meta["failure_class"] = "transient_timeout"
        if dry_run:
            ready_state = (
                _check_greenhouse_submit_ready(port, browser_stream=browser_stream)
                if prefill_status and prefill_status.get("ats") == "greenhouse"
                else {}
            )
            if prefill_status is not None and ready_state:
                prefill_status["submit_ready_after_agent"] = ready_state
            if ready_state.get("ready"):
                screenshot = _capture_browser_artifact(port, job, worker_id, "dry_run_ready_after_timeout")
                if screenshot:
                    add_event(f"[W{worker_id}] Dry-run ready screenshot: {screenshot.name}")
                add_event(f"[W{worker_id}] TIMEOUT after submit-ready dry-run ({elapsed}s)")
                update_state(worker_id, status="done", last_action=f"dry-run ready ({elapsed}s)")
                return "applied", duration_ms, prefill_status
        add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
        update_state(worker_id, status="needs_review", last_action=f"TIMEOUT ({elapsed}s)")
        _write_job_runtime_metadata(
            job["url"],
            last_failure_class=job_meta["failure_class"],
        )
        return "needs_review:timeout", duration_ms, prefill_status
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
        update_state(worker_id, status="failed", last_action=f"ERROR: {str(e)[:25]}")
        job_meta["failure_class"] = "transient_exception"
        _write_job_runtime_metadata(
            job["url"],
            last_failure_class=job_meta["failure_class"],
        )
        return f"failed:{str(e)[:100]}", duration_ms, prefill_status
    finally:
        with _claude_lock:
            _claude_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired", "captcha", "login_issue",
    "not_eligible_location", "not_eligible_salary",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "sso_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
    "candidate_profile_only", "apply_button_not_found",
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")

NEEDS_REVIEW_REASONS: set[str] = {
    "timeout",
    "no_result_line",
    "browser_unavailable",
    "interrupted",
    "email_verification_required",
    "phone_country_validation",
    "possible_duplicate_guard",
    "unverified_submission",
    "legal_attestation_ambiguous",
    "stream_snapshot_needed",
}


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


def _classify_apply_result(result: str) -> tuple[str, str | None, bool]:
    """Map an agent result string to (db_status, reason, permanent)."""
    if result == "applied":
        return "applied", None, False

    reason = result.split(":", 1)[-1] if ":" in result else result
    if result.startswith("needs_review:") or reason in NEEDS_REVIEW_REASONS:
        return "needs_review", reason, False

    return "failed", reason, _is_permanent_failure(result)


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 8, headless: bool = False,
                model: str = "sonnet", dry_run: bool = False,
                job_timeout: int | None = None,
                verify_threshold: float = 0.75,
                max_transient_retries: int = 2,
                navigation_timeout: int = 45,
                interaction_timeout: int = 20,
                assert_timeout: int = 15,
                escalation_mode: str = "pause",
                legacy_result_fallback: bool = True,
                startup_stagger: float = 0.0,
                max_age_hours: int | None = None,
                site_contains: str | None = None) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Claude model name.
        dry_run: Don't click Submit.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id
    if startup_stagger > 0 and worker_id > 0:
        delay = startup_stagger * worker_id
        add_event(f"[W{worker_id}] Staggering startup {delay:.1f}s")
        if _stop_event.wait(timeout=delay):
            update_state(worker_id, status="done", last_action="stopped")
            return applied, failed

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        # Single spend enforcement point: pause + stop dispatch when over cap.
        # Resume path (`applypilot resume`) clears engine_control.paused.
        from applypilot import database as db
        from applypilot.spend_ledger import SpendLedger
        _led = SpendLedger(config.SPEND_LEDGER_PATH,
                           daily_cap_usd=config.DEFAULTS["daily_budget_usd"],
                           monthly_cap_usd=config.DEFAULTS["monthly_budget_usd"])
        if _led.over_cap():
            db.set_paused(db.get_connection(), "budget")
            logger.warning("Paused: spend cap reached (today=$%.2f, month=$%.2f). Raise the cap or resume.",
                           _led.spent_today(), _led.spent_month())
            break   # stop dispatching this worker; resume path clears engine_control.paused

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id, max_age_hours=max_age_hours,
                          site_contains=site_contains)
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0

        chrome_proc = None
        browser_stream = None
        visual_trace = None
        attempt = 0
        total_duration_ms = 0
        result = "failed:unknown"
        prefill_status: dict | None = None
        while True:
            try:
                add_event(f"[W{worker_id}] Launching Chrome...")
                _wait_for_resources(worker_id)
                if _stop_event.is_set():
                    release_lock(job["url"])
                    break
                chrome_proc = launch_chrome(worker_id, port=port, headless=headless)
                stream_telemetry_enabled = os.environ.get(
                    "APPLYPILOT_STREAM_TELEMETRY",
                    "0",
                ).strip().lower() in {"1", "true", "yes", "on"}
                # Submit broker + job identity for CDP network containment. The
                # broker is owned by worker_loop, keyed by identity_id, and lives
                # in the stream so its context route can block ATS submit POSTs
                # (dry-run fails closed) and consume the one-shot ticket on submit.
                from applypilot.apply.submit_broker import SubmitBroker
                from applypilot.identity import identity_id as _identity_id
                ident = _identity_id(job["url"], company=job.get("site"),
                                     title=job.get("title"), location=job.get("location"))
                broker = SubmitBroker(
                    config.APP_DIR / f".submit-ticket-{worker_id}.json", dry_run=dry_run)
                browser_stream = BrowserStateStream(
                    port,
                    poll_interval_s=0.75,
                    telemetry_path=(
                        config.LOG_DIR / f"browser_stream_{_artifact_stem(job, worker_id, 'state')}.jsonl"
                        if stream_telemetry_enabled
                        else None
                    ),
                    broker=broker,
                    identity_id=ident,
                    dry_run=dry_run,
                ).start()
                visual_trace_enabled = os.environ.get(
                    "APPLYPILOT_VISUAL_TRACE",
                    "0",
                ).strip().lower() in {"1", "true", "yes", "on"}
                if visual_trace_enabled:
                    visual_trace = VisualTraceRecorder(
                        port,
                        config.LOG_DIR / f"visual_trace_{_artifact_stem(job, worker_id, 'frames')}",
                        interval_s=float(os.environ.get("APPLYPILOT_VISUAL_TRACE_INTERVAL", "2.0")),
                        max_frames=int(os.environ.get("APPLYPILOT_VISUAL_TRACE_MAX_FRAMES", "240")),
                    ).start()
                    add_event(f"[W{worker_id}] Visual trace: {visual_trace.index_path}")

                # Phase 4: route through the skill-playbook dispatcher.
                # When APPLYPILOT_USE_SKILLS is unset/0 the dispatcher is a
                # pure passthrough — identical call to run_job, identical args.
                from applypilot.apply.skill_runner import dispatch_apply
                result, duration_ms, prefill_status = dispatch_apply(
                    job=job, port=port, worker_id=worker_id,
                    model=model, dry_run=dry_run,
                    verify_threshold=verify_threshold if verify_threshold is not None
                                       else float(config.DEFAULTS["verify_threshold"]),
                    run_job_fn=run_job,
                    run_job_kwargs={
                        "model": model, "dry_run": dry_run,
                        "job_timeout": job_timeout,
                        "verify_threshold": verify_threshold,
                        "navigation_timeout": navigation_timeout,
                        "interaction_timeout": interaction_timeout,
                        "assert_timeout": assert_timeout,
                        "escalation_mode": escalation_mode,
                        "legacy_result_fallback": legacy_result_fallback,
                        "retry_count": attempt,
                        "visual_trace": visual_trace,
                        "broker": broker,
                        "identity_id": ident,
                    },
                    browser_stream=browser_stream,
                )
                total_duration_ms += duration_ms
            finally:
                if visual_trace is not None:
                    visual_trace.close()
                    visual_trace = None
                if browser_stream is not None:
                    browser_stream.close()
                    browser_stream = None
                if chrome_proc:
                    cleanup_worker(worker_id, chrome_proc)
                    chrome_proc = None

            if result.startswith("rate_limited:"):
                break

            meta = job.get("_run_meta", {})
            reason = result.split(":", 1)[-1] if ":" in result else result
            if result == "applied":
                failure_class = None
                meta.pop("failure_class", None)
            else:
                failure_class = meta.get("failure_class") or _classify_failure_class(result, reason)
                meta["failure_class"] = failure_class

            # BUG FIX (Phase 6, iter 11): never retry a result that's already
            # terminal — success or permanent failure. Previously, result="applied"
            # had no specific reason matcher in _classify_failure_class, so it fell
            # through to the "transient_unknown" fallback, which made should_retry
            # True for SUCCESSFUL applies — producing duplicate submissions to the
            # employer. Explicitly exclude these terminal results from the retry gate.
            is_terminal_result = (
                result in {"applied", "expired", "captcha", "login_issue"}
                or _is_permanent_failure(result)
            )

            # iter-13 money-saver: a timeout means the form did NOT finish
            # within the FULL job_timeout budget. Retrying with the same
            # budget on the same (deterministically heavy) form near-always
            # times out again — observed live: sofi ran 1444s = 2x720s, both
            # timed out, ~12min + cost wasted for a guaranteed-failed retry.
            # Treat timeout as non-retryable: take the one full attempt, mark
            # needs_review, move on. (A genuinely transient slow-network
            # timeout is rare vs. "form too heavy"; not worth 2x the budget.)
            # no_result_line may hide a real submit/manual intervention where
            # the agent failed to report cleanly. Retrying the same role can
            # duplicate the application, so stop and mark needs_review.
            NON_RETRYABLE_TRANSIENT = {"transient_timeout", "transient_no_result_line"}

            should_retry = (
                not dry_run
                and not is_terminal_result
                and failure_class.startswith(TRANSIENT_FAILURE_PREFIXES)
                and failure_class not in NON_RETRYABLE_TRANSIENT
                and not result.startswith("needs_review:possible_duplicate_guard")
                and attempt < max_transient_retries
            )
            if not should_retry:
                break

            attempt += 1
            meta["retry_count"] = attempt
            backoff = min(10.0, 2 ** attempt) + random.uniform(0.0, 0.5)
            add_event(f"[W{worker_id}] Transient failure ({reason[:25]}), retry {attempt}/{max_transient_retries} in {backoff:.1f}s")
            _write_job_runtime_metadata(
                job["url"],
                last_failure_class=failure_class,
                checkpoint=_checkpoint(CHECKPOINT_PAGE_REACHED, retry_count=attempt),
            )
            if _stop_event.wait(timeout=backoff):
                break

        if _stop_event.is_set() and total_duration_ms == 0 and result == "failed:unknown":
            release_lock(job["url"])
            break

        try:
            meta = job.get("_run_meta", {})
            failure_class = meta.get("failure_class")
            retry_count_value = int(meta.get("retry_count") or attempt)
            verification_confidence = meta.get("verification_confidence")
            idempotency_key = meta.get("idempotency_key")
            checkpoint_stage = meta.get("checkpoint_stage")

            # Phase 4: persist skill-playbook telemetry if the dispatcher
            # tucked any onto prefill_status. NULL when the legacy run_job
            # path ran with the feature flag off, so existing rows / flows
            # see no schema or behavior change.
            if isinstance(prefill_status, dict):
                _write_job_runtime_metadata(
                    job["url"],
                    skill_used=prefill_status.get("skill_used"),
                    replay_duration_ms=prefill_status.get("replay_duration_ms"),
                    patch_duration_ms=prefill_status.get("patch_duration_ms"),
                )

            if result == "skipped":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                write_review_log(job, "skipped", model, total_duration_ms, dry_run,
                                 worker_id=worker_id, prefill_status=prefill_status,
                                 failure_class=failure_class,
                                 retry_count=retry_count_value,
                                 verification_confidence=verification_confidence,
                                 idempotency_key=idempotency_key,
                                 checkpoint_stage=checkpoint_stage)
                continue
            if result.startswith("rate_limited:"):
                release_lock(job["url"])
                reason = result.split(":", 1)[-1]
                write_review_log(job, "paused", model, total_duration_ms, dry_run,
                                 error=reason, worker_id=worker_id,
                                 prefill_status=prefill_status,
                                 failure_class=failure_class or "transient_rate_limited",
                                 retry_count=retry_count_value,
                                 verification_confidence=verification_confidence,
                                 idempotency_key=idempotency_key,
                                 checkpoint_stage=checkpoint_stage)
                add_event(f"[W{worker_id}] Pausing queue: {reason}")
                _stop_event.set()
                break
            if dry_run:
                release_lock(job["url"])
                update_state(worker_id, status="done",
                             last_action=f"dry-run: {result[:25]}")
                write_review_log(job, f"dry_run:{result}", model,
                                 total_duration_ms, dry_run, worker_id=worker_id,
                                 prefill_status=prefill_status,
                                 failure_class=failure_class,
                                 retry_count=retry_count_value,
                                 verification_confidence=verification_confidence,
                                 idempotency_key=idempotency_key,
                                 checkpoint_stage=checkpoint_stage)
            elif result == "applied":
                mark_result(job["url"], "applied", duration_ms=total_duration_ms)
                _write_job_runtime_metadata(
                    job["url"],
                    apply_result_json=meta.get("apply_result_json"),
                    verification_evidence=meta.get("verification_evidence"),
                    verification_confidence=verification_confidence,
                    idempotency_key=idempotency_key,
                    checkpoint=_checkpoint(CHECKPOINT_VERIFICATION_COMPLETE, retry_count=retry_count_value),
                )
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
                write_review_log(job, "applied", model, total_duration_ms, dry_run,
                                 worker_id=worker_id, prefill_status=prefill_status,
                                 failure_class=failure_class,
                                 retry_count=retry_count_value,
                                 verification_confidence=verification_confidence,
                                 idempotency_key=idempotency_key,
                                 checkpoint_stage=CHECKPOINT_VERIFICATION_COMPLETE)
            else:
                db_status, reason, permanent = _classify_apply_result(result)
                if escalation_mode == "skip" and db_status == "needs_review":
                    db_status = "failed"
                mark_result(job["url"], db_status, reason,
                            permanent=permanent,
                            duration_ms=total_duration_ms)
                _write_job_runtime_metadata(
                    job["url"],
                    apply_result_json=meta.get("apply_result_json"),
                    verification_evidence=meta.get("verification_evidence"),
                    verification_confidence=verification_confidence,
                    idempotency_key=idempotency_key,
                    last_failure_class=failure_class or _classify_failure_class(result, reason),
                    checkpoint=_checkpoint(meta.get("checkpoint_stage") or CHECKPOINT_PAGE_REACHED, retry_count=retry_count_value),
                )
                failed += 1
                update_state(worker_id, status=db_status,
                             last_action=f"{db_status}: {(reason or '')[:20]}")
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)
                write_review_log(job, db_status, model, total_duration_ms, dry_run,
                                 error=reason, worker_id=worker_id,
                                 prefill_status=prefill_status,
                                 failure_class=failure_class,
                                 retry_count=retry_count_value,
                                 verification_confidence=verification_confidence,
                                 idempotency_key=idempotency_key,
                                 checkpoint_stage=checkpoint_stage)

        except KeyboardInterrupt:
            release_lock(job["url"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            release_lock(job["url"])
            failed += 1
            update_state(worker_id, jobs_failed=failed)

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 8, headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1,
         no_live: bool = False, job_timeout: int | None = None,
         verify_threshold: float | None = None,
         max_transient_retries: int | None = None,
         navigation_timeout: int | None = None,
         interaction_timeout: int | None = None,
         assert_timeout: int | None = None,
         escalation_mode: str | None = None,
         legacy_result_fallback: bool | None = None,
         startup_stagger: float = 4.0,
         max_age_hours: int | None = None,
         site_contains: str | None = None) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Claude model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
        no_live: Disable the Rich live dashboard.
        job_timeout: Max seconds per Claude-driven application.
        startup_stagger: Seconds to delay each additional worker launch.
    """
    global POLL_INTERVAL, _print_tool_calls
    POLL_INTERVAL = poll_interval
    _print_tool_calls = no_live  # mirror Sonnet tool calls to stdout in no-live
    _stop_event.clear()
    with _run_seen_lock:
        _run_seen_urls.clear()
        _run_seen_sites.clear()
    if job_timeout is None:
        job_timeout = config.DEFAULTS["apply_timeout"]

    config.ensure_dirs()
    console = Console()
    reset_count = reset_stale_in_progress(max(job_timeout + 120, 600))
    if reset_count:
        console.print(f"[yellow]Reset {reset_count} stale in-progress job lock(s).[/yellow]")

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    console.print(f"[dim]Job timeout: {job_timeout}s | Startup stagger: {startup_stagger:.1f}s | Live UI: {not no_live}[/dim]")
    if workers > 1 and not headless:
        console.print("[yellow]Visible multi-worker mode is heavy. Use --headless or --workers auto for safer throughput.[/yellow]")
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Kill all active Claude processes to skip current jobs
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    def _run_workers() -> tuple[int, int]:
        if workers == 1:
            return worker_loop(
                worker_id=0,
                limit=effective_limit,
                target_url=target_url,
                min_score=min_score,
                max_age_hours=max_age_hours,
                headless=headless,
                model=model,
                dry_run=dry_run,
                job_timeout=job_timeout,
                verify_threshold=verify_threshold,
                max_transient_retries=max_transient_retries,
                navigation_timeout=navigation_timeout,
                interaction_timeout=interaction_timeout,
                assert_timeout=assert_timeout,
                escalation_mode=escalation_mode,
                legacy_result_fallback=legacy_result_fallback,
                startup_stagger=startup_stagger,
                site_contains=site_contains,
            )

        if effective_limit:
            base = effective_limit // workers
            extra = effective_limit % workers
            limits = [base + (1 if i < extra else 0) for i in range(workers)]
        else:
            limits = [0] * workers

        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="apply-worker") as executor:
            futures = {
                executor.submit(
                    worker_loop,
                    worker_id=i,
                    limit=limits[i],
                    target_url=target_url,
                    min_score=min_score,
                    max_age_hours=max_age_hours,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                    job_timeout=job_timeout,
                    verify_threshold=verify_threshold,
                    max_transient_retries=max_transient_retries,
                    navigation_timeout=navigation_timeout,
                    interaction_timeout=interaction_timeout,
                    assert_timeout=assert_timeout,
                    escalation_mode=escalation_mode,
                    legacy_result_fallback=legacy_result_fallback,
                    startup_stagger=startup_stagger,
                    site_contains=site_contains,
                ): i
                for i in range(workers)
            }

            results: list[tuple[int, int]] = []
            for future in as_completed(futures):
                wid = futures[future]
                try:
                    results.append(future.result())
                except Exception:
                    logger.exception("Worker %d crashed", wid)
                    results.append((0, 0))

        return sum(r[0] for r in results), sum(r[1] for r in results)

    if no_live:
        try:
            total_applied, total_failed = _run_workers()
            totals = get_totals()
            console.print(
                f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
                f"(${totals['cost']:.3f})[/bold]"
            )
            console.print(f"Logs: {config.LOG_DIR}")
        except KeyboardInterrupt:
            pass
        finally:
            _stop_event.set()
            kill_all_chrome()
        return

    try:
        with Live(render_full(), console=console, auto_refresh=False) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    wait_for_change(timeout=1.0)
                    live.update(render_full(), refresh=True)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                # Single worker â€” run directly in main thread
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    max_age_hours=max_age_hours,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                    job_timeout=job_timeout,
                    verify_threshold=verify_threshold,
                    max_transient_retries=max_transient_retries,
                    navigation_timeout=navigation_timeout,
                    interaction_timeout=interaction_timeout,
                    assert_timeout=assert_timeout,
                    escalation_mode=escalation_mode,
                    legacy_result_fallback=legacy_result_fallback,
                    startup_stagger=startup_stagger,
                    site_contains=site_contains,
                )
            else:
                # Multi-worker â€” distribute limit across workers
                if effective_limit:
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0)
                              for i in range(workers)]
                else:
                    limits = [0] * workers  # continuous mode

                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=limits[i],
                            target_url=target_url,
                            min_score=min_score,
                            max_age_hours=max_age_hours,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                            job_timeout=job_timeout,
                            verify_threshold=verify_threshold,
                            max_transient_retries=max_transient_retries,
                            navigation_timeout=navigation_timeout,
                            interaction_timeout=interaction_timeout,
                            assert_timeout=assert_timeout,
                            escalation_mode=escalation_mode,
                            legacy_result_fallback=legacy_result_fallback,
                            startup_stagger=startup_stagger,
                            site_contains=site_contains,
                        ): i
                        for i in range(workers)
                    }

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            wait_for_change(timeout=0)
            refresh_thread.join(timeout=2)
            live.update(render_full(), refresh=True)

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()

