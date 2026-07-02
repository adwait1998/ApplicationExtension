"""Direct ATS job board scraper: Greenhouse, Lever, Ashby.

Hits the public JSON APIs of company-specific job boards. Each company
posts on one of the three major modern ATSes; this module reads the package
registry plus the user's discovered ats_companies.yaml overlay.

Why direct APIs vs HTML scraping:
  - 10-50x faster (single JSON request vs full page render)
  - No CAPTCHA / anti-bot
  - Stable structure (vs DOM that changes)
  - Direct apply_url returned (no enrichment needed)
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import httpx
from applypilot import config
from applypilot.database import get_connection, init_db
from applypilot.discovery.ats_discovery import load_ats_registry
from applypilot.discovery.freshness import iso_or_none, is_recent
from applypilot.discovery.location_filter import load_location_filter, location_ok
from applypilot.discovery.title_filter import build_title_filter, title_matches

log = logging.getLogger(__name__)

_TIMEOUT = 20
_USER_AGENT = "Mozilla/5.0 (compatible; ApplyPilotBot/1.0)"


def _load_config() -> dict:
    return load_ats_registry()


# Module-level cache so the filter lists are loaded once per process, not
# once per fetched job. discover() resets this at the top of each run so
# searches.yaml edits take effect on the next discover.
_LOCATION_FILTER_CACHE: tuple[list[str], list[str]] | None = None


def _get_location_filter() -> tuple[list[str], list[str]]:
    global _LOCATION_FILTER_CACHE
    if _LOCATION_FILTER_CACHE is None:
        _LOCATION_FILTER_CACHE = load_location_filter()
    return _LOCATION_FILTER_CACHE


def _fetch_greenhouse(
    company: str,
    title_include: list[str],
    title_exclude: list[str],
    hours_old: int | None,
    keep_unknown_posted_at: bool,
) -> list[dict]:
    """Return job dicts from boards-api.greenhouse.io/v1/boards/{token}/jobs."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true"
    try:
        resp = httpx.get(url, timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("Greenhouse fetch failed for %s: %s", company, e)
        return []

    accept, reject = _get_location_filter()
    jobs: list[dict] = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        if not title_matches(title, title_include, title_exclude):
            continue
        posted_at = j.get("first_published") or j.get("updated_at")
        if not is_recent(posted_at, hours_old, keep_unknown=keep_unknown_posted_at):
            continue
        loc = (j.get("location") or {}).get("name", "")
        if not location_ok(loc, accept, reject):
            continue
        # Strip HTML tags from content for plain description
        import re
        desc = re.sub(r"<[^>]+>", " ", j.get("content", "") or "")
        desc = re.sub(r"\s+", " ", desc).strip()[:5000]
        jobs.append({
            "url": j.get("absolute_url"),
            "title": title,
            "location": loc,
            "description": desc,
            "salary": None,
            "posted_at": iso_or_none(posted_at),
        })
    return jobs


def _fetch_lever(
    company: str,
    title_include: list[str],
    title_exclude: list[str],
    hours_old: int | None,
    keep_unknown_posted_at: bool,
) -> list[dict]:
    """Return job dicts from api.lever.co/v0/postings/{company}?mode=json."""
    url = f"https://api.lever.co/v0/postings/{company}?mode=json"
    try:
        resp = httpx.get(url, timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("Lever fetch failed for %s: %s", company, e)
        return []

    accept, reject = _get_location_filter()
    jobs: list[dict] = []
    for j in data:
        title = j.get("text", "")
        if not title_matches(title, title_include, title_exclude):
            continue
        posted_at = j.get("createdAt")
        if not is_recent(posted_at, hours_old, keep_unknown=keep_unknown_posted_at):
            continue
        cats = j.get("categories", {}) or {}
        loc = cats.get("location", "")
        if not location_ok(loc, accept, reject):
            continue
        desc = (j.get("descriptionPlain") or j.get("description", ""))[:5000]
        jobs.append({
            "url": j.get("hostedUrl"),
            "title": title,
            "location": loc,
            "description": desc,
            "salary": None,
            "posted_at": iso_or_none(posted_at),
        })
    return jobs


def _fetch_ashby(
    company: str,
    title_include: list[str],
    title_exclude: list[str],
    hours_old: int | None,
    keep_unknown_posted_at: bool,
) -> list[dict]:
    """Return job dicts from api.ashbyhq.com/posting-api/job-board/{org}."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{company}?includeCompensation=true"
    try:
        resp = httpx.get(url, timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("Ashby fetch failed for %s: %s", company, e)
        return []

    accept, reject = _get_location_filter()
    jobs: list[dict] = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        if not title_matches(title, title_include, title_exclude):
            continue
        posted_at = j.get("publishedAt")
        if not is_recent(posted_at, hours_old, keep_unknown=keep_unknown_posted_at):
            continue
        loc = j.get("location", "") or ""
        if not location_ok(loc, accept, reject):
            continue
        desc = (j.get("descriptionPlain") or j.get("descriptionHtml", ""))[:5000]
        jobs.append({
            "url": j.get("jobUrl"),
            "title": title,
            "location": loc,
            "description": desc,
            "salary": None,
            "posted_at": iso_or_none(posted_at),
        })
    return jobs


_FETCHERS = {
    "greenhouse": _fetch_greenhouse,
    "lever": _fetch_lever,
    "ashby": _fetch_ashby,
}


def run_ats_boards_discovery(workers: int = 4, hours_old: int | None = None) -> dict:
    """Crawl all configured ATS company boards and store matching jobs."""
    init_db()
    # Reload location filter so edits to searches.yaml take effect each run.
    global _LOCATION_FILTER_CACHE
    _LOCATION_FILTER_CACHE = None
    cfg = _load_config()
    search_cfg = config.load_search_config() or {}
    if hours_old is None:
        hours_old = search_cfg.get("defaults", {}).get("hours_old", 72)
    title_include, title_exclude = build_title_filter(search_cfg, local_cfg=cfg)
    keep_unknown_posted_at = bool(cfg.get("keep_unknown_posted_at", True))

    work: list[tuple[str, str]] = []  # (ats, company)
    for ats in _FETCHERS:
        for company in cfg.get(ats, []) or []:
            work.append((ats, company))

    if not work:
        log.info("No ATS companies configured.")
        return {"total": 0, "new": 0}

    log.info(
        "Crawling %d ATS company boards (Greenhouse/Lever/Ashby), hours_old=%s...",
        len(work), hours_old,
    )
    t0 = time.time()
    all_jobs: list[tuple[str, str, list[dict]]] = []  # (ats, company, jobs)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _FETCHERS[ats],
                company,
                title_include,
                title_exclude,
                hours_old,
                keep_unknown_posted_at,
            ): (ats, company)
            for ats, company in work
        }
        for fut in as_completed(futures):
            ats, company = futures[fut]
            try:
                jobs = fut.result()
                all_jobs.append((ats, company, jobs))
                if jobs:
                    log.info("  %s/%s: %d matching jobs", ats, company, len(jobs))
            except Exception as e:
                log.warning("  %s/%s failed: %s", ats, company, e)

    # Store all. We bypass store_jobs() so we can populate full_description
    # AND application_url from the API directly — no enrichment needed.
    import sqlite3
    conn = get_connection()
    total_new = 0
    total_dup = 0
    now = datetime.now(timezone.utc).isoformat()
    for ats, company, jobs in all_jobs:
        if not jobs:
            continue
        site_label = f"{company} ({ats})"
        strategy = f"ats_api:{ats}"
        for j in jobs:
            url = j.get("url")
            if not url:
                continue
            try:
                conn.execute(
                    "INSERT INTO jobs ("
                    "url, title, salary, description, full_description, "
                    "application_url, location, site, strategy, "
                    "discovered_at, detail_scraped_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        url, j.get("title"), j.get("salary"),
                        j.get("description"), j.get("description"),
                        url,  # application_url = job URL (apply on same page)
                        j.get("location"), site_label, strategy,
                        j.get("posted_at") or now, now,
                    ),
                )
                total_new += 1
            except sqlite3.IntegrityError:
                total_dup += 1
    conn.commit()

    elapsed = time.time() - t0
    total = sum(len(j) for _, _, j in all_jobs)
    log.info(
        "ATS boards: %d total, %d new, %d duplicates, %.1fs",
        total, total_new, total_dup, elapsed,
    )
    return {"total": total, "new": total_new, "duplicates": total_dup}
