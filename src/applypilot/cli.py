"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
import os
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "gate", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _preapply_prune(min_score: int, limit: int = 200, check_queue_fn=None) -> None:
    """Freshness pre-check before an apply batch: park dead links as 'expired'.

    Never blocks the batch — any error is logged and applying proceeds
    (the per-job flow has its own failure handling). `check_queue_fn` is
    injectable for tests.
    """
    try:
        from applypilot.config import DB_PATH
        from applypilot.freshness import check_queue
        import sqlite3 as _sqlite3

        fn = check_queue_fn or check_queue
        conn = _sqlite3.connect(DB_PATH)
        try:
            res = fn(conn, min_score=min_score, limit=limit)
        finally:
            conn.close()
        if res.checked:
            console.print(
                f"  Freshness pre-check: {res.checked} links checked — "
                f"[green]{res.live} live[/green], [red]{res.expired} expired (parked)[/red], "
                f"[yellow]{res.unknown} unknown[/yellow]"
            )
    except Exception as exc:  # noqa: BLE001 — pre-check must never block applying
        log.warning("freshness pre-check skipped: %s", exc)


def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Board Atlas sub-app (v2 Phase 2, shadow mode) — thin shells over discovery.atlas
# ---------------------------------------------------------------------------

atlas_app = typer.Typer(help="Board Atlas — discovery at scale (v2 Phase 2, shadow mode).")
app.add_typer(atlas_app, name="atlas")


@atlas_app.command("import")
def atlas_import() -> None:
    """One-time seed of the boards registry from ats_companies.yaml (56 tokens)."""
    _bootstrap()
    from applypilot import database as db
    from applypilot.discovery.atlas.importer import import_registry
    n = import_registry(db.get_connection())
    console.print(f"Imported [bold]{n}[/bold] new boards from the ATS registry.")


@atlas_app.command("mine")
def atlas_mine() -> None:
    """Harvest candidate board tokens from the jobs DB + bundled CC snapshot."""
    _bootstrap()
    from applypilot import database as db
    from applypilot.discovery.atlas.miner import mine_candidates
    n = mine_candidates(db.get_connection())
    console.print(f"Mined [bold]{n}[/bold] new candidate boards (status=candidate).")


@atlas_app.command("validate")
def atlas_validate(
    limit: int = typer.Option(500, "--limit", help="Max candidate boards to validate this run."),
) -> None:
    """Promote surviving candidate boards to 'active' via one cheap existence check each."""
    _bootstrap()
    from applypilot import database as db
    from applypilot.discovery.atlas import validator
    from applypilot.discovery.atlas.politeness import HostRateLimiter
    conn = db.get_connection()
    limiter = HostRateLimiter(rps=4.0)
    cands = conn.execute(
        "SELECT ats, token FROM boards WHERE status = 'candidate' LIMIT ?", (limit,)).fetchall()
    ok = 0
    for r in cands:
        if validator.validate_board(conn, r["ats"], r["token"], limiter=limiter).ok:
            ok += 1
    console.print(f"Validated [bold]{ok}[/bold]/{len(cands)} candidate boards to active.")


@atlas_app.command("tick")
def atlas_tick(
    budget: int = typer.Option(6000, "--budget", help="Max board polls this tick (ring 0/1)."),
) -> None:
    """Run one idempotent freshness TICK (poll due ring-0/1 boards; no daemon)."""
    _bootstrap()
    from applypilot import database as db
    from applypilot.discovery.atlas.tick import run_tick
    from applypilot.gate.profile_map import gate_profile
    from applypilot.config import load_profile, load_search_config
    policy = gate_profile(load_profile(), load_search_config())
    res = run_tick(db.get_connection(), policy, budget=budget)
    console.print(
        f"Atlas tick: polled [bold]{res['boards_polled']}[/bold] boards, "
        f"{res['jobs_new']} new jobs, {res['jobs_eligible']} eligible"
        + (f" [red](error: {res['error']})[/red]" if res.get("error") else "")
    )


