"""Tier 2 of the resolution ladder: deterministic field matching.

Precedence: the HTML ``autocomplete`` attribute first (a web standard and
the highest-precision signal available — browsers themselves rely on it for
autofill), then name/id/label regex matching as a fallback for forms that
omit it (most ATS forms do).

The semantic-key -> profile-path mapping mirrors
``applypilot.apply.v2.resolver._PROFILE_PATHS`` (read for reference, not
imported) rather than inventing a new one. It is not imported directly
because resolver.py pulls in the apply/v2 IR + self-healing + mapping-cache
machinery, which this lightweight, always-available local service has no
need for and should not depend on.

Canary fields (work auth, sponsorship, salary, EEO, address, ...) are
intercepted by ``applypilot.extension.resolve`` at tier 1, before this
module ever runs — this module never sees them.
"""
from __future__ import annotations

import re

from applypilot.extension.schema import FieldDescriptor, FillResult, SkipResult

# HTML autocomplete token -> profile path. The "#first"/"#last" suffix is
# this module's own convention meaning "split personal.full_name at fill
# time" (Lever/Greenhouse alike store only a single full_name in the
# profile; the split happens here, never upstream).
AUTOCOMPLETE_MAP: dict[str, str] = {
    "given-name": "personal.full_name#first",
    "family-name": "personal.full_name#last",
    "name": "personal.full_name",
    "email": "personal.email",
    "tel": "personal.phone",
    "tel-national": "personal.phone",
    "address-line1": "personal.address",
    "address-line2": "personal.address",
    "address-level2": "personal.city",
    "address-level1": "personal.province_state",
    "postal-code": "personal.postal_code",
    "country": "personal.country",
    "country-name": "personal.country",
    "organization": "experience.current_company",
    "organization-title": "experience.current_job_title",
    "url": "personal.portfolio_url",
}

# Fallback: (compiled regex, profile path) checked against "name id label
# placeholder". Order matters — first/last name must be checked before the
# generic full-name pattern, which must be checked before nothing (there is
# no other name-shaped catch-all).
_NAME_LABEL_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Before first/full name: "Preferred Name (if different from legal name)"
    # contains "legal name" and used to get the full legal name.
    # "Preferred First & Last Name" -> preferred first + legal last.
    (re.compile(r"\bpreferred\s+(first\s*(&|and|\+)\s*last|full)\s+name\b", re.I), "personal.preferred_full_name"),
    (re.compile(r"\b(preferred|nick)\s*(first\s*)?name\b|\bname\s+you\s+go\s+by\b", re.I),
     "personal.preferred_name"),
    # "First and Last Name" / "First & Last Name" is the FULL name; checked
    # before the last-name rule, which used to claim it and give "Quill Testperson".
    (re.compile(r"\bfirst\s*(&|and|\+|/)\s*last\s*name\b", re.I), "personal.full_name"),
    (re.compile(r"\b(first\s*name|given\s*name|fname)\b", re.I), "personal.full_name#first"),
    (re.compile(r"\b(last\s*name|family\s*name|surname|lname)\b", re.I), "personal.full_name#last"),
    (
        re.compile(
            r"\bfull\s*name\b|\byour\s*name\b|\bapplicant\s*name\b|\blegal\s*name\b"
            r"|\bfirst\s*and\s*last\s*name\b|name\s*\(first\s*and\s*last\)",
            re.I,
        ),
        "personal.full_name",
    ),
    (re.compile(r"\be-?mail\b", re.I), "personal.email"),
    (re.compile(r"\b(phone|mobile|cell)\s*(number)?\b", re.I), "personal.phone"),
    (re.compile(r"\blinkedin\b", re.I), "personal.linkedin_url"),
    (re.compile(r"\bgithub\b", re.I), "personal.github_url"),
    (re.compile(r"\b(portfolio|personal\s*site)\b", re.I), "personal.portfolio_url"),
    (re.compile(r"\bwebsite\b", re.I), "personal.website_url"),
    (re.compile(r"\bcity\b", re.I), "personal.city"),
    # "state" the noun, not the verb: "Please state why you want to join"
    # was filled with the applicant's state.
    (re.compile(r"(?<!please )\b(state|province)\b(?!\s+(why|how|what|whether|which|your|the|any|if|"
                r"briefly|in\s+detail|clearly|below|here)\b)", re.I), "personal.province_state"),
    (re.compile(r"\bcountry\b", re.I), "personal.country"),
    # "Location" / "Current location" / "What is your location?" — where the
    # applicant is, as "City, State". Not a job-location preference.
    (re.compile(r"(?<!preferred )(?<!desired )(?<!office )(?<!work )(?<!job )\b(current\s+)?location\b"
                r"(?!\s+(preference|you\s+are\s+applying|of\s+(the|this)\s+(role|job|position)))"
                r"|\bwhere\s+are\s+you\s+(currently\s+)?(located|based)\b", re.I),
     "personal.location"),
    (re.compile(r"\b(current|most\s+recent)(\s*/\s*most\s+recent)?\s*(company|employer)\b"
                r"|\bwhere\s+(are|were)\s+you\s+(currently\s+|most\s+recently\s+)?(employed|working)\b", re.I),
     "experience.current_company"),
    (re.compile(r"\b(current|most\s+recent)(\s*/\s*most\s+recent)?\s*(title|role|job\s*title|position)\b", re.I),
     "experience.current_job_title"),
]

