"""Incremental Atlas poller (spec §7.2). Per board: fetch id-set, hash it, diff
against stored job_id_set_hash. Unchanged -> stop. Changed -> gate + store only
the NEW ids. Reuses the ATS JSON shapes from ats_boards but drops the discovery
title/location prefilter: membership + eligibility are the gate's job, so every
posting is gated and stored (audit), only eligible+auto rows become queue-visible."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from applypilot import database as _db
from applypilot.discovery.atlas import USER_AGENT
from applypilot.discovery.atlas import boards_repo as repo
from applypilot.discovery.atlas.politeness import HostRateLimiter
from applypilot.gate.engine import gate_job

_TIMEOUT = 20

_HOSTS = {
    "greenhouse": "boards-api.greenhouse.io",
    "lever": "api.lever.co",
    "ashby": "api.ashbyhq.com",
}


@dataclass(frozen=True)
class PollResult:
    ok: bool
    total_ids: int = 0
    new_ids: int = 0
    stored: int = 0
    changed: bool = False
    error: str | None = None


def _hash_ids(ids) -> str:
    payload = "\n".join(sorted(str(i) for i in ids))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _fetch_all(ats: str, token: str, client: httpx.Client) -> list[dict]:
    """Return normalized rows [{id, url, title, location, description, posted_at}]
    for ALL postings (no prefilter). URL/field mapping mirrors ats_boards._fetch_*.

    Ashby's board API is not guaranteed to expose a stable per-job `id`; when it
    is absent the `jobUrl` is used as the id so the id-set diff still works
    (documented fallback, plan Task 5 Step 1)."""
    headers = {"User-Agent": USER_AGENT}
    if ats == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        out = []
        for j in data.get("jobs", []):
            desc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", j.get("content", "") or "")).strip()[:5000]
            out.append({"id": j.get("id"), "url": j.get("absolute_url"), "title": j.get("title", ""),
                        "location": (j.get("location") or {}).get("name", ""), "description": desc,
                        "posted_at": j.get("first_published") or j.get("updated_at")})
        return out
    if ats == "lever":
        url = f"https://api.lever.co/v0/postings/{token}?mode=json"
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        out = []
        for j in data or []:
            out.append({"id": j.get("id"), "url": j.get("hostedUrl"), "title": j.get("text", ""),
                        "location": (j.get("categories") or {}).get("location", ""),
                        "description": (j.get("descriptionPlain") or j.get("description", ""))[:5000],
                        "posted_at": j.get("createdAt")})
        return out
    if ats == "ashby":
        url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        out = []
        for j in data.get("jobs", []):
            # Fall back to jobUrl when Ashby omits a stable per-job id.
            out.append({"id": j.get("id") or j.get("jobUrl"), "url": j.get("jobUrl"),
                        "title": j.get("title", ""), "location": j.get("location", "") or "",
                        "description": (j.get("descriptionPlain") or j.get("descriptionHtml", ""))[:5000],
                        "posted_at": j.get("publishedAt")})
        return out
    return []


def poll_board(conn, board: dict, policy: dict, *, client: httpx.Client | None = None,
               limiter: HostRateLimiter | None = None) -> PollResult:
    """Poll one board incrementally. Fetches the full board response, hashes the
    sorted id-set, and short-circuits when it matches the stored hash (unchanged
    board = one request, zero stores). On change, only NEW postings (not already
    in `jobs` under this strategy) are gated with gate_job and written via
    store_gated (audit: every posting is stored, verdict included). One bad row
    is contained so it never kills the board poll."""
    ats, token = board["ats"], board["token"]
    site_label = f"{board.get('company_name') or token} ({ats})"
    strategy = f"atlas:{ats}"
    now = datetime.now(timezone.utc).isoformat()
    owns = client is None
    client = client or httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT})
    try:
        if limiter is not None:
            limiter.acquire(_HOSTS[ats])
        rows = _fetch_all(ats, token, client)
        ids = [r["id"] for r in rows if r.get("id") is not None]
        new_hash = _hash_ids(ids)
        old_hash = board.get("job_id_set_hash") or ""
        changed = new_hash != old_hash
        if not changed:
            repo.mark_checked(conn, ats, token, job_id_set_hash=new_hash,
                              job_count=len(ids), new_last_poll=0, changed=False)
            return PollResult(True, total_ids=len(ids), new_ids=0, stored=0, changed=False)

        # Determine which ids are NEW vs already-stored (by url in jobs).
        stored = 0
        new_ids = 0
        existing_urls = {r[0] for r in conn.execute(
            "SELECT url FROM jobs WHERE strategy = ?", (strategy,)).fetchall()}
        for r in rows:
            if not r.get("url") or r["url"] in existing_urls:
                continue
            new_ids += 1
            job_row = {"url": r["url"], "title": r["title"], "salary": None,
                       "description": r["description"], "full_description": r["description"],
                       "application_url": r["url"], "location": r["location"],
                       "site": site_label, "posted_at": r.get("posted_at") or now,
                       # Atlas rows arrive with content (like ats_boards): stamp
                       # detail_scraped_at so they skip the enrich stage.
                       "detail_scraped_at": now}
            try:
                if _db.store_gated(conn, job_row, gate_job(job_row, policy), strategy=strategy):
                    stored += 1
            except Exception:  # noqa: BLE001 — one bad row never kills the board
                continue
        repo.mark_checked(conn, ats, token, job_id_set_hash=new_hash,
                          job_count=len(ids), new_last_poll=new_ids, changed=True)
        return PollResult(True, total_ids=len(ids), new_ids=new_ids, stored=stored, changed=True)
    except Exception as exc:  # noqa: BLE001
        repo.mark_dead(conn, ats, token)
        return PollResult(False, error=str(exc))
    finally:
        if owns:
            client.close()