@atlas_app.command("report")
def atlas_report() -> None:
    """Shadow-mode telemetry + go/no-go for the Phase 2 exit."""
    _bootstrap()
    from applypilot import database as db
    from applypilot.discovery.atlas import telemetry
    conn = db.get_connection()
    verdict = telemetry.go_no_go(conn)
    cov = verdict["signals"]["board_coverage"]
    table = Table(title="Atlas board coverage", header_style="bold cyan")
    table.add_column("ATS")
    table.add_column("active", justify="right")
    table.add_column("candidate", justify="right")
    table.add_column("dead", justify="right")
    for ats, d in sorted(cov.items()):
        table.add_row(ats, str(d.get("active", 0)), str(d.get("candidate", 0)), str(d.get("dead", 0)))
    console.print(table)
    vol = verdict["signals"]["poll_volume"]
    sig = verdict["signals"]
    console.print(f"Poll volume: {vol['total_requests']} requests over {vol['runs']} runs")
    console.print(f"Review-ready depth: [bold]{sig['review_ready_depth']}[/bold]"
                  + (f"  (auto-eligible: {sig['fresh_eligible_depth']}; "
                     f"rest are sponsorship-unknown -> review, needs_sponsorship={sig['needs_sponsorship']})"
                     if sig.get("needs_sponsorship") else ""))
    console.print(f"Go/no-go: {'[green]GO[/green]' if verdict['go'] else '[yellow]NO-GO (iterate)[/yellow]'}")


# ---------------------------------------------------------------------------
# Flight-recorder fixture sub-app (v2 Phase 3) — turn a recorded bundle into a
# replayable CI regression (real recorded DOM replaces synthetic-only tests).
# ---------------------------------------------------------------------------

fixtures_app = typer.Typer(help="Flight-recorder fixture management (v2 CI regressions).")
app.add_typer(fixtures_app, name="fixtures")


