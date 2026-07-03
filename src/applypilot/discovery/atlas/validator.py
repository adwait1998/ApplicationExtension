"""Profile-AGNOSTIC board validator. One cheap existence check per token; a
board is a member iff its public API responds -- what it posts is irrelevant to
membership (spec §7.1). Explicitly does NOT run title_matches/min_matching_jobs
(that is the crawler's scheduling concern, not the registry's).

All live HTTP goes through the per-host politeness limiter with the honest bot
UA (spec §7.2). Tests inject an httpx.MockTransport client + fake clock and
never hit the network."""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from applypilot.discovery.atlas import USER_AGENT
from applypilot.discovery.atlas import boards_repo as repo
from applypilot.discovery.atlas.politeness import HostRateLimiter

_TIMEOUT = 8

# (host, url_template, json-list extractor). Cheap existence-check variants only
# -- these mirror ats_discovery.validate_board's URLs (content=false /
# includeCompensation=false) but carry NO title filter.
_ENDPOINTS = {
    "greenhouse": ("boards-api.greenhouse.io",
                   "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false",
                   lambda d: d.get("jobs", [])),
    "lever": ("api.lever.co",
              "https://api.lever.co/v0/postings/{token}?mode=json",
              lambda d: d if isinstance(d, list) else []),
    "ashby": ("api.ashbyhq.com",
              "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false",
              lambda d: d.get("jobs", [])),
}


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    job_count: int = 0
    error: str | None = None


def validate_board(conn, ats: str, token: str, *, client: httpx.Client | None = None,
                   limiter: HostRateLimiter | None = None) -> ValidationResult:
    """Confirm a board exists via one cheap GET and record it in the registry.

    On success the board is promoted to 'active' with its current job_count --
    even zero, because a board that posts nothing today may post tomorrow (and
    the next user may target a different profile). On any HTTP >= 400 or
    exception the board is marked dead (error_streak++)."""
    spec = _ENDPOINTS.get(ats.lower())
    if spec is None:
        return ValidationResult(False, error="unknown_ats")
    host, tmpl, extract = spec
    owns_client = client is None
    client = client or httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT})
    try:
        if limiter is not None:
            limiter.acquire(host)
        resp = client.get(tmpl.format(token=token), headers={"User-Agent": USER_AGENT})
        if resp.status_code >= 400:
            repo.mark_dead(conn, ats, token)
            return ValidationResult(False, error=f"http_{resp.status_code}")
        jobs = extract(resp.json())
        count = len(jobs)
        # Profile-agnostic: existence alone promotes to 'active'. Ring assignment
        # (Task 6) decides HOW OFTEN to poll based on what it posts. The id-set
        # hash is the poller's (Task 5) concern; here we preserve any existing
        # hash and record only existence + count. mark_checked sets
        # status='active' and clears error_streak.
        existing = repo.get(conn, ats, token) or {}
        repo.mark_checked(conn, ats, token,
                          job_id_set_hash=existing.get("job_id_set_hash") or "",
                          job_count=count, new_last_poll=0, changed=False)
        return ValidationResult(True, job_count=count)
    except Exception as exc:  # noqa: BLE001 -- one bad board must not kill a batch
        repo.mark_dead(conn, ats, token)
        return ValidationResult(False, error=str(exc))
    finally:
        if owns_client:
            client.close()
