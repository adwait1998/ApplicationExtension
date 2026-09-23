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
    (re.compile(r"\b(first\s*name|given\s*name|fname)\b", re.I), "personal.full_name#first"),
    (re.compile(r"\b(last\s*name|family\s*name|surname|lname)\b", re.I), "personal.full_name#last"),
    (re.compile(r"\b(full\s*name|your\s*name|applicant\s*name)\b", re.I), "personal.full_name"),
    (re.compile(r"\be-?mail\b", re.I), "personal.email"),
    (re.compile(r"\b(phone|mobile|cell)\s*(number)?\b", re.I), "personal.phone"),
    (re.compile(r"\blinkedin\b", re.I), "personal.linkedin_url"),
    (re.compile(r"\bgithub\b", re.I), "personal.github_url"),
    (re.compile(r"\b(portfolio|personal\s*site)\b", re.I), "personal.portfolio_url"),
    (re.compile(r"\bwebsite\b", re.I), "personal.website_url"),
    (re.compile(r"\bcity\b", re.I), "personal.city"),
    (re.compile(r"\b(state|province)\b", re.I), "personal.province_state"),
    (re.compile(r"\bcountry\b", re.I), "personal.country"),
    (re.compile(r"\bcurrent\s*(company|employer)\b", re.I), "experience.current_company"),
    (re.compile(r"\bcurrent\s*(title|role|job\s*title)\b", re.I), "experience.current_job_title"),
]

# Candidate profile keys eligible for the Laya (tier 3) semantic
# classification pass — deterministic-tier paths only, secret paths are
# never included (resolve.py's guard is the enforcement point, this is
# belt-and-suspenders so Laya is never even offered the option).
CANDIDATE_KEYS: list[str] = sorted(
    {p.split("#", 1)[0] for p in AUTOCOMPLETE_MAP.values()}
    | {path.split("#", 1)[0] for _pattern, path in _NAME_LABEL_PATTERNS}
)


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


def value_for_key(key: str, profile: dict) -> str | None:
    """Resolve a profile path (optionally suffixed ``#first``/``#last``) to
    a string value, or None if unset/blank."""
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
        return SkipResult(
            id=field.id,
            source="deterministic",
            reason="file upload is not automated (v1) — attach your resume yourself",
            auto_fill=False,
        )

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

    haystack = " ".join(str(x or "") for x in (field.label, field.name, field.placeholder))
    for pattern, path in _NAME_LABEL_PATTERNS:
        if pattern.search(haystack):
            base_path = path.split("#", 1)[0]
            value = value_for_key(path, profile)
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
