"""TheirStack API job discovery.

Fetches paid/API-backed job search results from TheirStack and stores them in
the same jobs table used by the rest of the pipeline. The implementation is
kept as an explicit discovery source because TheirStack consumes credits for
returned jobs.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from math import ceil
from typing import Any

import httpx

from applypilot import config
from applypilot.database import get_connection, init_db
from applypilot.discovery.freshness import iso_or_none, is_recent
from applypilot.discovery.location_filter import load_location_filter, location_ok
from applypilot.discovery.title_filter import build_title_filter, title_matches

log = logging.getLogger(__name__)

API_URL = "https://api.theirstack.com/v1/jobs/search"
_TIMEOUT = 30
_MAX_RETRIES = 2
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_TOKEN_ENV_KEYS = ("THEIRSTACK_API_KEY", "APPLYPILOT_THEIRSTACK_API_KEY")


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(v).strip() for v in value if str(v).strip()]


def _valid_http_url(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "nan", "nat", "null"}:
        return None
    if not text.lower().startswith(("http://", "https://")):
        return None
    return text


def _get_token(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit.strip()
    for key in _TOKEN_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return None


def _posted_at_max_age_days(hours_old: int | None) -> int:
    if not hours_old or hours_old <= 0:
        return 30
    return max(1, ceil(hours_old / 24))


def _query_terms(search_cfg: dict, source_cfg: dict) -> list[str]:
    configured = _as_list(source_cfg.get("job_title_or"))
    if configured:
        return configured

    tiers = search_cfg.get("tiers")
    queries = search_cfg.get("queries", []) or []
    terms: list[str] = []
    for query in queries:
        if tiers and query.get("tier") not in tiers:
            continue
        text = str(query.get("query", "")).strip()
        if text:
            terms.append(text)

    seen: set[str] = set()
    deduped: list[str] = []
    for term in terms:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(term)
    return deduped


def _location_patterns(search_cfg: dict, source_cfg: dict) -> tuple[list[str], bool]:
    configured = _as_list(source_cfg.get("job_location_pattern_or"))
    if configured:
        return configured, False

    patterns: list[str] = []
    has_remote = False
    for item in search_cfg.get("locations", []) or []:
        location = str(item.get("location", "")).strip()
        if not location:
            continue
        if item.get("remote") or "remote" in location.lower():
            has_remote = True
        else:
            patterns.append(location.split(",")[0].strip() or location)
    return patterns, has_remote


def _country_codes(search_cfg: dict, source_cfg: dict) -> list[str]:
    configured = _as_list(source_cfg.get("job_country_code_or"))
    if configured:
        return [c.upper() for c in configured]

    country = str(search_cfg.get("country", "")).strip().lower()
    if country in {"usa", "us", "united states", "united states of america"}:
        return ["US"]
    return []


def _base_payload(
    search_cfg: dict,
    source_cfg: dict,
    *,
    limit: int,
    page: int,
    hours_old: int | None,
) -> dict:
    payload: dict[str, Any] = {
        "limit": limit,
        "page": page,
        "include_total_results": False,
        "posted_at_max_age_days": _posted_at_max_age_days(hours_old),
    }

    title_terms = _query_terms(search_cfg, source_cfg)
    if title_terms:
        payload["job_title_or"] = title_terms

    country_codes = _country_codes(search_cfg, source_cfg)
    if country_codes:
        payload["job_country_code_or"] = country_codes

    # User-supplied TheirStack filters pass through last so advanced users can
    # use any API-supported field without needing code changes.
    extra = source_cfg.get("filters") or {}
    if isinstance(extra, dict):
        payload.update(extra)

    return payload


def _request_page(client: httpx.Client, token: str, payload: dict) -> list[dict]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "ApplyPilot/0.3 TheirStack discovery",
    }

    for attempt in range(_MAX_RETRIES + 1):
        resp = client.post(API_URL, json=payload, headers=headers)
        if resp.status_code not in _RETRY_STATUSES:
            break

        wait = _retry_after_seconds(resp) or (2 ** attempt)
        log.warning("TheirStack returned %s; retrying in %.1fs", resp.status_code, wait)
        time.sleep(wait)

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = _error_detail(exc.response)
        raise RuntimeError(f"TheirStack API error {exc.response.status_code}: {detail}") from exc

    data = resp.json()
    rows = data.get("data", [])
    if not isinstance(rows, list):
        raise RuntimeError("TheirStack API response did not contain a list under 'data'.")
    return rows


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After") or resp.headers.get("RateLimit-Reset")
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None


def _error_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:500]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("description") or error.get("title") or error)
    return str(data)[:500]


def _best_url(job: dict) -> str | None:
    return (
        _valid_http_url(job.get("final_url"))
        or _valid_http_url(job.get("url"))
        or _valid_http_url(job.get("source_url"))
    )


def _best_location(job: dict) -> str | None:
    parts = [
        job.get("long_location"),
        job.get("location"),
        job.get("short_location"),
    ]
    location = next((str(p).strip() for p in parts if str(p or "").strip()), None)
    if job.get("remote") and (not location or "remote" not in location.lower()):
        return f"Remote, {location}" if location else "Remote"
    return location


def _salary(job: dict) -> str | None:
    salary = str(job.get("salary_string") or "").strip()
    if salary:
        return salary

    min_salary = job.get("min_annual_salary_usd") or job.get("min_annual_salary")
    max_salary = job.get("max_annual_salary_usd") or job.get("max_annual_salary")
    if min_salary and max_salary:
        return f"${int(float(min_salary)):,}-${int(float(max_salary)):,}/year"
    if min_salary:
        return f"${int(float(min_salary)):,}+/year"
    if max_salary:
        return f"Up to ${int(float(max_salary)):,}/year"
    return None


def _company(job: dict) -> str | None:
    company = str(job.get("company") or "").strip()
    if company:
        return company
    company_obj = job.get("company_object") or {}
    if isinstance(company_obj, dict):
        return str(company_obj.get("name") or "").strip() or None
    return None


def _normalize_job(job: dict) -> dict | None:
    url = _best_url(job)
    title = str(job.get("job_title") or job.get("normalized_title") or "").strip()
    if not url or not title:
        return None

    description = str(job.get("description") or "").strip() or None
    company = _company(job)
    return {
        "url": url,
        "title": title,
        "salary": _salary(job),
        "description": description,
        "location": _best_location(job),
        "site": f"{company} (TheirStack)" if company else "TheirStack",
        "posted_at": iso_or_none(job.get("date_posted")) or iso_or_none(job.get("discovered_at")),
        "full_description": description,
        "application_url": url,
    }


def _filter_jobs(
    rows: list[dict],
    *,
    title_include: list[str],
    title_exclude: list[str],
    location_accept: list[str],
    location_reject: list[str],
    hours_old: int | None,
    keep_unknown_posted_at: bool,
) -> tuple[list[dict], int]:
    jobs: list[dict] = []
    filtered = 0

    for row in rows:
        job = _normalize_job(row)
        if not job:
            filtered += 1
            continue
        if not title_matches(job["title"], title_include, title_exclude):
            filtered += 1
            continue
        if not location_ok(job["location"], location_accept, location_reject):
            filtered += 1
            continue
        if not is_recent(job["posted_at"], hours_old, keep_unknown=keep_unknown_posted_at):
            filtered += 1
            continue
        jobs.append(job)

    return jobs, filtered


def _store_jobs(conn: sqlite3.Connection, jobs: list[dict]) -> tuple[int, int]:
    from applypilot.gate.engine import gate_job
    from applypilot.gate.profile_map import gate_profile
    from applypilot import database as _db
    from applypilot.config import load_profile, load_search_config
    try:
        _policy = gate_profile(load_profile(), load_search_config())
    except Exception:
        _policy = gate_profile({}, {})   # missing profile -> permissive-ish defaults; rows still gated

    now = datetime.now(timezone.utc).isoformat()
    new = 0
    duplicates = 0

    for job in jobs:
        detail_scraped_at = now if job.get("full_description") else None
        job_row = {
            "url": job["url"],
            "title": job["title"],
            "salary": job.get("salary"),
            "description": job.get("description"),
            "full_description": job.get("full_description"),
            "application_url": job.get("application_url"),
            "location": job.get("location"),
            "site": job.get("site"),
            "posted_at": job.get("posted_at"),
            "detail_scraped_at": detail_scraped_at,
        }
        try:
            if _db.store_gated(conn, job_row, gate_job(job_row, _policy), strategy="theirstack_api"):
                new += 1
            else:
                duplicates += 1
        except Exception as e:  # noqa: BLE001 — one bad row must not kill the batch
            log.warning("gate/store failed for %s: %s", job_row.get("url"), e)
            continue

    return new, duplicates


def _payloads_for_run(
    search_cfg: dict,
    source_cfg: dict,
    *,
    limit: int,
    hours_old: int | None,
) -> list[dict]:
    location_patterns, has_remote = _location_patterns(search_cfg, source_cfg)
    explicit_remote = source_cfg.get("remote", None)
    base = _base_payload(search_cfg, source_cfg, limit=limit, page=0, hours_old=hours_old)

    if explicit_remote is not None:
        payload = dict(base)
        payload["remote"] = bool(explicit_remote)
        if location_patterns and not bool(explicit_remote):
            payload["job_location_pattern_or"] = location_patterns
        return [payload]

    payloads: list[dict] = []
    if location_patterns:
        local_payload = dict(base)
        local_payload["remote"] = False
        local_payload["job_location_pattern_or"] = location_patterns
        payloads.append(local_payload)
    if has_remote:
        remote_payload = dict(base)
        remote_payload["remote"] = True
        payloads.append(remote_payload)
    if not payloads:
        payloads.append(base)
    return payloads


def run_theirstack_discovery(
    cfg: dict | None = None,
    *,
    client: httpx.Client | None = None,
) -> dict:
    """Fetch jobs from TheirStack and store them in ApplyPilot's DB."""
    if cfg is None:
        cfg = config.load_search_config() or {}
    source_cfg = cfg.get("theirstack") or {}

    token = _get_token(source_cfg.get("api_key"))
    if not token:
        log.warning("TheirStack source skipped: set THEIRSTACK_API_KEY or APPLYPILOT_THEIRSTACK_API_KEY.")
        return {"status": "skipped", "reason": "missing_api_key", "total": 0, "new": 0, "duplicates": 0}

    init_db()
    defaults = cfg.get("defaults", {}) or {}
    hours_old = source_cfg.get("hours_old", defaults.get("hours_old", 72))
    limit = int(source_cfg.get("limit", defaults.get("results_per_site", 25)))
    max_results = int(source_cfg.get("max_results", limit))
    limit = max(1, min(limit, max_results))
    max_pages = max(1, ceil(max_results / limit))
    keep_unknown_posted_at = bool(source_cfg.get("keep_unknown_posted_at", True))

    title_include, title_exclude = build_title_filter(cfg, local_cfg=source_cfg)
    location_accept, location_reject = load_location_filter(cfg)
    payloads = _payloads_for_run(cfg, source_cfg, limit=limit, hours_old=hours_old)

    owns_client = client is None
    http_client = client or httpx.Client(timeout=_TIMEOUT)

    total_returned = 0
    total_filtered = 0
    all_jobs: list[dict] = []
    try:
        for base_payload in payloads:
            for page in range(max_pages):
                if total_returned >= max_results:
                    break
                payload = dict(base_payload)
                payload["page"] = page
                remaining = max_results - total_returned
                payload["limit"] = min(limit, remaining)

                rows = _request_page(http_client, token, payload)
                total_returned += len(rows)
                jobs, filtered = _filter_jobs(
                    rows,
                    title_include=title_include,
                    title_exclude=title_exclude,
                    location_accept=location_accept,
                    location_reject=location_reject,
                    hours_old=hours_old,
                    keep_unknown_posted_at=keep_unknown_posted_at,
                )
                all_jobs.extend(jobs)
                total_filtered += filtered
                if len(rows) < payload["limit"]:
                    break
    finally:
        if owns_client:
            http_client.close()

    # De-dupe within this run before touching SQLite.
    deduped: list[dict] = []
    seen: set[str] = set()
    for job in all_jobs:
        if job["url"] in seen:
            continue
        seen.add(job["url"])
        deduped.append(job)

    conn = get_connection()
    new, duplicates = _store_jobs(conn, deduped)
    log.info(
        "TheirStack: %d returned, %d filtered, %d new, %d duplicates",
        total_returned, total_filtered, new, duplicates,
    )

    return {
        "status": "ok",
        "total": total_returned,
        "filtered": total_filtered,
        "new": new,
        "duplicates": duplicates,
    }
