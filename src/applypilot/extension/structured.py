"""Tier 3 of the resolution ladder: structured work-history / education
matching.

Runs after the deterministic tier (tier 2) has had a chance and found no
opinion, and before Laya (tier 4). It owns exactly the fields a repeating
"Work Experience N" / "Education N" section asks for: job title, company,
location, current-employer checkbox, start/end dates and a role
description (mirrored for education as school/degree/field of study).

The bug this tier fixes was never a matching problem: on the real Workday
form that motivated it, every one of these fields was perfectly
recognisable — the profile simply had no ``work_history``/``education``
data to answer from. So the rule here is conservative on purpose:

* A field is only ever claimed once both a *kind* (work vs education) and a
  *slot* within that kind (title, company, ...) are established. Generic
  slots (location, from, to, description) never establish a kind by
  themselves — only an explicit ``section`` heading, or an unambiguous
  slot like "Job Title" / "School", can.
* ``section_index`` (1-based, from the scanner) maps straight to
  ``work_history[section_index - 1]`` / ``education[section_index - 1]``.
  Out of range is a **terminal skip** ("no 3rd position in your work
  history") — it never wraps around and never falls back to position 0.
  Silently putting a person's current job in the "previous employer" box
  is a real-world harm, not a UX nicety.
* No ``section_index`` but the field is clearly record-shaped -> the most
  recent position (index 0) is used, per the spec.
* A profile with no ``work_history``/``education`` at all behaves exactly
  as it did before this tier existed: this module returns ``None`` (not a
  skip) so the field falls through to Laya/unresolved and gets the same
  generic message it always got. GPA is the one deliberate exception (see
  below) -- it is a factual claim about the applicant's record, so a GPA
  question must never fall through to Laya/answer-bank/draft even when the
  profile has no education data at all to answer it from.
* A work-history "description" is normalised to bullet lines at fill time
  (``resume_import._normalize_description``, reused rather than
  reimplemented) -- profiles imported before that normalisation existed
  still hold a single paragraph on disk, and this tier's job is to fill the
  form correctly regardless of when the profile was written, without ever
  rewriting the profile itself.
* GPA is only ever filled from a matching ``education[]`` entry, exactly
  like school/degree/field -- never from the answer bank or a draft (those
  tiers run after this one and are never even reached once this tier
  recognises a field as a GPA question). If the form states a scale
  ("GPA out of 4.0") that does not match the stored value's own scale, the
  field is skipped with a clear reason rather than converted -- converting
  grade scales is a judgment call this tier does not make.
"""
from __future__ import annotations

import re

from applypilot.extension.resume_import import _normalize_description
from applypilot.extension.schema import FieldDescriptor, FillResult, SkipResult

# ---------------------------------------------------------------------------
# Section-heading -> kind
# ---------------------------------------------------------------------------

_WORK_SECTION_RE = re.compile(r"\b(work\s*experience|work\s*history|employment(?:\s*history)?|job\s*history)\b", re.I)
_EDU_SECTION_RE = re.compile(r"\b(education|academic\s*(?:history|background)?|school(?:ing)?)\b", re.I)

# Kind hints strong enough to establish "this is a work/education field"
# even with no section context at all (a single-entry mini-form with no
# repeating-section heading). Deliberately narrower than the full slot
# pattern lists below: generic slots (location, from, to, description,
# "currently work here") are NOT kind-defining on their own, because
# without a section heading there is nothing to disambiguate a bare
# "Location" or "From" field from an unrelated one elsewhere on the page.
_WORK_KIND_HINT_RE = re.compile(
    r"\bjob\s*title\b|\brole\s*description\b|\bcompany\b|\bemployer\b", re.I
)
_EDU_KIND_HINT_RE = re.compile(
    r"\bschool\b|\buniversity\b|\bcollege\b|\bdegree\b|\bfield\s*of\s*study\b|\bdiscipline\b|\bmajor\b"
    r"|\bgpa\b|\bgrade\s*point\s*average\b",
    re.I,
)
# "Which university are you currently attending or did you last attend?" —
# a question, but one whose answer IS the most recent school.
_SCHOOL_QUESTION_RE = re.compile(
    r"\b(which|what)\s+(university|school|college)\b|\b(university|school|college)\s+(did|do|are)\s+you\b", re.I)

# ---------------------------------------------------------------------------
# Slot patterns, per kind. Order matters: more specific first.
# ---------------------------------------------------------------------------

