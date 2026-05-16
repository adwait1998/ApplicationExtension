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
from applypilot.apply import chrome, dashboard, prompt as prompt_mod
from applypilot.apply.prefill import prefill_application
from applypilot.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome,
    reset_worker_dir, cleanup_on_exit, _kill_process_tree,
    BASE_CDP_PORT,
)
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
_run_seen_urls: set[str] = set()
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

def _make_mcp_config(cdp_port: int) -> dict:
    """Build MCP config dict for a specific CDP port."""
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


def _allowed_tools_arg() -> str:
    """Restrict Claude to the browser MCP and read/send-only Gmail tools."""
    return ",".join([
        "mcp__playwright__browser_navigate",
        "mcp__playwright__browser_snapshot",
        "mcp__playwright__browser_click",
        "mcp__playwright__browser_type",
        "mcp__playwright__browser_fill_form",
        "mcp__playwright__browser_file_upload",
        "mcp__playwright__browser_evaluate",
        "mcp__playwright__browser_take_screenshot",
        "mcp__playwright__browser_tabs",
        "mcp__playwright__browser_press_key",
        "mcp__playwright__browser_hover",
        "mcp__playwright__browser_select_option",
        "mcp__playwright__browser_console_messages",
        "mcp__gmail__search_emails",
        "mcp__gmail__read_email",
        "mcp__gmail__send_email",
    ])


def _is_claude_usage_limit(text: str) -> bool:
    """Detect Claude CLI account/usage-limit responses in plain or JSON text."""
    lower = text.lower()
    return any(pattern in lower for pattern in CLAUDE_LIMIT_PATTERNS)


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


def _compute_idempotency_key(job: dict, profile: dict | None = None) -> str:
    """Build a stable idempotency key for this candidate+job submission intent."""
    apply_url = job.get("application_url") or job.get("url") or ""
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
    if r in {"unsafe_permissions", "unsafe_verification", "not_a_job_application", "sso_required"}:
        return f"policy_{r}"
    if r in {"captcha", "email_verification_required", "login_issue"}:
        return f"blocker_{r}"
    if r in {"unverified_submission", "possible_duplicate_guard"}:
        return f"verification_{r}"
    if r in {"form_validation_error", "resume_upload_failed", "phone_country_validation"}:
        return f"validation_{r}"
    if r in {"timeout", "no_result_line", "browser_unavailable", "interrupted", "rate_limited", "stuck", "page_error"}:
        return f"transient_{r}"
    if result.startswith("needs_review:"):
        return f"verification_{r or 'needs_review'}"
    if result.startswith("failed:"):
        return f"validation_{r or 'failed'}"
    return "transient_unknown"


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


def _check_greenhouse_submit_ready(cdp_port: int) -> dict:
    """Inspect the active Greenhouse form and report whether required fields are filled."""
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


def _validate_required_fields(cdp_port: int) -> dict:
    """Cross-ATS required field validator for pre/post submit checks."""
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


def _verify_submission_success(cdp_port: int, verify_threshold: float = 0.75,
                               previous_url: str | None = None) -> dict:
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
        required_state = _validate_required_fields(cdp_port)
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

