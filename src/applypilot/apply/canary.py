"""Canary classes — work-auth, sponsorship, citizenship/legal, compensation,
EEO, address, DOB — resolve ONLY from exact profile paths, with explicit
polarity handling. Never fuzzy-matched, never LLM-answered, never cached.
An unresolvable canary returns None so the caller keeps the field UNRESOLVED
(which blocks deterministic auto-submit — the interlock)."""
from __future__ import annotations

import re

_MARKERS = {
    "workauth": re.compile(r"\b(authoriz\w+|eligible to work|legally able|work permit)\b", re.I),
    "sponsorship": re.compile(r"\b(sponsor\w*|visa)\b", re.I),
    "citizenship": re.compile(r"\b(citizen\w*|us person|green card|permanent resident)\b", re.I),
    "salary": re.compile(r"\b(salary|compensation|pay expectation|expected (pay|comp))\b", re.I),
    "eeo_gender": re.compile(r"\bgender\b", re.I),
    "eeo_race": re.compile(r"\b(race|ethnicit\w+)\b", re.I),
    "eeo_veteran": re.compile(r"\bveteran\b", re.I),
    "eeo_disability": re.compile(r"\bdisabilit\w+\b", re.I),
    "address": re.compile(r"\b(street address|mailing address|home address|zip|postal code|address)\b", re.I),
    "dob": re.compile(r"\b(date of birth|birth ?date|dob)\b", re.I),
    "clearance": re.compile(r"\b(security clearance|clearance)\b", re.I),
    "password": re.compile(r"\bpassword\b", re.I),
}

_NEGATION_RE = re.compile(r"\b(without|not require|don'?t require|do not require|no need)\b", re.I)


def is_canary(question: str) -> bool:
    q = question or ""
    return any(rx.search(q) for rx in _MARKERS.values())


def _yn(flag: bool) -> str:
    return "Yes" if flag else "No"


def resolve_canary(question: str, profile: dict) -> str | None:
    """Deterministic answer from exact profile paths, or None (-> stay unresolved)."""
    q = question or ""
    p = profile or {}
    wa = p.get("work_authorization", {}) or {}
    comp = p.get("compensation", {}) or {}
    eeo = p.get("eeo_voluntary", {}) or {}
    per = p.get("personal", {}) or {}

    if _MARKERS["password"].search(q):
        return None  # never surface credentials into a form answer

    # citizenship first: not derivable from this profile -> always refuse
    if _MARKERS["citizenship"].search(q) and not _MARKERS["sponsorship"].search(q):
        return None

    # sponsorship / workauth: polarity-sensitive
    if _MARKERS["sponsorship"].search(q):
        requires = bool(wa.get("require_sponsorship"))
        negated = _NEGATION_RE.search(q) is not None
        # need explicit polarity context; a bare "Sponsorship?" is ambiguous
        if not re.search(r"\b(require|need|without|now or in the future|will you|can you|are you able)\b", q, re.I):
            return None
        return _yn(not requires) if negated else _yn(requires)
    if _MARKERS["workauth"].search(q):
        authorized = bool(wa.get("legally_authorized_to_work"))
        negated = _NEGATION_RE.search(q) is not None
        return _yn(not authorized) if negated else _yn(authorized)

    if _MARKERS["salary"].search(q):
        val = comp.get("salary_expectation")
        cur = comp.get("salary_currency", "")
        return f"{val} {cur}".strip() if val else None

    if _MARKERS["eeo_gender"].search(q):
        return eeo.get("gender") or None
    if _MARKERS["eeo_race"].search(q):
        return eeo.get("race_ethnicity") or None
    if _MARKERS["eeo_veteran"].search(q):
        return eeo.get("veteran_status") or None
    if _MARKERS["eeo_disability"].search(q):
        return eeo.get("disability_status") or None

    if _MARKERS["dob"].search(q):
        return None  # no DOB in profile — never guess

    if _MARKERS["address"].search(q):
        parts = [per.get("address"), per.get("city"), per.get("province_state"),
                 per.get("postal_code"), per.get("country")]
        joined = ", ".join(x for x in parts if x)
        return joined or None

    if _MARKERS["clearance"].search(q):
        return None  # not in profile — refuse

    return None
