"""Resolver (spec §6.4): FormSchema + profile + answer_cache + mapping_cache ->
FillPlan. Deterministic ladder; canary-first (invariant 7); demote-never-archive
cache; safe-answer policy table (EEO decline, hard refusals). Zero I/O beyond
the DB reads."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from applypilot.apply.healing import ElementSpec
from applypilot.apply.v2 import ir
from applypilot.apply.v2 import mapping_cache as mc

# semantic_key -> profile path (dotted). Canary keys map here EXCLUSIVELY.
_PROFILE_PATHS = {
    "full_name": "personal.full_name",      # single-field name (Lever); bound whole
    "first_name": "personal.full_name",     # split at fill
    "last_name": "personal.full_name",
    "email": "personal.email",
    "phone": "personal.phone",
    "location": "personal.city",
    "linkedin": "personal.linkedin_url",
    "portfolio": "personal.portfolio_url",
    "work_auth": "work_authorization.legally_authorized_to_work",
    "sponsorship": "work_authorization.require_sponsorship",
}

# EEO safe canonical answers (spec §6.4 policy table). Absent-in-profile -> decline.
_EEO_DEFAULTS = {
    "eeo.gender": "eeo_voluntary.gender",
    "eeo.race": "eeo_voluntary.race_ethnicity",
    "eeo.veteran": "eeo_voluntary.veteran_status",
    "eeo.disability": "eeo_voluntary.disability_status",
    "eeo.gender_identity": "eeo_voluntary.gender_identity",
    "eeo.sexual_orientation": "eeo_voluntary.sexual_orientation",
    "eeo.first_generation": "eeo_voluntary.first_generation",
}
_DECLINE = "decline to self-identify"

# Labels that are legal attestations the profile cannot cover -> park, never guess.
_HARD_REFUSAL_MARKERS = ("penalty of perjury", "i attest", "i certify under", "felony")

# A file widget is a COVER LETTER (not the resume target) when its label/question/
# key mentions a cover letter. Greenhouse labels BOTH its dropzones "Attach", so a
# real cover-letter upload is only distinguishable when the label is explicit;
# an ambiguous "Attach" is treated as a resume candidate (the required blocker).
_COVER_LETTER_MARKERS = ("cover letter", "coverletter", "cover_letter")


def _is_cover_letter_file(f) -> bool:
    hay = f"{f.label_text or ''} {f.question_text or ''} {f.semantic_key or ''}".lower()
    return any(m in hay for m in _COVER_LETTER_MARKERS)


def _pick_resume_file_field(schema):
    """Choose the SINGLE file widget that should receive the resume (live gap #2).

    Greenhouse labels its Resume/CV and Cover Letter dropzones identically
    ("Attach"), so both parse as semantic_key='custom.attach' and the old
    key=='resume' rung never claimed the required resume upload. Selection:
      1. an explicit resume-keyed file widget wins (label matched 'resume'/'cv');
      2. else the FIRST non-cover-letter file widget (an 'Attach'/'custom.*'
         dropzone with no resume-ish label still binds the resume — the required
         blocker);
      3. a cover-letter-labeled file widget is NEVER the resume target.
    Returns the chosen Field (object identity), or None if no eligible file
    widget exists. Only ONE file widget is ever chosen, so a form with a resume
    AND a cover-letter dropzone binds the resume once and leaves the cover letter
    to the normal ladder (oracle/park)."""
    file_fields = [f for step in schema.steps for f in step.fields
                   if f.widget.kind == "file"]
    for f in file_fields:
        if f.semantic_key == "resume":
            return f
    for f in file_fields:
        if not _is_cover_letter_file(f):
            return f
    return None


@dataclass
class PlannedField:
    field: ir.Field
    binding: str | None = None            # profile.<path> | answer:<qfp> | policy.<k>
    value: str | None = None              # concrete text for text/file drivers
    option_intent: str | None = None      # intent string for enumerated drivers
    driver: str = "text"                  # widget-driver registry key
    park: bool = False                    # park-don't-guess (required + no safe answer)


@dataclass
class FillPlan:
    planned: list[PlannedField] = field(default_factory=list)
    needs_oracle: list[ir.Field] = field(default_factory=list)


def _dig(profile: dict, dotted: str) -> Any:
    cur: Any = profile
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _yn(v: Any) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v or "").strip().lower()


def build_element_spec(locator_spec: dict) -> ElementSpec:
    """Front-end locator_spec dict -> healing.ElementSpec (fill-time input).

    Consumes the keys frontend_greenhouse._locator_spec emits: label / name /
    role / elem_id / name_attr / selector / frame_url. name_attr is the real DOM
    name attribute the front-end recovers from the CSS selector (distinct from
    the accessible `name`); the raw selector becomes a CSS fallback for healing's
    lowest tier. frame_url is stashed in the fingerprint for frame resolution
    since ElementSpec has no dedicated field for it."""
    selector = locator_spec.get("selector")
    return ElementSpec(
        role=locator_spec.get("role"),
        name=locator_spec.get("name"),
        label=locator_spec.get("label"),
        elem_id=locator_spec.get("elem_id"),
        name_attr=locator_spec.get("name_attr"),
        css_fallbacks=[selector] if selector else [],
        fingerprint={"label_text": locator_spec.get("label"), "tag": "input",
                     "frame_url": locator_spec.get("frame_url")},
    )


def _driver_for(kind: str) -> str:
    return {"text": "text", "textarea": "textarea", "file": "file",
            "react_select": "react_select", "native_select": "native_select",
            "phone_intl": "phone_intl", "typeahead_location": "typeahead_location",
            "radio_group": "radio_group", "checkbox": "checkbox",
            "date": "date"}.get(kind, "text")


def resolve(schema: ir.FormSchema, profile: dict, *, conn=None,
            answer_lookup: Callable[[str], str | None] | None = None,
            answer_cache=None, resume_path: str | None = None) -> FillPlan:
    """Walk the resolver ladder per field -> FillPlan. Deterministic, zero I/O
    beyond the DB reads (mapping_cache lookup) and the injected answer_lookup /
    answer_cache seams. Canary fields are resolved BEFORE any oracle batching so
    they never reach needs_oracle (invariant 7).

    `resume_path` is the authoritative resume path already resolved UPSTREAM by
    launcher._safety_prologue (with fallbacks) — the resolver treats it as pure
    data (no os.path.exists; file existence is the prologue's job) and binds the
    Resume/CV file field to it, so a file widget the Operator cannot answer never
    reaches needs_oracle."""
    plan = FillPlan()
    ats = schema.ats
    # Pick the ONE file widget that binds the resume BEFORE walking the ladder, so
    # the required Resume/CV upload is claimed even when Greenhouse labels its
    # dropzone "Attach" (custom.attach), not "Resume" (live gap #2).
    resume_field = _pick_resume_file_field(schema)
    for step in schema.steps:
        for f in step.fields:
            pf = _resolve_field(f, profile, ats, conn, answer_lookup, answer_cache,
                                resume_path, resume_field)
            if pf is None:
                plan.needs_oracle.append(f)
            else:
                plan.planned.append(pf)
    return plan


def _resolve_field(f, profile, ats, conn, answer_lookup, answer_cache, resume_path=None,
                   resume_field=None):
    key = f.semantic_key
    driver = _driver_for(f.widget.kind)

    # (b) canary: EXACT profile path only, never oracle/bank/fuzzy (invariant 7).
    if ir.is_canary_key(key):
        path = _PROFILE_PATHS.get(key)
        raw = _dig(profile, path) if path else None
        if raw is None:
            return PlannedField(f, park=True, driver=driver)   # canary + no data -> park
        if key in ("work_auth", "sponsorship"):
            return PlannedField(f, binding=f"profile.{path}", option_intent=_yn(raw), driver=driver)
        return PlannedField(f, binding=f"profile.{path}",
                            value=str(raw), option_intent=str(raw).lower(), driver=driver)

    # resume: a file widget the Operator cannot answer -> must NEVER reach the
    # oracle. Bind deterministically to the prologue-resolved path (bindings-not-
    # values provenance: binding names it, value carries the concrete path, same
    # shape as the personal.* rungs); with no path, park-don't-guess. Placed
    # before the mapping-cache / answer-bank / oracle rungs. resume is not canary.
    # GATE ON THE FILE WIDGET (identity, not key): _pick_resume_file_field chose
    # the single file widget that should receive the resume — an explicit
    # resume-keyed file OR the first non-cover-letter file widget (Greenhouse
    # labels its dropzone "Attach" -> custom.attach, so the required resume upload
    # has no resume-ish label). A non-file custom question (text 'Link to your
    # resume', textarea 'gap in your CV') is never chosen, so it falls through the
    # normal ladder rather than getting a filesystem path typed into a screening
    # box; a cover-letter file widget is never chosen either.
    if f.widget.kind == "file" and resume_field is not None and f is resume_field:
        if resume_path:
            return PlannedField(f, binding="profile.resume_path",
                                value=str(resume_path), driver=driver)
        return PlannedField(f, park=True, driver=driver)

    # (a) non-canary semantic_key -> profile path.
    if key in _PROFILE_PATHS:
        path = _PROFILE_PATHS[key]
        raw = _dig(profile, path)
        if raw is None:
            return PlannedField(f, park=True, driver=driver) if f.required else PlannedField(f, driver=driver)
        # first_name/last_name both map to personal.full_name; SPLIT so the
        # PlannedField carries the exact token (test asserts first_name == "Nida",
        # NOT the full "Nida Shah"). Reuse prefill._split_name (never rewrite it).
        if key in ("first_name", "last_name"):
            from applypilot.apply.prefill import _split_name
            first, last = _split_name(str(raw))
            val = first if key == "first_name" else last
            return PlannedField(f, binding=f"profile.{path}", value=val,
                                option_intent=val.lower(), driver=driver)
        val = str(raw)
        return PlannedField(f, binding=f"profile.{path}", value=val,
                            option_intent=val.lower(), driver=driver)

    # EEO: safe canonical decline default (never oracle).
    if key in _EEO_DEFAULTS:
        raw = _dig(profile, _EEO_DEFAULTS[key]) or _DECLINE
        return PlannedField(f, binding=f"policy.{key}", option_intent=str(raw).lower(), driver=driver)

    # hard refusal: an uncovered legal attestation -> park (spec §6.4).
    hay = f"{f.label_text} {f.question_text}".lower()
    if any(m in hay for m in _HARD_REFUSAL_MARKERS):
        return PlannedField(f, park=True, driver=driver)

    # (d) mapping-cache hit per field_fp. Dereference the BINDING against the
    # current profile/answer store (invariant 3) — never treat cached text as
    # the answer.
    if conn is not None:
        fp = ir.field_fp(f)
        cached = mc.get_active(conn, ats, fp)
        if cached is not None:
            binding = cached["binding"]
            drv = cached.get("widget_driver") or driver
            if binding.startswith("profile."):
                raw = _dig(profile, binding[len("profile."):])
                if raw is not None:
                    return PlannedField(f, binding=binding, value=str(raw),
                                        option_intent=str(raw).lower(), driver=drv)
            elif binding.startswith("answer:") and answer_lookup is not None:
                ans = answer_lookup(binding)
                if ans:
                    return PlannedField(f, binding=binding, value=ans, driver=drv)

    # (c) answer-bank hit (NON-canary custom free-text only; bank is §10.3-scrubbed).
    # Consult the bank READ-ONLY via the nearest-neighbor lookup and gate on the
    # cache's own threshold — the exact hit-path AnswerCache.answer() takes. We do
    # NOT call answer_cache.answer() here: on a MISS it fires the real network LLM
    # (_default_llm_fn) and persists the fabricated answer (bank write + _entries
    # mutation), which would bypass the oracle and break the resolver's zero-I/O-
    # beyond-DB-reads contract. A miss must fall through to (e) -> the Operator.
    if answer_cache is not None and f.widget.kind in ("text", "textarea"):
        entry, sim = answer_cache._nearest(f.question_text or f.label_text)
        if entry is not None and sim >= answer_cache.threshold and entry.get("a"):
            return PlannedField(f, binding="answer:bank", value=entry["a"], driver=driver)

    # (e) unresolved -> oracle.
    return None
