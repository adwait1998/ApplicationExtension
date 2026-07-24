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

# Free-text IDENTITY/PROFILE keys that describe the applicant themselves. A
# checkbox is NEVER one of these — a first name / email / LinkedIn URL is not a
# boolean. When a checkbox's observation label bleeds concatenated container
# text (a GDPR consent paragraph mentioning 'First name'; a 'How did you hear'
# option reading 'LinkedIn'/'Portfolio'), _semantic_key substring-matches one of
# these and mis-keys the checkbox. Guard: identity key + checkbox kind -> bleed
# -> fall back to custom.*. Deliberately EXCLUDES work_auth/sponsorship/eeo.*,
# which legitimately appear as checkboxes ("I require visa sponsorship").
_IDENTITY_KEYS = frozenset({
    "first_name", "last_name", "email", "phone", "location",
    "linkedin", "portfolio", "resume",
})

_SELECT_PLACEHOLDER_RE = re.compile(r"select[\s.…]*$")


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


def _is_checkbox_like(ctrl, kind: str) -> bool:
    """True when the control represents a checkbox — whether observed as the
    <input type=checkbox> itself (widget kind 'checkbox') OR as its wrapping
    <label>/ARIA node (control_type 'label' but role 'checkbox'). The identity-
    key bleed guard keys off this so both duplicate observations of one checkbox
    are covered."""
    return kind == "checkbox" or (ctrl.role or "").lower() == "checkbox"


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


def _frame_path(ctrl) -> tuple[str, ...]:
    """Frame chain for the field; () means the top document (ir.py Field contract).

    collect_browser_observation stamps frame.url on EVERY control — including the
    main frame, where it is the full page URL (in prod the job URL with a per-job
    token query string). So we MUST gate on frame DEPTH (frame_index), not on the
    truthiness of frame_url: gating on frame_url would give every standard
    single-frame Greenhouse form a non-empty frame_path, violating the '() = top
    document' contract and baking the per-job URL into question_fp (ir.py) so
    recurring custom questions never share a fingerprint across jobs on a board.

    For embedded forms (frame_index > 0, e.g. vanity-domain iframes) we use the
    frame origin+path as a STABLE handle and strip the query string/fragment (the
    per-job token) so identical recurring questions share a question_fp across
    jobs. The raw frame_url is retained in locator_spec for frame resolution."""
    if not ctrl.frame_index or not ctrl.frame_url:
        return ()
    stable = ctrl.frame_url.split("?", 1)[0].split("#", 1)[0]
    return (stable,) if stable else ()


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


def _bbox_contains(outer: dict, inner: dict) -> bool:
    """True when inner's center point sits inside outer's box (small pad for
    rounding). Used to tie a react-select's internal typeahead <input> to the
    combobox wrapper that geometrically contains it. Degenerate/missing boxes
    (zero area) fail closed -> no suppression."""
    if not outer or not inner:
        return False
    try:
        ox, oy = float(outer["x"]), float(outer["y"])
        ow, oh = float(outer["width"]), float(outer["height"])
        ix, iy = float(inner["x"]), float(inner["y"])
        iw, ih = float(inner["width"]), float(inner["height"])
    except (KeyError, TypeError, ValueError):
        return False
    if ow <= 0 or oh <= 0:
        return False
    cx, cy = ix + iw / 2.0, iy + ih / 2.0
    pad = 2.0
    return (ox - pad <= cx <= ox + ow + pad) and (oy - pad <= cy <= oy + oh + pad)


def _is_react_select_inner(ctrl, react_selects: list) -> bool:
    """Conservatively identify react-select's INTERNAL typeahead <input> so it is
    not ingested as a phantom custom.* text field beside the real combobox.

    react-select renders the styled control (classified react_select via
    role=combobox / a select__ selector) AND a bare inner <input> used only for
    typeahead. That inner input surfaces as a text control whose signals are all
    distinctively non-field: it classifies 'text', its selector is the bare tag
    'input' (no id/name/aria-label — a real field always has one) or carries a
    select__ token, and its label is empty or the 'Select…' placeholder. We only
    suppress it when it is geometrically CONTAINED by a sibling react_select in
    the same frame — so the phantom is dropped exactly when its parent combobox
    is already represented, and a lone stray input is never touched."""
    if _widget_kind(ctrl) != "text":
        return False
    sel = (ctrl.selector or "").strip().lower()
    if sel != "input" and "select__" not in sel:
        return False
    lbl = _norm(ctrl.label)
    if lbl and not _SELECT_PLACEHOLDER_RE.fullmatch(lbl):
        return False
    for rs in react_selects:
        if rs is ctrl or rs.frame_index != ctrl.frame_index:
            continue
        if _bbox_contains(rs.bbox, ctrl.bbox):
            return True
    return False


def _to_field(ctrl) -> ir.Field:
    label = ctrl.label or ""
    question = ctrl.label or ""
    kind = _widget_kind(ctrl)
    sem = _semantic_key(label, question)
    # Checkbox label-bleed guard: an identity/profile key on a checkbox is a
    # sibling-text bleed, never a real binding -> fall back to a custom.* key.
    if sem in _IDENTITY_KEYS and _is_checkbox_like(ctrl, kind):
        sem = None
    sem = sem or _custom_key(question)
    # char_limit is ALWAYS None at parse: _OBSERVE_JS / ControlObservation do not
    # capture maxlength, so there is nothing to read here. The oracle length-clamps
    # free-text answers (§6.5) and the textarea DRIVER (Task 6) clamps only when
    # char_limit is explicitly set on a field.
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


def parse_observation(obs, *, company: str, url: str) -> ir.FormSchema:
    """Translate a BrowserObservation into a single-step Greenhouse FormSchema.

    Greenhouse is a one-page form, so exactly one terminal Step is emitted with
    the submit button as its advance_control. The executor (Task 7) still loops
    over steps so multi-step ATSes drop in later without an executor rewrite."""
    candidates = [c for c in obs.controls if c.visible and (c.label or c.control_id)]
    # react-select inner-input suppression: drop the combobox's internal typeahead
    # <input> (a phantom text field) when a sibling react_select geometrically
    # contains it, so exactly one react_select field remains per combobox.
    react_selects = [c for c in candidates if _widget_kind(c) == "react_select"]
    fields = [
        _to_field(c) for c in candidates
        if not _is_react_select_inner(c, react_selects)
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
