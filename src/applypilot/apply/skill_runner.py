"""Phase 4 launcher integration for the Skill Playbook.

This module is the *dispatcher* that sits between worker_loop and run_job
when `APPLYPILOT_USE_SKILLS=1`. Three responsibilities:

  1. Resolve a saved skill for the current job's company, if any.
  2. If a skill exists, run Tier 1 replay (+ Tier 2 patch + submit + verify)
     entirely in-process — no Claude Code subprocess for the deterministic 80%.
  3. If no skill exists (or the saved one drifted), fall through to the
     existing run_job flow with a SkillRecorder attached so the next apply
     to this company is Tier 1.

With the env var OFF, dispatch_apply is a trivial passthrough to run_job —
NO skill lookup, NO recorder, byte-for-byte the same call sequence as before.
That property is the Phase 4 done-when criterion.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from applypilot import config
from applypilot.apply import replay as replay_mod
from applypilot.apply.recorder import SkillRecorder
from applypilot.apply.skill_schema import Skill, SkillValidationError, load_skill

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Feature flag + skill directory layout
# ---------------------------------------------------------------------------

FEATURE_FLAG_ENV = "APPLYPILOT_USE_SKILLS"

# Skills live next to profile.json / resume.pdf in the user data directory so
# they survive package upgrades. Per-company filename = `<company>.yaml`.
def skills_dir() -> Path:
    return config.APP_DIR / "skills"


def archive_dir() -> Path:
    return skills_dir() / "_archive"


def is_skill_flow_enabled() -> bool:
    """Read the feature flag fresh each call so tests / env edits take effect."""
    raw = (os.environ.get(FEATURE_FLAG_ENV) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def normalize_company_key(site: str | None) -> str:
    """Derive a stable filesystem-safe slug from a job's `site` field.

    The launcher's `job["site"]` values look like 'figma (greenhouse)',
    'figma', 'stripe (lever)'. We keep just the first token before any
    parenthetical ATS tag and lowercase / slugify it. Empty input → "".
    """
    if not site:
        return ""
    head = site.split("(")[0].strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", head).strip("_")
    return slug


def _slugify_company(value: str | None) -> str:
    if not value:
        return ""
    text = unquote(value).strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _company_from_apply_url(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    parts = [unquote(p) for p in parsed.path.split("/") if p]

    if "greenhouse.io" in host and parts:
        return _slugify_company(parts[0])
    if "lever.co" in host and parts:
        return _slugify_company(parts[0])
    if "ashbyhq.com" in host and parts:
        return _slugify_company(parts[0])
    if "myworkdayjobs.com" in host:
        return _slugify_company(host.split(".", 1)[0])
    if host.startswith("careers.") and len(host.split(".")) >= 3:
        return _slugify_company(host.split(".")[1])
    if host.startswith("jobs.") and len(host.split(".")) >= 3:
        return _slugify_company(host.split(".")[1])
    labels = [p for p in host.split(".") if p and p not in {"www", "careers", "jobs", "company"}]
    return _slugify_company(labels[0]) if labels else ""


def company_key_for_job(job: dict) -> str:
    """Return the skill namespace for a job.

    Source sites like LinkedIn and Indeed are not company identities. When a
    job has a resolved outbound URL, use that destination company so one
    LinkedIn-resolved Ashby recording does not apply to unrelated LinkedIn jobs.
    """
    site_key = normalize_company_key(job.get("site"))
    if site_key in {"linkedin", "indeed", "glassdoor", "ziprecruiter", "google"}:
        return _company_from_apply_url(job.get("application_url")) or site_key
    return site_key or _company_from_apply_url(job.get("application_url"))


def skill_path(company: str) -> Path:
    """Where the YAML for one company lives."""
    return skills_dir() / f"{company}.yaml"


def resolve_skill(company: str) -> Skill | None:
    """Return the saved Skill for this company, or None if absent/malformed.

    A malformed file is logged but treated as "no skill" — we fall through
    to record mode rather than crash the apply. The malformed file is NOT
    archived here; we leave that to the user / a separate housekeeping step.
    """
    if not company:
        return None
    p = skill_path(company)
    if not p.exists():
        return None
    try:
        return load_skill(p)
    except SkillValidationError as e:
        log.warning("skill %s malformed (%s); falling through to record mode", p, e)
        return None


def verify_skill_integrity(skill: Skill) -> tuple[bool, str | None]:
    """Cheap pre-flight check that runs WITHOUT launching Chrome.

    Catches the "manual hash mutation" drift signal from the spec's
    Validation criterion #4: if a user (or a recording bug) edits
    `form_layout_hash` to a value that doesn't match the hash of the
    recorded `required_selectors`, the skill is structurally inconsistent
    and should be archived before we waste a Chrome launch on it.

    Returns (ok, reason). On (False, reason) the dispatcher archives the
    file and routes to record mode.
    """
    if not skill.form_layout_hash:
        # No hash to verify against — treat as clean and let in-replay
        # drift checks (missing required selectors) catch real-DOM drift.
        return True, None
    expected = replay_mod.form_layout_hash(skill.required_selectors)
    if skill.form_layout_hash != expected:
        return False, (
            f"form_layout_hash mismatch: recorded={skill.form_layout_hash[:18]}... "
            f"expected_for_required_selectors={expected[:18]}..."
        )
    return True, None


def archive_stale_skill(company: str) -> Path | None:
    """Move a drifted skill out of the active dir so the next apply re-records.

    File goes to `skills/_archive/<company>.<utc-ts>.yaml`. Returns the new
    path, or None if the source didn't exist.
    """
    src = skill_path(company)
    if not src.exists():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dst = archive_dir() / f"{company}.{ts}.yaml"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    log.info("archived stale skill %s -> %s", src, dst)
    return dst


# ---------------------------------------------------------------------------
# Tier 1 + Tier 2 + submit + verify orchestration
# ---------------------------------------------------------------------------

# Sentinel returned by run_skill_flow when drift forces a fall-through to
# Tier 3. The dispatcher recognizes this and routes to run_job WITH a
# recorder so the apply still happens and a fresh skill gets written.
DRIFT_FALL_THROUGH = "skill_drift_fall_through"


def run_skill_flow(
    *,
    skill: Skill,
    job: dict,
    port: int,
    worker_id: int,
    model: str,
    dry_run: bool,
    verify_threshold: float,
    profile: dict | None = None,
    tailored_resume: str = "",
    interaction_timeout_ms: int = 4000,
    patch_timeout_s: int = 180,
    browser_stream=None,
) -> tuple[str, int, dict | None]:
    """Run a saved skill end-to-end via Python Playwright.

    Connects to the already-running Chrome over CDP on `port`, navigates to
    the apply URL, runs the deterministic replay, optionally runs Tier 2 for
    any unresolved fields, submits, verifies.

    Returns (status_string, duration_ms, prefill_status) matching the shape
    that run_job returns, so the caller can swap the two transparently.

    On drift, returns (DRIFT_FALL_THROUGH, ms, None) so the dispatcher can
    route to record mode.
    """
    # Lazy imports — keep cold-path heavy deps out of import-time graphs.
    from playwright.sync_api import sync_playwright
    from applypilot.apply import launcher as launcher_mod
    from applypilot.apply.patcher import (
        PATCH_PATCHED, PATCH_FAILED_DRIFT, run_patch,
    )

    started = time.monotonic()
    if profile is None:
        profile = config.load_profile()
    apply_url = job.get("application_url") or job.get("url") or ""

    prefill_status: dict | None = {
        "ats": skill.ats,
        "fields_filled": [],
        "error": None,
        "duration_ms": 0,
        "via_skill": skill_path(skill.company).name,
    }

    pw = None
    browser = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{port}", timeout=8000,
        )
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(apply_url, wait_until="domcontentloaded", timeout=30000)

        replay_result = replay_mod.replay_skill(
            skill, page, profile,
            dry_run=dry_run, interaction_timeout_ms=interaction_timeout_ms,
        )

        if replay_result.status == replay_mod.STATUS_DRIFT_DETECTED:
            duration_ms = int((time.monotonic() - started) * 1000)
            log.info("skill drift for %s: %s", skill.company, replay_result.error)
            return DRIFT_FALL_THROUGH, duration_ms, None

        patch_duration_ms = 0
        if replay_result.status == replay_mod.STATUS_NEEDS_PATCH:
            mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
            patch_result = run_patch(
                job=job,
                apply_url=apply_url,
                unresolved=replay_result.unresolved,
                profile=profile,
                tailored_resume=tailored_resume,
                model=model,
                dry_run=dry_run,
                patch_timeout_s=patch_timeout_s,
                mcp_config_path=mcp_config_path if mcp_config_path.exists() else None,
            )
            patch_duration_ms = patch_result.duration_ms
            prefill_status["patch_status"] = patch_result.status
            if patch_result.status == PATCH_FAILED_DRIFT:
                duration_ms = int((time.monotonic() - started) * 1000)
                return DRIFT_FALL_THROUGH, duration_ms, prefill_status
            if patch_result.status != PATCH_PATCHED:
                duration_ms = int((time.monotonic() - started) * 1000)
                return (
                    f"needs_review:{_patch_failure_reason(patch_result.status)}",
                    duration_ms,
                    prefill_status,
                )

            submit_result = replay_mod.submit_only(
                skill, page, interaction_timeout_ms=interaction_timeout_ms,
            )
            if submit_result.status != replay_mod.STATUS_SUBMITTED:
                duration_ms = int((time.monotonic() - started) * 1000)
                return (
                    f"failed:skill_submit_{(submit_result.error or 'unknown')[:30]}",
                    duration_ms,
                    prefill_status,
                )

        if replay_result.status == replay_mod.STATUS_FAILED:
            duration_ms = int((time.monotonic() - started) * 1000)
            return (
                f"failed:skill_replay_{(replay_result.error or 'unknown')[:30]}",
                duration_ms,
                prefill_status,
            )

        # At this point Tier 1 (and possibly Tier 2 + submit) finished.
        # Run the existing verifier unless we're in dry_run.
        if dry_run:
            duration_ms = int((time.monotonic() - started) * 1000)
            prefill_status["replay_duration_ms"] = replay_result.duration_ms
            prefill_status["patch_duration_ms"] = patch_duration_ms
            return "applied", duration_ms, prefill_status

        verification = launcher_mod._verify_submission_success(
            port, verify_threshold=verify_threshold, previous_url=apply_url,
            browser_stream=browser_stream,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        prefill_status["replay_duration_ms"] = replay_result.duration_ms
        prefill_status["patch_duration_ms"] = patch_duration_ms
        prefill_status["verification"] = verification
        if not verification.get("verified"):
            return "needs_review:unverified_submission", duration_ms, prefill_status
        return "applied", duration_ms, prefill_status

    except Exception as e:
        duration_ms = int((time.monotonic() - started) * 1000)
        log.exception("skill flow failed for %s: %s", skill.company, e)
        return f"failed:skill_flow_{type(e).__name__}", duration_ms, prefill_status
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


def _patch_failure_reason(patch_status: str) -> str:
    """Map a patcher.PATCH_FAILED_* sentinel into a launcher-style reason."""
    tail = patch_status.split(":", 1)[-1] if ":" in patch_status else patch_status
    return tail or "patch_failed"


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------

def dispatch_apply(
    *,
    job: dict,
    port: int,
    worker_id: int,
    model: str,
    dry_run: bool,
    verify_threshold: float,
    run_job_fn: Callable[..., tuple[str, int, dict | None]],
    run_job_kwargs: dict[str, Any],
    record_fn: Callable[[str, Path], None] | None = None,
    skill_flow_fn: Callable[..., tuple[str, int, dict | None]] | None = None,
    resolve_skill_fn: Callable[[str], Skill | None] | None = None,
    flag_fn: Callable[[], bool] | None = None,
    browser_stream=None,
) -> tuple[str, int, dict | None]:
    """Route one job to either the skill flow or legacy run_job.

    Always returns the same shape as run_job: (status, duration_ms, prefill).

    Injection seams (kept tight, only what tests need):
      run_job_fn        — the legacy path (defaults supplied by caller)
      skill_flow_fn     — Tier 1+2 entry point (defaults to run_skill_flow)
      resolve_skill_fn  — defaults to resolve_skill
      flag_fn           — defaults to is_skill_flow_enabled
      record_fn(company, skill_yaml_path) — called after successful Tier 3
                          record to persist telemetry. Optional.
    """
    flag = (flag_fn or is_skill_flow_enabled)()
    if not flag:
        # OFF path: byte-for-byte the same call as before Phase 4.
        passthrough_kwargs = dict(run_job_kwargs)
        if browser_stream is not None:
            passthrough_kwargs["browser_stream"] = browser_stream
        status, duration_ms, prefill = run_job_fn(
            job=job, port=port, worker_id=worker_id, **passthrough_kwargs
        )
        if isinstance(prefill, dict):
            prefill.setdefault("tier_used", "legacy_llm")
        return status, duration_ms, prefill

    company = company_key_for_job(job)
    resolver = resolve_skill_fn or resolve_skill
    skill = resolver(company) if company else None

    if skill is not None:
        # Phase 5 pre-flight: catch manual-hash-mutation drift BEFORE Chrome.
        ok, reason = verify_skill_integrity(skill)
        if not ok:
            log.info("skill integrity check failed for %s: %s", company, reason)
            archive_stale_skill(company)
            skill = None  # fall through to record mode below

    if skill is not None:
        runner = skill_flow_fn or run_skill_flow
        status, duration_ms, prefill = runner(
            skill=skill, job=job, port=port, worker_id=worker_id,
            model=model, dry_run=dry_run, verify_threshold=verify_threshold,
            browser_stream=browser_stream,
        )
        # The skill flow only OWNS the outcome if it cleanly applied OR it
        # actually submitted the form (then the verifier ran). Those are the
        # only states where re-running the LLM would be wrong (double-submit
        # / discard a real success).
        #
        # Every other outcome — exceptions (failed:skill_flow_*), replay
        # engine failure (failed:skill_replay_*), submit failure
        # (failed:skill_submit_*), a failed Tier-2 patch, or live-DOM drift —
        # means NO application was submitted. MCP-recorded skills are not
        # replay-grade (documented v1 limitation), so a broken skill MUST
        # NOT hard-fail the job: archive it (so we stop re-crashing on it
        # for every future apply to this company) and fall back to the
        # proven LLM apply below. This was the dominant yield-killer in the
        # 2026-05-15 batch — recording a skill made subsequent same-company
        # applies crash at 0s with skill_flow_attributeerror.
        SKILL_OWNS_RESULT = {"applied", "needs_review:unverified_submission"}
        if status in SKILL_OWNS_RESULT:
            if isinstance(prefill, dict):
                prefill.setdefault("skill_used", skill_path(skill.company).name)
                # Phase A telemetry: distinguish a pure Tier-1 replay from
                # one that needed a Tier-2 LLM patch (prefill carries
                # patch_duration_ms when the patcher ran).
                prefill.setdefault(
                    "tier_used",
                    "skill_patch" if prefill.get("patch_duration_ms")
                    else "skill_replay",
                )
            return status, duration_ms, prefill
        log.info(
            "skill flow non-terminal (%s) for %s — archiving skill and "
            "falling back to LLM apply", status, company,
        )
        archive_stale_skill(company)

    # No skill (or just archived). Attach a recorder so the next apply is Tier 1.
    recorder = SkillRecorder(
        company=company or "unknown",
        ats=_infer_ats(job),
        apply_url=job.get("application_url") or job.get("url") or "",
        profile=run_job_kwargs.get("profile") or config.load_profile(),
        resume_pdf_path=str(config.RESUME_PDF_PATH),
    )
    kwargs_with_recorder = dict(run_job_kwargs)
    kwargs_with_recorder["recorder"] = recorder
    if browser_stream is not None:
        kwargs_with_recorder["browser_stream"] = browser_stream

    status, duration_ms, prefill = run_job_fn(
        job=job, port=port, worker_id=worker_id, **kwargs_with_recorder,
    )
    if isinstance(prefill, dict):
        # Record mode: LLM-driven apply with a SkillRecorder observing.
        prefill.setdefault("tier_used", "skill_record")

    if status == "applied" and company and not dry_run:
        try:
            out_path = skill_path(company)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            recorder.commit(out_path)
            if record_fn is not None:
                record_fn(company, out_path)
            if isinstance(prefill, dict):
                prefill["skill_used"] = out_path.name
                prefill["recorded_new_skill"] = True
        except Exception as e:
            log.warning("skill recorder commit failed for %s: %s", company, e)
            recorder.discard()
    else:
        recorder.discard()

    return status, duration_ms, prefill


def _infer_ats(job: dict) -> str:
    """Heuristic ATS detection from URL host + site tag. Recorder needs this."""
    url = (job.get("application_url") or job.get("url") or "").lower()
    if "greenhouse" in url:
        return "greenhouse"
    if "lever" in url:
        return "lever"
    if "ashby" in url:
        return "ashby"
    if "workday" in url or "myworkdayjobs" in url:
        return "workday"
    site = (job.get("site") or "").lower()
    for ats in ("greenhouse", "lever", "ashby", "workday"):
        if ats in site:
            return ats
    return "custom"
