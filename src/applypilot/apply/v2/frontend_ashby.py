"""Ashby front-end: BrowserObservation -> FormSchema IR (spec §6.3). PURE
translation (no browser I/O). Mirrors frontend_greenhouse structurally
(invariant 13) but classifies Ashby's widgets from the LIVE probes under
docs/superpowers/probes/ashby_*.json (invariant 12), NOT copied blind from
Greenhouse.

Empirical ground truth (10 probes across 6 tenants: linear, notion, openai,
ramp, vanta, sentry):
  * System fields carry STABLE ids: #_systemfield_name (text), _email (email),
    _resume (file). A leading unlabeled <input type=file> is the resume-autofill
    dropzone. Cover letter is a <input type=file> or a textarea.
  * Custom questions carry UUID-hash / question_<n> ids, role=textbox, and
    control_type text | email | tel (short) or textarea (long).
  * EVERY choice field — EEO gender/race/veteran/disability, work-authorization,
    "how did you hear", years-of-experience, consent, Yes/No — renders as a
    native radio or checkbox group. Each option surfaces TWICE in the
    observation: once as its wrapping <label> (role=radio/checkbox,
    control_type=label) and once as the <input> (control_type=radio/checkbox);
    both classify to the same kind.
  * NO native <select> and NO react-select combobox appeared in ANY probe. Those
    widget kinds are therefore treated as UNSUPPORTED-with-named-terminal
    (WidgetKind 'unknown') so the executor routes them to review rather than the
    parser guessing a fill strategy on a live application. There is likewise no
    react-select phantom inner <input> to suppress (Greenhouse's suppression is
    deliberately NOT ported — the probe shows none).

Options are LAZY at parse (invariant 4)."""
from __future__ import annotations

from applypilot.apply.v2 import ir
# Reuse the Greenhouse helpers that are genuinely ATS-neutral (locator/frame/
# label taxonomy + the checkbox identity-bleed guard). Confirmed module-level.
from applypilot.apply.v2.frontend_greenhouse import (
    _IDENTITY_KEYS,
    _custom_key,
    _frame_path,
    _is_checkbox_like,
    _locator_spec,
    _name_semantic_key,
    _norm,
    _parse_selector,
    promote_full_name,
)

# Grounded in the Ashby probes. Order: most-specific first (mirrors the
# Greenhouse ordering note). Ashby's single "Full Name" / "First and Last Name"
# field is keyed by the shared word-boundary name matcher (_name_semantic_key),
# NOT this table — so first_name/last_name are intentionally absent here (the
# matcher owns all three name keys with full_name precedence; Task 7 addendum).
_SEMANTIC_SYNONYMS: list[tuple[str, tuple[str, ...]]] = [
    ("email", ("email",)),
    ("phone", ("phone", "mobile")),
    ("location", ("current location", "location", "city", "where are you based")),
    ("linkedin", ("linkedin",)),
    ("portfolio", ("portfolio", "website", "personal site")),
    ("resume", ("resume", "cv", "resume/cv")),
    ("work_auth", ("authorized to work", "legally authorized", "work authorization",
                   "eligible to work", "currently authorized")),
    ("sponsorship", ("require sponsorship", "need sponsorship", "sponsorship", "visa")),
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


def _widget_kind(ctrl) -> str:
    """Classify from what the probe ACTUALLY shows. Observed Ashby kinds are
    mapped confidently; any widget kind never seen in the probes (native select,
    react-select combobox, date) falls through to the named 'unknown' terminal
    rather than a guessed 'text' fill — safer on a live application."""
    t = (ctrl.control_type or "").lower()
    role = (ctrl.role or "").lower()
    if t == "file":
        return "file"
    if t == "textarea":
        return "textarea"
    # Radio / checkbox groups: each option is observed twice — as its <label>
    # (role set, control_type='label') and as the <input> (control_type set).
    # Gate on EITHER so both observations collapse to the same kind. Must precede
    # the text check (the label duplicate has no textbox role).
    if t == "radio" or role == "radio":
        return "radio_group"
    if t == "checkbox" or role == "checkbox":
        return "checkbox"
    # Free text: an explicit text-family input type OR the ARIA textbox role
    # (a bare <input> with no type reports control_type='input' but role='textbox').
    if role == "textbox" or t in ("text", "email", "tel", "url", "search", "number", "password"):
        return "text"
    # Unobserved in every Ashby probe (native <select>=combobox/select,
    # react-select, date, ...) -> unsupported, named terminal (invariant 12).
    return "unknown"


def _to_field(ctrl) -> ir.Field:
    label = ctrl.label or ""
    kind = _widget_kind(ctrl)
    # Name matcher FIRST (full_name > first_name > last_name, word-boundary), then
    # the Ashby synonym table for everything else (Task 7 addendum).
    sem = _name_semantic_key(label, label) or _semantic_key(label, label)
    # Checkbox identity-bleed guard (reused verbatim from Greenhouse): an
    # identity/profile key on a checkbox — e.g. "How did you hear ... - LinkedIn"
    # — is sibling-text bleed, not a real binding -> fall back to custom.*.
    if sem in _IDENTITY_KEYS and _is_checkbox_like(ctrl, kind):
        sem = None
    sem = sem or _custom_key(label)
    return ir.Field(
        field_id=ctrl.control_id or ctrl.selector or "",
        frame_path=_frame_path(ctrl),
        label_text=label,
        question_text=label,
        semantic_key=sem,
        widget=ir.Widget(kind=kind),
        options=ir.LAZY,               # invariant 4: never enumerated at parse
        required=bool(ctrl.required),
        char_limit=None,
        locator_spec=_locator_spec(ctrl),
    )


def parse_observation(obs, *, company: str, url: str) -> ir.FormSchema:
    """Ashby is a single-page React form -> one terminal Step (mirrors Greenhouse;
    the executor loop lets a future multi-step Ashby drop in without a rewrite).
    No react-select inner-input suppression: the probes show no combobox and thus
    no phantom typeahead <input> to drop."""
    candidates = [c for c in obs.controls if c.visible and (c.label or c.control_id)]
    # Single-name-input heuristic: a lone "Name" text box with no separate
    # first/last inputs binds full_name (Task 7 addendum).
    fields = promote_full_name([_to_field(c) for c in candidates])
    advance = None
    if obs.submit_buttons:
        b = obs.submit_buttons[0]
        b_id, b_name = _parse_selector(b.selector)
        advance = {
            "role": "button",
            "name": b.label or "Submit Application",
            "label": b.label or "Submit Application",
            "text": b.label or None,
            "elem_id": b_id,
            "name_attr": b_name,
            "selector": b.selector or None,
            "frame_url": b.frame_url or None,
        }
    step = ir.Step(index=0, fields=fields, advance_control=advance, terminal=True)
    return ir.FormSchema(ats="ashby", company=company, url=url, steps=[step])
