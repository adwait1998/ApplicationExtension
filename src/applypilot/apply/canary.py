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
    # The bare `address` alternative matched "Email Address", "E-mail Address"
    # and "Web address", so an email input resolved as the POSTAL-address canary
    # and got filled with a street address. The lookbehinds disqualify the words
    # that turn "address" into something that is not a postal address; each is
    # fixed-width, which is what Python's re requires.
    "address": re.compile(
        r"\b(street address|mailing address|home address|zip|postal code"
        r"|(?<!email )(?<!e-mail )(?<!web )(?<!url )(?<!ip )address)\b",
        re.I,
    ),
    "dob": re.compile(r"\b(date of birth|birth ?date|dob)\b", re.I),
    "clearance": re.compile(r"\b(security clearance|clearance)\b", re.I),
    "immigration": re.compile(r"\b(immigration|immigrant status|work visa case|commence .* case)\b", re.I),
    "export_control": re.compile(r"\b(itar|export[- ]?control|ear\b|us person|export administration)\b", re.I),
    "password": re.compile(r"\bpassword\b", re.I),
}

_NEGATION_RE = re.compile(r"\b(without|not require|don'?t require|do not require|no need)\b", re.I)

_TRUTHY = {"true", "yes", "y", "1"}
_FALSY = {"false", "no", "n", "0"}


def _as_bool(value) -> bool | None:
    """Strict tri-state: real bools pass through; yes/no-style strings normalize;
    anything else (None, '', 'maybe', 3) is None -> the canary stays unresolved."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUTHY:
            return True
        if v in _FALSY:
            return False
    return None


def is_canary(question: str) -> bool:
    q = question or ""
    return any(rx.search(q) for rx in _MARKERS.values())


def _yn(flag: bool) -> str:
    return "Yes" if flag else "No"


# Which part of an address a label is asking for. Checked in order: the most
# specific component wins, because "Zip/Postal Code" also contains the generic
# word that made it an address canary in the first place.
_ADDR_POSTAL = re.compile(r"\b(zip|postal|post\s*code|postcode)\b", re.I)
_ADDR_LINE2 = re.compile(
    r"\b(address\s*(line)?\s*2|address\s*line\s*two|line\s*2|apt|apartment|suite|unit)\b", re.I)
_ADDR_FULL = re.compile(
    r"\b(full|complete|mailing|home|current|permanent|residential)\s+address\b", re.I)


def _address_component(question: str, per: dict) -> str | None:
    """Answer only the part of the address the label asks for.

    This used to join every component into one string for ANY address-shaped
    label, so a form with separate fields got the whole address pasted into
    "Zip/Postal Code" and duplicated into "Address 2" — caught on a real ATS.
    Most application forms that ask for an address split it into fields, so a
    bare "Address" means the street line; only an explicitly full/mailing
    address gets the joined form.
    """
    if _ADDR_POSTAL.search(question):
        return per.get("postal_code") or None
    if _ADDR_LINE2.search(question):
        # The profile holds a single street line. The honest answer to a
        # second-line field is "leave it blank", never a copy of line one.
        return None
    if _ADDR_FULL.search(question):
        parts = [per.get("address"), per.get("city"), per.get("province_state"),
                 per.get("postal_code"), per.get("country")]
        joined = ", ".join(x for x in parts if x)
        return joined or None
    return per.get("address") or None


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
        requires = _as_bool(wa.get("require_sponsorship"))
        if requires is None:
            return None  # missing/unparseable flag -> park, never guess an attestation
        negated = _NEGATION_RE.search(q) is not None
        # need explicit polarity context; a bare "Sponsorship?" is ambiguous
        if not re.search(r"\b(require|need|without|now or in the future|will you|can you|are you able)\b", q, re.I):
            return None
        return _yn(not requires) if negated else _yn(requires)
    if _MARKERS["workauth"].search(q):
        authorized = _as_bool(wa.get("legally_authorized_to_work"))
        if authorized is None:
            return None  # missing/unparseable flag -> park, never guess an attestation
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
        return _address_component(q, per)

    if _MARKERS["clearance"].search(q):
        return None  # not in profile — refuse

    return None