# Candidate profile keys eligible for the Laya (tier 4) semantic
# classification pass — deterministic-tier paths only, secret paths are
# never included (resolve.py's guard is the enforcement point, this is
# belt-and-suspenders so Laya is never even offered the option).
CANDIDATE_KEYS: list[str] = sorted(
    {p.split("#", 1)[0] for p in AUTOCOMPLETE_MAP.values()}
    | {path.split("#", 1)[0] for _pattern, path in _NAME_LABEL_PATTERNS}
)

# Laya's confidence is only calibrated up to ~10 choice options — the library
# itself warns that 11+ ships out-of-range temperatures and that confidence
# should then be treated as uncalibrated. Since the confidence gate is the
# ONLY thing standing between Laya and a wrong value in a real application,
# an uncalibrated score is worse than no Laya at all. So candidates are
# pre-ranked here and the tier offers the top few plus "none", never the
# whole catalogue.
LAYA_MAX_CANDIDATES = 9

# Extra vocabulary per key, for terms a human uses that the dotted path does
# not contain ("mobile" -> phone, "zip" -> postal_code).
_KEY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "personal.full_name": ("name", "applicant", "candidate", "legal"),
    "personal.email": ("email", "e-mail", "mail", "contact"),
    "personal.phone": ("phone", "mobile", "cell", "telephone", "tel", "number"),
    "personal.city": ("city", "town", "municipality", "location"),
    "personal.province_state": ("state", "province", "region"),
    "personal.country": ("country", "nation"),
    "personal.postal_code": ("postal", "zip", "postcode", "code"),
    "personal.address": ("address", "street", "residence", "line"),
    "personal.linkedin_url": ("linkedin", "profile", "url", "link"),
    "personal.github_url": ("github", "git", "repo", "url", "link"),
    "personal.portfolio_url": ("portfolio", "work", "url", "link", "site"),
    "personal.website_url": ("website", "site", "url", "link", "homepage"),
    "experience.current_company": ("company", "employer", "organization", "org", "firm"),
    "experience.current_job_title": ("title", "role", "position", "job", "occupation"),
}

# ---------------------------------------------------------------------------
# Skills — a multi-value field (Workday's "Type to Add Skills" prompt widget
# adds one skill at a time; a plain text "Skills" field just wants them
# joined). Recognised by label alone -- like GPA in the structured tier,
# this is treated as TERMINAL once recognised: the applicant's own declared
# skill list must only ever come from the profile, never the answer bank or
# a draft, so a skills-shaped field that matches this pattern is always
# claimed here (a FillResult when the profile has skills, else a
# SkipResult) and never falls through to a lower tier.
_SKILLS_LABEL_RE = re.compile(r"\bskills?\b", re.I)