_WORK_SLOTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brole\s*description\b|\bjob\s*description\b|\bresponsibilit(?:y|ies)\b", re.I), "description"),
    (re.compile(r"\bjob\s*title\b|\bposition\s*title\b", re.I), "title"),
    (re.compile(r"\bcompany\b|\bemployer\b|\borgani[sz]ation(?:\s*name)?\b", re.I), "company"),
    (re.compile(r"\blocation\b", re.I), "location"),
    (
        re.compile(r"\b(?:i\s*)?currently\s*work\s*here\b|\bcurrent\s*(?:position|job|role)\b", re.I),
        "current",
    ),
    (re.compile(r"\bfrom\b|\bstart\s*date\b", re.I), "start"),
    (re.compile(r"\bto\b|\bend\s*date\b", re.I), "end"),
    (re.compile(r"\bdescription\b|\bsummary\b", re.I), "description"),
    (re.compile(r"\brole\b|\btitle\b", re.I), "title"),
]

_EDU_SLOTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bschool\b|\buniversity\b|\bcollege\b|\binstitution\b", re.I), "school"),
    (re.compile(r"\bdegree\b", re.I), "degree"),
    (re.compile(r"\bfield\s*of\s*study\b|\bmajor\b|\bdiscipline\b", re.I), "field"),
    (re.compile(r"\bgpa\b|\bgrade\s*point\s*average\b", re.I), "gpa"),
    (re.compile(r"\bfrom\b|\bstart\s*date\b", re.I), "start"),
    (re.compile(r"\bto\b|\bend\s*date\b", re.I), "end"),
]

# A scale the FORM explicitly states it wants ("GPA out of 4.0", "GPA/4.0",
# "4.0-point scale") -- checked against the stored value's own scale before
# filling. Deliberately narrow: only an explicit denominator on the field
# side counts, never an inference, since the whole point is to never
# convert between scales.
_GPA_FIELD_SCALE_RE = re.compile(
    r"\bout\s*of\s*(\d{1,2}(?:\.\d+)?)\b"
    r"|\bgpa\s*/\s*(\d{1,2}(?:\.\d+)?)\b"
    r"|(\d{1,2}(?:\.\d+)?)\s*[- ]?point\s*scale\b",
    re.I,
)

# The stored value's own scale, when the résumé/profile stated one
# ("8.9/10"). No "/" at all means the profile's default scale, 4.0 -- see
# resume_import._validate_gpa, which enforces that same default at import
# time.
_GPA_STORED_SCALE_RE = re.compile(r"^\s*\d{1,2}(?:\.\d+)?\s*/\s*(\d{1,2}(?:\.\d+)?)\s*$")
_DEFAULT_GPA_SCALE = 4.0


def _requested_gpa_scale(haystack: str) -> float | None:
    m = _GPA_FIELD_SCALE_RE.search(haystack)
    if not m:
        return None
    for g in m.groups():
        if g:
            return float(g)
    return None


def _stored_gpa_scale(value: str) -> float:
    m = _GPA_STORED_SCALE_RE.match(value)
    return float(m.group(1)) if m else _DEFAULT_GPA_SCALE

_TRAILING_DIGITS_RE = re.compile(r"(\d+)\s*$")

_ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(n: int) -> str:
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = _ORDINAL_SUFFIXES.get(n % 10, "th")
    return f"{n}{suffix}"


_QUESTION_LIKE_RE = re.compile(
    r"\?|^\W*(what|which|who|where|when|why|how|do|does|did|are|is|have|has|will|would|can|could|"
    r"please|before|tell|describe)\b", re.I)


def _kind_for(field: FieldDescriptor, haystack: str) -> str | None:
    section = field.section or ""
    if section:
        if _WORK_SECTION_RE.search(section):
            return "work"
        if _EDU_SECTION_RE.search(section):
            return "education"
        return None  # a section heading exists but names neither kind
    # No section context at all: only an unambiguous slot can establish kind —
    # and only on a field-name-shaped label. A QUESTION that mentions a
    # company ("Before seeing this job posting, how familiar were you with
    # Faire as a company?", "Have you worked at or been a consultant for
    # SoFi?") is not a work-history "Company" box; both got the applicant's
    # current employer on real forms.
    if _SCHOOL_QUESTION_RE.search(field.label or ""):
        return "education"
    if _QUESTION_LIKE_RE.search(field.label or "") or len((field.label or "").split()) > 8:
        return None
    is_work = bool(_WORK_KIND_HINT_RE.search(haystack))
    is_edu = bool(_EDU_KIND_HINT_RE.search(haystack))
    if is_work and not is_edu:
        return "work"
    if is_edu and not is_work:
        return "education"
    return None


