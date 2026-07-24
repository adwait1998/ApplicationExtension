"""Expired-link pre-check ("freshness pruner").

Validates `application_url` for queued jobs BEFORE an apply attempt is spent
on them. A stale queue (links discovered weeks ago) otherwise burns real
apply attempts — Chrome launch + LLM cost — on postings that are long gone.

Design:
- `classify_liveness()` is a pure function over (status_code, body_text) so it
  is testable at $0 with synthetic responses.
- `check_queue()` does the network pass (httpx, thread pool) and marks dead
  rows `apply_status='expired'` / `apply_error='link_expired_precheck:<why>'`.
  Reversible: `UPDATE jobs SET apply_status=NULL WHERE apply_error LIKE
  'link_expired_precheck%'`.
- Conservative: anything ambiguous (403/5xx/timeouts/Cloudflare) is left
  untouched — only confident "this posting is gone" signals park a row.
"""

from __future__ import annotations

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

LIVE = "live"
EXPIRED = "expired"
UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Greenhouse soft-302 signature (shared with the v2 apply path)
# ---------------------------------------------------------------------------
# An expired Greenhouse req does NOT 404 — it 302-redirects the
# /<company>/jobs/<id> link to the board index (e.g.
# job-boards.greenhouse.io/twilio?error=true). Body-marker matching misses this
# (the board index is a normal 200 with neither an expired phrase nor a live
# application form), so it needs a URL-shape check on the FINAL landed URL.
# ONE source of truth used by both classify_liveness (freshness pre-check) and
# the v2 dispatch closure (launcher) so the two never disagree on what "expired"
# means. Deliberately conservative — only the unambiguous signatures below.


def _is_greenhouse_host(host: str | None) -> bool:
    h = (host or "").lower()
    return h == "greenhouse.io" or h.endswith(".greenhouse.io")


def _has_job_path(path: str | None) -> bool:
    """A Greenhouse job req path carries a /jobs/<id> segment."""
    return "/jobs/" in (path or "")


def is_greenhouse_expired_redirect(
    intended_url: str | None, landed_url: str | None
) -> bool:
    """True iff a Greenhouse job link soft-302'd to its board index — the
    unambiguous 'this req is expired' signature.

    `intended_url` is where we meant to go (the job req link); `landed_url` is
    where we ACTUALLY ended up (browser page.url after navigation, or the HTTP
    client's final URL after following redirects).

    Conservative by construction — returns False whenever the signal is not
    clearly present: no landed URL, a non-Greenhouse intent we can't reason
    about, or a link still sitting on its /jobs/<id> path. A false 'expired'
    silently drops a LIVE job, which is worse than a mislabeled park, so we only
    fire on the two signatures actually observed on expired Greenhouse reqs.
    """
    if not intended_url or not landed_url:
        return False
    try:
        want = urlsplit(intended_url)
        got = urlsplit(landed_url)
    except Exception:  # noqa: BLE001 — a malformed URL is never 'expired'
        return False

    # The intended link must itself be a Greenhouse job req: either a
    # boards.greenhouse.io/<co>/jobs/<id> path OR a vanity careers host carrying
    # the canonical ?gh_jid=<id> embed param. Anything else is out of scope
    # (we cannot reason about arbitrary vanity-domain redirects) -> not expired.
    intended_is_gh_job = (
        (_is_greenhouse_host(want.hostname) and _has_job_path(want.path))
        or "gh_jid=" in (want.query or "").lower()
    )
    if not intended_is_gh_job:
        return False

    landed_is_gh = _is_greenhouse_host(got.hostname)
    # Signature 1: Greenhouse appends ?error=true to the board root when a req
    # is gone. Unambiguous — but only trust it on a Greenhouse landing host.
    if landed_is_gh and "error=true" in (got.query or "").lower():
        return True
    # Signature 2: the link left its /jobs/<id> path and landed on the
    # Greenhouse board root (no /jobs/ segment). A live req keeps /jobs/<id>.
    if landed_is_gh and _has_job_path(want.path) and not _has_job_path(got.path):
        return True
    return False

# Confident "posting is gone" body markers, all lowercase.
# Sources: Greenhouse, Lever, Ashby, Workday, generic ATS wording.
_EXPIRED_MARKERS = (
    # Greenhouse
    "the job you're looking for is no longer open",
    "the job you are looking for is no longer open",
    "this job is no longer open",
    "job post not found",
    # Lever
    "this job is no longer accepting applications",
    "sorry, we couldn't find that job",
    "this posting has been closed",
    # Ashby
    "this job posting is no longer accepting applications",
    "job not found",
    "posting not found",
    # Workday / generic
    "this job is no longer available",
    "position has been filled",
    "this position is no longer open",
    "no longer accepting applications",
    "this requisition is no longer active",
    "job has been closed",
    "job posting has expired",
    "this opportunity is no longer available",
)