@fixtures_app.command("promote")
def fixtures_promote(
    run: str = typer.Argument(..., help="Path to a flight-recorder bundle .json (or run stem)."),
    out_dir: str = typer.Option("tests/fixtures/v2", "--out", help="Fixture dir."),
) -> None:
    """Turn a flight-recorder bundle into a replayable CI fixture: writes
    <company>.html (the captured real DOM) + <company>.expected.json (semantic
    keys the front-end must recover). Real recorded DOM replaces synthetic tests."""
    _bootstrap()
    import json
    from pathlib import Path

    from applypilot.apply.v2.flight_recorder import safe_stem
    p = Path(run)
    if not p.exists():
        from applypilot import config
        p = config.APP_DIR / "flight" / f"{run}.json"
    bundle = json.loads(p.read_text(encoding="utf-8"))
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    # `company` is untrusted ATS/job data; a value with a path separator or `..`
    # would otherwise write <company>.html OUTSIDE out_dir and clobber unrelated
    # files. Sanitize to one safe segment, then assert the resolved target is a
    # direct child of out_dir (defence-in-depth) before any write.
    stem = safe_stem(bundle.get("company"))
    out_resolved = out.resolve()
    if (out_resolved / f"{stem}.html").resolve().parent != out_resolved:
        raise typer.BadParameter(f"refusing to promote: unsafe fixture stem {stem!r}")
    (out / f"{stem}.html").write_text(bundle.get("dom_html", ""), encoding="utf-8")
    expected = {"semantic_keys": sorted({f["semantic_key"] for f in bundle.get("fields", [])
                                         if f.get("semantic_key")})}
    (out / f"{stem}.expected.json").write_text(json.dumps(expected, indent=2), encoding="utf-8")
    console.print(f"Promoted fixture [bold]{stem}[/bold] to {out} "
                  f"({len(expected['semantic_keys'])} semantic keys).")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    quick: bool = typer.Option(
        False,
        "--quick",
        help=(
            "Fast lane for small apply batches: use targeted discovery, "
            "skip slow default sources, and cap enrichment/scoring work."
        ),
    ),
    target_ready: int = typer.Option(
        10,
        "--target-ready",
        help="Fresh ready-to-apply jobs to aim for in --quick mode.",
    ),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
    source: Optional[str] = typer.Option(
        None, "--source",
        help=(
            "Comma-separated discovery sub-sources to run within the discover stage. "
            "Valid: jobspy, workday, ats_boards, theirstack, smartextract. "
            "Default: jobspy + workday + ats_boards (theirstack/smartextract opt-in). "
            "Example: --source ats_boards   (only Greenhouse/Lever/Ashby)"
        ),
    ),
    site_contains: Optional[str] = typer.Option(
        None,
        "--site-contains",
        help="Only run source-scoped stages like tailor against jobs whose site/source contains this text.",
    ),
    applyable_only: bool = typer.Option(
        False,
        "--applyable-only",
        help="For tailoring, only process jobs that are still eligible for the apply queue.",
    ),
    llm_provider: Optional[str] = typer.Option(
        None,
        "--llm-provider",
        help="LLM backend for score/tailor/cover: auto or claude. Auto keeps Gemini/OpenAI/local env detection.",
    ),
    llm_model: Optional[str] = typer.Option(
        None,
        "--llm-model",
        help="Model for score/tailor/cover. For Claude: sonnet or claude-haiku-4-5-20251001.",
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline, VALID_SOURCES

    stage_list = stages if stages else ["all"]

    if llm_provider:
        provider = llm_provider.strip().lower()
        valid_providers = {"auto", "claude", "claude-code", "claude_code"}
        if provider not in valid_providers:
            console.print(
                f"[red]Invalid --llm-provider:[/red] '{llm_provider}'. "
                f"Choose from: {', '.join(sorted(valid_providers))}"
            )
            raise typer.Exit(code=1)
        if provider == "auto":
            os.environ.pop("APPLYPILOT_LLM_PROVIDER", None)
            os.environ.pop("LLM_PROVIDER", None)
        else:
            os.environ["APPLYPILOT_LLM_PROVIDER"] = provider
    if llm_model:
        os.environ["LLM_MODEL"] = llm_model.strip()

    # Parse --source filter
    sources: Optional[list[str]] = None
    if source:
        sources = [s.strip() for s in source.split(",") if s.strip()]
        for s in sources:
            if s not in VALID_SOURCES:
                console.print(
                    f"[red]Unknown source:[/red] '{s}'. "
                    f"Valid sources: {', '.join(VALID_SOURCES)}"
                )
                raise typer.Exit(code=1)

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    if target_ready < 1:
        console.print("[red]--target-ready must be at least 1.[/red]")
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
        sources=sources,
        site_contains=site_contains,
        applyable_only=applyable_only,
        quick=quick,
        target_ready=target_ready,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command("discover-ats")
def discover_ats(
    query: Optional[str] = typer.Option(
        None,
        "--query",
        "-q",
        help="Comma-separated role queries. Defaults to tier 1-2 searches from searches.yaml.",
    ),
    search_limit: int = typer.Option(
        200,
        "--search-limit",
        help="Maximum candidate ATS URLs to collect from web search.",
    ),
    workers: int = typer.Option(12, "--workers", "-w", help="Parallel API validation workers."),
    min_matching_jobs: int = typer.Option(
        1,
        "--min-matching-jobs",
        help="Only save boards with at least this many title/location-matching jobs.",
    ),
    hours_old: Optional[int] = typer.Option(
        None,
        "--hours-old",
        help="Freshness window for metadata. Defaults to searches.yaml defaults.hours_old.",
    ),
    no_db: bool = typer.Option(False, "--no-db", help="Do not mine existing DB URLs for ATS tokens."),
    no_seeds: bool = typer.Option(False, "--no-seeds", help="Do not probe packaged high-signal company slug seeds."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and print results without writing registry."),
) -> None:
    """Discover more Greenhouse/Lever/Ashby company boards."""
    _bootstrap()

    if search_limit < 0:
        console.print("[red]--search-limit cannot be negative.[/red]")
        raise typer.Exit(code=1)
    if workers < 1:
        console.print("[red]--workers must be at least 1.[/red]")
        raise typer.Exit(code=1)
    if min_matching_jobs < 0:
        console.print("[red]--min-matching-jobs cannot be negative.[/red]")
        raise typer.Exit(code=1)

    queries = [q.strip() for q in query.split(",") if q.strip()] if query else None

    from applypilot.discovery.ats_discovery import discover_ats_boards

    console.print("\n[bold blue]Discovering ATS Boards[/bold blue]")
    result = discover_ats_boards(
        queries=queries,
        search_limit=search_limit,
        workers=workers,
        hours_old=hours_old,
        min_matching_jobs=min_matching_jobs,
        include_db=not no_db,
        include_seeds=not no_seeds,
        write=not dry_run,
    )

    console.print(
        f"  Candidates: {result['candidates']} | "
        f"Validated: {result['validated']} | "
        f"Added: {result['added']}"
    )
    console.print(f"  Registry:   {result['registry_path']}")

    boards = result.get("boards", [])
    if boards:
        table = Table(title="Validated ATS Boards", show_header=True, header_style="bold cyan")
        table.add_column("ATS")
        table.add_column("Token")
        table.add_column("Match", justify="right")
        table.add_column("Fresh", justify="right")
        table.add_column("Sample Titles")
        for board in boards[:25]:
            table.add_row(
                board.ats,
                board.token,
                str(board.matching_jobs),
                str(board.fresh_matching_jobs),
                "; ".join(board.sample_titles[:2]),
            )
        console.print(table)
        if len(boards) > 25:
            console.print(f"[dim]... {len(boards) - 25} more validated boards omitted[/dim]")
    elif dry_run:
        console.print("[yellow]No new matching boards validated.[/yellow]")

    if dry_run:
        console.print("[yellow]Dry run: registry was not updated.[/yellow]")


@app.command("resolve-linkedin")
def resolve_linkedin(
    limit: int = typer.Option(25, "--limit", "-l", help="Maximum LinkedIn rows to inspect."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score to include."),
    write: bool = typer.Option(
        False,
        "--write/--dry-run",
        help="Persist resolved application URLs. Default is dry-run.",
    ),
    headless: bool = typer.Option(True, "--headless/--visible", help="Run the resolver browser headless."),
    login_wait: int = typer.Option(
        180,
        "--login-wait",
        help="Visible mode: seconds to wait while you clear LinkedIn login/CAPTCHA.",
    ),
    retry_hours: float = typer.Option(24.0, "--retry-hours", help="Skip rows resolved or failed within this window."),
    site_contains: Optional[str] = typer.Option(None, "--site-contains", help="Only resolve rows whose site/source contains this text."),
    max_age_hours: Optional[float] = typer.Option(None, "--max-age-hours", help="Only resolve rows discovered within this many hours."),
    corpus_report: bool = typer.Option(False, "--corpus-report", help="Print read-only LinkedIn corpus counters first."),
) -> None:
    """Resolve LinkedIn job listings to direct company/ATS apply URLs."""
    _bootstrap()

    if limit < 1:
        console.print("[red]--limit must be at least 1.[/red]")
        raise typer.Exit(code=1)
    if retry_hours < 0:
        console.print("[red]--retry-hours cannot be negative.[/red]")
        raise typer.Exit(code=1)
    if max_age_hours is not None and max_age_hours <= 0:
        console.print("[red]--max-age-hours must be positive when provided.[/red]")
        raise typer.Exit(code=1)
    if login_wait < 0:
        console.print("[red]--login-wait cannot be negative.[/red]")
        raise typer.Exit(code=1)

    from applypilot.enrichment.linkedin_outbound import (
        linkedin_corpus_report,
        resolve_linkedin_jobs,
    )

    if corpus_report:
        report = linkedin_corpus_report()
        table = Table(title="LinkedIn Corpus", show_header=True, header_style="bold cyan")
        table.add_column("Metric")
        table.add_column("Count", justify="right")
        for key, value in report.items():
            table.add_row(key, str(value or 0))
        console.print(table)

    console.print("\n[bold blue]Resolving LinkedIn Outbound Apply URLs[/bold blue]")
    console.print(f"  Limit:     {limit}")
    console.print(f"  Min score: {min_score}")
    if site_contains:
        console.print(f"  Site:      contains {site_contains}")
    if max_age_hours:
        console.print(f"  Freshness: last {max_age_hours:g}h")
    console.print(f"  Mode:      {'write' if write else 'dry-run'}")
    console.print(f"  Browser:   {'headless' if headless else 'visible'}")
    if not headless:
        console.print(f"  Login wait:{login_wait}s")

    stats = resolve_linkedin_jobs(
        limit=limit,
        min_score=min_score,
        write=write,
        use_browser=True,
        headless=headless,
        manual_unblock_seconds=0 if headless else login_wait,
        retry_hours=retry_hours,
        site_contains=site_contains,
        max_age_hours=max_age_hours,
    )

    console.print(
        f"\nProcessed {stats['processed']}/{stats['candidates']} candidates | "
        f"resolved={stats['resolved']} easy_apply={stats['easy_apply_only']} "
        f"expired={stats['expired']} login={stats['login_blocked']} "
        f"captcha={stats['captcha']} unknown={stats['unknown']} error={stats['error']} "
        f"duplicates={stats['duplicates']}"
    )

    rows = stats.get("results", [])
    if rows:
        table = Table(title="Resolver Results", show_header=True, header_style="bold cyan")
        table.add_column("Status")
        table.add_column("LinkedIn URL")
        table.add_column("Outbound URL")
        table.add_column("Duplicate")
        for row in rows[:25]:
            table.add_row(
                row["status"],
                str(row["url"])[:72],
                str(row.get("outbound_url") or "")[:72],
                str(row.get("duplicate_url") or "")[:48],
            )
        console.print(table)
        if len(rows) > 25:
            console.print(f"[dim]... {len(rows) - 25} more rows omitted[/dim]")

    if not write:
        console.print("[yellow]Dry run: database was not updated. Re-run with --write to persist.[/yellow]")


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: str = typer.Option("1", "--workers", "-w", help="Number of parallel browser workers, or 'auto'."),
    min_score: int = typer.Option(8, "--min-score", help="Minimum fit score for job selection (apply only to strong matches; raise to 9 for near-perfect only)."),
    model: str = typer.Option("sonnet", "--model", "-m", help="Claude model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    no_live: bool = typer.Option(False, "--no-live", "--quiet", help="Disable the live terminal dashboard."),
    job_timeout: int = typer.Option(480, "--job-timeout", help="Max seconds per application before needs_review."),
    verify_threshold: float = typer.Option(0.75, "--verify-threshold", help="Minimum confidence to auto-verify submission."),
    max_transient_retries: int = typer.Option(2, "--max-transient-retries", help="Max retry attempts for transient apply failures."),
    navigation_timeout: int = typer.Option(45, "--navigation-timeout", help="Navigation timeout budget in seconds."),
    interaction_timeout: int = typer.Option(20, "--interaction-timeout", help="Interaction timeout budget in seconds."),
    assert_timeout: int = typer.Option(15, "--assert-timeout", help="Assertion timeout budget in seconds."),
    escalation_mode: str = typer.Option("pause", "--escalation-mode", help="Blocker handling mode: pause or skip."),
    legacy_result_fallback: bool = typer.Option(True, "--legacy-result-fallback/--no-legacy-result-fallback", help="Allow legacy RESULT:* fallback if structured JSON is missing."),
    startup_stagger: float = typer.Option(4.0, "--startup-stagger", help="Seconds to stagger each additional worker startup."),
    max_age_hours: int = typer.Option(24, "--max-age-hours", help="Only apply to jobs discovered within this many hours (freshness gate; 0 = no limit, drain everything)."),
    site_contains: Optional[str] = typer.Option(None, "--site-contains", help="Only apply jobs whose site/source contains this text, e.g. TheirStack."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
    reset_manual: bool = typer.Option(False, "--reset-manual", help="Reset jobs marked manual ATS for retry; combine with --site-contains to scope."),
    resolved_only: bool = typer.Option(False, "--resolved-only", help="With --reset-manual, only reset rows with resolved non-LinkedIn application URLs."),
    prune: bool = typer.Option(True, "--prune/--no-prune", help="Before applying, HTTP-check queued application links and park dead ones as 'expired' (freshness pre-check)."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    if resolved_only and not reset_manual:
        console.print("[red]--resolved-only is only valid with --reset-manual.[/red]")
        raise typer.Exit(code=1)

    if reset_manual:
        from applypilot.apply.launcher import reset_manual as do_reset_manual
        count = do_reset_manual(site_contains=site_contains, resolved_only=resolved_only)
        scope_text = f" matching site '{site_contains}'" if site_contains else ""
        resolved_text = " with resolved outbound URLs" if resolved_only else ""
        console.print(f"[green]Reset {count} manual job(s){scope_text}{resolved_text} for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Claude Code CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: At least one scored job that hasn't been applied to (uses
    # tailored resume if available, master resume.pdf as fallback).
    # Route through the single queue_policy() predicate (Task 12) so this
    # preflight counts the SAME gated-eligible + auto + attempt-cap rows the
    # apply worker would pick, not the old fit_score-only set that overstated
    # readiness. site_contains is a CLI-only extra queue_policy doesn't own.
    if not (gen and url):
        from applypilot.database import queue_policy
        conn = get_connection()
        _frag, ready_params = queue_policy(min_score=min_score)
        site_filter = ""
        if site_contains and not url:
            site_filter = "AND LOWER(site) LIKE ?"
            ready_params.append(f"%{site_contains.lower()}%")
        ready = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE {_frag} {site_filter}",
            ready_params,
        ).fetchone()[0]
        if ready == 0:
            scope_text = f" matching site '{site_contains}'" if site_contains and not url else ""
            console.print(
                f"[red]No scored jobs (score >= {min_score}){scope_text} ready to apply.[/red]\n"
                "Run [bold]applypilot run score[/bold] first to score discovered jobs."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print("\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from applypilot.apply.launcher import main as apply_main, preview_apply_queue

    effective_limit = limit if limit is not None else (0 if continuous else 1)
    if workers.lower() == "auto":
        effective_workers = 2
    else:
        try:
            effective_workers = int(workers)
        except ValueError:
            console.print("[red]--workers must be a positive integer or 'auto'.[/red]")
            raise typer.Exit(code=1)
    if effective_workers < 1:
        console.print("[red]--workers must be at least 1.[/red]")
        raise typer.Exit(code=1)
    if job_timeout < 30:
        console.print("[red]--job-timeout must be at least 30 seconds.[/red]")
        raise typer.Exit(code=1)
    if not (0.0 <= verify_threshold <= 1.0):
        console.print("[red]--verify-threshold must be between 0 and 1.[/red]")
        raise typer.Exit(code=1)
    if max_transient_retries < 0:
        console.print("[red]--max-transient-retries cannot be negative.[/red]")
        raise typer.Exit(code=1)
    if min(navigation_timeout, interaction_timeout, assert_timeout) < 5:
        console.print("[red]--navigation-timeout, --interaction-timeout, and --assert-timeout must be at least 5 seconds.[/red]")
        raise typer.Exit(code=1)
    if escalation_mode not in {"pause", "skip"}:
        console.print("[red]--escalation-mode must be one of: pause, skip.[/red]")
        raise typer.Exit(code=1)
    if startup_stagger < 0:
        console.print("[red]--startup-stagger cannot be negative.[/red]")
        raise typer.Exit(code=1)

    fresh_age = max_age_hours if max_age_hours and max_age_hours > 0 else None

    if prune and not url:
        _preapply_prune(min_score=min_score)

    queue_preview = []
    if not url:
        preview_limit = effective_limit if effective_limit and effective_limit > 0 else 10
        queue_preview = preview_apply_queue(
            limit=preview_limit,
            min_score=min_score,
            max_age_hours=fresh_age,
            site_contains=site_contains,
        )
        if not queue_preview and not continuous:
            age_text = f" in the last {fresh_age}h" if fresh_age else ""
            console.print(
                f"[yellow]No auto-applyable jobs found for score >= {min_score}{age_text}.[/yellow]\n"
                "The remaining candidates are likely manual-only links (for example LinkedIn listing pages) "
                "or jobs without a usable apply URL.\n"
                "Run [bold]applypilot run discover enrich score --quick --target-ready 10[/bold] "
                "to fetch more direct ATS jobs, or use [bold]--max-age-hours 0[/bold] to drain older jobs."
            )
            raise typer.Exit(code=0)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {effective_workers}{' (auto)' if workers.lower() == 'auto' else ''}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    console.print(f"  Live UI:  {not no_live}")
    console.print(f"  Timeout:  {job_timeout}s")
    console.print(f"  Verify:   {verify_threshold:.2f}")
    console.print(f"  Retries:  {max_transient_retries}")
    console.print(f"  Escalate: {escalation_mode}")
    if site_contains and not url:
        console.print(f"  Site:     contains {site_contains}")
    if not url:
        queue_label = "unlimited" if continuous else str(effective_limit)
        console.print(f"  Queue:    {len(queue_preview)}/{queue_label} auto-applyable now")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=effective_workers,
        no_live=no_live,
        job_timeout=job_timeout,
        verify_threshold=verify_threshold,
        max_transient_retries=max_transient_retries,
        navigation_timeout=navigation_timeout,
        interaction_timeout=interaction_timeout,
        assert_timeout=assert_timeout,
        escalation_mode=escalation_mode,
        legacy_result_fallback=legacy_result_fallback,
        startup_stagger=startup_stagger,
        max_age_hours=fresh_age,
        site_contains=site_contains if not url else None,
    )


@app.command()
def report(
    v2_cutover: bool = typer.Option(
        False, "--v2-cutover",
        help="Also print the v2->Greenhouse cutover gate "
             "(§12.1 pass-rate / §12.2 speed p50 / §12.3 safety-audit trio).",
    ),
) -> None:
    """Reliability/cost report from logs/review.jsonl: $/apply, cache
    hit-rate, pass-rate by ATS/tier, and the (A) removable vs
    (B) irreducible failure split."""
    _bootstrap()
    from applypilot import config
    from applypilot.reporting import (
        load_review_rows, summarize_review, format_report, format_v2_cutover,
    )

    rows = load_review_rows(config.LOG_DIR / "review.jsonl")
    if not rows:
        typer.echo("No review.jsonl rows yet — run some applies first.")
        raise typer.Exit()
    typer.echo(format_report(summarize_review(rows)))
    if v2_cutover:
        typer.echo(format_v2_cutover(rows))


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Auto-applyable (all ages)", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def ui(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address (keep on localhost)."),
    port: int = typer.Option(8765, "--port", "-p", help="Port for the dashboard."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the browser automatically."),
) -> None:
    """Launch the local web dashboard (queue, triage, runs console).

    Safe by design: the UI can run discovery/scoring/pruning and DRY-RUN
    applies, but can never start a live apply.
    """
    _bootstrap()

    try:
        import uvicorn  # noqa: F401
        from applypilot.webui.server import create_app
    except ImportError:
        console.print("[red]The web UI needs extra packages:[/red] pip install 'applypilot[ui]'")
        console.print("(or: pip install fastapi uvicorn)")
        raise typer.Exit(1)

    url = f"http://{host}:{port}"
    console.print(f"[bold]ApplyPilot dashboard[/bold] → {url}  (Ctrl+C to stop)")
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


@app.command("prune-expired")
def prune_expired(
    min_score: int = typer.Option(7, "--min-score", help="Only check jobs at or above this score."),
    limit: int = typer.Option(500, "--limit", help="Max queued URLs to check."),
    workers: int = typer.Option(8, "--workers", "-w", help="Parallel HTTP checks."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report without updating the DB."),
) -> None:
    """Check queued application links and park dead ones as 'expired'.

    Run this before an apply batch so attempts aren't wasted on postings
    that closed since discovery. Conservative: only confident 404/'no longer
    open' signals park a row; reversible via apply_error LIKE
    'link_expired_precheck%'.
    """
    _bootstrap()
    import sqlite3 as _sqlite3

    from applypilot.config import DB_PATH
    from applypilot.freshness import check_queue

    conn = _sqlite3.connect(DB_PATH)
    try:
        result = check_queue(conn, min_score=min_score, limit=limit, workers=workers, dry_run=dry_run)
    finally:
        conn.close()

    tag = " (dry run — nothing written)" if dry_run else ""
    console.print(
        f"Checked [bold]{result.checked}[/bold] queued links{tag}: "
        f"[green]{result.live} live[/green], "
        f"[red]{result.expired} expired[/red], "
        f"[yellow]{result.unknown} unknown (left untouched)[/yellow]"
    )
    for u in result.expired_urls[:20]:
        console.print(f"  [red]expired[/red] {u}")
    if len(result.expired_urls) > 20:
        console.print(f"  … and {len(result.expired_urls) - 20} more")


@app.command("gate")
def gate_cmd(
    rerun: bool = typer.Option(False, "--rerun", help="Re-gate all rows whose gate_version is stale."),
) -> None:
    """Run (or re-run) the eligibility gate over stored jobs."""
    _bootstrap()
    from applypilot import database as db
    from applypilot.gate import GATE_VERSION
    from applypilot.gate.engine import gate_job
    from applypilot.gate.profile_map import gate_profile
    from applypilot.config import load_profile, load_search_config
    conn = db.get_connection()
    try:
        policy = gate_profile(load_profile(), load_search_config())
    except Exception:
        policy = gate_profile({}, {})
    if rerun:
        rows = conn.execute("SELECT * FROM jobs WHERE gate_version IS NULL OR gate_version < ?",
                            (GATE_VERSION,)).fetchall()
    else:
        # Same shared predicate as the pipeline 'gate' stage: ungated rows OR
        # rows gated before enrichment finished (stale verdict on thin text).
        rows = db.get_jobs_by_stage(conn, "pending_gate", limit=0)
    n = 0
    for row in rows:
        try:
            db.update_gate(conn, row["url"], gate_job(dict(row), policy))
            n += 1
        except Exception as e:  # noqa: BLE001 — one bad row must not kill the batch
            log.warning("gate failed for %s: %s", row["url"], e)
            continue
    console.print(f"Gated [bold]{n}[/bold] jobs at version {GATE_VERSION}.")


@app.command("resume")
def resume_cmd() -> None:
    """Clear a budget/manual pause so the engine can dispatch again."""
    _bootstrap()
    from applypilot import database as db
    conn = db.get_connection()
    prior = db.paused_reason(conn)
    db.set_paused(conn, None)
    console.print(f"Resumed (was: {prior or 'not paused'}).")


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    if has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        "Set GEMINI_API_KEY in ~/.applypilot/.env (run 'applypilot init')"))

    # --- Tier 3 checks ---
    # Claude Code CLI (also checks fallback install locations on Windows)
    from applypilot.config import find_claude_binary
    claude_bin = find_claude_binary()
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")

    console.print()


if __name__ == "__main__":
    app()
