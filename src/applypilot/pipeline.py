"""ApplyPilot Pipeline Orchestrator.

Runs pipeline stages in sequence or concurrently (streaming mode).

Usage (via CLI):
    applypilot run                        # all stages, sequential
    applypilot run --stream               # all stages, concurrent
    applypilot run discover enrich        # specific stages
    applypilot run score tailor cover     # LLM-only stages
    applypilot run --dry-run              # preview without executing
"""

from __future__ import annotations

import logging
import os
import threading
import time
from copy import deepcopy
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from applypilot.config import DEFAULTS, load_env, ensure_dirs
from applypilot.database import init_db, get_connection, get_stats

log = logging.getLogger(__name__)
console = Console()


# ---------------------------------------------------------------------------
# Stage definitions
# ---------------------------------------------------------------------------

STAGE_ORDER = ("discover", "enrich", "gate", "score", "tailor", "cover", "pdf")

STAGE_META: dict[str, dict] = {
    "discover": {"desc": "Job discovery (JobSpy + Workday + ATS boards + TheirStack)"},
    "enrich":   {"desc": "Detail enrichment (full descriptions + apply URLs)"},
    "gate":     {"desc": "Eligibility gate (location/visa/seniority/automatability)"},
    "score":    {"desc": "LLM scoring (fit 1-10)"},
    "tailor":   {"desc": "Resume tailoring (LLM + validation)"},
    "cover":    {"desc": "Cover letter generation"},
    "pdf":      {"desc": "PDF conversion (tailored resumes + cover letters)"},
}

# Upstream dependency: a stage only finishes when its upstream is done AND
# it has no remaining pending work.
_UPSTREAM: dict[str, str | None] = {
    "discover": None,
    "enrich":   "discover",
    "gate":     "enrich",
    "score":    "gate",
    "tailor":   "score",
    "cover":    "tailor",
    "pdf":      "cover",
}


# ---------------------------------------------------------------------------
# Individual stage runners
# ---------------------------------------------------------------------------

VALID_SOURCES = ("jobspy", "workday", "ats_boards", "theirstack", "smartextract")
QUICK_MAX_AGE_HOURS = 24


def _ready_to_apply_count(
    min_score: int = 8,
    max_age_hours: int = QUICK_MAX_AGE_HOURS,
    limit: int = 100,
    site_contains: str | None = None,
) -> int:
    """Count fresh jobs that the apply queue can actually automate."""
    from applypilot.apply.launcher import preview_apply_queue

    return len(preview_apply_queue(
        limit=limit,
        min_score=min_score,
        max_age_hours=max_age_hours,
        site_contains=site_contains,
    ))


def _top_apply_queue_urls(
    target_ready: int,
    min_score: int = 8,
    max_age_hours: int = QUICK_MAX_AGE_HOURS,
    site_contains: str | None = None,
) -> list[str]:
    """Return the current top apply queue URLs without acquiring jobs."""
    from applypilot.apply.launcher import preview_apply_queue

    return [
        job["url"]
        for job in preview_apply_queue(
            limit=target_ready,
            min_score=min_score,
            max_age_hours=max_age_hours,
            site_contains=site_contains,
        )
    ]


def _is_theirstack_scope(sources: list[str] | None = None, site_contains: str | None = None) -> bool:
    if sources and "theirstack" in {s.lower() for s in sources}:
        return True
    return bool(site_contains and "theirstack" in site_contains.lower())


def _theirstack_resolver_kwargs(
    *,
    sources: list[str] | None = None,
    site_contains: str | None = None,
    min_score: int = 8,
    target_ready: int = 10,
    quick: bool = False,
) -> dict:
    if not _is_theirstack_scope(sources, site_contains):
        return {}
    return {
        "resolve_linkedin_site_contains": "TheirStack",
        "resolve_linkedin_limit": max(target_ready * 2, 10),
        "resolve_linkedin_min_score": max(min_score, 8),
        "resolve_linkedin_max_age_hours": QUICK_MAX_AGE_HOURS if quick else None,
    }