def _slot_for(kind: str, haystack: str) -> str | None:
    slots = _WORK_SLOTS if kind == "work" else _EDU_SLOTS
    for pattern, slot in slots:
        if pattern.search(haystack):
            return slot
    return None


def _index_for(field: FieldDescriptor) -> tuple[int, bool]:
    """(zero_based_index, explicit). explicit=True means a section index was
    actually named (by the scanner, or parsed here as a fallback from the
    section heading text) — that distinction only matters for phrasing the
    out-of-range skip message; the range check itself is identical either
    way."""
    if isinstance(field.section_index, int) and field.section_index >= 1:
        return field.section_index - 1, True
    m = _TRAILING_DIGITS_RE.search(field.section or "")
    if m:
        n = int(m.group(1))
        if n >= 1:
            return n - 1, True
    return 0, False  # "clearly record-shaped, no index" -> most recent position


_MM_YYYY_RE = re.compile(r"^\s*(\d{1,2})\s*/\s*(\d{4})\s*$")


_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August",
                "September", "October", "November", "December")


def infer_run_sections(fields: list[FieldDescriptor]) -> list[FieldDescriptor]:
    """Greenhouse's education block has no numbered heading: School, Degree,
    Discipline, then "Start date month", "End date year" arrive as a run of
    unsectioned fields. Date/detail fields that directly follow a field
    which establishes education (or work) inherit that kind as block 1, so
    the structured tier can fill them. The run ends at the first field that
    is neither a kind hint nor such a slot; locations never inherit (too
    easily the applicant's own)."""
    import dataclasses

    out: list[FieldDescriptor] = []
    kind: str | None = None
    for f in fields:
        label = f.label or ""
        if f.section or f.section_index is not None or len(label.split()) > 8:
            kind = None
            out.append(f)
            continue
        if _EDU_KIND_HINT_RE.search(label) and not _WORK_KIND_HINT_RE.search(label):
            kind = "education"
            out.append(f)
            continue
        if _WORK_KIND_HINT_RE.search(label) and not _EDU_KIND_HINT_RE.search(label):
            kind = "work"
            out.append(f)
            continue
        if kind and re.search(r"\b(start|end|from|to|graduat\w*|month|year|dates?)\b", label, re.I) \
                and not re.search(r"\blocation\b|\bcity\b", label, re.I):
            out.append(dataclasses.replace(
                f, section="Education" if kind == "education" else "Work Experience", section_index=1))
            continue
        kind = None
        out.append(f)
    return out


def _format_date(value: str, field: FieldDescriptor) -> str:
    """Profile dates are stored ``MM/YYYY``. Emit whatever the field wants:

    - ``type="date"`` (a native date picker) -> ISO ``YYYY-MM-DD``, the only
      format such an input accepts. Day is unknown, so the 1st of the month
      is used.
    - Anything else (a text field, a Workday ``MM/YYYY``-placeholder field,
      or a field we can't read a hint from) -> the profile's own ``MM/YYYY``
      unchanged. Guessing a format we were not told to produce is worse
      than leaving it in the profile's native shape.
    """
    if not value:
        return value
    if (field.type or "").strip().lower() == "date":
        m = _MM_YYYY_RE.match(value)
        if m:
            mm, yyyy = m.group(1).zfill(2), m.group(2)
            return f"{yyyy}-{mm}-01"
    # A date split into separate month and year boxes ("End date month",
    # "End date year" on Greenhouse): each box gets only its part — the
    # month as its name, which is what a month dropdown lists.
    label = (field.label or "").lower()
    m = _MM_YYYY_RE.match(value)
    if m and re.search(r"\bmonth\b", label) and not re.search(r"\byear\b", label):
        month = int(m.group(1))
        return _MONTH_NAMES[month - 1] if 1 <= month <= 12 else value
    if m and re.search(r"\byear\b", label) and not re.search(r"\bmonth\b", label):
        return m.group(2)
    return value


def _records_for(kind: str, profile: dict) -> list[dict]:
    key = "work_history" if kind == "work" else "education"
    records = (profile or {}).get(key)
    if not isinstance(records, list):
        return []
    return [r for r in records if isinstance(r, dict)]


def _kind_label(kind: str) -> str:
    return "work history" if kind == "work" else "education"