def acquire_job(target_url: str | None = None, min_score: int = 8,
                worker_id: int = 0, max_age_hours: int | None = None) -> dict | None:
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
            url_clauses = ""
            if blocked_patterns:
                url_clauses = " ".join(f"AND url NOT LIKE ?" for _ in blocked_patterns)
                params.extend(blocked_patterns)
            # Fetch a batch so we can skip manual_ats jobs in one pass instead
            # of returning None and making the worker think the queue is empty.
            #
            # Round-robin across sites within each fit_score tier so we don't
            # stack 9 Stripe applies in a row. Greenhouse fraud-detection is
            # per-tenant; rapid same-company applies trigger email verification.
            # ROW_NUMBER() partitioned by site assigns each job an "Nth pick
            # from this site" index in random order; the outer ORDER BY then
            # interleaves: round 1 of every site, round 2, etc.
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
                       fit_score, location, full_description, cover_letter_path
                FROM (
                    SELECT url, title, site, application_url, tailored_resume_path,
                           fit_score, location, full_description, cover_letter_path,
                           ROW_NUMBER() OVER (
                               PARTITION BY site ORDER BY RANDOM()
                           ) AS site_idx
                    FROM (
                        SELECT url, title, site, application_url, tailored_resume_path,
                               fit_score, location, full_description, cover_letter_path,
                               ROW_NUMBER() OVER (
                                   PARTITION BY site, LOWER(TRIM(title))
                                   ORDER BY
                                     CASE WHEN application_url LIKE '%greenhouse%'
                                            OR application_url LIKE '%lever.co%'
                                            OR application_url LIKE '%ashby%'
                                            OR application_url LIKE '%myworkdayjobs%'
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
                          {url_clauses}
                    )
                    WHERE dup_rn = 1
                )
                ORDER BY fit_score DESC, site_idx ASC, RANDOM()
                LIMIT 50
            """, [config.DEFAULTS["max_apply_attempts"]] + params).fetchall()

        # Walk the batch: mark manual_ats hits and pick the first applyable.
        row = None
        for candidate in rows:
            with _run_seen_lock:
                if candidate["url"] in _run_seen_urls:
                    continue
            apply_url = candidate["application_url"] or candidate["url"]
            if is_manual_ats(apply_url):
                conn.execute(
                    "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                    (candidate["url"],),
                )
                logger.info("Skipping manual ATS: %s", candidate["url"][:80])
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
        conn.commit()

        return dict(row)
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
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "worker_id": worker_id,
        "job_url": job.get("url"),
        "apply_url": job.get("application_url") or job.get("url"),
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
            recorder=None) -> tuple[str, int, dict | None]:
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
    apply_url = job.get("application_url") or job.get("url") or ""
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

    # Write per-worker MCP config
    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_config_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

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
            apply_url=job.get("application_url") or job["url"],
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
    logger.info(
        "prefill: ats=%s filled=%s err=%s dur=%dms",
        prefill_status["ats"], prefill_status["fields_filled"],
        prefill_status["error"], prefill_status["duration_ms"],
    )
    add_event(f"[W{worker_id}] Pre-filled {len(prefill_status['fields_filled'])} fields ({prefill_status['ats']})")
    if "resume" in (prefill_status.get("fields_filled") or []):
        _write_job_runtime_metadata(
            job["url"],
            checkpoint=_checkpoint(CHECKPOINT_RESUME_UPLOADED, ats=prefill_status.get("ats")),
        )
        job_meta["checkpoint_stage"] = CHECKPOINT_RESUME_UPLOADED

    if dry_run and prefill_status.get("ats") == "greenhouse" and not prefill_status.get("error"):
        time.sleep(1.5)
        ready_state = _check_greenhouse_submit_ready(port)
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
        return f"needs_review:needs_human_{_blocker}", duration_ms, prefill_status

    # Build the prompt (after prefill so we can tell the agent what was already filled)
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
        prefill_status=prefill_status,
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
        "--disallowedTools", (
            "Task,WebFetch,WebSearch,TodoWrite,Read,Write,Edit,MultiEdit,"
            "NotebookRead,NotebookEdit,Bash,PowerShell,Glob,Grep,LS,"
            "mcp__playwright__browser_run_code_unsafe,"
            "mcp__playwright__browser_wait_for,"
            "mcp__gmail__draft_email,mcp__gmail__modify_email,"
            "mcp__gmail__delete_email,mcp__gmail__download_attachment,"
            "mcp__gmail__batch_modify_emails,mcp__gmail__batch_delete_emails,"
            "mcp__gmail__create_label,mcp__gmail__update_label,"
            "mcp__gmail__delete_label,mcp__gmail__get_or_create_label,"
            "mcp__gmail__list_email_labels,mcp__gmail__create_filter,"
            "mcp__gmail__list_filters,mcp__gmail__get_filter,"
            "mcp__gmail__delete_filter"
        ),
        "--output-format", "stream-json",
        "--verbose", "-",
    ]
    if _claude_supports_allowed_tools(claude_bin):
        cmd[6:6] = ["--allowedTools", _allowed_tools_arg()]

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
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    stats: dict = {}
    proc = None

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

                                lf.write(f"  >> {desc}\n")
                                if _print_tool_calls:
                                    elapsed_s = int(time.time() - start)
                                    print(f"  [W{worker_id} {elapsed_s:3d}s] >> {desc}", flush=True)
                                ws = get_state(worker_id)
                                cur_actions = ws.actions if ws else 0
                                update_state(worker_id,
                                             actions=cur_actions + 1,
                                             last_action=desc[:35])
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
        elif not result_code:
            job_meta["failure_class"] = "verification_missing_structured_result"
            _write_job_runtime_metadata(
                job["url"],
                apply_result_json={"raw_output_truncated": output[-1000:]},
                last_failure_class=job_meta["failure_class"],
            )
            add_event(f"[W{worker_id}] Missing structured result ({elapsed}s)")
            update_state(worker_id, status="needs_review",
                         last_action=f"missing structured result ({elapsed}s)")
            return "needs_review:no_result_line", duration_ms, prefill_status

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
                )
                job_meta["verification_confidence"] = verification.get("confidence")
                job_meta["verification_evidence"] = verification
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
            return result_code, duration_ms, prefill_status

        if result_code and result_code.startswith("failed:"):
            reason = result_code.split(":", 1)[1] or "unknown"
            failure_class = _classify_failure_class(result_code, reason, job_meta.get("failure_class"))
            job_meta["failure_class"] = failure_class
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
        job_meta["failure_class"] = "transient_no_result_line"
        _write_job_runtime_metadata(
            job["url"],
            apply_result_json=structured_result,
            last_failure_class=job_meta["failure_class"],
        )
        return "needs_review:no_result_line", duration_ms, prefill_status

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)
        job_meta["failure_class"] = "transient_timeout"
        if dry_run:
            ready_state = _check_greenhouse_submit_ready(port) if prefill_status and prefill_status.get("ats") == "greenhouse" else {}
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
                max_age_hours: int | None = None) -> tuple[int, int]:
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

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id, max_age_hours=max_age_hours)
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
                    },
                )
                total_duration_ms += duration_ms
            finally:
                if chrome_proc:
                    cleanup_worker(worker_id, chrome_proc)
                    chrome_proc = None

            if result.startswith("rate_limited:"):
                break

            meta = job.get("_run_meta", {})
            reason = result.split(":", 1)[-1] if ":" in result else result
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
            NON_RETRYABLE_TRANSIENT = {"transient_timeout"}

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
         max_age_hours: int | None = None) -> None:
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

