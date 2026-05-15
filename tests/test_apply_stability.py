import json
import sqlite3

import pytest

from applypilot.apply import dashboard, launcher


def _make_jobs_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE jobs (
            url TEXT PRIMARY KEY,
            title TEXT,
            site TEXT,
            application_url TEXT,
            tailored_resume_path TEXT,
            fit_score INTEGER,
            location TEXT,
            full_description TEXT,
            cover_letter_path TEXT,
            apply_status TEXT,
            apply_attempts INTEGER DEFAULT 0,
            apply_error TEXT,
            agent_id TEXT,
            last_attempted_at TEXT,
            applied_at TEXT,
            apply_duration_ms INTEGER,
            apply_task_id TEXT,
            apply_result_json TEXT,
            verification_evidence_json TEXT,
            verification_confidence REAL,
            idempotency_key TEXT,
            submit_attempt_count INTEGER DEFAULT 0,
            checkpoint_json TEXT,
            last_failure_class TEXT
        )
    """)
    conn.execute("""
        INSERT INTO jobs (
            url, title, site, application_url, tailored_resume_path,
            fit_score, location, full_description, cover_letter_path,
            apply_status, apply_attempts
        ) VALUES (
            'https://example.com/job', 'Designer', 'greenhouse',
            'https://boards.greenhouse.io/example/jobs/1',
            NULL, 9, 'Remote', 'Role description', NULL, NULL, 0
        )
    """)
    conn.commit()
    return conn


def _worker_job():
    return {
        "url": "https://example.com/job",
        "title": "Designer",
        "site": "greenhouse",
        "application_url": "https://boards.greenhouse.io/example/jobs/1",
        "tailored_resume_path": None,
        "fit_score": 9,
        "location": "Remote",
        "full_description": "Role description",
        "cover_letter_path": None,
    }


def _lock_job(conn):
    conn.execute(
        "UPDATE jobs SET apply_status = 'in_progress', agent_id = 'worker-0' WHERE url = ?",
        ("https://example.com/job",),
    )
    conn.commit()


def _install_worker_fakes(monkeypatch, conn, result, prefill_status=None):
    calls = {"cleanup": 0, "mark_result": 0, "review_logs": []}
    job = _worker_job()
    prefill_status = prefill_status or {
        "ats": "greenhouse",
        "fields_filled": ["first_name", "last_name", "email", "resume"],
        "duration_ms": 25,
        "error": None,
    }

    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "acquire_job", lambda **kwargs: job)
    monkeypatch.setattr(launcher, "_wait_for_resources", lambda worker_id: None)
    monkeypatch.setattr(launcher, "launch_chrome", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        launcher,
        "cleanup_worker",
        lambda *args, **kwargs: calls.__setitem__("cleanup", calls["cleanup"] + 1),
    )
    monkeypatch.setattr(
        launcher,
        "run_job",
        lambda *args, **kwargs: (result, 123, prefill_status),
    )

    def fail_if_marked(*args, **kwargs):
        calls["mark_result"] += 1
        raise AssertionError("mark_result should not be called")

    monkeypatch.setattr(launcher, "mark_result", fail_if_marked)
    monkeypatch.setattr(
        launcher,
        "write_review_log",
        lambda *args, **kwargs: calls["review_logs"].append((args, kwargs)),
    )
    monkeypatch.setattr(launcher, "add_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "_write_job_runtime_metadata", lambda *args, **kwargs: None)
    launcher._stop_event.clear()
    return calls


def test_apply_result_classification_needs_review():
    assert launcher._classify_apply_result("needs_review:timeout") == (
        "needs_review",
        "timeout",
        False,
    )
    assert launcher._classify_apply_result("failed:no_result_line") == (
        "needs_review",
        "no_result_line",
        False,
    )


def test_apply_result_classification_permanent_and_success():
    assert launcher._classify_apply_result("applied") == ("applied", None, False)
    assert launcher._classify_apply_result("captcha") == ("failed", "captcha", True)
    assert launcher._classify_apply_result("failed:not_eligible_location") == (
        "failed",
        "not_eligible_location",
        True,
    )


def test_acquire_job_seen_guard_skips_same_url(monkeypatch):
    conn = _make_jobs_conn()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "_load_blocked", lambda: (set(), []))
    monkeypatch.setattr("applypilot.config.is_manual_ats", lambda url: False)

    with launcher._run_seen_lock:
        launcher._run_seen_urls.clear()

    first = launcher.acquire_job(min_score=7, worker_id=0)
    second = launcher.acquire_job(min_score=7, worker_id=1)

    assert first is not None
    assert first["url"] == "https://example.com/job"
    assert second is None


def test_dashboard_updates_mark_dirty():
    dashboard.init_worker(99)
    dashboard.wait_for_change(timeout=0)

    dashboard.update_state(99, status="applying")

    assert dashboard.wait_for_change(timeout=0.1) is True


def test_worker_dry_run_releases_lock_without_marking_applied(monkeypatch):
    conn = _make_jobs_conn()
    _lock_job(conn)
    calls = _install_worker_fakes(monkeypatch, conn, result="applied")

    try:
        applied, failed = launcher.worker_loop(worker_id=0, limit=1, dry_run=True)
    finally:
        launcher._stop_event.clear()

    row = conn.execute(
        "SELECT apply_status, apply_attempts, apply_error, agent_id FROM jobs WHERE url = ?",
        ("https://example.com/job",),
    ).fetchone()
    assert (applied, failed) == (0, 0)
    assert dict(row) == {
        "apply_status": None,
        "apply_attempts": 0,
        "apply_error": None,
        "agent_id": None,
    }
    assert calls["mark_result"] == 0
    assert calls["cleanup"] == 1
    assert calls["review_logs"][0][0][1] == "dry_run:applied"


def test_worker_rate_limited_releases_lock_and_pauses_without_failure(monkeypatch):
    conn = _make_jobs_conn()
    _lock_job(conn)
    calls = _install_worker_fakes(
        monkeypatch,
        conn,
        result="rate_limited:claude_usage_limit",
    )

    try:
        applied, failed = launcher.worker_loop(worker_id=0, limit=1, dry_run=False)
        stop_was_set = launcher._stop_event.is_set()
    finally:
        launcher._stop_event.clear()

    row = conn.execute(
        "SELECT apply_status, apply_attempts, apply_error, agent_id FROM jobs WHERE url = ?",
        ("https://example.com/job",),
    ).fetchone()
    assert (applied, failed) == (0, 0)
    assert dict(row) == {
        "apply_status": None,
        "apply_attempts": 0,
        "apply_error": None,
        "agent_id": None,
    }
    assert stop_was_set is True
    assert calls["mark_result"] == 0
    assert calls["cleanup"] == 1
    assert calls["review_logs"][0][0][1] == "paused"
    assert calls["review_logs"][0][1]["error"] == "claude_usage_limit"


def test_review_log_preserves_submit_ready_prefill_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(launcher.config, "ensure_dirs", lambda: tmp_path.mkdir(exist_ok=True))
    prefill_status = {
        "ats": "greenhouse",
        "fields_filled": ["first_name", "last_name", "email", "resume"],
        "duration_ms": 25,
        "error": None,
        "submit_ready": {
            "ready": True,
            "missing": [],
            "submit_enabled": True,
            "error": None,
        },
    }

    launcher.write_review_log(
        _worker_job(),
        "dry_run:applied",
        "sonnet",
        25,
        True,
        worker_id=0,
        prefill_status=prefill_status,
    )

    row = json.loads((tmp_path / "review.jsonl").read_text(encoding="utf-8"))
    assert row["prefill_submit_ready"] == prefill_status["submit_ready"]


def test_structured_result_parsing_precedence():
    output = "\n".join([
        "RESULT:FAILED:no_result_line",
        "APPLYPILOT_RESULT_JSON: {\"status\":\"failed\",\"reason\":\"form_validation_error\",\"failure_class\":\"validation_form_validation_error\",\"checkpoint_stage\":\"submit_attempted\",\"submit_attempted\":true}",
    ])
    payload = launcher._extract_structured_result(output)
    assert payload is not None
    assert payload["status"] == "failed"
    assert launcher._result_from_structured(payload) == "failed:form_validation_error"


def test_applied_result_does_not_trigger_retry(monkeypatch):
    """Regression for the iter-11 duplicate-submission bug.

    When run_job returns 'applied', the worker MUST NOT call run_job again.
    Previously _classify_failure_class('applied', 'applied') fell through to
    'transient_unknown' which passed should_retry's `failure_class.startswith
    (TRANSIENT_FAILURE_PREFIXES)` check — so a successful apply got submitted
    2-3 times to the same employer.
    """
    conn = _make_jobs_conn()
    _lock_job(conn)

    run_job_calls = []

    def counting_run_job(*args, **kwargs):
        run_job_calls.append(kwargs)
        return "applied", 100, {
            "ats": "greenhouse", "fields_filled": ["email"],
            "duration_ms": 25, "error": None,
        }

    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "acquire_job", lambda **kwargs: _worker_job())
    monkeypatch.setattr(launcher, "_wait_for_resources", lambda worker_id: None)
    monkeypatch.setattr(launcher, "launch_chrome", lambda *args, **kwargs: object())
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "run_job", counting_run_job)
    monkeypatch.setattr(launcher, "mark_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "write_review_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "add_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "_write_job_runtime_metadata", lambda *args, **kwargs: None)
    launcher._stop_event.clear()

    try:
        applied, failed = launcher.worker_loop(
            worker_id=0, limit=1, dry_run=False, max_transient_retries=2,
        )
    finally:
        launcher._stop_event.clear()

    assert len(run_job_calls) == 1, (
        f"run_job was called {len(run_job_calls)} times for an 'applied' result — "
        "should be exactly 1. The duplicate-submission bug has regressed."
    )
    assert applied == 1
    assert failed == 0


def test_permanent_failure_does_not_trigger_retry(monkeypatch):
    """Sister regression: 'expired'/'captcha'/'login_issue' / permanent
    failures must not retry either."""
    conn = _make_jobs_conn()
    _lock_job(conn)

    run_job_calls = []
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "acquire_job", lambda **kwargs: _worker_job())
    monkeypatch.setattr(launcher, "_wait_for_resources", lambda worker_id: None)
    monkeypatch.setattr(launcher, "launch_chrome", lambda *args, **kwargs: object())
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        launcher, "run_job",
        lambda *a, **kw: (run_job_calls.append(kw), ("expired", 100, {}))[1],
    )
    monkeypatch.setattr(launcher, "mark_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "write_review_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "add_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "_write_job_runtime_metadata", lambda *args, **kwargs: None)
    launcher._stop_event.clear()

    try:
        launcher.worker_loop(worker_id=0, limit=1, dry_run=False, max_transient_retries=2)
    finally:
        launcher._stop_event.clear()

    assert len(run_job_calls) == 1


def test_transient_failure_does_retry(monkeypatch):
    """The retry mechanism still works for genuine transient failures."""
    conn = _make_jobs_conn()
    _lock_job(conn)

    run_job_calls = []
    # First call timeout, second call applied
    responses = [
        ("needs_review:timeout", 100, {"ats": "greenhouse", "fields_filled": [], "duration_ms": 1, "error": None}),
        ("applied", 100, {"ats": "greenhouse", "fields_filled": [], "duration_ms": 1, "error": None}),
    ]

    def run_job_seq(*args, **kwargs):
        run_job_calls.append(kwargs)
        return responses[min(len(run_job_calls) - 1, len(responses) - 1)]

    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "acquire_job", lambda **kwargs: _worker_job())
    monkeypatch.setattr(launcher, "_wait_for_resources", lambda worker_id: None)
    monkeypatch.setattr(launcher, "launch_chrome", lambda *args, **kwargs: object())
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "run_job", run_job_seq)
    monkeypatch.setattr(launcher, "mark_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "write_review_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "add_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "_write_job_runtime_metadata", lambda *args, **kwargs: None)
    # Override the backoff sleep so the test is fast
    monkeypatch.setattr(launcher._stop_event, "wait", lambda timeout=None: False)
    launcher._stop_event.clear()

    try:
        launcher.worker_loop(worker_id=0, limit=1, dry_run=False, max_transient_retries=2)
    finally:
        launcher._stop_event.clear()

    assert len(run_job_calls) == 2, "transient failure should trigger one retry"


def test_verifier_persistent_confirmation_page_verified():
    """The classic happy path: confirmation text is still on screen."""
    conf, verified = launcher._compute_verification_verdict(
        has_confirmation=True, url_changed=True, submit_gone=True,
        submit_disabled=False, no_validation_errors=True, required_ok=True,
        verify_threshold=0.75,
    )
    assert verified is True
    assert conf >= 0.75


def test_verifier_redirect_after_submit_verified_iter12_regression():
    """Iter-12 regression: Instacart/Stripe show the confirmation page for
    ~2-3s then redirect to the careers homepage. has_confirmation=False (text
    gone), required_ok=False (no form on the redirected page). The url_changed
    + submit_gone + no_validation_errors combo MUST still verify — otherwise
    real successful applies get mis-marked needs_review:unverified_submission
    and (pre-iter-11) re-submitted.
    """
    conf, verified = launcher._compute_verification_verdict(
        has_confirmation=False,   # success text already redirected away
        url_changed=True,         # navigated off the apply form
        submit_gone=True,         # no submit button on the new page
        submit_disabled=False,
        no_validation_errors=True,
        required_ok=False,        # KEY: redirected page has no form to validate
        verify_threshold=0.75,
    )
    assert verified is True, (
        "redirect-after-submit must verify even with required_ok=False — "
        "this is the exact Stripe/Instacart false-negative from iter 11-12"
    )
    assert conf >= 0.75


def test_verifier_still_on_form_with_errors_not_verified():
    """Submission failed: still on the form, submit button present, field
    errors showing. Must NOT verify (no false positives)."""
    conf, verified = launcher._compute_verification_verdict(
        has_confirmation=False, url_changed=False, submit_gone=False,
        submit_disabled=False, no_validation_errors=False, required_ok=False,
        verify_threshold=0.75,
    )
    assert verified is False
    assert conf < 0.75


def test_verifier_url_unchanged_no_confirmation_not_verified():
    """Edge: page didn't navigate and no confirmation text → not verified
    even though submit happens to be gone (e.g. SPA hid it transiently)."""
    conf, verified = launcher._compute_verification_verdict(
        has_confirmation=False, url_changed=False, submit_gone=True,
        submit_disabled=False, no_validation_errors=True, required_ok=False,
        verify_threshold=0.75,
    )
    assert verified is False


def test_failure_class_mapping():
    assert launcher._classify_failure_class("needs_review:timeout", "timeout") == "transient_timeout"
    assert launcher._classify_failure_class("failed:form_validation_error", "form_validation_error") == "validation_form_validation_error"
    assert launcher._classify_failure_class("needs_review:unverified_submission", "unverified_submission") == "verification_unverified_submission"
    assert launcher._classify_failure_class("failed:unsafe_permissions", "unsafe_permissions") == "policy_unsafe_permissions"


def test_idempotency_duplicate_detection(monkeypatch):
    conn = _make_jobs_conn()
    conn.execute(
        "INSERT INTO jobs (url, title, site, fit_score, apply_status, applied_at, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("https://example.com/job-2", "Designer 2", "greenhouse", 9, "applied", "2026-05-09T00:00:00Z", "idem-123"),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    assert launcher._idempotency_already_applied("idem-123", "https://example.com/job") is True
    assert launcher._idempotency_already_applied("idem-999", "https://example.com/job") is False