def _quick_jobspy_config(target_ready: int) -> dict:
    """Build a small high-signal JobSpy config for quick mode."""
    from applypilot import config

    cfg = deepcopy(config.load_search_config() or {})
    cfg["sites"] = ["linkedin", "google"]
    cfg["tiers"] = [1]
    defaults = dict(cfg.get("defaults", {}))
    defaults["hours_old"] = min(int(defaults.get("hours_old", QUICK_MAX_AGE_HOURS)), QUICK_MAX_AGE_HOURS)
    defaults["results_per_site"] = min(int(defaults.get("results_per_site", 25)), max(8, target_ready))
    cfg["defaults"] = defaults

    locations = cfg.get("locations", []) or []
    preferred: list[dict] = []
    for loc in locations:
        value = str(loc.get("location", "")).lower()
        if loc.get("remote") or "remote" in value:
            preferred.append(loc)
    for loc in locations:
        value = str(loc.get("location", "")).lower()
        if "san francisco" in value or "san jose" in value:
            preferred.append(loc)

    deduped = []
    seen = set()
    for loc in preferred or locations:
        key = (loc.get("location"), bool(loc.get("remote")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(loc)
        if len(deduped) >= 2:
            break
    if deduped:
        cfg["locations"] = deduped
    return cfg


def _run_discover(
    workers: int = 1,
    sources: list[str] | None = None,
    quick: bool = False,
    target_ready: int = 10,
) -> dict:
    """Stage: Job discovery — JobSpy, Workday, ATS boards, smart-extract.

    Args:
        workers: Concurrency for sub-stages that support it.
        sources: Which sub-stages to run. None = all enabled by default.
                 Valid: "jobspy", "workday", "ats_boards", "smartextract".
    """
    enabled = set(sources) if sources else (
        {"ats_boards", "jobspy"} if quick else {"jobspy", "workday", "ats_boards"}
    )
    if (
        "theirstack" not in enabled
        and (
            os.environ.get("APPLYPILOT_THEIRSTACK_ENABLED") == "1"
            or os.environ.get("THEIRSTACK_ENABLED") == "1"
        )
    ):
        enabled.add("theirstack")
    if "smartextract" not in enabled and os.environ.get("APPLYPILOT_SMART_EXTRACT") == "1":
        enabled.add("smartextract")

    stats: dict = {}

    # JobSpy
    if "jobspy" in enabled:
        console.print("  [cyan]JobSpy quick crawl...[/cyan]" if quick else "  [cyan]JobSpy full crawl...[/cyan]")
        try:
            from applypilot.discovery.jobspy import run_discovery
            run_discovery(_quick_jobspy_config(target_ready) if quick else None)
            stats["jobspy"] = "ok"
        except Exception as e:
            log.error("JobSpy crawl failed: %s", e)
            console.print(f"  [red]JobSpy error:[/red] {e}")
            stats["jobspy"] = f"error: {e}"
    else:
        stats["jobspy"] = "skipped"

    # Workday corporate scraper
    if "workday" in enabled:
        console.print("  [cyan]Workday corporate scraper...[/cyan]")
        try:
            from applypilot.discovery.workday import run_workday_discovery
            run_workday_discovery(workers=workers)
            stats["workday"] = "ok"
        except Exception as e:
            log.error("Workday scraper failed: %s", e)
            console.print(f"  [red]Workday error:[/red] {e}")
            stats["workday"] = f"error: {e}"
    else:
        stats["workday"] = "skipped"

    # Greenhouse/Lever/Ashby company boards (direct JSON APIs)
    if "ats_boards" in enabled:
        console.print("  [cyan]ATS boards (Greenhouse/Lever/Ashby)...[/cyan]")
        try:
            from applypilot.discovery.ats_boards import run_ats_boards_discovery
            run_ats_boards_discovery(workers=workers)
            stats["ats_boards"] = "ok"
        except Exception as e:
            log.error("ATS boards crawl failed: %s", e)
            console.print(f"  [red]ATS boards error:[/red] {e}")
            stats["ats_boards"] = f"error: {e}"
    else:
        stats["ats_boards"] = "skipped"

    # TheirStack API source. Opt-in because returned jobs consume TheirStack credits.
    if "theirstack" in enabled:
        console.print("  [cyan]TheirStack API...[/cyan]")
        try:
            from applypilot.discovery.theirstack import run_theirstack_discovery
            result = run_theirstack_discovery()
            stats["theirstack"] = result.get("status", "ok")
        except Exception as e:
            log.error("TheirStack crawl failed: %s", e)
            console.print(f"  [red]TheirStack error:[/red] {e}")
            stats["theirstack"] = f"error: {e}"
    else:
        stats["theirstack"] = "skipped"

    # Smart extract — opt-in only; replaced by ats_boards which hits JSON
    # APIs directly (~50x faster, no LLM cost).
    if "smartextract" in enabled:
        console.print("  [cyan]Smart extract (AI-powered scraping)...[/cyan]")
        try:
            from applypilot.discovery.smartextract import run_smart_extract
            run_smart_extract(workers=workers)
            stats["smartextract"] = "ok"
        except Exception as e:
            log.error("Smart extract failed: %s", e)
            console.print(f"  [red]Smart extract error:[/red] {e}")
            stats["smartextract"] = f"error: {e}"
    else:
        stats["smartextract"] = "skipped"

    # Board Atlas freshness tick (v2 Phase 2, shadow). Opt-in until go/no-go —
    # OFF during shadow mode so it doesn't perturb the live crawler.
    if "atlas" in enabled or os.environ.get("APPLYPILOT_ATLAS_ENABLED") == "1":
        console.print("  [cyan]Board Atlas tick (ring 0/1)...[/cyan]")
        try:
            from applypilot import database as _db
            from applypilot.discovery.atlas.tick import run_tick
            from applypilot.gate.profile_map import gate_profile
            from applypilot.config import load_profile, load_search_config
            policy = gate_profile(load_profile(), load_search_config())
            res = run_tick(_db.get_connection(), policy)
            stats["atlas"] = f"ok: {res['jobs_new']} new, {res['jobs_eligible']} eligible"
        except Exception as e:
            log.error("Atlas tick failed: %s", e)
            console.print(f"  [red]Atlas error:[/red] {e}")
            stats["atlas"] = f"error: {e}"
    else:
        stats["atlas"] = "skipped"

    return stats


def _run_enrich(
    workers: int = 1,
    limit: int | None = None,
    max_total: int | None = None,
) -> dict:
    """Stage: Detail enrichment — scrape full descriptions and apply URLs."""
    try:
        from applypilot.enrichment.detail import run_enrichment
        kwargs = {"workers": workers}
        if limit is not None:
            kwargs["limit"] = limit
        if max_total is not None:
            kwargs["max_total"] = max_total
        run_enrichment(**kwargs)
        return {"status": "ok"}
    except Exception as e:
        log.error("Enrichment failed: %s", e)
        return {"status": f"error: {e}"}


def _run_gate(min_score=None, **kwargs) -> dict:
    """Stage: Eligibility gate — (re-)stamp gate verdicts on ungated / stale rows.

    Runs the deterministic gate over `pending_gate` rows (never gated, or
    gated before enrichment produced the full description). The scorer only
    sees gated-eligible rows downstream.
    """
    try:
        from applypilot import database as db
        from applypilot.gate.engine import gate_job
        from applypilot.gate.profile_map import gate_profile
        from applypilot.config import load_profile, load_search_config
        conn = db.get_connection()
        try:
            policy = gate_profile(load_profile(), load_search_config())
        except Exception:
            policy = gate_profile({}, {})
        rows = db.get_jobs_by_stage(conn, "pending_gate", limit=0)
        n = 0
        for row in rows:
            try:
                db.update_gate(conn, row["url"], gate_job(dict(row), policy))
                n += 1
            except Exception as e:  # noqa: BLE001 — one bad row must not kill the stage
                log.warning("gate failed for %s: %s", row["url"], e)
                continue
        return {"status": "ok", "gated": n}
    except Exception as e:
        log.error("Gate failed: %s", e)
        return {"status": f"error: {e}"}


def _run_score(
    limit: int = 0,
    resolve_linkedin_site_contains: str | None = None,
    resolve_linkedin_limit: int = 0,
    resolve_linkedin_min_score: int = 8,
    resolve_linkedin_max_age_hours: float | None = None,
) -> dict:
    """Stage: LLM scoring - assign fit scores 1-10."""
    try:
        from applypilot.scoring.scorer import run_scoring
        stats = run_scoring(limit=limit)
        result = {"status": "ok", "scoring": stats}
        if resolve_linkedin_site_contains and resolve_linkedin_limit > 0:
            from applypilot.enrichment.linkedin_outbound import resolve_linkedin_jobs

            resolver_stats = resolve_linkedin_jobs(
                limit=resolve_linkedin_limit,
                min_score=resolve_linkedin_min_score,
                write=True,
                use_browser=True,
                headless=True,
                manual_unblock_seconds=0,
                retry_hours=0,
                site_contains=resolve_linkedin_site_contains,
                max_age_hours=resolve_linkedin_max_age_hours,
            )
            if resolver_stats["processed"]:
                log.info(
                    "Post-score LinkedIn resolver (%s): %d/%d resolved | easy=%d expired=%d unknown=%d error=%d",
                    resolve_linkedin_site_contains,
                    resolver_stats["resolved"],
                    resolver_stats["processed"],
                    resolver_stats["easy_apply_only"],
                    resolver_stats["expired"],
                    resolver_stats["unknown"],
                    resolver_stats["error"],
                )
            result["linkedin_outbound"] = resolver_stats
        return result
    except Exception as e:
        log.error("Scoring failed: %s", e)
        return {"status": f"error: {e}"}


def _run_tailor(
    min_score: int = 7,
    validation_mode: str = "normal",
    limit: int = 20,
    job_urls: list[str] | None = None,
    site_contains: str | None = None,
    applyable_only: bool = False,
) -> dict:
    """Stage: Resume tailoring — generate tailored resumes for high-fit jobs."""
    try:
        from applypilot.scoring.tailor import run_tailoring
        run_tailoring(
            min_score=min_score,
            limit=limit,
            validation_mode=validation_mode,
            job_urls=job_urls,
            site_contains=site_contains,
            applyable_only=applyable_only,
        )
        return {"status": "ok"}
    except Exception as e:
        log.error("Tailoring failed: %s", e)
        return {"status": f"error: {e}"}


def _run_cover(min_score: int = 7, validation_mode: str = "normal") -> dict:
    """Stage: Cover letter generation."""
    try:
        from applypilot.scoring.cover_letter import run_cover_letters
        run_cover_letters(min_score=min_score, validation_mode=validation_mode)
        return {"status": "ok"}
    except Exception as e:
        log.error("Cover letter generation failed: %s", e)
        return {"status": f"error: {e}"}


def _run_pdf() -> dict:
    """Stage: PDF conversion — convert tailored resumes and cover letters to PDF."""
    try:
        from applypilot.scoring.pdf import batch_convert
        batch_convert()
        return {"status": "ok"}
    except Exception as e:
        log.error("PDF conversion failed: %s", e)
        return {"status": f"error: {e}"}


# Map stage names to their runner functions
_STAGE_RUNNERS: dict[str, callable] = {
    "discover": _run_discover,
    "enrich":   _run_enrich,
    "gate":     _run_gate,
    "score":    _run_score,
    "tailor":   _run_tailor,
    "cover":    _run_cover,
    "pdf":      _run_pdf,
}


# ---------------------------------------------------------------------------
# Stage resolution
# ---------------------------------------------------------------------------

def _resolve_stages(stage_names: list[str]) -> list[str]:
    """Resolve 'all' and validate/order stage names."""
    if "all" in stage_names:
        return list(STAGE_ORDER)

    resolved = []
    for name in stage_names:
        if name not in STAGE_META:
            console.print(
                f"[red]Unknown stage:[/red] '{name}'. "
                f"Available: {', '.join(STAGE_ORDER)}, all"
            )
            raise SystemExit(1)
        if name not in resolved:
            resolved.append(name)

    # Maintain canonical order
    return [s for s in STAGE_ORDER if s in resolved]


# ---------------------------------------------------------------------------
# Streaming pipeline helpers
# ---------------------------------------------------------------------------

class _StageTracker:
    """Thread-safe tracker for which stages have finished producing work."""

    def __init__(self):
        self._events: dict[str, threading.Event] = {
            stage: threading.Event() for stage in STAGE_ORDER
        }
        self._results: dict[str, dict] = {}
        self._lock = threading.Lock()

    def mark_done(self, stage: str, result: dict | None = None) -> None:
        with self._lock:
            self._results[stage] = result or {"status": "ok"}
        self._events[stage].set()

    def is_done(self, stage: str) -> bool:
        return self._events[stage].is_set()

    def wait(self, stage: str, timeout: float | None = None) -> bool:
        return self._events[stage].wait(timeout=timeout)

    def get_results(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._results)


# SQL to count pending work for each stage
_PENDING_SQL: dict[str, str] = {
    "gate": (
        "SELECT COUNT(*) FROM jobs WHERE gated_at IS NULL "
        "OR (detail_scraped_at IS NOT NULL AND gated_at IS NOT NULL AND detail_scraped_at > gated_at)"
    ),
    # Mirror database.get_jobs_by_stage("pending_score") IN LOCKSTEP: gated
    # 'eligible' OR 'unknown' (sponsorship-review) rows, gated after enrichment,
    # are countable pending-score work. 'ineligible'/ungated stay excluded, else
    # the streaming score loop would spin forever on rows it never scores. Keep
    # this predicate byte-for-byte aligned with the database one.
    "score": (
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL AND fit_score IS NULL "
        "AND gated_at IS NOT NULL AND gate_result IN ('eligible', 'unknown') "
        "AND (detail_scraped_at IS NULL OR gated_at >= detail_scraped_at)"
    ),
    "tailor": (
        "SELECT COUNT(*) FROM jobs WHERE fit_score >= ? "
        "AND full_description IS NOT NULL "
        "AND tailored_resume_path IS NULL "
        "AND COALESCE(tailor_attempts, 0) < 5"
    ),
    "cover": (
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL "
        "AND fit_score >= ? "
        "AND full_description IS NOT NULL "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '') "
        "AND COALESCE(cover_attempts, 0) < 5"
    ),
}

# How long to sleep between polling loops in streaming mode (seconds)
_STREAM_POLL_INTERVAL = 10


def _count_pending(
    stage: str,
    min_score: int = 7,
    site_contains: str | None = None,
    applyable_only: bool = False,
) -> int:
    """Count pending work items for a stage."""
    if stage == "enrich":
        from applypilot.enrichment.detail import SKIP_DETAIL_SITES

        conn = get_connection()
        if not SKIP_DETAIL_SITES:
            return conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL"
            ).fetchone()[0]

        placeholders = ",".join("?" for _ in SKIP_DETAIL_SITES)
        return conn.execute(
            f"SELECT COUNT(*) FROM jobs "
            f"WHERE detail_scraped_at IS NULL AND site NOT IN ({placeholders})",
            tuple(SKIP_DETAIL_SITES),
        ).fetchone()[0]

    if stage == "pdf":
        from applypilot.config import TAILORED_DIR

        if not TAILORED_DIR.exists():
            return 0
        return sum(
            1
            for path in TAILORED_DIR.glob("*.txt")
            if not path.name.endswith("_JOB.txt") and not path.with_suffix(".pdf").exists()
        )

    sql = _PENDING_SQL.get(stage)
    if sql is None:
        return 0
    conn = get_connection()
    params: list[object] = []
    if site_contains and stage in {"tailor", "cover"}:
        sql += " AND LOWER(site) LIKE ?"
        params.append(f"%{site_contains.lower()}%")
    if applyable_only and stage == "tailor":
        sql += (
            " AND applied_at IS NULL "
            "AND (apply_status IS NULL OR apply_status = 'failed') "
            f"AND (apply_attempts IS NULL OR apply_attempts < {int(DEFAULTS['max_apply_attempts'])}) "
            "AND application_url IS NOT NULL "
            "AND LOWER(TRIM(application_url)) NOT IN ('', 'none', 'null', 'nan', 'nat') "
            "AND (LOWER(TRIM(application_url)) LIKE 'http://%' OR LOWER(TRIM(application_url)) LIKE 'https://%') "
            "AND LOWER(TRIM(application_url)) NOT LIKE '%linkedin.com/jobs/view%'"
        )
    if "?" in sql:
        return conn.execute(sql, (min_score, *params)).fetchone()[0]
    return conn.execute(sql, params).fetchone()[0]