# Adding 60 skills to a Workday prompt widget one popup at a time is slow
# and noisy for the operator to watch/verify -- 15 covers a realistic résumé
# without turning the fill into a multi-minute loop.
MAX_SKILLS = 15


def _flatten_skills(profile: dict, limit: int = MAX_SKILLS) -> list[str]:
    """Every skill across every skills_boundary category (languages,
    frameworks, devops, databases, tools, ... -- whatever categories the
    profile actually has, mirroring the flattening already done by
    scoring/tailor.py, scoring/validator.py and scoring/cover_letter.py
    rather than hardcoding a fixed set of category names), profile order
    preserved, deduplicated case-insensitively (first-seen casing wins),
    capped at `limit`."""
    boundary = (profile or {}).get("skills_boundary") or {}
    if not isinstance(boundary, dict):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for items in boundary.values():
        if not isinstance(items, list):
            continue
        for item in items:
            skill = str(item).strip()
            if not skill:
                continue
            key = skill.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(skill)
    return out[:limit]


_PAREN_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")


def _skill_search_term(skill: str) -> str:
    """What to TYPE into a skills search box: "React (basic understanding)"
    searches for "React" — the qualifier is for a human reader and matches
    no skill tag. The joined text value (for a plain text field) keeps the
    qualifier, so nothing is overstated there."""
    return _PAREN_RE.sub("", skill).strip(" ,;-") or skill


# Skills are claimed only when the field IS a skills field: a short label
# ("Skills", "Technical skills"), an explicit request for a list ("List your
# skills", "Please list any software tools you have used"), or Workday's
# skills prompt. A question that merely mentions skills or services was
# filled with the whole skills list on a real Ashby form.
_SKILLS_REQUEST_RE = re.compile(
    r"^\W*(please\s+)?(list|enter|add|share|select|what\s+are)\s+(your|any|some|relevant|key|top)?\s*"
    r"(\w+\s+){0,2}skills\b"
    r"|\b(list|what)\b.{0,30}\b(software|tools|technologies|programs)\b.{0,40}\b(used|use|proficient|"
    r"familiar|experienced?|trained)\b",
    re.I)


def _is_skills_field(field: FieldDescriptor, haystack: str) -> bool:
    label = (field.label or "").strip() or haystack
    if (field.widget or "") == "wd-prompt" and _SKILLS_LABEL_RE.search(label):
        return True
    if _SKILLS_LABEL_RE.search(label) and len(label.split()) <= 5 and "?" not in label:
        return True
    return bool(_SKILLS_REQUEST_RE.search(label))


def _match_skills(field: FieldDescriptor, haystack: str, profile: dict) -> FillResult | SkipResult | None:
    if not _is_skills_field(field, haystack):
        return None
    skills = _flatten_skills(profile)
    if not skills:
        return SkipResult(
            id=field.id,
            source="deterministic",
            reason="skills-shaped field matched but no skills_boundary set in profile",
            auto_fill=False,
        )
    return FillResult(
        id=field.id,
        value=", ".join(skills),
        values=_dedupe_ci(_skill_search_term(x) for x in skills),
        source="deterministic",
        profile_key="skills_boundary",
        confidence=0.9,
        auto_fill=True,
        reason="skills-shaped label matched profile skills_boundary",
    )


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _terms_for_key(key: str) -> set[str]:
    """Vocabulary describing a profile key: its own path tokens plus synonyms."""
    tokens = set(_TOKEN_RE.findall(key.lower().replace(".", " ").replace("_", " ")))
    tokens.discard("personal")
    tokens.discard("experience")
    tokens.discard("url")
    return tokens | set(_KEY_SYNONYMS.get(key, ()))


def _field_terms(field: FieldDescriptor) -> set[str]:
    hay = " ".join(
        str(x or "")
        for x in (field.label, field.name, field.placeholder, field.autocomplete)
    )
    hay = hay.lower().replace("_", " ").replace("-", " ")
    return set(_TOKEN_RE.findall(hay))


