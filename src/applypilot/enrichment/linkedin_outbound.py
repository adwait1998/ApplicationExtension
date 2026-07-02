"""Resolve LinkedIn job listings to outbound company/ATS apply URLs.

This module deliberately stops at URL resolution. It may open a LinkedIn job
page and click a non-Easy-Apply "Apply" control to reveal the outbound URL, but
it never submits an application or interacts with an ATS form.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os
import re
import sqlite3
import time
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from applypilot import config
from applypilot.apply.chrome import setup_worker_profile
from applypilot.database import get_connection, init_db

log = logging.getLogger(__name__)

RESOLVER_SOURCE = "linkedin_outbound"
LINKEDIN_HOST_RE = re.compile(r"(^|\.)linkedin\.com$", re.I)
LINKEDIN_JOB_PATH_RE = re.compile(r"^/jobs/(view|collections|search)", re.I)
RESOLVER_STATUSES = {
    "resolved",
    "easy_apply_only",
    "expired",
    "login_blocked",
    "captcha",
    "unknown",
    "error",
}


@dataclass(frozen=True)
class LinkedInResolution:
    """Result from attempting to resolve one LinkedIn job row."""

    status: str
    outbound_url: str | None = None
    error: str | None = None
    source: str = RESOLVER_SOURCE
    evidence: str | None = None

    def __post_init__(self) -> None:
        if self.status not in RESOLVER_STATUSES:
            raise ValueError(f"unknown LinkedIn resolver status: {self.status}")
        if self.status == "resolved" and not _valid_http_url(self.outbound_url):
            raise ValueError("resolved LinkedInResolution requires a valid outbound_url")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_linkedin_job_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(str(url))
    except Exception:
        return False
    return bool(LINKEDIN_HOST_RE.search(parsed.netloc) and LINKEDIN_JOB_PATH_RE.search(parsed.path))


def _is_linkedin_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(str(url))
    except Exception:
        return False
    return bool(LINKEDIN_HOST_RE.search(parsed.netloc))


def _valid_http_url(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if text.lower() in {"", "none", "nan", "nat", "null"}:
        return None
    if not text.lower().startswith(("http://", "https://")):
        return None
    return text


def _extract_embedded_url(value: str | None) -> str | None:
    """Pull an outbound URL out of a LinkedIn redirect URL or data attribute."""
    if not value:
        return None

    text = unquote(str(value).strip())
    direct = _valid_http_url(text)
    if direct and not _is_linkedin_url(direct):
        return direct

    try:
        parsed = urlparse(text)
    except Exception:
        parsed = None

    if parsed:
        for values in parse_qs(parsed.query).values():
            for item in values:
                found = _extract_embedded_url(item)
                if found:
                    return found

    match = re.search(r"https?://(?![^/\s\"']*linkedin\.com)[^\s\"'<>]+", text, re.I)
    if match:
        return match.group(0).rstrip(").,;")
    return None


def _safe_outbound_url(url: str | None) -> str | None:
    url = _extract_embedded_url(url) or _valid_http_url(url)
    if not url or _is_linkedin_url(url) or config.is_manual_ats(url):
        return None
    return url


def classify_linkedin_html(html: str, page_url: str = "") -> LinkedInResolution:
    """Classify a LinkedIn job page from static HTML without side effects."""
    text = BeautifulSoup(html or "", "html.parser").get_text("\n", strip=True)
    low = text.lower()
    page_low = (page_url or "").lower()

    if any(token in page_low for token in ("/checkpoint/", "captcha", "challenge")) or any(
        token in low for token in ("security verification", "captcha", "verify you're not a robot")
    ):
        return LinkedInResolution("captcha", error="captcha_or_security_check")

    login_markers = (
        "sign in to view",
        "sign in to linkedin",
        "join linkedin",
        "authwall",
    )
    if "linkedin.com/login" in page_low or "authwall" in page_low or any(m in low for m in login_markers):
        return LinkedInResolution("login_blocked", error="login_required")

    expired_markers = (
        "no longer accepting applications",
        "this job is no longer accepting applications",
        "this job is no longer available",
        "job has expired",
        "position has been filled",
    )
    if any(m in low for m in expired_markers):
        return LinkedInResolution("expired", error="expired_or_closed")

    soup = BeautifulSoup(html or "", "html.parser")
    for element in soup.select("a[href], button"):
        parts = [
            element.get_text(" ", strip=True),
            element.get("aria-label", ""),
            element.get("data-control-name", ""),
            element.get("data-tracking-control-name", ""),
        ]
        label = " ".join(p for p in parts if p).lower()
        attrs = " ".join(str(v) for v in element.attrs.values())
        if "apply" not in label and "externalapply" not in attrs.lower():
            continue
        if "easy apply" in label:
            continue
        href = element.get("href")
        outbound = _safe_outbound_url(urljoin(page_url, href) if href else None)
        if not outbound:
            outbound = _safe_outbound_url(attrs)
        if outbound:
            return LinkedInResolution("resolved", outbound_url=outbound, evidence="static_apply_link")

    if "easy apply" in low:
        return LinkedInResolution("easy_apply_only", error="easy_apply_only")
    return LinkedInResolution("unknown", error="no_outbound_apply_link")


def _candidate_selector_js() -> str:
    return """
    () => {
      const nodes = Array.from(document.querySelectorAll('a, button'));
      const candidates = [];
      let sawEasyApply = false;
      for (const el of nodes) {
        const text = [
          el.innerText || '',
          el.getAttribute('aria-label') || '',
          el.getAttribute('data-control-name') || '',
          el.getAttribute('data-tracking-control-name') || ''
        ].join(' ').trim();
        const low = text.toLowerCase();
        if (!low.includes('apply')) continue;
        if (low.includes('easy apply')) {
          sawEasyApply = true;
          continue;
        }
        el.dataset.applypilotLinkedinApplyCandidate = String(candidates.length);
        candidates.push({
          index: candidates.length,
          text,
          href: el.href || el.getAttribute('href') || '',
          tag: el.tagName.toLowerCase()
        });
      }
      return {sawEasyApply, candidates};
    }
    """


def _wait_for_manual_unblock(page, *, seconds: int) -> bool:
    """Wait while the operator clears login/CAPTCHA in a visible browser."""
    if seconds <= 0:
        return False
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            current = classify_linkedin_html(page.content(), page.url)
        except Exception:
            current = LinkedInResolution("unknown", error="page_unreadable")
        if current.status not in {"login_blocked", "captcha"}:
            return True
        time.sleep(2)
    return False


def resolve_linkedin_with_page(
    page,
    url: str,
    *,
    click_timeout_ms: int = 7000,
    manual_unblock_seconds: int = 0,
) -> LinkedInResolution:
    """Resolve one LinkedIn URL using an existing Playwright page."""
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if response and response.status in {404, 410}:
            return LinkedInResolution("expired", error=f"HTTP {response.status}")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeoutError:
            pass

        html_result = classify_linkedin_html(page.content(), page.url)
        if html_result.status in {"login_blocked", "captcha"} and manual_unblock_seconds > 0:
            log.info(
                "LinkedIn resolver waiting up to %ds for manual %s clearance in visible browser...",
                manual_unblock_seconds,
                html_result.status,
            )
            if _wait_for_manual_unblock(page, seconds=manual_unblock_seconds):
                return resolve_linkedin_with_page(
                    page,
                    url,
                    click_timeout_ms=click_timeout_ms,
                    manual_unblock_seconds=0,
                )
        if html_result.status in {"resolved", "expired", "login_blocked", "captcha", "easy_apply_only"}:
            return html_result

        data = page.evaluate(_candidate_selector_js())
        for candidate in data.get("candidates", []):
            outbound = _safe_outbound_url(candidate.get("href"))
            if outbound:
                return LinkedInResolution("resolved", outbound_url=outbound, evidence="browser_href")

        if not data.get("candidates"):
            if data.get("sawEasyApply"):
                return LinkedInResolution("easy_apply_only", error="easy_apply_only")
            return html_result

        before_pages = set(page.context.pages)
        locator = page.locator('[data-applypilot-linkedin-apply-candidate="0"]').first
        try:
            locator.click(timeout=click_timeout_ms)
        except PlaywrightTimeoutError:
            return LinkedInResolution("unknown", error="apply_button_click_timeout")

        time.sleep(1.5)
        pages = page.context.pages
        new_pages = [p for p in pages if p not in before_pages]
        target_page = new_pages[-1] if new_pages else page
        try:
            target_page.wait_for_load_state("domcontentloaded", timeout=10000)
        except PlaywrightTimeoutError:
            pass

        outbound = _safe_outbound_url(target_page.url)
        if outbound:
            return LinkedInResolution("resolved", outbound_url=outbound, evidence="browser_click_redirect")

        html_result = classify_linkedin_html(target_page.content(), target_page.url)
        if html_result.status == "resolved":
            return html_result
        if data.get("sawEasyApply"):
            return LinkedInResolution("easy_apply_only", error="easy_apply_only")
        return LinkedInResolution("unknown", error="click_did_not_reveal_outbound")
    except Exception as exc:
        return LinkedInResolution("error", error=str(exc)[:240])


def resolve_linkedin_browser(
    url: str,
    *,
    headless: bool = True,
    worker_id: int | None = None,
    manual_unblock_seconds: int = 0,
) -> LinkedInResolution:
    """Open LinkedIn with an authenticated worker profile and resolve outbound Apply."""
    worker_id = worker_id if worker_id is not None else int(os.environ.get("APPLYPILOT_LINKEDIN_WORKER_ID", "50"))
    profile_dir = setup_worker_profile(worker_id)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            executable_path=config.get_chrome_path(),
            headless=headless,
            viewport={"width": 1280, "height": 900},
            args=[
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-session-crashed-bubble",
                "--disable-notifications",
                "--deny-permission-prompts",
            ],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            return resolve_linkedin_with_page(
                page,
                url,
                manual_unblock_seconds=manual_unblock_seconds,
            )
        finally:
            context.close()


def _row_linkedin_url(row: sqlite3.Row | dict) -> str | None:
    data = dict(row)
    app_url = _valid_http_url(data.get("application_url"))
    if is_linkedin_job_url(app_url):
        return app_url
    url = _valid_http_url(data.get("url"))
    if is_linkedin_job_url(url):
        return url
    return None


def select_linkedin_candidates(
    conn: sqlite3.Connection,
    *,
    limit: int = 25,
    min_score: int = 0,
    retry_hours: float = 24.0,
    site_contains: str | None = None,
    max_age_hours: float | None = None,
) -> list[sqlite3.Row]:
    """Select unresolved LinkedIn rows, including old manual rows."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=retry_hours)).isoformat()
    params: list = [min_score]
    retry_clause = ""
    if retry_hours > 0:
        retry_clause = "AND (application_url_resolved_at IS NULL OR application_url_resolved_at < ?)"
        params.append(cutoff)
    site_clause = ""
    if site_contains:
        site_clause = "AND LOWER(site) LIKE ?"
        params.append(f"%{site_contains.lower()}%")
    age_clause = ""
    if max_age_hours is not None and max_age_hours > 0:
        age_cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        age_clause = "AND discovered_at >= ?"
        params.append(age_cutoff)
    query = f"""
        SELECT *
        FROM jobs
        WHERE fit_score >= ?
          AND (
            LOWER(COALESCE(url, '')) LIKE '%linkedin.com/jobs/%'
            OR LOWER(COALESCE(application_url, '')) LIKE '%linkedin.com/jobs/%'
          )
          AND (
            application_url IS NULL
            OR TRIM(application_url) = ''
            OR LOWER(application_url) IN ('none', 'null', 'nan', 'nat')
            OR LOWER(application_url) LIKE '%linkedin.com/jobs/%'
            OR apply_status = 'manual'
          )
          {retry_clause}
          {site_clause}
          {age_clause}
        ORDER BY fit_score DESC NULLS LAST, discovered_at DESC NULLS LAST
        LIMIT ?
    """
    params.append(limit)
    return conn.execute(query, params).fetchall()


