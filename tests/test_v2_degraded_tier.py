import json

from applypilot.apply.v2 import operator as op
from applypilot.apply.v2 import frontend_generic as fg
from applypilot.apply.browser_stream import BrowserObservation, ControlObservation


class _FakeClient:
    def __init__(self, reply):
        self._reply = reply
        self.calls = []

    def chat(self, messages, temperature=0.0, max_tokens=4096, **kw):
        self.calls.append(messages)
        return self._reply


def test_label_controls_returns_labels_by_control_id():
    reply = json.dumps({"labels": [
        {"control_id": "c1", "label": "First name", "widget": "text"},
        {"control_id": "c2", "label": "Why us?", "widget": "textarea"},
    ]})
    operator = op.LLMOperator(client=_FakeClient(reply))
    snap = [{"control_id": "c1", "selector": "#a"}, {"control_id": "c2", "selector": "#b"}]
    labels = operator.label_controls(snap)
    assert labels["c1"]["label"] == "First name" and labels["c1"]["widget"] == "text"
    assert labels["c2"]["widget"] == "textarea"


def test_label_controls_invalid_json_returns_empty_not_crash():
    operator = op.LLMOperator(client=_FakeClient("not json"))
    assert operator.label_controls([{"control_id": "c1"}]) == {}


def test_generic_frontend_builds_ir_from_labels():
    obs = BrowserObservation(controls=[
        ControlObservation(control_id="c1", control_type="text", selector="#a", visible=True),
        ControlObservation(control_id="c2", control_type="textarea", selector="#b", visible=True),
    ], submit_buttons=[ControlObservation(label="Submit", selector="button")])

    class _Op:
        def label_controls(self, snapshot):
            return {"c1": {"label": "Email", "widget": "text"},
                    "c2": {"label": "Cover letter", "widget": "textarea"}}
    schema = fg.parse_observation(obs, company="acme", url="u", operator=_Op())
    keys = {f.semantic_key for s in schema.steps for f in s.fields}
    assert "email" in keys                                   # oracle label -> semantic key
    assert schema.ats == "generic"


def test_generic_frontend_never_labels_canary_via_oracle():
    # A control whose EXISTING label is canary-ish is NOT sent to label_controls;
    # generic only labels controls with no usable label, and canary keys still
    # resolve deterministically downstream (invariant 7/14).
    obs = BrowserObservation(controls=[
        ControlObservation(control_id="c1", label="Desired salary", control_type="text",
                           selector="#s", visible=True),
    ])
    sent = {}

    class _Op:
        def label_controls(self, snapshot):
            sent["ids"] = [s["control_id"] for s in snapshot]
            return {}
    fg.parse_observation(obs, company="acme", url="u", operator=_Op())
    assert "c1" not in sent.get("ids", [])                   # canary-labeled control not sent to oracle