def rank_candidates(
    field: FieldDescriptor,
    keys: list[str] | None = None,
    limit: int = LAYA_MAX_CANDIDATES,
) -> list[str]:
    """The most plausible profile keys for this field, best first, capped at
    `limit` so Laya is only ever asked a question it can calibrate.

    Scoring is deliberately cheap and deterministic (term overlap, not a
    model): its only job is to narrow the field before the real classifier
    runs. Keys that share no vocabulary with the field still fill out the
    tail in stable alphabetical order, so the list is never short enough to
    exclude a correct-but-oddly-worded answer.
    """
    pool = list(keys if keys is not None else CANDIDATE_KEYS)
    field_terms = _field_terms(field)

    def score(key: str) -> tuple[int, str]:
        overlap = len(field_terms & _terms_for_key(key))
        return (-overlap, key)          # higher overlap first, then stable by name

    return sorted(pool, key=score)[:limit]


def _dig(profile: dict, dotted_path: str):
    node = profile or {}
    for part in dotted_path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _split_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


# Label rules map a field to ONE profile value by a keyword in its label.
# That is only safe for a short-answer box that asks for that value: an
# essay box whose prompt merely mentions a keyword ("Please state why...",
# "How did you hear about us? (LinkedIn, ...)") must never get a profile
# value, and a choice field can only take a value that is one of its
# options (locations are the one family worth trying there).
_LOCATION_KEYS = {"personal.city", "personal.province_state", "personal.country", "personal.location"}
_URL_KEYS = {"personal.linkedin_url", "personal.github_url", "personal.portfolio_url", "personal.website_url"}
_ESSAY_PROMPT_RE = re.compile(
    r"\b(why|describe|explain|tell\s+us|share\s+(a|an|your)\s+(time|example|story)|how\s+did\s+you|"
    r"hear\s+about|referr\w*|source)\b", re.I)
_MAX_LABEL_WORDS = 14


def _label_rules_apply(field: FieldDescriptor) -> bool:
    ftype = (field.type or "").strip().lower()
    tag = (field.tag or "").strip().lower()
    if tag == "textarea" or ftype in ("textarea", "checkbox", "checkbox-group", "radio", "file"):
        return False
    return len((field.label or "").split()) <= _MAX_LABEL_WORDS


# A question whose subject is something else, that merely mentions a profile
# keyword: "What is your notice period to your current employer?" got the
# employer's name; "How may we pronounce your name?" got the full name.
_OTHER_SUBJECT_RE = re.compile(
    r"\b(notice\s+period|how\s+(long|many|much|familiar|may\s+we|do\s+we|should\s+we)|years?\s+of|why|"
    r"reason|relocat\w*|commut\w*|travel\w*|visa|salary|compensation|start\s+date|availab\w+|when|"
    r"pronounc\w*|spell\w*|referr\w*|refer\s+you|hear\s+about|familiar)\b", re.I)


_QUESTION_LIKE_RE = re.compile(
    r"\?|^\W*(what|which|who|where|when|why|how|do|does|did|are|is|have|has|will|would|can|could|please)\b",
    re.I)


def _is_choice(field: FieldDescriptor) -> bool:
    return (bool(field.options) or (field.tag or "").strip().lower() == "select"
            or (field.type or "").strip().lower() in ("checkbox-group", "radio")
            or (field.widget or "") in ("wd-dropdown", "combobox", "button-group"))


# Inside a work-history or education block, "Location"/"City" describe that
# job or school — the structured tier's to fill, never the applicant's own.
_HISTORY_SECTION_RE = re.compile(
    r"\b(work\s*experience|employment|job\s*history|experience|education|academic|school)\b", re.I)


def _label_rule_fits(path: str, field: FieldDescriptor, haystack: str) -> bool:
    base = path.split("#", 1)[0]
    if base in _LOCATION_KEYS and (field.section_index is not None
                                   or _HISTORY_SECTION_RE.search(field.section or "")):
        return False
    label = field.label or ""
    if _QUESTION_LIKE_RE.search(label) and _OTHER_SUBJECT_RE.search(label):
        return False
    if _is_choice(field) and base not in _LOCATION_KEYS:
        return False
    if base in _URL_KEYS and _ESSAY_PROMPT_RE.search(field.label or ""):
        return False
    return True