def _duplicate_for_outbound(conn: sqlite3.Connection, source_url: str, outbound_url: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT url, apply_status, applied_at
        FROM jobs
        WHERE url != ?
          AND (
            url = ?
            OR application_url = ?
          )
        LIMIT 1
        """,
        (source_url, outbound_url, outbound_url),
    ).fetchone()


def _can_restore_manual_status(row: dict) -> bool:
    if row.get("apply_status") != "manual":
        return False
    error = (row.get("apply_error") or "").strip().lower()
    return error in {"", "manual ats", "no apply url"}


def write_resolution(
    conn: sqlite3.Connection,
    row: sqlite3.Row | dict,
    result: LinkedInResolution,
    *,
    write: bool,
    now: str | None = None,
) -> dict:
    """Persist resolver metadata and return a small write summary."""
    data = dict(row)
    now = now or utc_now()
    duplicate = None
    if result.status == "resolved" and result.outbound_url:
        duplicate = _duplicate_for_outbound(conn, data["url"], result.outbound_url)

    summary = {
        "url": data.get("url"),
        "status": result.status,
        "outbound_url": result.outbound_url,
        "duplicate_url": duplicate["url"] if duplicate else None,
        "would_clear_manual": bool(
            result.status == "resolved"
            and not duplicate
            and _can_restore_manual_status(data)
        ),
    }

    if not write:
        return summary

    if result.status == "resolved" and result.outbound_url:
        if duplicate:
            conn.execute(
                """
                UPDATE jobs
                SET application_url = ?,
                    application_url_source = ?,
                    application_url_resolved_at = ?,
                    application_url_error = NULL,
                    apply_status = 'duplicate',
                    apply_error = ?
                WHERE url = ?
                """,
                (
                    result.outbound_url,
                    result.source,
                    now,
                    f"duplicate resolved application URL: {duplicate['url']}",
                    data["url"],
                ),
            )
        elif _can_restore_manual_status(data):
            conn.execute(
                """
                UPDATE jobs
                SET application_url = ?,
                    application_url_source = ?,
                    application_url_resolved_at = ?,
                    application_url_error = NULL,
                    apply_status = NULL,
                    apply_error = NULL
                WHERE url = ?
                """,
                (result.outbound_url, result.source, now, data["url"]),
            )
        else:
            conn.execute(
                """
                UPDATE jobs
                SET application_url = ?,
                    application_url_source = ?,
                    application_url_resolved_at = ?,
                    application_url_error = NULL
                WHERE url = ?
                """,
                (result.outbound_url, result.source, now, data["url"]),
            )
    else:
        conn.execute(
            """
            UPDATE jobs
            SET application_url_source = ?,
                application_url_resolved_at = ?,
                application_url_error = ?
            WHERE url = ?
            """,
            (result.source, now, result.error or result.status, data["url"]),
        )

    conn.commit()
    return summary


def resolve_linkedin_jobs(
    conn: sqlite3.Connection | None = None,
    *,
    limit: int = 25,
    min_score: int = 0,
    write: bool = False,
    use_browser: bool = True,
    headless: bool = True,
    manual_unblock_seconds: int = 0,
    retry_hours: float = 24.0,
    site_contains: str | None = None,
    max_age_hours: float | None = None,
    resolver=None,
) -> dict:
    """Resolve a batch of LinkedIn rows and optionally persist results."""
    own_conn = conn is None
    if conn is None:
        conn = init_db()

    rows = select_linkedin_candidates(
        conn,
        limit=limit,
        min_score=min_score,
        retry_hours=retry_hours,
        site_contains=site_contains,
        max_age_hours=max_age_hours,
    )
    stats = {
        "candidates": len(rows),
        "processed": 0,
        "resolved": 0,
        "easy_apply_only": 0,
        "expired": 0,
        "login_blocked": 0,
        "captcha": 0,
        "unknown": 0,
        "error": 0,
        "duplicates": 0,
        "write": write,
        "results": [],
    }

    if resolver is None:
        resolver = resolve_linkedin_browser if use_browser else None

    try:
        for row in rows:
            linkedin_url = _row_linkedin_url(row)
            if not linkedin_url:
                continue
            if resolver is None:
                result = LinkedInResolution("unknown", error="browser_disabled")
            else:
                result = resolver(
                    linkedin_url,
                    headless=headless,
                    manual_unblock_seconds=manual_unblock_seconds,
                )
            summary = write_resolution(conn, row, result, write=write)
            stats["processed"] += 1
            stats[result.status] += 1
            if summary.get("duplicate_url"):
                stats["duplicates"] += 1
            stats["results"].append(summary)
            log.info("LinkedIn resolver: %s -> %s %s", linkedin_url, result.status, result.outbound_url or result.error or "")
    finally:
        if own_conn:
            conn.close()

    return stats


def linkedin_corpus_report(conn: sqlite3.Connection | None = None) -> dict:
    """Read-only LinkedIn corpus counters for Ralph iteration 0."""
    own_conn = conn is None
    if conn is None:
        conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN fit_score >= 8 THEN 1 ELSE 0 END) AS high_score,
              SUM(CASE WHEN application_url IS NOT NULL
                        AND LOWER(application_url) NOT IN ('', 'none', 'null', 'nan', 'nat')
                       THEN 1 ELSE 0 END) AS with_application_url,
              SUM(CASE WHEN apply_status = 'manual' THEN 1 ELSE 0 END) AS manual_rows,
              SUM(CASE WHEN LOWER(COALESCE(application_url_error, '')) LIKE '%easy_apply%'
                       THEN 1 ELSE 0 END) AS easy_apply_candidates,
              SUM(CASE WHEN LOWER(COALESCE(application_url_error, '')) LIKE '%expired%'
                       THEN 1 ELSE 0 END) AS expired_or_private
            FROM jobs
            WHERE LOWER(COALESCE(url, '')) LIKE '%linkedin.com/jobs/%'
               OR LOWER(COALESCE(application_url, '')) LIKE '%linkedin.com/jobs/%'
            """
        ).fetchone()
        return dict(row)
    finally:
        if own_conn:
            conn.close()
