# tests/test_v2_cutover_audit.py
import json

from applypilot import reporting as rp


class _Ledger:
    def __init__(self, dangling=0, per_token=None):
        self._d = dangling
        self._t = per_token or {}
    def dangling_count(self):
        return self._d
    def confirmed_count_for_token(self, token, since_iso):
        return self._t.get(token, 0)


def test_audit_clean_true_when_no_dangling_no_dupes_no_canary(tmp_path):
    ledger = _Ledger(dangling=0, per_token={"acme": 1})
    # a flight dir with one clean bundle (email via profile binding)
    fdir = tmp_path / "flight"; fdir.mkdir()
    (fdir / "acme_x.json").write_text(json.dumps({"fields": [
        {"semantic_key": "email", "provenance": "profile.personal.email"},
        {"semantic_key": "work_auth", "provenance": "profile.work_authorization.legally_authorized_to_work"},
    ]}), encoding="utf-8")
    res = rp.v2_audit_clean(ledger=ledger, flight_dir=fdir, tokens=["acme"])
    assert res["go"] is True
    assert res["dangling"] == 0 and res["duplicates"] == 0 and res["canary_violations"] == 0


def test_audit_hold_on_dangling_intent(tmp_path):
    ledger = _Ledger(dangling=2, per_token={})
    res = rp.v2_audit_clean(ledger=ledger, flight_dir=tmp_path / "nope", tokens=[])
    assert res["go"] is False and res["dangling"] == 2


def test_audit_hold_on_identity_double_submit(tmp_path):
    ledger = _Ledger(dangling=0, per_token={"acme": 2})     # >1 confirmed for one token
    res = rp.v2_audit_clean(ledger=ledger, flight_dir=tmp_path / "nope", tokens=["acme"])
    assert res["go"] is False and res["duplicates"] == 1


def test_audit_hold_on_canary_violation(tmp_path):
    ledger = _Ledger(dangling=0, per_token={})
    fdir = tmp_path / "flight"; fdir.mkdir()
    # a canary key COMMITTED from the ORACLE is a hard violation (invariant 7).
    (fdir / "bad.json").write_text(json.dumps({"fields": [
        {"semantic_key": "salary", "provenance": "oracle", "committed": True},
    ]}), encoding="utf-8")
    res = rp.v2_audit_clean(ledger=ledger, flight_dir=fdir, tokens=[])
    assert res["go"] is False and res["canary_violations"] == 1


def test_audit_parked_canary_is_clean(tmp_path):
    # A parked canary ("canary + no data -> park, never guess") is the SAFE
    # invariant-7 outcome, NOT a violation. 4 of 6 canary keys are always parked,
    # so counting parked as a violation would chronically false-HOLD (reviewer).
    ledger = _Ledger(dangling=0, per_token={})
    fdir = tmp_path / "flight"; fdir.mkdir()
    (fdir / "parked.json").write_text(json.dumps({"fields": [
        {"semantic_key": "citizenship", "provenance": "parked", "committed": False},
        {"semantic_key": "salary", "provenance": "parked", "committed": False},
    ]}), encoding="utf-8")
    res = rp.v2_audit_clean(ledger=ledger, flight_dir=fdir, tokens=[])
    assert res["go"] is True and res["canary_violations"] == 0


def test_audit_uncommitted_oracle_canary_is_clean(tmp_path):
    # An oracle-provenance canary that was NOT committed is not a submitted
    # answer -> not a violation (the committed gate is what excludes it).
    ledger = _Ledger(dangling=0, per_token={})
    fdir = tmp_path / "flight"; fdir.mkdir()
    (fdir / "u.json").write_text(json.dumps({"fields": [
        {"semantic_key": "salary", "provenance": "oracle", "committed": False},
    ]}), encoding="utf-8")
    res = rp.v2_audit_clean(ledger=ledger, flight_dir=fdir, tokens=[])
    assert res["go"] is True and res["canary_violations"] == 0


def test_audit_committed_profile_canary_is_clean(tmp_path):
    # A canary COMMITTED from an exact profile path (work_auth/sponsorship) is
    # the intended path -> clean even though it was committed.
    ledger = _Ledger(dangling=0, per_token={})
    fdir = tmp_path / "flight"; fdir.mkdir()
    (fdir / "ok.json").write_text(json.dumps({"fields": [
        {"semantic_key": "work_auth",
         "provenance": "profile.work_authorization.legally_authorized_to_work",
         "committed": True},
    ]}), encoding="utf-8")
    res = rp.v2_audit_clean(ledger=ledger, flight_dir=fdir, tokens=[])
    assert res["go"] is True and res["canary_violations"] == 0


def test_audit_reason_discloses_dup_skip_and_canary_scope(tmp_path):
    # IMPORTANT disclosures must surface in the reason text: empty token set =>
    # duplicate leg did not run; and the canary leg's non-applied-only scope.
    ledger = _Ledger(dangling=0, per_token={})
    res = rp.v2_audit_clean(ledger=ledger, flight_dir=tmp_path / "nope", tokens=[])
    assert "no board_token" in res["reason"]
    assert "non-applied shadow traffic only" in res["reason"]


def test_cutover_gate_consumes_computed_audit():
    # v2_cutover_gate already takes audit_clean; assert the bool flows to the verdict.
    rows = []
    gate = rp.v2_cutover_gate(rows, audit_clean=True)
    assert gate["audit"]["go"] is True
    gate2 = rp.v2_cutover_gate(rows, audit_clean=False)
    assert gate2["audit"]["go"] is False


def test_format_v2_cutover_renders_go_when_audit_true():
    out = rp.format_v2_cutover([], audit_clean=True)
    assert "????" not in out.split("§12.3")[1][:10]     # §12.3 line is not the unknown mark