# Options that describe a work arrangement ("Remote - US") are a different
# question from where the applicant lives; never picked as a location.
_ARRANGEMENT_RE = re.compile(r"\b(remote|hybrid|on-?site|in[- ]office|relocat\w*)\b", re.I)


def _names_in(options: list[str], value: str) -> bool:
    v = re.escape(value.strip().lower())
    return any(re.search(rf"(?<![a-z]){v}(?![a-z])", o.lower()) for o in options
               if o and not _ARRANGEMENT_RE.search(o))


_US_COUNTRY_ALIASES = ("united states", "united states of america", "usa", "us", "u.s.")


def _location_for_options(field: FieldDescriptor, profile: dict, prefer: str) -> str | None:
    """With the options unknown (a Workday/combobox menu) keep the requested
    part; with options known, the most specific part some option names."""
    per = (profile or {}).get("personal") or {}
    parts = {"personal.city": per.get("city"), "personal.province_state": per.get("province_state"),
             "personal.country": per.get("country")}
    if not field.options:
        key = "personal.city" if prefer == "personal.location" else prefer
        if key == "personal.city" and (field.widget or "") == "combobox" and parts.get("personal.city")                 and parts.get("personal.province_state"):
            # A city type-ahead (Greenhouse "Location (City)", geocoded options
            # like "Seattle, Washington, United States"): the state lets the
            # driver pick THE Seattle instead of refusing several.
            return f"{parts['personal.city']}, {parts['personal.province_state']}"
        return parts.get(key) or None
    order = ["personal.city", "personal.province_state", "personal.country"]
    if prefer in order:
        order = [prefer] + [k for k in order if k != prefer]
    for key in order:
        val = parts.get(key)
        if val and _names_in(field.options, str(val)):
            return str(val)
    country = str(parts.get("personal.country") or "").strip().lower()
    if country in _US_COUNTRY_ALIASES:
        for alias in ("United States", "United States of America", "USA", "US"):
            if _names_in(field.options, alias):
                return next(o for o in field.options if not _ARRANGEMENT_RE.search(o)
                            and re.search(rf"(?<![a-z]){re.escape(alias.lower())}(?![a-z])", o.lower()))
    return None


# "How many years of (relevant) experience do you have?" with range options
# ("0-2 years", "3-5", "5+", "Less than 1 year"): the option whose range
# holds the profile's total years — and nothing when two ranges both do.
_YEARS_Q_RE = re.compile(r"\byears?\b.{0,40}\bexperience\b|\bexperience\b.{0,40}\byears?\b|\bhow\s+many\s+years\b",
                         re.I)


def _option_range(opt: str) -> tuple[float, float] | None:
    o = opt.lower().replace("–", "-").replace("—", "-")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)", o)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:\+|or\s+more|and\s+(?:up|above|over)|plus)", o) \
        or re.search(r"(?:more\s+than|over|at\s+least|above)\s+(\d+(?:\.\d+)?)", o)
    if m:
        lo = float(m.group(1))
        return (lo + (0.001 if re.search(r"more\s+than|over|above", o) else 0), float("inf"))
    m = re.search(r"(?:less\s+than|under|below|fewer\s+than)\s+(\d+(?:\.\d+)?)", o)
    if m:
        return 0.0, float(m.group(1)) - 0.001
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(years?|yrs?)?\s*", o)
    if m:
        return float(m.group(1)), float(m.group(1)) + 0.999
    return None


def _match_years_range(field: FieldDescriptor, profile: dict) -> FillResult | SkipResult | None:
    if not field.options or not _YEARS_Q_RE.search(field.label or ""):
        return None
    raw = str(((profile or {}).get("experience") or {}).get("years_of_experience_total") or "").strip()
    try:
        years = float(raw)
    except ValueError:
        return None
    hits = [o for o in field.options if (r := _option_range(o)) and r[0] <= years <= r[1]]
    if len(hits) != 1:
        return None if not hits else SkipResult(
            id=field.id, source="deterministic",
            reason=f"{len(hits)} options fit {raw} years — pick the right one yourself", auto_fill=False)
    return FillResult(id=field.id, value=hits[0], source="deterministic",
                      profile_key="experience.years_of_experience_total", confidence=0.9, auto_fill=True,
                      reason=f"your total experience ({raw} years)")


