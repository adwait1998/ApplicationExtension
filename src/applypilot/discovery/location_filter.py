"""Shared location-filter logic for discovery modules.

All discovery modules (workday, jobspy, smartextract, ats_boards) use the same
accept/reject lists from `searches.yaml`. Centralizing here so the four code
paths can't drift.

Semantics (in order):
  1. Empty location  -> keep (some boards don't populate; let the scorer decide).
  2. Accept marker present -> keep, even if a reject marker also appears (e.g.
     "Remote, Canada; Remote, US" should keep — the candidate is US-eligible).
  3. Reject marker present (and no accept) -> reject.
  4. Default -> keep (don't aggressively reject; uncertain locations get scored).

If `location_accept` is empty in searches.yaml, the filter degrades to a
no-op so existing user configs without the lists keep working.
"""

from __future__ import annotations

from applypilot import config


def load_location_filter(search_cfg: dict | None = None) -> tuple[list[str], list[str]]:
    """Return (accept, reject) lists from search config."""
    if search_cfg is None:
        search_cfg = config.load_search_config() or {}
    accept = search_cfg.get("location_accept", []) or []
    reject = search_cfg.get("location_reject_non_remote", []) or []
    return accept, reject


def location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    """Check whether a job location passes the user's filter.

    See module docstring for the decision rules.
    """
    # Step 0: if no accept list configured, filter is a no-op.
    if not accept:
        return True

    # Step 1: empty location -> keep.
    if not location:
        return True

    loc = location.lower()

    # Step 2: explicit accept marker (e.g. ", us", "san francisco") wins,
    # even if a reject marker also appears in the same string.
    for a in accept:
        if a and a.lower() in loc:
            return True

    # Step 3: reject marker (e.g. "canada", "warsaw") -> reject.
    for r in reject:
        if r and r.lower() in loc:
            return False

    # Step 4: default keep when uncertain.
    return True
