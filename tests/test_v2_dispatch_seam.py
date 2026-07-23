"""Task 10: the v2 dispatch seam (`_dispatch_apply_v2_aware`).

Two layers of coverage, both pure injection (no Chrome, no network):

1. ROUTING (the 6 spec tests) — the wrapper is a pure router: fresh-read flag +
   Greenhouse gate + FALLBACK_SENTINEL fall-open + tier_used labeling. Every
   engine is injected, so the real prologue / ledger / CDP never run.

2. PRODUCTION DEFAULT PATH (the ordering tests) — when `run_form_compiler_fn` is
   left None the wrapper builds the real engine closure, whose single most
   important obligation is: _safety_prologue (all gates + record_intent +
   broker.issue) runs BEFORE the orchestrator, its blocked decisions
   short-circuit without ever invoking v2, and a PRE-SUBMIT sentinel releases
   the recorded INTENT so the legacy re-entry APPLIES instead of parking. These
   monkeypatch the prologue / CDP-connect / orchestrator to assert real ordering.
"""
import os

from applypilot.apply import launcher


# ===========================================================================
# 1. ROUTING — the 6 spec tests
# ===========================================================================

def test_v2_flag_fresh_read(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_V2_ENGINE", raising=False)
    assert launcher._v2_enabled() is False
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    assert launcher._v2_enabled() is True             # fresh-read, no import-time cache
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "off")
    assert launcher._v2_enabled() is False


def test_is_greenhouse_gate():
    assert launcher._is_greenhouse({"application_url": "https://boards.greenhouse.io/acme/jobs/1"})
    assert not launcher._is_greenhouse({"application_url": "https://jobs.lever.co/x/y"})


