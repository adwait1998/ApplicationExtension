# tests/test_v2_flight_wiring.py
import json

import pytest

from applypilot.apply.v2 import ir
from applypilot.apply.v2 import orchestrator as orch


class _Page:
    """Minimal fake page: content() returns a fixed pre-fill DOM string; a fresh
    object() (as the injected tests use) would raise on .content(), which the
    wiring must swallow (exception-guarded)."""
    def __init__(self, html="<html><body><form>PRE-FILL DOM</form></body></html>"):
        self._html = html
    def content(self):
        return self._html


def _schema():
    f = ir.Field(field_id="e", frame_path=(), label_text="Email", question_text="Email",
                 semantic_key="email", widget=ir.Widget(kind="text"), options=ir.LAZY,
                 required=True)
    return ir.FormSchema(ats="greenhouse", company="acme", url="u",
                         steps=[ir.Step(index=0, fields=[f], terminal=True)])


def _stages(status_report_ready=True):
    from applypilot.apply.v2.resolver import FillPlan, PlannedField
    schema = _schema()
    fld = schema.steps[0].fields[0]

    def parse(page, company, url): return schema
    def resolve(schema, profile, conn):
        return FillPlan(planned=[PlannedField(fld, binding="profile.personal.email",
                                              value="a@b.co", driver="text")])
    def run_oracle(plan, schema, operator): return plan
    def execute(page, schema, plan, conn):
        return orch.ExecStub(ready_to_submit=status_report_ready,
                             committed_keys=["email"])
    def submit(page, schema): return True
    def verify(evidence, conn, dom): return orch.VerifyStub(verified=True, tier=1)
    return orch.Stages(parse=parse, resolve=resolve, run_oracle=run_oracle,
                       execute=execute, submit=submit, verify=verify)


def test_flight_gate_off_writes_no_bundle(tmp_path, monkeypatch):
    monkeypatch.delenv("APPLYPILOT_V2_FLIGHT", raising=False)
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    status, ms, prefill = orch.run_form_compiler(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1"},
        page=_Page(), profile={}, conn=None, company="acme", operator=None,
        stages=_stages())
    assert status == "applied"
    assert not (tmp_path / "flight").exists() or not list((tmp_path / "flight").glob("*.json"))


def test_flight_gate_on_records_nonapplied_bundle(tmp_path, monkeypatch):
    # A non-applied outcome (incomplete required) MUST leave a promotable bundle
    # with pre-fill DOM, per-phase timings, and a field record mapped from plan+report.
    monkeypatch.setenv("APPLYPILOT_V2_FLIGHT", "1")
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    status, ms, prefill = orch.run_form_compiler(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1"},
        page=_Page(), profile={}, conn=None, company="acme", operator=None,
        stages=_stages(status_report_ready=False))
    assert status == "needs_review:v2_incomplete_required"
    bundles = list((tmp_path / "flight").glob("*.json"))
    assert len(bundles) == 1
    data = json.loads(bundles[0].read_text(encoding="utf-8"))
    assert data["status"] == "needs_review:v2_incomplete_required"
    assert data["dom_html"].startswith("<html")                 # pre-fill DOM captured
    assert data["phases"].get("parse") is not None              # per-phase delta timing
    provs = {f["provenance"] for f in data["fields"]}
    assert "profile.personal.email" in provs                    # mapped from plan.binding
    assert any(f["committed"] for f in data["fields"])          # mapped from report.committed_keys


def test_flight_gate_on_applied_is_not_recorded(tmp_path, monkeypatch):
    # Success is the hot path: non-applied-only policy => no bundle for 'applied'.
    monkeypatch.setenv("APPLYPILOT_V2_FLIGHT", "1")
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    status, ms, prefill = orch.run_form_compiler(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1"},
        page=_Page(), profile={}, conn=None, company="acme", operator=None,
        stages=_stages())
    assert status == "applied"
    assert not list((tmp_path / "flight").glob("*.json"))


def test_flight_recorder_fault_never_changes_outcome(tmp_path, monkeypatch):
    # A recorder that raises on commit must NOT change the apply status.
    monkeypatch.setenv("APPLYPILOT_V2_FLIGHT", "1")
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)

    class _BoomRecorder:
        def __init__(self, **kw): pass
        def set_schema(self, s): pass
        def record_field(self, **kw): pass
        def record_phase(self, *a): pass
        def set_dom(self, h): pass
        def commit(self, *, status): raise RuntimeError("disk full")
    monkeypatch.setattr(orch, "FlightRecorder", _BoomRecorder)
    status, ms, prefill = orch.run_form_compiler(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1"},
        page=_Page(), profile={}, conn=None, company="acme", operator=None,
        stages=_stages(status_report_ready=False))
    assert status == "needs_review:v2_incomplete_required"      # unchanged despite fault