def _dedupe_ci(items) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        k = item.lower()
        if k and k not in seen:
            seen.add(k)
            out.append(item)
    return out


# "When can you start?" / "Earliest start date" -> the profile's own
# availability. Deliberately strict: a bare "Start Date" is a work-history
# or education date (the structured tier's job) and must never receive
# "Immediately"; fields inside a numbered repeating section are skipped for
# the same reason.
_START_AVAILABILITY_RE = re.compile(
    r"\b(earliest|available|availability|desired|expected|potential|anticipated|preferred|possible|proposed)"
    r"\s+(start(ing)?|join(ing)?)\s+date\b"
    r"|\bwhen\s+(can|could|would|are)\s+you\s+(start|begin|join|be\s+available)\b"
    r"|\bavailab\w+\s+to\s+(start|begin|join)\b"
    r"|\bdate\s+(you\s+are\s+|you're\s+)?available\b"
    r"|\bstart\s+availability\b",
    re.I,
)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# "Today's Date of Application (MM/DD/YY Format)" — a plain fact, filled in
# the format the label asks for. A date next to a signature is part of the
# signature (the attestation tier leaves those for the applicant).
_TODAY_RE = re.compile(r"\b(today'?s|todays|current)\s+date\b|\bdate\s+of\s+application\b|\bapplication\s+date\b",
                       re.I)


def _match_todays_date(field: FieldDescriptor) -> FillResult | None:
    label = field.label or ""
    if not _TODAY_RE.search(label) or field.section_index is not None:
        return None
    import datetime as _dt
    d = _dt.date.today()
    ftype = (field.type or "").strip().lower()
    if ftype == "date":
        value = d.isoformat()
    elif re.search(r"\bdd\s*/\s*mm\b", label, re.I):
        value = d.strftime("%d/%m/%Y")
    elif re.search(r"\bmm\s*/\s*dd\s*/\s*yy\b(?!yy)", label, re.I):
        value = d.strftime("%m/%d/%y")
    elif re.search(r"\byyyy\s*-\s*mm\s*-\s*dd\b", label, re.I):
        value = d.isoformat()
    else:
        value = d.strftime("%m/%d/%Y")
    return FillResult(id=field.id, value=value, source="deterministic", profile_key="today",
                      confidence=1.0, auto_fill=True, reason="today's date")


def _match_start_availability(field: FieldDescriptor, haystack: str, profile: dict) -> FillResult | SkipResult | None:
    if field.section_index is not None or not _START_AVAILABILITY_RE.search(haystack):
        return None
    value = value_for_key("availability.earliest_start_date", profile)
    if not value:
        return None  # not an attestation: lower tiers / the human may answer
    ftype = (field.type or "").strip().lower()
    if ftype in ("date", "month") or (field.widget or "").startswith("wd-date"):
        if not _ISO_DATE_RE.match(value.strip()):
            return SkipResult(
                id=field.id,
                source="deterministic",
                reason=f"this start-date field needs a calendar date; your profile says '{value}' — pick one yourself",
                auto_fill=False,
            )
    return FillResult(
        id=field.id,
        value=value,
        source="deterministic",
        profile_key="availability.earliest_start_date",
        confidence=0.9,
        auto_fill=True,
        reason="your earliest start date",
    )


def value_for_key(key: str, profile: dict) -> str | None:
    """Resolve a profile path (optionally suffixed ``#first``/``#last``) to
    a string value, or None if unset/blank."""
    if key == "personal.preferred_full_name":
        pref = _dig(profile, "personal.preferred_name")
        full = _dig(profile, "personal.full_name")
        if not full:
            return None
        if not pref:
            return str(full)
        _first, last = _split_name(str(full))
        return f"{pref} {last}".strip()
    if key == "personal.location" and not _dig(profile, key):
        parts = [_dig(profile, "personal.city"), _dig(profile, "personal.province_state")]
        joined = ", ".join(str(x) for x in parts if x)
        return joined or None
    if key.endswith("#first") or key.endswith("#last"):
        base = key.split("#", 1)[0]
        full = _dig(profile, base)
        if not full:
            return None
        first, last = _split_name(str(full))
        picked = first if key.endswith("#first") else last
        return picked or None
    val = _dig(profile, key)
    if val is None or val == "":
        return None
    return str(val)


