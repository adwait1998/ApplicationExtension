"""Lever front-end: BrowserObservation -> FormSchema IR (spec §6.3).

The Lever twin of frontend_greenhouse. PURE translation (no browser I/O of its
own — it consumes an observation the reused browser_stream.collect_browser_
observation produced). Assigns semantic keys from a Lever-tuned synonym table,
classifies widget kinds, seeds locator_spec, and marks every enumerated control
options=LAZY (invariant 4 — dropdowns are NEVER opened at parse).

GROUNDED in the Task-4/6 live probes (docs/superpowers/probes/lever_*.json — 9
observations across 5 tenants: spotify/palantir/aircall/ro/zerohomes). What the
probes show, and how it maps:

  * Native <select> THROUGHOUT (office-location `opportunityLocationId`, a country
    dropdown, `eeo[gender]`/`eeo[race]`, `cards[..][field]` question selects,
    `#disabilitySelectElement`) -> native_select. browser_stream reports a native
    <select> as control_type in the select family AND role="combobox", so — as in
    Greenhouse — the native-select check precedes any combobox handling.
  * NO react-select / JS combobox appears on ANY probe. This front-end therefore
    does NOT classify or suppress a react_select (inventing one would be guessing
    an unobserved widget; the plan forbids it). A role="combobox" that is NOT a
    native <select> maps to 'unknown' -> a NAMED terminal downstream (no driver),
    never a wrong guess.
  * LABEL-BLEED: every checkbox/radio OPTION is a <label> WRAPPING its <input>,
    so collect_browser_observation emits a TWIN (control_type='label',
    role=checkbox/radio, selector 'label') beside each real input control. The
    twins carry no name/value binding and are 1:1 with the real inputs (probe-
    verified: 105 label/checkbox == 105 checkbox, 35 label/radio == 35 radio), so
    they are DROPPED here. Keeping them would spawn a phantom text field per
    option (Greenhouse tolerates that via its identity-bleed guard; Lever, whose
    label-wrap is universal, drops it outright).
  * Single full-name field: Lever renders ONE `input[name="name"]` ("Full name"),
    not first/last -> semantic key 'full_name' (resolver binds personal.full_name
    WHOLE; first_name/last_name would split off only one token).
  * Resume upload is a native `<input type=file>` (#resume-upload-input) -> file.
  * Submit is `#btn-submit` "SUBMIT APPLICATION" -> one terminal step.
  * A closed/404 posting yields 0 controls + 0 submit buttons; parse must NOT
    crash — it emits an empty terminal step (no advance_control), which the
    orchestrator surfaces as a NAMED terminal (v2_incomplete_required), never a
    raise.

Lever uses native selects everywhere, so the react_select driver is never invoked
for Lever and its sleep-tax (drivers.py) does not apply — a speed win for §12.2.
"""
from __future__ import annotations

from applypilot.apply.v2 import ir
# The name keys (full_name/first_name/last_name) are owned by the shared word-
# boundary matcher (_name_semantic_key) with full_name precedence + a single-name-
# input heuristic (promote_full_name) — see frontend_greenhouse (Task 7 addendum).
# The ATS-NEUTRAL helpers (_norm/_custom_key/_parse_selector/_frame_path/
# _locator_spec/_IDENTITY_KEYS) are imported from frontend_greenhouse rather than
# re-authored — they are dialect-independent (Task 8 carryover: kills the
# frontend_lever/frontend_ashby asymmetry). Only the Lever-SPECIFIC classifiers
# (_semantic_key synonym table, _widget_kind native-select map, _is_checkbox_like
# with radio coverage) stay local.
from applypilot.apply.v2.frontend_greenhouse import (
    _IDENTITY_KEYS,
    _custom_key,
    _frame_path,
    _locator_spec,
    _name_semantic_key,
    _norm,
    _parse_selector,
    promote_full_name,
)

