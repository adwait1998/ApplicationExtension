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
            answer_cache=None) -> FillPlan:
    """Walk the resolver ladder per field -> FillPlan. Deterministic, zero I/O
    beyond the DB reads (mapping_cache lookup) and the injected answer_lookup /
    answer_cache seams. Canary fields are resolved BEFORE any oracle batching so
    they never reach needs_oracle (invariant 7)."""
    plan = FillPlan()
    ats = schema.ats
    for step in schema.steps:
        for f in step.fields:
            pf = _resolve_field(f, profile, ats, conn, answer_lookup, answer_cache)
            if pf is None:
                plan.needs_oracle.append(f)
            else:
                plan.planned.append(pf)
    return plan


def _resolve_field(f, profile, ats, conn, answer_lookup, answer_cache):
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
    if answer_cache is not None and f.widget.kind in ("text", "textarea"):
        res = answer_cache.answer(f.question_text or f.label_text)
        ans = getattr(res, "answer", None)
        if ans:
            return PlannedField(f, binding="answer:bank", value=ans, driver=driver)

    # (e) unresolved -> oracle.
    return None
