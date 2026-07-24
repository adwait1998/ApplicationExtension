"""v2 expired-req detection (bucket B) — the empirical fix.

Tonight's live probe: expired Greenhouse req 7985610 soft-302'd its
/jobs/<id> link to the board index (job-boards.greenhouse.io/twilio?error=true).
The v2 engine navigated there, PARSED the board's search filters, filled 0
fields, and returned needs_review:v2_incomplete_required — a bucket-A
(removable) label on a bucket-B (irreducible, the posting is gone) outcome,
distorting the A/B metric the cutover gate reads.

The fix adds a PRE-parse guard in the production dispatch closure
(_make_v2_production_fn): AFTER navigation the landed drive_page.url is checked
against freshness.is_greenhouse_expired_redirect; the unambiguous soft-302
signature short-circuits to `failed:expired` (a provably PRE-submit terminal —
the recorded ledger INTENT is released, run_form_compiler / parse is never
reached, and legacy is NOT re-run for a dead link). worker_loop promotes
failed:expired to a permanent 'expired' and reporting buckets it B.

Everything is pure injection (no Chrome, no network) mirroring
test_v2_dispatch_seam.py's production-default-path harness.
"""
from __future__ import annotations

import pytest

from applypilot import reporting
from applypilot.apply import launcher
from applypilot.apply.v2.orchestrator import FALLBACK_SENTINEL
from applypilot.freshness import is_greenhouse_expired_redirect


_GH_JOB = "https://job-boards.greenhouse.io/twilio/jobs/7985610"
_GH_EXPIRED_LANDING = "https://job-boards.greenhouse.io/twilio?error=true"


# --- shared fakes (mirror test_v2_dispatch_seam.py) -----------------------

class _FakeLedger:
    def __init__(self, open_intent=True):
        self._open = open_intent
        self.failed = []          # (identity_id, reason)
        self.confirmed = []       # (identity_id, confidence)

    def has_open_intent(self, identity_id):
        return self._open

    def confirm(self, identity_id, *, confidence):
        self.confirmed.append((identity_id, confidence))
        self._open = False

    def fail(self, identity_id, *, reason):
        self.failed.append((identity_id, reason))
        self._open = False


class _FakePage:
    """A drive_page stand-in whose .url is the post-navigation landed URL."""
    def __init__(self, url=""):
        self.url = url
        self.listeners = []

    def on(self, event, cb):
        self.listeners.append((event, cb))


def _drive(monkeypatch, *, intended, landed, orch_spy):
    """Run the production default path (run_form_compiler_fn=None -> real
    closure) with the prologue + CDP + orchestrator faked. `intended` is the job
    req link the prologue resolved (dec.apply_url); `landed` is where navigation
    ended up (drive_page.url). Returns (status, ms, prefill, ledger)."""
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")

    page = _FakePage(url=landed)
    monkeypatch.setattr(launcher, "_v2_connect_page",
                        lambda port, apply_url: (object(), object(), page))
    monkeypatch.setattr(launcher, "_close_cdp", lambda pw, browser: None)

    ledger = _FakeLedger(open_intent=True)
    dec = launcher._PrologueDecision(
        False, None, 0, profile={"personal": {}}, apply_url=intended, ledger=ledger)
    monkeypatch.setattr(launcher, "_safety_prologue", lambda job, **k: dec)
    monkeypatch.setattr("applypilot.apply.v2.orchestrator.run_form_compiler", orch_spy)

    status, ms, prefill = launcher._dispatch_apply_v2_aware(
        job={"application_url": intended, "url": intended},
        page=None, conn=object(), company="twilio", operator=object(),
        broker=object(), identity_id="id1", browser_stream=object(), dry_run=False,
        verify_threshold=0.75, worker_id=0, run_started=1.0, port=9222,
        legacy_dispatch_fn=lambda **k: (_ for _ in ()).throw(
            AssertionError("legacy must not run for an expired dead link")))
    return status, ms, prefill, ledger


# ===========================================================================
# (a) expired landing -> failed:expired, parse NOT attempted, intent released
# ===========================================================================

def test_v2_expired_soft302_short_circuits_before_parse(monkeypatch):
    called = {"orch": 0}

    def orch_spy(**kwargs):
        called["orch"] += 1
        raise AssertionError("run_form_compiler must not run on an expired link")

    status, ms, prefill, ledger = _drive(
        monkeypatch, intended=_GH_JOB, landed=_GH_EXPIRED_LANDING, orch_spy=orch_spy)

    assert status == "failed:expired"
    assert prefill is None
    assert called["orch"] == 0                     # parse never attempted
    # PRE-submit terminal: the recorded INTENT is released (never dangles).
    assert ledger.failed == [("id1", "v2_expired")]
    assert ledger.confirmed == []


def test_v2_expired_board_root_without_error_param(monkeypatch):
    # Second signature: the link left /jobs/<id> for the board root, no ?error=.
    def orch_spy(**kwargs):
        raise AssertionError("run_form_compiler must not run on an expired link")

    status, _, prefill, ledger = _drive(
        monkeypatch, intended=_GH_JOB,
        landed="https://job-boards.greenhouse.io/twilio", orch_spy=orch_spy)
    assert status == "failed:expired"
    assert ledger.failed == [("id1", "v2_expired")]


