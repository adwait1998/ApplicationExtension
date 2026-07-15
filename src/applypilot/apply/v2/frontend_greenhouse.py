"""Greenhouse front-end: BrowserObservation -> FormSchema IR (spec §6.3).

PURE translation (no browser I/O of its own — it consumes an observation the
reused browser_stream.collect_browser_observation produced). Assigns semantic
keys from the promoted _standard_plan synonym table, classifies widget kinds,
seeds locator_spec, and marks every enumerated control options=LAZY (invariant
4 — dropdowns are NEVER opened at parse)."""
from __future__ import annotations

import re

from applypilot.apply.v2 import ir

# Promoted verbatim from adapters/greenhouse._standard_plan, remapped to the IR
# taxonomy (work_authorization -> work_auth; gender/race/veteran/disability/...
# -> eeo.*). Order matters: most-specific labels first so "...transgender..."
# isn't hijacked by the generic "gender" (mirrors the adapter's ordering note).
_SEMANTIC_SYNONYMS: list[tuple[str, tuple[str, ...]]] = [
    ("first_name", ("legal first name", "first name")),
    ("last_name", ("legal last name", "last name", "surname")),
    ("email", ("email",)),
    ("phone", ("phone", "mobile")),
    ("location", ("current location", "location", "city", "where are you")),
    ("linkedin", ("linkedin",)),
    ("portfolio", ("portfolio", "website", "personal site")),
    ("resume", ("resume", "cv", "resume/cv")),
    ("work_auth", ("authorized to work", "legally authorized", "work authorization",
                   "eligible to work", "currently authorized")),
    ("sponsorship", ("require sponsorship", "need sponsorship", "sponsorship", "visa")),
    ("eeo.sexual_orientation", ("sexual orientation",)),
    ("eeo.gender_identity", ("gender identity", "identify as transgender", "transgender")),
    ("eeo.first_generation", ("first-generation", "first generation")),
    ("eeo.gender", ("gender",)),
    ("eeo.race", ("race/ethnicity", "race", "ethnicity")),
    ("eeo.veteran", ("veteran",)),
    ("eeo.disability", ("disability",)),
]

_CUSTOM_PREFIX = "custom."


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _semantic_key(label: str, question: str) -> str | None:
    hay = _norm(f"{label} {question}")
    if not hay:
        return None
    for key, needles in _SEMANTIC_SYNONYMS:
        if any(n in hay for n in needles):
            return key
    return None


def _custom_key(question: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", _norm(question)).strip("_")[:40] or "unnamed"
    return _CUSTOM_PREFIX + slug


def _widget_kind(ctrl) -> str:
    t = (ctrl.control_type or "").lower()
    role = (ctrl.role or "").lower()
    selector = (ctrl.selector or "").lower()
    if t == "file":
        return "file"
    if t == "textarea":
        return "textarea"
    # Native <select> first: browser_stream reports it as control_type="select"
    # AND role="combobox", so this MUST precede the react_select check below.
    if t in ("select", "select-one", "select-multiple", "native_select"):
        return "native_select"
    if role == "combobox" or "select__" in selector or t == "combobox":
        return "react_select"
    if t == "radio":
        return "radio_group"
    if t == "checkbox":
        return "checkbox"
    if t == "date":
        return "date"
    return "text"


def _parse_selector(selector: str | None) -> tuple[str | None, str | None]:
    """Recover a real DOM id / name attribute from the observation's CSS
    selector. browser_stream.selectorFor emits '#<id>' (id preferred),
    'tag[name="x"]', 'tag[aria-label="x"]', 'label[for="x"]', or a bare tag.
    The synthetic control_id hash is NOT a DOM handle, so we seed the healing
    ElementSpec from the selector instead."""
    sel = selector or ""
    elem_id = None
    name_attr = None
    if sel.startswith("#") and "[" not in sel and " " not in sel:
        elem_id = sel[1:].replace("\\", "") or None
    m = re.search(r'\[name="([^"]+)"\]', sel)
    if m:
        name_attr = m.group(1)
    return elem_id, name_attr


def _locator_spec(ctrl) -> dict:
    """Dict that build_element_spec() (Task 5) turns into a healing.ElementSpec.
    Seeds the label + role + real DOM id/name (recovered from the selector) plus
    the raw selector as a CSS fallback and the frame_url for frame resolution;
    healing decides the winning tier at fill time and writes it back to the
    mapping cache."""
    elem_id, name_attr = _parse_selector(ctrl.selector)
    return {
        "label": ctrl.label or None,
        "name": ctrl.label or None,
        "role": ctrl.role or None,
        "elem_id": elem_id,
        "name_attr": name_attr,
        "selector": ctrl.selector or None,
        "frame_url": ctrl.frame_url or None,
    }


def _to_field(ctrl) -> ir.Field:
    label = ctrl.label or ""
    question = ctrl.label or ""
    sem = _semantic_key(label, question) or _custom_key(question)
    kind = _widget_kind(ctrl)
    # char_limit is ALWAYS None at parse: _OBSERVE_JS / ControlObservation do not
    # capture maxlength, so there is nothing to read here. The oracle length-clamps
    # free-text answers (§6.5) and the textarea DRIVER (Task 6) clamps only when
    # char_limit is explicitly set on a field.
    return ir.Field(
        field_id=ctrl.control_id or ctrl.selector or "",
        frame_path=(ctrl.frame_url,) if ctrl.frame_url else (),
        label_text=label,
        question_text=question,
        semantic_key=sem,
        widget=ir.Widget(kind=kind),
        options=ir.LAZY,
        required=bool(ctrl.required),
        char_limit=None,
        locator_spec=_locator_spec(ctrl),
    )


def parse_observation(obs, *, company: str, url: str) -> ir.FormSchema:
    """Translate a BrowserObservation into a single-step Greenhouse FormSchema.

    Greenhouse is a one-page form, so exactly one terminal Step is emitted with
    the submit button as its advance_control. The executor (Task 7) still loops
    over steps so multi-step ATSes drop in later without an executor rewrite."""
    fields = [
        _to_field(c) for c in obs.controls
        if c.visible and (c.label or c.control_id)
    ]
    advance = None
    if obs.submit_buttons:
        b = obs.submit_buttons[0]
        b_id, b_name = _parse_selector(b.selector)
        advance = {
            "role": "button",
            "name": b.label or "Submit application",
            "label": b.label or "Submit application",
            "text": b.label or None,
            "elem_id": b_id,
            "name_attr": b_name,
            "selector": b.selector or None,
            "frame_url": b.frame_url or None,
        }
    step = ir.Step(index=0, fields=fields, advance_control=advance, terminal=True)
    return ir.FormSchema(ats="greenhouse", company=company, url=url, steps=[step])