def _run_stage_streaming(
    stage: str,
    tracker: _StageTracker,
    stop_event: threading.Event,
    min_score: int = 7,
    workers: int = 1,
    validation_mode: str = "normal",
    sources: list[str] | None = None,
    site_contains: str | None = None,
    applyable_only: bool = False,
    quick: bool = False,
    target_ready: int = 10,
) -> None:
    """Run a single stage in streaming mode: loop until upstream done + no work.

    For discover: runs once, then marks done.
    For all others: polls DB for pending work, runs the batch processor,
    and repeats until upstream is done and no pending work remains.
    """
    runner = _STAGE_RUNNERS[stage]
    kwargs: dict = {}
    if stage in ("tailor", "cover"):
        kwargs["min_score"] = min_score
        kwargs["validation_mode"] = validation_mode
    if stage == "tailor" and site_contains:
        kwargs["site_contains"] = site_contains
    if stage == "tailor" and applyable_only:
        kwargs["applyable_only"] = True
    if stage in ("discover", "enrich"):
        kwargs["workers"] = workers
    if stage == "discover" and sources is not None:
        kwargs["sources"] = sources
    if stage == "discover":
        kwargs["quick"] = quick
        kwargs["target_ready"] = target_ready
    if quick and stage == "enrich":
        budget = max(target_ready * 3, 20)
        kwargs["limit"] = max(target_ready, 10)
        kwargs["max_total"] = budget
    if quick and stage == "score":
        kwargs["limit"] = max(target_ready * 3, 20)
    if stage == "score":
        kwargs.update(
            _theirstack_resolver_kwargs(
                sources=sources,
                site_contains=site_contains,
                min_score=min_score,
                target_ready=target_ready,
                quick=quick,
            )
        )
    if quick and stage == "tailor":
        kwargs["limit"] = target_ready
        kwargs["job_urls"] = _top_apply_queue_urls(
                target_ready,
                min_score=max(min_score, 8),
                max_age_hours=QUICK_MAX_AGE_HOURS,
                site_contains=site_contains,
            )

    upstream = _UPSTREAM[stage]

    if stage == "discover":
        # Discover runs once (its sub-scrapers already do their full crawl)
        try:
            result = runner(**kwargs)
            tracker.mark_done(stage, result)
        except Exception as e:
            log.exception("Stage '%s' crashed", stage)
            tracker.mark_done(stage, {"status": f"error: {e}"})
        return

    # For downstream stages: loop until upstream done + no pending work
    passes = 0
    while not stop_event.is_set():
        # Wait for upstream to start producing work (first pass only)
        if passes == 0 and upstream and not tracker.is_done(upstream):
            # Wait a bit for upstream to produce some work before first run
            tracker.wait(upstream, timeout=_STREAM_POLL_INTERVAL)

        pending = _count_pending(
            stage,
            min_score,
            site_contains=site_contains,
            applyable_only=applyable_only,
        )

        if pending > 0:
            try:
                runner(**kwargs)
                passes += 1
            except Exception as e:
                log.error("Stage '%s' error (pass %d): %s", stage, passes, e)
                passes += 1
        else:
            # No work right now
            upstream_done = upstream is None or tracker.is_done(upstream)
            if upstream_done:
                # No work and upstream is done — this stage is finished
                break
            # Upstream still running, wait and retry
            if stop_event.wait(timeout=_STREAM_POLL_INTERVAL):
                break  # Stop requested

    tracker.mark_done(stage, {"status": "ok", "passes": passes})


