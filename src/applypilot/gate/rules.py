"""Deterministic eligibility rules. Each: (value, profile_policy, **ctx) -> Verdict."""
from __future__ import annotations

import re

from applypilot.identity import parse_ats_url

from . import Verdict
from . import gazetteer as gz

_CARVEOUT_RE = re.compile(
    r"(not (available|eligible)|residents of|excluding|must reside|"
    r"overlap .* (\d+ )?(hours?|time ?zone)|must overlap|not .* residents|"
    r"itar|u\.?s\.? person|us citizens? only|must be a u\.?s\.? citizen)", re.I)

_MGMT_RE = re.compile(r"\b(manager|director|head of|vp|vice president|chief|"
                      r"people manager|hiring manager)\b", re.I)
_EARLY_RE = re.compile(r"\b(intern|internship|apprentice|apprenticeship|"
                       r"fellow|fellowship|new[ -]?grad|new[ -]?graduate|co[ -]?op)\b", re.I)

_SPONSOR_BLOCK_RE = re.compile(
    r"(without sponsorship|no (visa )?sponsorship|(cannot|unable to|do not|"
    r"does not) sponsor|must be (a )?(us|u\.s\.) citizen|us citizenship required|"
    r"security clearance|requires?\s+(a\s+)?(security\s+)?clearance|"
    r"not able to sponsor|opt/cpt not)", re.I)

_MANUAL_ATS_RE = re.compile(
    r"(linkedin\.com/jobs|indeed\.com|glassdoor\.com|ziprecruiter\.com|ibegin\.tcs)", re.I)

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


def seniority_rule(title: str, policy: dict) -> Verdict:
    t = (title or "").strip()
    if _EARLY_RE.search(t):
        m = _EARLY_RE.search(t)
        return Verdict("REJECT", "seniority_early_career", m.group(0))
    # Management tokens (manager/director/vp/head of/chief) => management track.
    # "Lead" is NOT a management token and is not in _MGMT_RE, so IC "Lead"
    # titles pass without a special carve-out. (User rule: reject Director/VP/
    # Head/Manager/Creative-Director for a 5yr IC designer.)
    if policy.get("ic_only") and _MGMT_RE.search(t):
        m = _MGMT_RE.search(t)
        return Verdict("REJECT", "seniority_management_track", m.group(0))
    return Verdict("PASS", "seniority_ok", t)


def sponsorship_rule(description: str, *, needs_sponsorship: bool) -> Verdict:
    if not needs_sponsorship:
        return Verdict("PASS", "sponsorship_not_applicable")
    m = _SPONSOR_BLOCK_RE.search(description or "")
    if m:
        start = max(0, m.start() - 30)
        return Verdict("REJECT", "sponsorship_blocked", (description or "")[start:m.end() + 30])
    return Verdict("UNKNOWN", "sponsorship_unknown")


# NOTE (spec §5.2 rule 3): the H-1B LCA company-level sponsorship prior is
# DEFERRED to Phase 4 (ranking bias, not the hard gate). v1 = the hard-marker
# regex above + sponsorship_unknown -> review queue.


def automatability_rule(url: str, *, workday_accounts: list[str]) -> Verdict:
    u = url or ""
    if _MANUAL_ATS_RE.search(u):
        return Verdict("REJECT", "manual_ats", u)
    ref = parse_ats_url(u)
    if ref is None:
        return Verdict("UNKNOWN", "automatability_unknown", u)
    if ref.ats == "workday":
        if ref.token.lower() in {a.lower() for a in (workday_accounts or [])}:
            return Verdict("PASS", "automatable_workday")
        return Verdict("REJECT", "account_required", ref.token)
    if ref.ats in {"greenhouse", "lever", "ashby"}:
        return Verdict("PASS", "automatable_supported")
    return Verdict("UNKNOWN", "automatability_unknown", u)
