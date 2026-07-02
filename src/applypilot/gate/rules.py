"""Deterministic eligibility rules. Each: (value, profile_policy, **ctx) -> Verdict."""
from __future__ import annotations

import re

from . import Verdict
from . import gazetteer as gz

_CARVEOUT_RE = re.compile(
    r"(not (available|eligible)|residents of|excluding|must reside|"
    r"overlap .* (\d+ )?(hours?|time ?zone)|must overlap|not .* residents|"
    r"itar|u\.?s\.? person|us citizens? only|must be a u\.?s\.? citizen)", re.I)

# Metros tried longest-first so "new york city" beats "new york" and
# "san francisco bay area" beats "san francisco" during region resolution.
_METROS_LONGEST_FIRST = sorted(gz.US_METROS.items(), key=lambda kv: -len(kv[0]))


def location_rule(location: str, policy: dict, *, description: str = "",
                  is_title_field: bool = False) -> Verdict:
    loc = (location or "").strip()
    # description-level carve-outs override a clean location field
    if description and _CARVEOUT_RE.search(description):
        m = _CARVEOUT_RE.search(description)
        return Verdict("UNKNOWN", "location_body_carveout", description[max(0, m.start()-20):m.end()+20])
    if not loc:
        return Verdict("UNKNOWN", "location_missing")
    low = loc.lower()
    is_remote = gz.REMOTE_RE.search(low) is not None
    non_us = any(gz.word_match(m, low) for m in gz.NON_US_MARKERS)
    us = gz.US_COUNTRY_RE.search(low) is not None or \
        any(gz.word_match(s, low) for s in gz.US_STATES) or \
        any(gz.word_match(m, low) for m in gz.US_METROS)
    if is_remote:
        if non_us and not us:
            return Verdict("REJECT", "location_remote_scope", loc)
        if policy.get("remote_ok"):
            return Verdict("PASS", "location_ok", loc)
        return Verdict("REJECT", "location_remote_not_ok", loc)
    # onsite: must be in an allowed metro/region (longest phrase wins)
    for metro, region in _METROS_LONGEST_FIRST:
        if gz.word_match(metro, low):
            return Verdict("PASS", "location_ok", loc) if region in policy.get("onsite_regions", []) \
                else Verdict("REJECT", "location_onsite_out_of_region", loc)
    if non_us:
        return Verdict("REJECT", "location_onsite_non_us", loc)
    return Verdict("UNKNOWN", "location_unresolved", loc)