# ---------------------------------------------------------------------------
# Pipeline orchestrators
# ---------------------------------------------------------------------------

def _run_sequential(ordered: list[str], min_score: int, workers: int = 1,
                    validation_mode: str = "normal",
                    sources: list[str] | None = None,
                    site_contains: str | None = None,
                    applyable_only: bool = False,
                    quick: bool = False,
                    target_ready: int = 10) -> dict:
    """Execute stages one at a time (original behavior)."""
    results: list[dict] = []
    errors: dict[str, str] = {}
    pipeline_start = time.time()

    for name in ordered:
        if quick and name in {"enrich", "score"}:
            ready = _ready_to_apply_count(
                min_score=max(min_score, 8),
                limit=target_ready,
                site_contains=site_contains,
            )
            if ready >= target_ready:
                console.print(
                    f"\n  [green]Quick target met:[/green] {ready}/{target_ready} fresh jobs ready. "
                    f"Skipping remaining stages."
                )
                break

        meta = STAGE_META[name]
        console.print(f"\n{'=' * 70}")
        console.print(f"  [bold]STAGE: {name}[/bold] — {meta['desc']}")
        console.print(f"  Started: {datetime.now().strftime('%H:%M:%S')}")
        console.print(f"{'=' * 70}")

        t0 = time.time()
        runner = _STAGE_RUNNERS[name]

        try:
            kwargs: dict = {}
            if name in ("tailor", "cover"):
                kwargs["min_score"] = min_score
                kwargs["validation_mode"] = validation_mode
            if name == "tailor" and site_contains:
                kwargs["site_contains"] = site_contains
            if name == "tailor" and applyable_only:
                kwargs["applyable_only"] = True
            if name in ("discover", "enrich"):
                kwargs["workers"] = workers
            if name == "discover" and sources is not None:
                kwargs["sources"] = sources
            if name == "discover":
                kwargs["quick"] = quick
                kwargs["target_ready"] = target_ready
            if quick and name == "enrich":
                budget = max(target_ready * 3, 20)
                kwargs["limit"] = max(target_ready, 10)
                kwargs["max_total"] = budget
            if quick and name == "score":
                kwargs["limit"] = max(target_ready * 3, 20)
            if name == "score":
                kwargs.update(
                    _theirstack_resolver_kwargs(
                        sources=sources,
                        site_contains=site_contains,
                        min_score=min_score,
                        target_ready=target_ready,
                        quick=quick,
                    )
                )
            if quick and name == "tailor":
                kwargs["limit"] = target_ready
                kwargs["job_urls"] = _top_apply_queue_urls(
                    target_ready,
                    min_score=max(min_score, 8),
                    max_age_hours=QUICK_MAX_AGE_HOURS,
                    site_contains=site_contains,
                )
            result = runner(**kwargs)
            elapsed = time.time() - t0

            status = "ok"
            if isinstance(result, dict):
                status = result.get("status", "ok")
                if name == "discover":
                    sub_errors = [
                        f"{k}: {v}" for k, v in result.items()
                        if isinstance(v, str) and v.startswith("error")
                    ]
                    if sub_errors:
                        status = "partial"

        except Exception as e:
            elapsed = time.time() - t0
            status = f"error: {e}"
            log.exception("Stage '%s' crashed", name)
            console.print(f"\n  [red]STAGE FAILED:[/red] {e}")

        results.append({"stage": name, "status": status, "elapsed": elapsed})
        if status not in ("ok", "partial"):
            errors[name] = status

        console.print(f"\n  Stage '{name}' completed in {elapsed:.1f}s — {status}")

    total_elapsed = time.time() - pipeline_start
    return {"stages": results, "errors": errors, "elapsed": total_elapsed}