def test_dispatch_v2_route_when_enabled_and_greenhouse(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    calls = {"v2": 0, "legacy": 0}

    def fake_v2(**kwargs):
        calls["v2"] += 1
        return "applied", 1234, {"tier_used": "v2_greenhouse", "ats": "greenhouse"}

    def fake_legacy(**kwargs):
        calls["legacy"] += 1
        return "applied", 10, {"tier_used": "legacy_llm"}

    status, ms, prefill = launcher._dispatch_apply_v2_aware(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1", "url": "u"},
        page=object(), conn=object(), company="acme", operator=None,
        broker=object(), identity_id="id1", browser_stream=object(), dry_run=False,
        verify_threshold=0.75,
        run_form_compiler_fn=fake_v2, legacy_dispatch_fn=fake_legacy)
    assert status == "applied" and prefill["tier_used"] == "v2_greenhouse"
    assert calls["v2"] == 1 and calls["legacy"] == 0


def test_dispatch_v2_fails_open_to_legacy(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    from applypilot.apply.v2.orchestrator import FALLBACK_SENTINEL
    calls = {"legacy": 0}

    def fake_v2(**kwargs):
        return FALLBACK_SENTINEL, 5, None             # v2 could not parse -> fall open

    def fake_legacy(**kwargs):
        calls["legacy"] += 1
        return "applied", 20, {"tier_used": "legacy_llm"}

    status, ms, prefill = launcher._dispatch_apply_v2_aware(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1", "url": "u"},
        page=object(), conn=object(), company="acme", operator=None,
        broker=object(), identity_id="id1", browser_stream=object(), dry_run=False,
        verify_threshold=0.75,
        run_form_compiler_fn=fake_v2, legacy_dispatch_fn=fake_legacy)
    assert status == "applied" and prefill["tier_used"] == "legacy_llm"
    assert calls["legacy"] == 1                        # legacy ran as counted fallback


def test_dispatch_legacy_when_flag_off(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_V2_ENGINE", raising=False)
    calls = {"v2": 0, "legacy": 0}
    launcher._dispatch_apply_v2_aware(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1", "url": "u"},
        page=object(), conn=object(), company="acme", operator=None,
        broker=object(), identity_id="id1", browser_stream=object(), dry_run=False,
        verify_threshold=0.75,
        run_form_compiler_fn=lambda **k: (calls.__setitem__("v2", 1), ("applied", 1, {}))[1],
        legacy_dispatch_fn=lambda **k: (calls.__setitem__("legacy", 1), ("applied", 1, {"tier_used": "legacy_llm"}))[1])
    assert calls["v2"] == 0 and calls["legacy"] == 1   # flag off -> legacy only


def test_dispatch_legacy_when_not_greenhouse(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    calls = {"v2": 0, "legacy": 0}
    launcher._dispatch_apply_v2_aware(
        job={"application_url": "https://jobs.lever.co/x/y", "url": "u"},
        page=object(), conn=object(), company="x", operator=None,
        broker=object(), identity_id="id1", browser_stream=object(), dry_run=False,
        verify_threshold=0.75,
        run_form_compiler_fn=lambda **k: (calls.__setitem__("v2", 1), ("applied", 1, {}))[1],
        legacy_dispatch_fn=lambda **k: (calls.__setitem__("legacy", 1), ("applied", 1, {"tier_used": "legacy_llm"}))[1])
    assert calls["v2"] == 0 and calls["legacy"] == 1   # Lever is Phase 4 -> legacy


def test_v2_greenhouse_vanity_ghjid_gate():
    # _is_greenhouse reuses prefill._detect_ats, so a vanity careers host with the
    # canonical ?gh_jid=<id> param routes to v2 just like a boards.greenhouse.io URL.
    assert launcher._is_greenhouse(
        {"application_url": "https://careers.acme.com/job?gh_jid=42"})
    # No application_url -> fall back to url; unsupported host stays legacy.
    assert not launcher._is_greenhouse({"url": "https://example.com/apply"})


def test_v2_tier_used_setdefault_does_not_clobber(monkeypatch):
    # The wrapper labels v2 results but must NOT overwrite a tier the engine
    # already set (setdefault, not assignment) — the A/B tier is authoritative.
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")

    def fake_v2(**kwargs):
        return "needs_review:v2_incomplete_required", 7, {"ats": "greenhouse"}

    status, ms, prefill = launcher._dispatch_apply_v2_aware(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1", "url": "u"},
        page=object(), conn=object(), company="acme", operator=None,
        broker=object(), identity_id="id1", browser_stream=object(), dry_run=False,
        verify_threshold=0.75,
        run_form_compiler_fn=fake_v2, legacy_dispatch_fn=lambda **k: (None, 0, None))
    # A non-sentinel v2 status is returned verbatim and gets the v2 label.
    assert status == "needs_review:v2_incomplete_required"
    assert prefill["tier_used"] == "v2_greenhouse"


# ===========================================================================
# 2. PRODUCTION DEFAULT PATH — INTENT-before-orchestrator ordering
# ===========================================================================

class _FakeLedger:
    def __init__(self, open_intent=True):
        self._open = open_intent
        self.failed = []                               # (identity_id, reason) tuples
        self.confirmed = []                            # (identity_id, confidence) tuples

    def has_open_intent(self, identity_id):
        return self._open

    def confirm(self, identity_id, *, confidence):
        self.confirmed.append((identity_id, confidence))
        self._open = False

    def fail(self, identity_id, *, reason):
        self.failed.append((identity_id, reason))
        self._open = False


class _FakePage:
    def __init__(self):
        self.listeners = []
        self.url = ""

    def on(self, event, cb):
        self.listeners.append((event, cb))


def _patch_cdp(monkeypatch, page, order=None):
    """Replace the CDP connect so no Chrome is touched. Appends 'cdp_connect' to
    the shared `order` list (when given) so tests can assert prologue->connect->
    orchestrator sequencing."""
    def fake_connect(port, apply_url):
        if order is not None:
            order.append("cdp_connect")
        return object(), object(), page             # (pw, browser, page)

    monkeypatch.setattr(launcher, "_v2_connect_page", fake_connect)
    monkeypatch.setattr(launcher, "_close_cdp", lambda pw, browser: None)


def test_production_fn_runs_prologue_before_orchestrator(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    order = []
    page = _FakePage()
    _patch_cdp(monkeypatch, page, order)

    ledger = _FakeLedger(open_intent=True)
    dec = launcher._PrologueDecision(
        False, None, 0, profile={"personal": {}},
        apply_url="https://boards.greenhouse.io/acme/jobs/1", ledger=ledger)

    def fake_prologue(job, **kwargs):
        order.append("prologue")
        return dec

    seen = {}

    def fake_orch(**kwargs):
        order.append("orchestrator")
        seen.update(kwargs)
        return "applied", 111, {"tier_used": "v2_greenhouse", "ats": "greenhouse"}

    monkeypatch.setattr(launcher, "_safety_prologue", fake_prologue)
    monkeypatch.setattr("applypilot.apply.v2.orchestrator.run_form_compiler", fake_orch)

    status, ms, prefill = launcher._dispatch_apply_v2_aware(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1", "url": "u"},
        page=None, conn=object(), company="acme", operator=object(),
        broker=object(), identity_id="id1", browser_stream=object(), dry_run=False,
        verify_threshold=0.75, worker_id=0, run_started=1.0, port=9222,
        legacy_dispatch_fn=lambda **k: (_ for _ in ()).throw(AssertionError("legacy must not run")))

    assert status == "applied" and prefill["tier_used"] == "v2_greenhouse"
    # Ordering obligation: the ledger INTENT is recorded (prologue) BEFORE the
    # orchestrator can fire a submit, and the CDP page is opened between them.
    assert order == ["prologue", "cdp_connect", "orchestrator"]
    # The orchestrator drives the CDP page we opened + the prologue's profile
    # (never a second config.load_profile()).
    assert seen["page"] is page
    assert seen["profile"] is dec.profile
    # The passive network-evidence listener is attached on THAT page (invariant 8).
    assert page.listeners and page.listeners[0][0] == "response"
    # And the recorded INTENT is CONFIRMED on the verified 'applied' result — the
    # confirm at verify_threshold's floor is what keeps the company-cooldown +
    # has_confirmed safety gates (which count only state='confirmed') alive for v2.
    assert ledger.confirmed == [("id1", 0.75)] and ledger.failed == []


def test_production_fn_forwards_resume_path_to_orchestrator(monkeypatch):
    # The blocker fix: the production closure threads the prologue-resolved resume
    # path (dec.resume_path) into run_form_compiler so 'resume' file fields become
    # a deterministic file PlannedField instead of parking every live apply.
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    page = _FakePage()
    _patch_cdp(monkeypatch, page)

    ledger = _FakeLedger(open_intent=True)
    dec = launcher._PrologueDecision(
        False, None, 0, profile={"personal": {}},
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
        resume_path=r"C:\x\resume.pdf", ledger=ledger)
    monkeypatch.setattr(launcher, "_safety_prologue", lambda job, **k: dec)

    seen = {}

    def fake_orch(**kwargs):
        seen.update(kwargs)
        return "applied", 1, {"tier_used": "v2_greenhouse"}

    monkeypatch.setattr("applypilot.apply.v2.orchestrator.run_form_compiler", fake_orch)

    status, ms, prefill = launcher._dispatch_apply_v2_aware(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1", "url": "u"},
        page=None, conn=object(), company="acme", operator=object(),
        broker=object(), identity_id="id1", browser_stream=object(), dry_run=False,
        verify_threshold=0.75, worker_id=0, run_started=1.0, port=9222,
        legacy_dispatch_fn=lambda **k: (_ for _ in ()).throw(AssertionError("legacy must not run")))

    assert status == "applied"
    assert seen["resume_path"] == dec.resume_path        # forwarded verbatim


def test_production_fn_blocked_prologue_short_circuits(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    opened = {"cdp": 0, "orch": 0, "legacy": 0}

    def fake_connect(port, apply_url):
        opened["cdp"] += 1
        return object(), object(), _FakePage()

    monkeypatch.setattr(launcher, "_v2_connect_page", fake_connect)
    monkeypatch.setattr(launcher, "_close_cdp", lambda pw, browser: None)

    dec = launcher._PrologueDecision(
        True, "needs_review:dangling_submission_intent", 42)
    monkeypatch.setattr(launcher, "_safety_prologue", lambda job, **k: dec)

    def fake_orch(**kwargs):
        opened["orch"] += 1
        return "applied", 1, {}

    monkeypatch.setattr("applypilot.apply.v2.orchestrator.run_form_compiler", fake_orch)

    def fake_legacy(**kwargs):
        opened["legacy"] += 1
        return "applied", 1, {"tier_used": "legacy_llm"}

    status, ms, prefill = launcher._dispatch_apply_v2_aware(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1", "url": "u"},
        page=None, conn=object(), company="acme", operator=object(),
        broker=object(), identity_id="id1", browser_stream=object(), dry_run=False,
        verify_threshold=0.75, worker_id=0, run_started=1.0, port=9222,
        legacy_dispatch_fn=fake_legacy)

    # A blocked gate is the exact short-circuit both engines return verbatim: no
    # CDP, no orchestrator, and NO legacy re-entry (the block already decided).
    assert status == "needs_review:dangling_submission_intent" and ms == 42 and prefill is None
    assert opened == {"cdp": 0, "orch": 0, "legacy": 0}


def test_production_fn_presubmit_sentinel_releases_intent_then_legacy(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    _patch_cdp(monkeypatch, _FakePage())

    ledger = _FakeLedger(open_intent=True)
    dec = launcher._PrologueDecision(
        False, None, 0, profile={"personal": {}},
        apply_url="https://boards.greenhouse.io/acme/jobs/1", ledger=ledger)
    monkeypatch.setattr(launcher, "_safety_prologue", lambda job, **k: dec)

    from applypilot.apply.v2.orchestrator import FALLBACK_SENTINEL
    monkeypatch.setattr("applypilot.apply.v2.orchestrator.run_form_compiler",
                        lambda **k: (FALLBACK_SENTINEL, 5, None))

    legacy_ran = {"n": 0}

    def fake_legacy(**kwargs):
        legacy_ran["n"] += 1
        return "applied", 20, {"tier_used": "legacy_llm"}

    status, ms, prefill = launcher._dispatch_apply_v2_aware(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1", "url": "u"},
        page=None, conn=object(), company="acme", operator=object(),
        broker=object(), identity_id="id1", browser_stream=object(), dry_run=False,
        verify_threshold=0.75, worker_id=0, run_started=1.0, port=9222,
        legacy_dispatch_fn=fake_legacy)

    # PRE-SUBMIT sentinel: the recorded INTENT is released (intent->failed) so the
    # legacy re-entry APPLIES as a normal counted apply instead of parking on the
    # dangling-INTENT guard; then legacy runs and its result is returned.
    assert ledger.failed == [("id1", "v2_presubmit_fallback")]
    assert legacy_ran["n"] == 1
    assert status == "applied" and prefill["tier_used"] == "legacy_llm"


def test_production_fn_dry_run_does_not_release_intent(monkeypatch):
    # In dry-run the prologue records NO intent (nothing to release); the release
    # must be a no-op and never call ledger.fail on a dry-run fallback.
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    _patch_cdp(monkeypatch, _FakePage())

    ledger = _FakeLedger(open_intent=False)
    dec = launcher._PrologueDecision(
        False, None, 0, profile={"personal": {}},
        apply_url="https://boards.greenhouse.io/acme/jobs/1", ledger=ledger)
    monkeypatch.setattr(launcher, "_safety_prologue", lambda job, **k: dec)

    from applypilot.apply.v2.orchestrator import FALLBACK_SENTINEL
    monkeypatch.setattr("applypilot.apply.v2.orchestrator.run_form_compiler",
                        lambda **k: (FALLBACK_SENTINEL, 5, None))

    status, ms, prefill = launcher._dispatch_apply_v2_aware(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1", "url": "u"},
        page=None, conn=object(), company="acme", operator=object(),
        broker=object(), identity_id="id1", browser_stream=object(), dry_run=True,
        verify_threshold=0.75, worker_id=0, run_started=1.0, port=9222,
        legacy_dispatch_fn=lambda **k: ("needs_review:v2_dry_run", 1, {"tier_used": "legacy_llm"}))

    assert ledger.failed == []                          # dry-run: nothing released


def test_production_fn_cdp_setup_failure_fails_open(monkeypatch):
    # If opening the CDP page raises (Chrome gone), that is PRE-submit: fail open
    # to legacy AND release the recorded INTENT so legacy actually re-applies.
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")

    def boom(port, apply_url):
        raise RuntimeError("chrome gone")

    monkeypatch.setattr(launcher, "_v2_connect_page", boom)
    monkeypatch.setattr(launcher, "_close_cdp", lambda pw, browser: None)

    ledger = _FakeLedger(open_intent=True)
    dec = launcher._PrologueDecision(
        False, None, 0, profile={"personal": {}},
        apply_url="https://boards.greenhouse.io/acme/jobs/1", ledger=ledger)
    monkeypatch.setattr(launcher, "_safety_prologue", lambda job, **k: dec)

    # orchestrator must never be reached when CDP setup fails.
    monkeypatch.setattr("applypilot.apply.v2.orchestrator.run_form_compiler",
                        lambda **k: (_ for _ in ()).throw(AssertionError("orchestrator must not run")))

    legacy_ran = {"n": 0}
    status, ms, prefill = launcher._dispatch_apply_v2_aware(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1", "url": "u"},
        page=None, conn=object(), company="acme", operator=object(),
        broker=object(), identity_id="id1", browser_stream=object(), dry_run=False,
        verify_threshold=0.75, worker_id=0, run_started=1.0, port=9222,
        legacy_dispatch_fn=lambda **k: (legacy_ran.__setitem__("n", 1), ("applied", 9, {"tier_used": "legacy_llm"}))[1])

    assert legacy_ran["n"] == 1 and status == "applied"
    assert ledger.failed == [("id1", "v2_presubmit_fallback")]


def _run_production_with_status(monkeypatch, v2_status, *, dry_run=False):
    """Drive the production default path with a fake prologue (open INTENT) + a
    fake orchestrator that returns `v2_status`, and return the _FakeLedger so the
    caller can assert how the recorded INTENT was reconciled. Legacy must not run
    for a non-sentinel terminal status."""
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    _patch_cdp(monkeypatch, _FakePage())
    ledger = _FakeLedger(open_intent=True)
    dec = launcher._PrologueDecision(
        False, None, 0, profile={"personal": {}},
        apply_url="https://boards.greenhouse.io/acme/jobs/1", ledger=ledger)
    monkeypatch.setattr(launcher, "_safety_prologue", lambda job, **k: dec)
    monkeypatch.setattr("applypilot.apply.v2.orchestrator.run_form_compiler",
                        lambda **k: (v2_status, 7, {"ats": "greenhouse"}))
    status, ms, prefill = launcher._dispatch_apply_v2_aware(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1", "url": "u"},
        page=None, conn=object(), company="acme", operator=object(),
        broker=object(), identity_id="id1", browser_stream=object(), dry_run=dry_run,
        verify_threshold=0.75, worker_id=0, run_started=1.0, port=9222,
        legacy_dispatch_fn=lambda **k: (_ for _ in ()).throw(
            AssertionError("legacy must not run for a non-sentinel terminal status")))
    return ledger, status


def test_production_fn_confirms_ledger_on_applied(monkeypatch):
    # The core critical fix: a verified 'applied' CONFIRMS the recorded INTENT
    # (intent->confirmed) — without it the company-cooldown + has_confirmed gates
    # (which count only state='confirmed') are silently defeated for v2.
    ledger, status = _run_production_with_status(monkeypatch, "applied")
    assert status == "applied"
    assert ledger.confirmed == [("id1", 0.75)] and ledger.failed == []


def test_production_fn_fails_ledger_on_unverified_submission(monkeypatch):
    # A submit fired but was not verified: the INTENT is FAILED (intent->failed),
    # mirroring legacy run_job's _resolve_ledger_intent on an unverified submit.
    ledger, status = _run_production_with_status(
        monkeypatch, "needs_review:unverified_submission")
    assert status == "needs_review:unverified_submission"
    assert ledger.failed == [("id1", "verification_unverified_submission")]
    assert ledger.confirmed == []


def test_production_fn_releases_ledger_on_incomplete_required(monkeypatch):
    # The required-completeness interlock parked the form BEFORE any submit — a
    # PRE-submit terminal park with no legacy re-entry, so the INTENT must be
    # released (intent->failed) here or it dangles forever and blocks next apply.
    ledger, status = _run_production_with_status(
        monkeypatch, "needs_review:v2_incomplete_required")
    assert status == "needs_review:v2_incomplete_required"
    assert ledger.failed == [("id1", "v2_incomplete_required")]
    assert ledger.confirmed == []


def test_production_fn_leaves_intent_dangling_on_post_submit_crash(monkeypatch):
    # A crash AFTER a submit MAY have fired must leave the INTENT DANGLING by
    # design: the next run's dangling-INTENT guard reconciles it rather than
    # risking a double-submit. So neither confirm nor fail is called.
    ledger, status = _run_production_with_status(
        monkeypatch, "needs_review:v2_crashed_post_submit")
    assert status == "needs_review:v2_crashed_post_submit"
    assert ledger.confirmed == [] and ledger.failed == []
    assert ledger.has_open_intent("id1") is True       # still dangling


# ===========================================================================
# 3. SAFETY INVARIANT the pre-submit INTENT release depends on
# ===========================================================================

def test_live_verify_never_unclear_after_click():
    """The pre-submit INTENT release is safe ONLY because the LIVE verify stage
    never returns verified=False AND needs_review=False: were that reachable, the
    orchestrator's post-submit 'totally unclear' sentinel (which returns the SAME
    FALLBACK_SENTINEL after a click) would fire and releasing the INTENT could
    double-submit. Pin that invariant here so a future verify() change that
    breaks it fails loudly against THIS release, not silently in production."""
    from applypilot.apply.v2 import verify as verify_mod

    ev = verify_mod.NetworkEvidence(ats="greenhouse", company="acme")  # not submitted

    # No network evidence, no DOM signals -> never a silent pass.
    r = verify_mod.verify(ev, conn=None, dom_signals=None)
    assert r.verified is False and r.needs_review is True

    # DOM signals present but failing the verdict -> not verified => needs_review.
    dom = verify_mod.DomSignals(has_confirmation=False, url_changed=False,
                                submit_gone=False, submit_disabled=False,
                                no_validation_errors=False, required_ok=False)
    r = verify_mod.verify(ev, conn=None, dom_signals=dom, verify_threshold=0.75)
    assert r.verified is False and r.needs_review is True