def match(field: FieldDescriptor, profile: dict) -> FillResult | SkipResult | None:
    """Tier 3: structured work_history/education matching.

    Returns a FillResult/SkipResult when this tier claims the field
    (including a terminal "index out of range" skip), or None to let the
    ladder continue on to Laya / unresolved — which is also what happens
    whenever the profile simply has no data for the kind this field wants,
    so a profile without ``work_history``/``education`` behaves exactly as
    it did before this tier existed.
    """
    haystack = " ".join(str(x or "") for x in (field.label, field.name, field.placeholder))

    kind = _kind_for(field, haystack)
    if kind is None:
        return None

    slot = _slot_for(kind, haystack)
    if slot is None:
        return None

    records = _records_for(kind, profile)
    if not records:
        if kind == "education" and slot == "gpa":
            # GPA is a factual claim about the applicant's record: it must
            # only ever come from the profile, never the answer bank or a
            # draft. Unlike every other slot, a GPA question this tier
            # recognises stays terminal even with zero education data, so
            # it can never fall through to a lower tier that would invent
            # or fuzzy-match an answer instead.
            return SkipResult(
                id=field.id,
                source="structured",
                reason="no education on file to answer a GPA question — never guessed, never sourced elsewhere",
                auto_fill=False,
            )
        # No data at all for this kind -> defer, unchanged from pre-tier-3
        # behaviour (the generic "no deterministic match" skip downstream).
        return None

    idx, explicit = _index_for(field)
    kind_label = _kind_label(kind)

    if idx < 0 or idx >= len(records):
        return SkipResult(
            id=field.id,
            source="structured",
            reason=f"no {_ordinal(idx + 1)} position in your {kind_label} — never guessed, never wrapped",
            auto_fill=False,
        )

    record = records[idx]
    base_key = f"{'work_history' if kind == 'work' else 'education'}[{idx}].{slot}"

    if slot == "current":
        value = "true" if record.get("current") else "false"
        return FillResult(
            id=field.id,
            value=value,
            source="structured",
            profile_key=base_key,
            confidence=1.0 if explicit else 0.9,
            auto_fill=True,
            reason=f"structured match: {kind_label}[{idx + 1}] currently-work-here checkbox",
        )

    if slot == "end" and kind == "work" and record.get("current"):
        # current:true -> the end date is deliberately left blank; the
        # "currently work here" checkbox is what carries that fact.
        return FillResult(
            id=field.id,
            value="",
            source="structured",
            profile_key=base_key,
            confidence=1.0 if explicit else 0.9,
            auto_fill=True,
            reason=f"structured match: {kind_label}[{idx + 1}] is current — end date left blank",
        )

    if slot == "gpa":
        raw = record.get("gpa")
        if raw is None or raw == "":
            return SkipResult(
                id=field.id,
                source="structured",
                reason=f"matched your {_ordinal(idx + 1)} {kind_label} entry but gpa is not set",
                auto_fill=False,
            )
        requested_scale = _requested_gpa_scale(haystack)
        if requested_scale is not None:
            stored_scale = _stored_gpa_scale(str(raw))
            if abs(stored_scale - requested_scale) > 1e-9:
                return SkipResult(
                    id=field.id,
                    source="structured",
                    reason=(
                        f"your stored GPA is on a {stored_scale:g} scale but this form "
                        f"asks for one out of {requested_scale:g} — not converting, fill in manually"
                    ),
                    auto_fill=False,
                )
        return FillResult(
            id=field.id,
            value=str(raw),
            source="structured",
            profile_key=base_key,
            confidence=1.0 if explicit else 0.9,
            auto_fill=True,
            reason=f"structured match: {kind_label}[{idx + 1}].gpa",
        )

    raw = record.get(slot)
    if raw is None or raw == "":
        return SkipResult(
            id=field.id,
            source="structured",
            reason=f"matched your {_ordinal(idx + 1)} {kind_label} entry but {slot} is not set",
            auto_fill=False,
        )

    value = str(raw)
    if slot in ("start", "end"):
        value = _format_date(value, field)
    elif slot == "description":
        # Reused verbatim from the résumé-import pipeline (never a second
        # implementation) so profiles saved before that normalisation
        # existed still fill as bullets, without ever rewriting the profile
        # on disk -- this only transforms the in-memory value being filled.
        value = _normalize_description(value)

    return FillResult(
        id=field.id,
        value=value,
        source="structured",
        profile_key=base_key,
        confidence=1.0 if explicit else 0.9,
        auto_fill=True,
        reason=f"structured match: {kind_label}[{idx + 1}].{slot}",
    )
