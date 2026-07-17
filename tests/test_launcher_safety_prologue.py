"""Regression proof for the _safety_prologue extraction (Task 9).

The pre-apply safety gate (pre-apply gates + record_intent + broker.issue) was
lifted out of run_job into a shared helper so v2's run_form_compiler reuses the
SAME broker/ledger/identity path (invariant 1). These tests prove the extraction
is behavior-preserving: the gate still short-circuits blocked jobs, and the
clean path still issues a ticket whose file path is carried on the decision.

The end-to-end short-circuit semantics are additionally covered by the
underlying-helper suites (test_apply_stability::test_preapply_location_gate_*,
test_submission_ledger, test_submit_broker), which stay green as regression
proof; these tests keep the new helper light.
"""
import time

from applypilot import database as db
from applypilot.apply import launcher
from applypilot.apply.submit_broker import SubmitBroker


def test_safety_prologue_returns_gate_decision_shape(monkeypatch):
    # The extracted helper returns a structured decision, not raw returns, so
    # both run_job and run_form_compiler branch on it identically.
    assert hasattr(launcher, "_safety_prologue")

    monkeypatch.setattr(launcher.config, "load_profile",
                        lambda: {"personal": {"city": "San Jose"}})
    monkeypatch.setattr(launcher.config, "load_search_config", lambda: {})
    # Isolate from the real jobs DB / dashboard — the block being tested is the
    # gate decision, not the persistence side-effects.
    monkeypatch.setattr(launcher, "_write_job_runtime_metadata", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "add_event", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "update_state", lambda *a, **k: None)

    # A location-reject job -> blocked with a 'failed:...' status.
    job = {"url": "https://boards.greenhouse.io/x/jobs/1",
           "application_url": "https://boards.greenhouse.io/x/jobs/1",
           "title": "Product Designer",
           "location": "New York, NY",
           "full_description": "This role is on-site and based in New York, NY."}
    dec = launcher._safety_prologue(
        job, worker_id=0, run_started=time.time(), job_meta={},
        identity_id=None, broker=None, dry_run=False)

    assert dec.blocked is True
    assert dec.status == "failed:not_eligible_location"
    assert isinstance(dec.duration_ms, int)
    # A blocked decision carries no clean-path payload and never raised.
    assert dec.broker_file is None
    assert dec.ledger is None


def test_safety_prologue_carries_broker_file_ticket_path(tmp_path, monkeypatch):
    # Regression guard: broker_file must be derived from the broker's `path`
    # attribute, NOT from broker.issue()'s return (which is None). A non-None
    # broker_file is what keeps fail-closed submit containment armed (invariant 1).
    dbp = tmp_path / "safety.db"
    db.close_connection(dbp)
    db.init_db(dbp)
    monkeypatch.setattr(launcher, "get_connection", lambda *a, **k: db.get_connection(dbp))
    monkeypatch.setattr(launcher.config, "load_profile",
                        lambda: {"personal": {"city": "San Jose"}})
    monkeypatch.setattr(launcher.config, "load_search_config", lambda: {})
    monkeypatch.setattr(launcher, "add_event", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "update_state", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "reset_worker_dir", lambda wid: tmp_path)

    ticket_path = tmp_path / "ticket.json"
    broker = SubmitBroker(ticket_path)               # .path set in __init__, file on issue()

    job = {"url": "https://boards.greenhouse.io/x/jobs/1",
           "application_url": "https://boards.greenhouse.io/x/jobs/1"}
    dec = launcher._safety_prologue(
        job, worker_id=0, run_started=time.time(), job_meta={}, identity_id="id-1",
        broker=broker, dry_run=False)

    assert dec.blocked is False
    assert dec.broker_file is not None               # points at the issued ticket path
    assert dec.broker_file == str(ticket_path)       # derived from .path, not issue()
    assert ticket_path.exists()                      # issue() actually wrote the ticket
    # INTENT recorded before any submit (two-phase ledger, invariant 1).
    assert dec.ledger is not None
    assert dec.ledger.has_open_intent("id-1") is True


def test_safety_prologue_dry_run_skips_intent_and_ticket(tmp_path, monkeypatch):
    # Dry-run must NOT record an INTENT and must NOT issue a ticket — submit is
    # structurally impossible, so stranding an intent/ticket would be wrong.
    dbp = tmp_path / "safety_dry.db"
    db.close_connection(dbp)
    db.init_db(dbp)
    monkeypatch.setattr(launcher, "get_connection", lambda *a, **k: db.get_connection(dbp))
    monkeypatch.setattr(launcher.config, "load_profile",
                        lambda: {"personal": {"city": "San Jose"}})
    monkeypatch.setattr(launcher.config, "load_search_config", lambda: {})
    monkeypatch.setattr(launcher, "add_event", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "update_state", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "reset_worker_dir", lambda wid: tmp_path)

    ticket_path = tmp_path / "ticket.json"
    broker = SubmitBroker(ticket_path, dry_run=True)

    job = {"url": "https://boards.greenhouse.io/x/jobs/1",
           "application_url": "https://boards.greenhouse.io/x/jobs/1"}
    dec = launcher._safety_prologue(
        job, worker_id=0, run_started=time.time(), job_meta={}, identity_id="id-2",
        broker=broker, dry_run=True)

    assert dec.blocked is False
    assert dec.ledger.has_open_intent("id-2") is False   # no INTENT in dry-run
    assert not ticket_path.exists()                      # dry-run never issues a ticket