# Lever-tuned synonym table (grounded in the probe labels). Order matters: most-
# specific first. The name fields are NOT in this table — _name_semantic_key owns
# them so Lever's single input[name="name"] ("Full name") always binds the WHOLE
# personal.full_name, never a split token.
_SEMANTIC_SYNONYMS: list[tuple[str, tuple[str, ...]]] = [
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

def _semantic_key(label: str, question: str) -> str | None:
    hay = _norm(f"{label} {question}")
    if not hay:
        return None
    for key, needles in _SEMANTIC_SYNONYMS:
        if any(n in hay for n in needles):
            return key
    return None


def _is_checkbox_like(ctrl, kind: str) -> bool:
    """True when the control represents a checkbox/radio — whether observed as the
    <input> itself OR as its wrapping <label>/ARIA node (role 'checkbox'/'radio').
    The identity-key bleed guard keys off this."""
    role = (ctrl.role or "").lower()
    return kind in ("checkbox", "radio_group") or role in ("checkbox", "radio")


def _widget_kind(ctrl) -> str:
    """Map a Lever control to an ir.WidgetKind. Every branch is grounded in an
    observed probe control_type; anything unobserved returns 'unknown' (a named
    terminal downstream) rather than a guessed widget."""
    t = (ctrl.control_type or "").lower()
    role = (ctrl.role or "").lower()
    if t == "file":
        return "file"
    if t == "textarea":
        return "textarea"
    # Native <select> first: browser_stream reports it as control_type in the
    # select family AND role="combobox". Lever is native-select throughout, so
    # this is the ONLY combobox path — there is no react_select branch.
    if t in ("select", "select-one", "select-multiple", "native_select"):
        return "native_select"
    if t == "radio" or role == "radio":
        return "radio_group"
    if t == "checkbox" or role == "checkbox":
        return "checkbox"
    if t == "date":
        return "date"
    # Text-entry: Lever's name/email/phone/org/url fields report control_type in
    # this set (or role="textbox"). Mapping to a text widget is unambiguous HTML
    # input semantics, not a guess.
    if role == "textbox" or t in ("text", "email", "tel", "url", "search", "password", "number"):
        return "text"
    # A widget kind NO Lever probe surfaced (e.g. a JS role="combobox" that is not
    # a native <select>). Do NOT guess a react_select/typeahead mapping.
    return "unknown"


def _to_field(ctrl) -> ir.Field:
    label = ctrl.label or ""
    question = ctrl.label or ""
    kind = _widget_kind(ctrl)
    # Name matcher FIRST (full_name > first_name > last_name, word-boundary), then
    # the Lever synonym table for everything else (Task 7 addendum).
    sem = _name_semantic_key(label, question) or _semantic_key(label, question)
    # Checkbox/radio label-bleed guard: an identity/profile key on a choice
    # control is a sibling-text bleed, never a real binding -> fall back custom.*.
    if sem in _IDENTITY_KEYS and _is_checkbox_like(ctrl, kind):
        sem = None
    sem = sem or _custom_key(question)
    # char_limit is ALWAYS None at parse (the observation does not capture
    # maxlength). The oracle length-clamps free-text answers (§6.5).
    return ir.Field(
        field_id=ctrl.control_id or ctrl.selector or "",
        frame_path=_frame_path(ctrl),
        label_text=label,
        question_text=question,
        semantic_key=sem,
        widget=ir.Widget(kind=kind),
        options=ir.LAZY,
        required=bool(ctrl.required),
        char_limit=None,
        locator_spec=_locator_spec(ctrl),
    )


def _is_label_twin(ctrl) -> bool:
    """A LABEL-BLEED twin: collect_browser_observation emits a <label> wrapping a
    checkbox/radio as its own control (control_type='label'). It is a duplicate
    observation of the option the real <input> already represents (1:1, probe-
    verified), so it is dropped — else every option spawns a phantom text field."""
    return (ctrl.control_type or "").lower() == "label"


def parse_observation(obs, *, company: str, url: str) -> ir.FormSchema:
    """Translate a BrowserObservation into a single-step Lever FormSchema.

    Lever is a one-page server-rendered form, so exactly one terminal Step is
    emitted with the submit button as its advance_control. A closed/expired
    posting (0 controls, 0 submit buttons) yields an empty terminal step with
    advance_control=None — no crash; the orchestrator names the terminal."""
    fields = promote_full_name([
        _to_field(c) for c in obs.controls
        if c.visible and not _is_label_twin(c) and (c.label or c.control_id)
    ])
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
    return ir.FormSchema(ats="lever", company=company, url=url, steps=[step])