# Markers that indicate a *live* application form. If one of these is present
# the page is live even if some generic "no longer" phrase appears elsewhere
# (e.g. in a careers-site footer or unrelated listing).
_LIVE_MARKERS = (
    "submit application",
    "apply for this job",
    "submit your application",
    'type="file"',  # resume upload input
    "autocomplete=\"given-name\"",
)


def classify_liveness(
    status_code: int | None,
    body_text: str | None,
    url: str | None = None,
    final_url: str | None = None,
) -> str:
    """Pure classifier: HTTP status + body (+ optional URLs) → live / expired /
    unknown.

    Conservative by construction — only 404/410, an explicit expired marker
    (without a live form on the same page), or a Greenhouse soft-302 to the board
    index returns EXPIRED.

    `url` (the request URL) and `final_url` (where redirects landed) enable the
    Greenhouse soft-302 check: an expired GH req 302s its /jobs/<id> link to the
    board root (?error=true), a 200 the body markers alone can't catch.
    """
    if status_code is None:
        return UNKNOWN
    if status_code in (404, 410):
        return EXPIRED
    # Greenhouse soft-302: the redirect already lands a 200 board index, so this
    # must be checked BEFORE the non-200 / body-marker logic below.
    if is_greenhouse_expired_redirect(url, final_url):
        return EXPIRED
    if status_code != 200:
        # 403/429/5xx → bot defenses or transient; never park on these.
        return UNKNOWN
    body = (body_text or "").lower()
    if not body:
        return UNKNOWN
    has_live = any(m in body for m in _LIVE_MARKERS)
    has_expired = any(m in body for m in _EXPIRED_MARKERS)
    if has_expired and not has_live:
        return EXPIRED
    if has_live:
        return LIVE
    # 200 with neither marker: probably a JS-rendered shell. Don't guess.
    return UNKNOWN


def _fetch(url: str, timeout: float = 15.0) -> tuple[int | None, str | None, str | None]:
    """GET a URL, returning (status_code, body_text, final_url). Never raises.

    `final_url` is the URL redirects actually landed on (resp.url) — needed to
    catch the Greenhouse soft-302-to-board expired signature the body misses.
    """
    try:
        import httpx

        with httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            },
        ) as client:
            resp = client.get(url)
            return resp.status_code, resp.text[:200_000], str(resp.url)
    except Exception as exc:  # noqa: BLE001 — network errors are expected
        log.debug("freshness fetch failed for %s: %s", url, exc)
        return None, None, None


@dataclass
class PruneResult:
    checked: int = 0
    expired: int = 0
    live: int = 0
    unknown: int = 0
    expired_urls: list[str] = field(default_factory=list)


def select_queue_urls(
    conn: sqlite3.Connection,
    min_score: int = 7,
    limit: int = 500,
) -> list[tuple[str, str]]:
    """Rows worth checking: the same gate the apply queue uses (minus age)."""
    rows = conn.execute(
        """
        SELECT url, application_url FROM jobs
        WHERE fit_score >= ?
          AND application_url IS NOT NULL
          AND (apply_status IS NULL OR apply_status = 'failed')
          AND applied_at IS NULL
        ORDER BY fit_score DESC, discovered_at DESC
        LIMIT ?
        """,
        (min_score, limit),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def check_queue(
    conn: sqlite3.Connection,
    min_score: int = 7,
    limit: int = 500,
    workers: int = 8,
    dry_run: bool = False,
    fetch_fn=None,
) -> PruneResult:
    """Check queued application URLs; park confidently-dead ones.

    `fetch_fn(url) -> (status_code, body)` is injectable for $0 tests.
    """
    fetch = fetch_fn or _fetch
    targets = select_queue_urls(conn, min_score=min_score, limit=limit)
    result = PruneResult()
    if not targets:
        return result

    verdicts: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, app_url): (url, app_url) for url, app_url in targets}
        for fut in as_completed(futures):
            url, app_url = futures[fut]
            fetched = fut.result()
            # Back-compat: a fetch_fn may return (status, body) OR the newer
            # (status, body, final_url). Tolerate both so injected test fetchers
            # and the real _fetch both work.
            status_code, body = fetched[0], fetched[1]
            final_url = fetched[2] if len(fetched) > 2 else None
            verdicts[url] = classify_liveness(
                status_code, body, url=app_url, final_url=final_url)

    for url, verdict in verdicts.items():
        result.checked += 1
        if verdict == EXPIRED:
            result.expired += 1
            result.expired_urls.append(url)
            if not dry_run:
                conn.execute(
                    "UPDATE jobs SET apply_status='expired', "
                    "apply_error='link_expired_precheck:http' WHERE url=?",
                    (url,),
                )
        elif verdict == LIVE:
            result.live += 1
        else:
            result.unknown += 1
    if not dry_run:
        conn.commit()
    return result