# ===========================================================================
# (b) healthy /jobs/<id> page -> unchanged normal flow
# ===========================================================================

def test_v2_healthy_job_page_runs_normal_flow(monkeypatch):
    called = {"orch": 0}

    def orch_spy(**kwargs):
        called["orch"] += 1
        return "applied", 111, {"tier_used": "v2_greenhouse", "ats": "greenhouse"}

    # landed URL still on /jobs/<id> (no redirect) -> not expired.
    status, ms, prefill, ledger = _drive(
        monkeypatch, intended=_GH_JOB, landed=_GH_JOB, orch_spy=orch_spy)

    assert status == "applied"
    assert called["orch"] == 1                     # normal flow: parse WAS attempted
    assert prefill["tier_used"] == "v2_greenhouse"
    # verified applied confirms the INTENT — the guard did not interfere.
    assert ledger.confirmed == [("id1", 0.75)] and ledger.failed == []


def test_v2_incomplete_form_still_parks_bucket_a(monkeypatch):
    # A live page that genuinely fails the required interlock STILL returns the
    # bucket-A park — the guard must not turn every incomplete form into expired.
    def orch_spy(**kwargs):
        return "needs_review:v2_incomplete_required", 7, {"ats": "greenhouse"}

    status, _, prefill, ledger = _drive(
        monkeypatch, intended=_GH_JOB, landed=_GH_JOB, orch_spy=orch_spy)
    assert status == "needs_review:v2_incomplete_required"


# ===========================================================================
# (c) vanity-domain gh_jid URL -> NOT false-expired
# ===========================================================================

def test_v2_vanity_ghjid_not_false_expired(monkeypatch):
    called = {"orch": 0}

    def orch_spy(**kwargs):
        called["orch"] += 1
        return "applied", 5, {"tier_used": "v2_greenhouse"}

    vanity = "https://careers.example.com/job?gh_jid=123"
    status, _, prefill, ledger = _drive(
        monkeypatch, intended=vanity, landed=vanity, orch_spy=orch_spy)

    assert status == "applied"
    assert called["orch"] == 1                     # vanity embed is live -> normal flow
    assert ledger.failed == []


# ===========================================================================
# Pure signature helper — the conservative rule
# ===========================================================================

@pytest.mark.parametrize(
    "intended,landed,expected",
    [
        (_GH_JOB, _GH_EXPIRED_LANDING, True),                       # error=true
        (_GH_JOB, "https://job-boards.greenhouse.io/twilio", True), # left /jobs/
        (_GH_JOB, _GH_JOB, False),                                  # still on /jobs/ -> live
        # vanity gh_jid that did not redirect -> not expired
        ("https://careers.example.com/job?gh_jid=1",
         "https://careers.example.com/job?gh_jid=1", False),
        # non-greenhouse intent -> out of scope
        ("https://careers.example.com/apply/1",
         "https://careers.example.com/home", False),
        # error=true but NON-greenhouse landing host -> not trusted
        ("https://careers.example.com/job?gh_jid=1",
         "https://evil.example.com/x?error=true", False),
        (_GH_JOB, None, False),                                     # no landed URL
    ],
)
def test_signature_helper_is_conservative(intended, landed, expected):
    assert is_greenhouse_expired_redirect(intended, landed) is expected


# ===========================================================================
# Bucket-B proof — an expired v2 row lands in B (irreducible), not A
# ===========================================================================

def test_failed_expired_is_permanent_and_terminal():
    # worker_loop treats failed:expired as a PERMANENT failure (no retry, no
    # legacy re-run) exactly like the legacy 'expired' status.
    assert launcher._is_permanent_failure("failed:expired") is True
    db_status, reason, permanent = launcher._classify_apply_result("failed:expired")
    assert (db_status, reason, permanent) == ("failed", "expired", True)


def test_expired_v2_row_buckets_B_through_reporting():
    # Prove the FULL chain: the v2 return status -> worker_loop classification ->
    # the review-log row shape -> reporting.classify_failure_bucket == 'B'.
    status = "failed:expired"
    db_status, reason, _ = launcher._classify_apply_result(status)
    failure_class = launcher._classify_failure_class(status, reason)
    row = {"status": db_status, "error": reason, "failure_class": failure_class}
    assert reporting.classify_failure_bucket(row) == "B"

    # The raw status alone also buckets B (the 'expired' substring marker).
    assert reporting.classify_failure_bucket({"status": status}) == "B"


def test_old_mislabel_would_have_polluted_bucket_A():
    # Contrast: the OLD outcome for the same expired req was a bucket-A park —
    # exactly the removable-failure pollution the fix removes.
    row = {"status": "needs_review:v2_incomplete_required",
           "failure_class": "verification_v2_incomplete_required"}
    assert reporting.classify_failure_bucket(row) == "A"
