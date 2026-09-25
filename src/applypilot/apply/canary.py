"""Canary classes — work-auth, sponsorship, citizenship/legal, compensation,
EEO, address, DOB — resolve ONLY from exact profile paths, with explicit
polarity handling. Never fuzzy-matched, never LLM-answered, never cached.
An unresolvable canary returns None so the caller keeps the field UNRESOLVED
(which blocks deterministic auto-submit — the interlock).

EEO is the one deliberate exception: voluntary self-identification always
has a safe, lawful answer (decline), so an EEO canary with nothing in
eeo_voluntary.* never stays unresolved — see _EEO_DECLINE below. This
mirrors apply/v2/resolver.py's own _EEO_DEFAULTS/_DECLINE policy for the
v2 pipeline (absent-in-profile -> decline); that module has its own
separate implementation and never imports this one, so there is nothing
here for it to conflict with — same rule, two independent call paths."""
from __future__ import annotations

import re

_MARKERS = {
    "workauth": re.compile(r"\b(authoriz\w+|eligible to work|legally able|work permit)\b", re.I),
    "sponsorship": re.compile(r"\b(sponsor\w*|visa)\b", re.I),
    "citizenship": re.compile(r"\b(citizen\w*|us person|green card|permanent resident)\b", re.I),
    "salary": re.compile(r"\b(salary|compensation|pay expectation|expected (pay|comp))\b", re.I),
    # EEO / OFCCP voluntary self-identification markers. Each stays a CANARY:
    # resolve_canary answers these ONLY from eeo_voluntary.* or the decline
    # default (_EEO_DECLINE below) — never the answer bank, never a draft,
    # never inferred from anything else.
    # "sex" is only an EEO word when it is not describing a crime: "Are you a
    # registered sex offender?" / "convicted of a sex offense?" are criminal
    # background attestations. Without the lookahead they matched here and
    # were answered with the applicant's GENDER ("Male") — in both the
    # extension and the pipeline's Greenhouse adapter, which share this module.
    "eeo_gender": re.compile(
        r"\b(gender|sex(?!\s*-?\s*(offen\w*|crimes?|abuse|trafficking|work)\b))\b", re.I),
    # Ethnicity ("Are you Hispanic or Latino?") is asked as its OWN question
    # on OFCCP-style forms, separate from race — its own marker/profile
    # field (eeo_voluntary.hispanic_latino), not folded into eeo_race.
    "eeo_hispanic_latino": re.compile(r"\b(hispanic|latino|latina|latinx)\b", re.I),
    # A bare "race" used to fire on "race condition" (a real label/question
    # on engineering-adjacent forms), and on "Tracer"/"Embrace" before word
    # boundaries were added. \b already stops the latter two (no boundary
    # mid-token); "race condition" IS a standalone word "race" though, so it
    # needs an explicit carve-out — never treat the engineering term as EEO.
    "eeo_race": re.compile(r"\b(race(?!\s*-?\s*conditions?\b)|ethnicit\w+)\b", re.I),
    # "What is your military status?" is the veteran self-ID question too.
    "eeo_veteran": re.compile(r"\b(veteran|vevraa|military\s+status)\b", re.I),
    "eeo_disability": re.compile(r"\bdisabilit\w+\b", re.I),
    # Voluntary LGBTQ+ self-identification. Nothing marked these before, so a
    # draft-enabled path could hand "Do you identify as transgender?" to an
    # LLM, which would guess. Same policy as the rest of EEO: the profile
    # value if the applicant set one, else decline. ("transgender" needs its
    # own alternative: \bgender\b can never match inside it.)
    "eeo_orientation": re.compile(
        r"\b(sexual orientation|transgender|lgbt\w*)\b", re.I),
    # Pronouns are the applicant's to state, never something to infer: a
    # guessed pronoun misgenders the applicant on their own application.
    # Profile value or unresolved — deliberately NOT the decline default.
    "pronouns": re.compile(r"\bpronouns?\b", re.I),
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

_ACCOMMODATION_RE = re.compile(r"\baccommodat\w*\b", re.I)

_NEGATION_RE = re.compile(r"\b(without|not require|don'?t require|do not require|no need)\b", re.I)

# Canonical decline text for EEO voluntary self-identification — verbatim to
# the JS-stored eeo_voluntary.* enum's own "Decline to self-identify" option
# (extension/resolve.py hands this straight back as the field's literal
# value, which the JS side then maps to each site's option wording — so the
# casing here must match that stored value exactly, not be fuzzy-mapped).
# Declining is always a lawful answer to voluntary self-identification, so
# an EEO canary is NEVER left unresolved: absent or empty in the profile
# falls back to this. apply/v2/resolver.py applies the identical absent ->
# decline rule for its own separate pipeline (_EEO_DEFAULTS/_DECLINE there,
# lowercased because it only feeds a fuzzy option_intent match rather than
# emitting a literal value) — same policy, independent implementation;
# resolver.py never imports this module, so there is nothing to conflict.
_EEO_DECLINE = "Decline to self-identify"

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
_ADDR_POSTAL = re.compile(r"\b(zip|postal|post\s*code|postcode|cep)\b", re.I)
_ADDR_CITY = re.compile(r"\b(city|town|municipality)\b", re.I)
_ADDR_STATE = re.compile(r"\b(state|province|region)\b", re.I)
_ADDR_COUNTRY = re.compile(r"\bcountry\b", re.I)
# "(Brazil Only)", "(US only)": a field for some other country's applicants.
_ADDR_ONLY_FOR = re.compile(r"\(([^()]{2,40}?)\s+only\)", re.I)
_ADDR_LINE2 = re.compile(
    r"\b(address\s*(line)?\s*2|address\s*line\s*two|line\s*2|apt|apartment|suite|unit)\b", re.I)
_ADDR_FULL = re.compile(
    r"\b(full|complete|mailing|home|current|permanent|residential)\s+address\b", re.I)


_COUNTRY_ALIASES = {
    "united states": ("united states", "usa", "us", "u.s.", "u.s.a.", "america"),
    "united states of america": ("united states", "usa", "us", "u.s.", "u.s.a.", "america"),
    "usa": ("united states", "usa", "us", "u.s.", "america"),
    "united kingdom": ("united kingdom", "uk", "u.k.", "britain", "great britain", "england"),
    "uk": ("united kingdom", "uk", "u.k.", "britain", "england"),
}


def _country_named(country: str, text: str) -> bool:
    """Does `text` ("Brazil", "US", "U.S. applicants") name the applicant's country?"""
    c = country.strip().lower()
    if not c:
        return False
    for alias in _COUNTRY_ALIASES.get(c, (c,)):
        if re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", text.lower()):
            return True
    return False


def _address_component(question: str, per: dict) -> str | None:
    """Answer only the part of the address the label asks for.

    This used to join every component into one string for ANY address-shaped
    label, so a form with separate fields got the whole address pasted into
    "Zip/Postal Code" and duplicated into "Address 2" — caught on a real ATS.
    Most application forms that ask for an address split it into fields, so a
    bare "Address" means the street line; only an explicitly full/mailing
    address gets the joined form.
    """
    only_for = _ADDR_ONLY_FOR.search(question)
    if only_for and not _country_named(str(per.get("country") or ""), only_for.group(1)):
        return None  # e.g. "Home Address CEP (Brazil Only)" for a US applicant
    if _ADDR_POSTAL.search(question):
        return per.get("postal_code") or None
    if _ADDR_LINE2.search(question):
        # The profile holds a single street line. The honest answer to a
        # second-line field is "leave it blank", never a copy of line one.
        return None
    # A component named alongside "Home Address" ("Home Address City") asks
    # for that component — the joined address went into City, State and
    # Country on a real form before these were checked first.
    if _ADDR_CITY.search(question):
        return per.get("city") or None
    if _ADDR_STATE.search(question):
        return per.get("province_state") or None
    if _ADDR_COUNTRY.search(question):
        return per.get("country") or None
    if re.search(r"\b(line\s*1|line\s*one|street)\b", question, re.I):
        return per.get("address") or None
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

    # EEO: profile value if set, else the safe decline default — never None,
    # so these never surface as "answer this yourself" (see _EEO_DECLINE).
    if _MARKERS["eeo_gender"].search(q):
        return eeo.get("gender") or _EEO_DECLINE
    if _MARKERS["eeo_hispanic_latino"].search(q):
        return eeo.get("hispanic_latino") or _EEO_DECLINE
    if _MARKERS["eeo_race"].search(q):
        return eeo.get("race_ethnicity") or _EEO_DECLINE
    if _MARKERS["eeo_orientation"].search(q):
        if re.search(r"\btransgender\b", q, re.I):
            return eeo.get("transgender") or _EEO_DECLINE
        return eeo.get("sexual_orientation") or _EEO_DECLINE
    if _MARKERS["eeo_veteran"].search(q):
        return eeo.get("veteran_status") or _EEO_DECLINE
    if _MARKERS["eeo_disability"].search(q):
        if _ACCOMMODATION_RE.search(q):
            # "Do you need an accommodation for a disability?" asks about the
            # applicant's own needs, not self-identification: neither
            # disability_status nor the decline text answers it.
            return None
        return eeo.get("disability_status") or _EEO_DECLINE

    if _MARKERS["pronouns"].search(q):
        return per.get("pronouns") or None

    if _MARKERS["dob"].search(q):
        return None  # no DOB in profile — never guess

    if _MARKERS["address"].search(q):
        return _address_component(q, per)

    if _MARKERS["clearance"].search(q):
        return None  # not in profile — refuse

    return None


# ---------------------------------------------------------------------------
# Choosing among a question's OPTIONS (the extension knows them for selects,
# radio groups and button groups). A bare "Yes" is not enough when the options
# bundle facts: "Yes, I am a U.S. citizen or permanent resident" vs "Yes, I am
# authorized but will require sponsorship" — picking the first "Yes" made a
# false citizenship claim for an applicant on a work visa.
# ---------------------------------------------------------------------------

_CITIZEN_CLAIM = re.compile(r"\b(citizen|permanent\s+resident|green\s*card|lawful\s+permanent)\b", re.I)
_NEG_NEAR = re.compile(r"\b(not|no|never|without|don'?t|do\s+not|will\s+not|won'?t)\b", re.I)
_SPONSOR_WORD = re.compile(r"\bsponsor\w*\b|\bvisa\b", re.I)
_NEED_WORD = re.compile(r"\b(require|requires|required|need|needs|will\s+need)\b", re.I)
_VISA_TYPES = re.compile(r"\b(h-?1b|h-?4|f-?1|opt|stem\s*opt|cpt|l-?1|tn|o-?1|e-?[123]|j-?1|ead|visa)\b", re.I)
_CITIZEN_TYPES = re.compile(r"\b(citizen|green\s*card|permanent\s+resident|lpr)\b", re.I)
_DECLINE_OPT = re.compile(
    r"\b(decline|prefer\s+not|rather\s+not|not\s+declared|do(?:n'?t|\s+not)\s+(?:wish|want)|"
    r"choose\s+not|not\s+to\s+(?:say|answer|disclose|self[- ]identify))\b", re.I)
_ASSERTS = re.compile(r"^\s*(yes|no)\b|\bi\s+am\s+(a|an)\b|\bi\s+(self[- ])?identify\s+as\b|\bi\s+have\s+(a|an)\b",
                      re.I)


# Specific visa/permit names an option may carry ("Yes, I will require H-1B
# sponsorship" vs "Yes, TN"). Normalised so "H1B", "H-1B" and "h1-b" agree.
_VISA_NAME_RE = re.compile(
    r"\b(h[\s-]?1[\s-]?b|h[\s-]?4(?:\s*ead)?|f[\s-]?1|stem\s*opt|opt|cpt|l[\s-]?1[ab]?|tn|o[\s-]?1|"
    r"e[\s-]?3|e[\s-]?2|j[\s-]?1|ead|green\s*card)\b", re.I)


def _visa_names(text: str) -> set[str]:
    out = set()
    for m in _VISA_NAME_RE.finditer(text or ""):
        tok = re.sub(r"[\s-]", "", m.group(1).lower())
        out.add({"stemopt": "opt"}.get(tok, tok))
    return out


def _polarity(text: str) -> str | None:
    t = text.strip().lower()
    if re.match(r"^yes\b", t):
        return "yes"
    if re.match(r"^no\b", t):
        return "no"
    return None


def _citizen_or_pr(wa: dict) -> bool | None:
    kind = str(wa.get("work_permit_type") or wa.get("citizenship") or "").strip()
    if not kind:
        return None
    if _CITIZEN_TYPES.search(kind):
        return True
    if _VISA_TYPES.search(kind):
        return False
    return None


def _sponsorship_claim(option: str) -> bool | None:
    """True: the option says the applicant needs sponsorship; False: says they
    don't; None: says nothing about it."""
    if not _SPONSOR_WORD.search(option):
        return None
    if re.search(r"\bwithout\s+(\w+\s+){0,2}(sponsor\w*|visa)", option, re.I):
        return False
    m = _NEED_WORD.search(option)
    if not m:
        return None
    before = option[max(0, m.start() - 14):m.start()]
    return not bool(_NEG_NEAR.search(before))


def choose_option(question: str, options: list[str], profile: dict) -> tuple[str | None, str]:
    """(exact option text, "") when exactly one option is consistent with every
    fact the profile states; (None, reason) otherwise. Only for questions
    resolve_canary answers; the caller keeps the plain answer when there are
    no options to choose from."""
    answer = resolve_canary(question, profile)
    opts = [o for o in (options or []) if o and o.strip() and not re.match(r"^\s*(select|choose|--)", o, re.I)]
    if not answer or not opts:
        return None, "not resolvable from profile"
    if answer == _EEO_DECLINE:
        plain = [o for o in opts if _DECLINE_OPT.search(o) and not _ASSERTS.search(o)]
        if len(plain) == 1:
            return plain[0], ""
        return None, ("no plain decline option" if not plain else "several decline options")
    wa = (profile or {}).get("work_authorization", {}) or {}
    if not (_MARKERS["workauth"].search(question) or _MARKERS["sponsorship"].search(question)):
        exact = [o for o in opts if o.strip().lower() == answer.strip().lower()]
        return (exact[0], "") if len(exact) == 1 else (None, "no exact option")
    want_pol = _polarity(answer)
    needs = _as_bool(wa.get("require_sponsorship"))
    citizen = _citizen_or_pr(wa)
    mine = _visa_names(str(wa.get("work_permit_type") or ""))
    fits = []
    for o in opts:
        pol = _polarity(o)
        if want_pol and pol and pol != want_pol:
            continue
        if want_pol and not pol and not _SPONSOR_WORD.search(o) and not _CITIZEN_CLAIM.search(o):
            continue  # an option with no yes/no and no facts says nothing we can check
        if _CITIZEN_CLAIM.search(o) and not _NEG_NEAR.search(o) and citizen is not True:
            continue  # never claim citizenship/permanent residency the profile doesn't state
        claim = _sponsorship_claim(o)
        if claim is not None and (needs is None or claim != needs):
            continue
        named = _visa_names(o)
        if named and not (named & mine):
            continue  # names a specific visa that isn't the applicant's
        fits.append(o)
    if len(fits) > 1 and mine:
        # Several options fit the yes/no and sponsorship facts; the one that
        # names the applicant's own visa ("... H-1B ...") is the answer.
        specific = [o for o in fits if _visa_names(o) & mine]
        if len(specific) == 1:
            return specific[0], ""
    if len(fits) == 1:
        return fits[0], ""
    return None, ("no option matches your work-authorization facts" if not fits
                  else "several options could fit — answer this yourself")