def _run_streaming(ordered: list[str], min_score: int, workers: int = 1,
                   validation_mode: str = "normal",
                   sources: list[str] | None = None,
                   site_contains: str | None = None,
                   applyable_only: bool = False,
                   quick: bool = False,
                   target_ready: int = 10) -> dict:
    """Execute stages concurrently with DB as conveyor belt."""
    tracker = _StageTracker()
    stop_event = threading.Event()
    pipeline_start = time.time()

    console.print("\n  [bold cyan]STREAMING MODE[/bold cyan] — stages run concurrently")
    console.print(f"  Poll interval: {_STREAM_POLL_INTERVAL}s\n")

    # Mark stages NOT in `ordered` as done so downstream doesn't wait for them
    for stage in STAGE_ORDER:
        if stage not in ordered:
            tracker.mark_done(stage, {"status": "skipped"})

    # Launch each stage in its own thread
    threads: dict[str, threading.Thread] = {}
    start_times: dict[str, float] = {}

    for name in ordered:
        start_times[name] = time.time()
        t = threading.Thread(
            target=_run_stage_streaming,
            args=(name, tracker, stop_event, min_score, workers, validation_mode, sources, site_contains, applyable_only, quick, target_ready),
            name=f"stage-{name}",
            daemon=True,
        )
        threads[name] = t
        t.start()
        console.print(f"  [dim]Started thread:[/dim] {name}")

    # Wait for all threads to finish
    try:
        for name in ordered:
            threads[name].join()
            elapsed = time.time() - start_times[name]
            console.print(
                f"  [green]Completed:[/green] {name} ({elapsed:.1f}s)"
            )
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted — stopping stages...[/yellow]")
        stop_event.set()
        for t in threads.values():
            t.join(timeout=10)

    total_elapsed = time.time() - pipeline_start

    # Build results from tracker
    all_results = tracker.get_results()
    results: list[dict] = []
    errors: dict[str, str] = {}

    for name in ordered:
        r = all_results.get(name, {"status": "unknown"})
        elapsed = time.time() - start_times.get(name, pipeline_start)
        status = r.get("status", "ok")

        results.append({"stage": name, "status": status, "elapsed": elapsed})
        if status not in ("ok", "partial", "skipped"):
            errors[name] = status

    return {"stages": results, "errors": errors, "elapsed": total_elapsed}