def match(field: FieldDescriptor, profile: dict) -> FillResult | SkipResult | None:
    """Tier 2: deterministic matching.

    Returns a FillResult/SkipResult when this tier has an opinion about the
    field, or None to let the ladder continue on to Laya / unresolved.
    """
    # File inputs (resume/cover letter) are explicitly out of scope for v1 —
    # the file picker is OS-level. Surfaced as a skip naming what it is,
    # never faked as a fill.
    if (field.type or "").strip().lower() == "file":
        label = f"{field.label} {field.name}".lower()
        if "cover" in label:
            why = "cover letter upload — attach one yourself (a résumé is never put here)"
        elif any(w in label for w in ("resume", "résumé", "cv")):
            why = "résumé upload — attached by the résumé step (see the résumé line), not by field filling"
        else:
            why = "file upload — attach this yourself"
        return SkipResult(id=field.id, source="deterministic", reason=why, auto_fill=False)

    haystack = " ".join(str(x or "") for x in (field.label, field.name, field.placeholder))

    # Skills: terminal once recognised, same invariant as GPA in the
    # structured tier -- checked before autocomplete/name-label matching so
    # a skills-shaped field can never be misread as something else, and
    # never falls through to a lower tier that could source it from the
    # answer bank or a draft instead of the profile's own declared skills.
    skills_result = _match_skills(field, haystack, profile)
    if skills_result is not None:
        return skills_result

    start = _match_start_availability(field, haystack, profile)
    if start is not None:
        return start

    today = _match_todays_date(field)
    if today is not None:
        return today

    years = _match_years_range(field, profile)
    if years is not None:
        return years

    autocomplete = (field.autocomplete or "").strip().lower()
    if autocomplete:
        # autocomplete can carry multiple tokens ("shipping given-name") —
        # the field-purpose token is always the last one.
        token = autocomplete.split()[-1]
        path = AUTOCOMPLETE_MAP.get(token)
        if path:
            base_path = path.split("#", 1)[0]
            value = value_for_key(path, profile)
            if value:
                return FillResult(
                    id=field.id,
                    value=value,
                    source="deterministic",
                    profile_key=base_path,
                    confidence=1.0,
                    auto_fill=True,
                    reason=f"autocomplete={autocomplete}",
                )
            return SkipResult(
                id=field.id,
                source="deterministic",
                reason=f"autocomplete={autocomplete} recognized but {base_path} is not set in profile",
                auto_fill=False,
            )

    if not _label_rules_apply(field):
        return None

    for pattern, path in _NAME_LABEL_PATTERNS:
        if pattern.search(haystack):
            if not _label_rule_fits(path, field, haystack):
                continue
            base_path = path.split("#", 1)[0]
            value = value_for_key(path, profile)
            if base_path in _LOCATION_KEYS and _is_choice(field):
                # A list of places may be cities ("Seattle, WA"), states or
                # countries: offer the most specific part of the applicant's
                # location that one of the options actually names. A
                # "What is your location?" select on Lever was a country list.
                value = _location_for_options(field, profile, prefer=base_path)
                if value is None and field.options:
                    return SkipResult(id=field.id, source="deterministic",
                                      reason="none of this field's options names your city, state or country",
                                      auto_fill=False)
                value = value if value is not None else value_for_key(path, profile)
            if value:
                return FillResult(
                    id=field.id,
                    value=value,
                    source="deterministic",
                    profile_key=base_path,
                    confidence=0.85,
                    auto_fill=True,
                    reason=f"name/label match: {pattern.pattern}",
                )
            return SkipResult(
                id=field.id,
                source="deterministic",
                reason=f"matched {base_path} but it is not set in profile",
                auto_fill=False,
            )

    return None
