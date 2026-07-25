"""GenericFrontend (spec §6.8 degraded tier): best-effort observation->IR when a
concrete front-end fails (DOM churn). Controls with a usable label are keyed
deterministically (reusing the Greenhouse taxonomy); UNLABELED controls are
batched to Operator.label_controls in ONE call. Canary-looking controls are NEVER
sent to the oracle (invariant 7/14) — they keep their observed label and resolve
deterministically downstream. Explicitly counted (ats='generic') and the caller
caps its use per run."""
from __future__ import annotations

from applypilot.apply.v2 import ir
from applypilot.apply.v2.frontend_greenhouse import (
    _custom_key,
    _frame_path,
    _locator_spec,
    _parse_selector,
    _semantic_key,
    _widget_kind,
)


def _has_label(c) -> bool:
    return bool((c.label or "").strip())


def parse_observation(obs, *, company: str, url: str, operator=None) -> ir.FormSchema:
    candidates = [c for c in obs.controls if c.visible and (c.label or c.control_id)]
    # (1) controls we can key deterministically keep their observed label.
    labeled, unlabeled = [], []
    for c in candidates:
        if _has_label(c):
            labeled.append(c)
        else:
            unlabeled.append(c)
    # (2) batch the truly-unlabeled controls to the oracle for a LABEL only.
    #     A control whose observed label already reads canary is treated as
    #     labeled (never sent) — handled above since it has a label.
    oracle_labels = {}
    if unlabeled and operator is not None:
        snapshot = [{"control_id": c.control_id, "selector": c.selector,
                     "widget_hint": c.control_type} for c in unlabeled]
        try:
            oracle_labels = operator.label_controls(snapshot) or {}
        except Exception:                                # noqa: BLE001 — degraded is best-effort
            oracle_labels = {}

    fields = []
    for c in labeled:
        fields.append(_field(c, c.label or ""))
    for c in unlabeled:
        lab = (oracle_labels.get(c.control_id) or {}).get("label") or ""
        fields.append(_field(c, lab))

    advance = None
    if obs.submit_buttons:
        b = obs.submit_buttons[0]
        b_id, b_name = _parse_selector(b.selector)
        advance = {"role": "button", "name": b.label or "Submit", "label": b.label or "Submit",
                   "text": b.label or None, "elem_id": b_id, "name_attr": b_name,
                   "selector": b.selector or None, "frame_url": b.frame_url or None}
    step = ir.Step(index=0, fields=fields, advance_control=advance, terminal=True)
    return ir.FormSchema(ats="generic", company=company, url=url, steps=[step])


def _field(c, label: str) -> ir.Field:
    kind = _widget_kind(c)
    sem = _semantic_key(label, label) or (_custom_key(label) if label else None)
    return ir.Field(field_id=c.control_id or c.selector or "", frame_path=_frame_path(c),
                    label_text=label, question_text=label, semantic_key=sem,
                    widget=ir.Widget(kind=kind), options=ir.LAZY,
                    required=bool(c.required), char_limit=None, locator_spec=_locator_spec(c))