def run_pipeline(
    stages: list[str] | None = None,
    min_score: int = 7,
    dry_run: bool = False,
    stream: bool = False,
    workers: int = 1,
    validation_mode: str = "normal",
    sources: list[str] | None = None,
    site_contains: str | None = None,
    applyable_only: bool = False,
    quick: bool = False,
    target_ready: int = 10,
) -> dict:
    """Run pipeline stages.

    Args:
        stages: List of stage names, or None / ["all"] for full pipeline.
        min_score: Minimum fit score for tailor/cover stages.
        dry_run: If True, preview stages without executing.
        stream: If True, run stages concurrently (streaming mode).
        workers: Number of parallel threads for discovery/enrichment stages.
        quick: If True, use small targeted budgets for discovery/enrich/score.
        target_ready: Desired fresh ready-to-apply count for quick mode.

    Returns:
        Dict with keys: stages (list of result dicts), errors (dict), elapsed (float).
    """
    # Bootstrap
    load_env()
    ensure_dirs()
    init_db()

    # Resolve stages
    if stages is None:
        stages = ["all"]
    ordered = _resolve_stages(stages)
    if quick and stream:
        console.print("[yellow]Quick mode uses bounded sequential stages; ignoring --stream.[/yellow]")
        stream = False

    # Banner
    mode = "streaming" if stream else "sequential"
    console.print()
    console.print(Panel.fit(
        f"[bold]ApplyPilot Pipeline[/bold] ({mode})",
        border_style="blue",
    ))
    console.print(f"  Min score:  {min_score}")
    console.print(f"  Workers:    {workers}")
    console.print(f"  Validation: {validation_mode}")
    if site_contains:
        console.print(f"  Site:       contains {site_contains}")
    if applyable_only:
        console.print("  Tailor:     applyable jobs only")
    if quick:
        console.print(f"  Quick:      target {target_ready} ready jobs")
    console.print(f"  Stages:     {' -> '.join(ordered)}")

    # Pre-run stats
    pre_stats = get_stats()
    console.print(f"  DB:        {pre_stats['total']} jobs, {pre_stats['pending_detail']} pending enrichment")

    if dry_run:
        console.print(f"\n  [yellow]DRY RUN[/yellow] — would execute ({mode}):")
        for name in ordered:
            meta = STAGE_META[name]
            console.print(f"    {name:<12s}  {meta['desc']}")
        console.print("\n  No changes made.")
        return {"stages": [], "errors": {}, "elapsed": 0.0}

    # Execute
    if stream:
        result = _run_streaming(ordered, min_score, workers=workers,
                                validation_mode=validation_mode,
                                sources=sources,
                                site_contains=site_contains,
                                applyable_only=applyable_only,
                                quick=quick,
                                target_ready=target_ready)
    else:
        result = _run_sequential(ordered, min_score, workers=workers,
                                 validation_mode=validation_mode,
                                 sources=sources,
                                 site_contains=site_contains,
                                 applyable_only=applyable_only,
                                 quick=quick,
                                 target_ready=target_ready)

    # Summary table
    console.print(f"\n{'=' * 70}")
    summary = Table(title="Pipeline Summary", show_header=True, header_style="bold")
    summary.add_column("Stage", style="bold")
    summary.add_column("Status")
    summary.add_column("Time", justify="right")

    for r in result["stages"]:
        elapsed_str = f"{r['elapsed']:.1f}s"
        status_display = r["status"][:30]
        if r["status"] == "ok":
            style = "green"
        elif r["status"] in ("partial", "skipped"):
            style = "yellow"
        else:
            style = "red"
        summary.add_row(r["stage"], f"[{style}]{status_display}[/{style}]", elapsed_str)

    summary.add_row("", "", "")
    summary.add_row("[bold]Total[/bold]", "", f"[bold]{result['elapsed']:.1f}s[/bold]")
    console.print(summary)

    # Final DB stats
    final = get_stats()
    console.print("\n  [bold]DB Final State:[/bold]")
    console.print(f"    Total jobs:     {final['total']}")
    console.print(f"    With desc:      {final['with_description']}")
    console.print(f"    Scored:         {final['scored']}")
    console.print(f"    Tailored:       {final['tailored']}")
    console.print(f"    Cover letters:  {final['with_cover_letter']}")
    console.print(f"    Auto-applyable: {final['ready_to_apply']} (all ages)")
    fresh_ready = _ready_to_apply_count(
        min_score=max(min_score, 8),
        max_age_hours=QUICK_MAX_AGE_HOURS,
        limit=100,
    )
    console.print(f"    Fresh queue:    {fresh_ready} (24h, score >= {max(min_score, 8)})")
    console.print(f"    Applied:        {final['applied']}")
    console.print(f"{'=' * 70}\n")

    return result
