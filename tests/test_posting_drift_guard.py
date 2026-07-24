"""Apply-time posting-drift guard + --url writeback keying fix.

Live near-miss (2026-07-24): an operator-approved posting
"Sr. Product Designer, AI/BI" at a wrapper URL that embeds ?gh_jid=<id> was
served a DIFFERENT posting ("Engineering Manager - UI Platform") at apply time
because the Greenhouse id was recycled / the wrapper re-mapped. The whole form
was filled for the wrong job; only a client-side validation error stopped a real
submission under the candidate's name. The freshness pre-check only catches
redirect-to-board expiry, so an embedded-gh_jid wrapper on a LIVE form evades it.

Two fixes, both proven here with SYNTHETIC data only:
  * Bug 1 — a title-drift guard that short-circuits to needs_review:posting_drift
    BEFORE any bulk form fill/submit (pure decision + integration-shaped run_job).
  * Bug 2 — writeback resolves the canonical jobs.url so status lands on the
    correct row even when launched via --url <application_url> (url != app_url).
"""
import pytest

from applypilot import database as db
from applypilot.apply import launcher
from applypilot.apply.browser_stream import BrowserObservation


# ---------------------------------------------------------------------------
# Bug 1a: pure title-similarity decision
# ---------------------------------------------------------------------------

def test_drift_exact_match_proceeds():
    drift, sim = launcher.is_posting_drift(
        "Senior Product Designer", "Senior Product Designer")
    assert drift is False
    assert sim == pytest.approx(1.0)


def test_drift_wrapper_suffix_noise_is_near_match():
    # The live <title> wraps the role in "Job Application for ... at <company>".
    drift, sim = launcher.is_posting_drift(
        "Sr. Product Designer, AI/BI",
        "Job Application for Sr. Product Designer, AI/BI at Databricks",
    )
    assert drift is False
    assert sim >= launcher.POSTING_DRIFT_SIMILARITY_THRESHOLD


def test_drift_prefix_of_other_proceeds():
    # DB title is a strict prefix of the live title (extra location qualifier).
    drift, sim = launcher.is_posting_drift(
        "Product Designer",
        "Product Designer (Remote, US)",
    )
    assert drift is False
    assert sim == pytest.approx(1.0)


def test_drift_clear_mismatch_is_flagged():
    # The exact near-miss: approved designer role vs served manager role.
    drift, sim = launcher.is_posting_drift(
        "Sr. Product Designer, AI/BI",
        "Engineering Manager - UI Platform",
    )
    assert drift is True
    assert sim < launcher.POSTING_DRIFT_SIMILARITY_THRESHOLD


def test_drift_missing_live_title_fails_open():
    # A targeting guard, not a submit gate: an unreadable live title must not
    # short-circuit (drift False) and must be undecidable (sim None).
    for live in ("", None, "   ", "Job Application"):
        drift, sim = launcher.is_posting_drift("Sr. Product Designer, AI/BI", live)
        assert drift is False
        assert sim is None


def test_drift_similarity_is_relative_to_shorter_title():
    # Overlap is measured against the shorter informative token set, so a short
    # DB title fully present in a long live title scores high, not low.
    sim = launcher.posting_title_similarity(
        "UX Designer",
        "UX Designer, Growth - San Francisco or Remote - Full Time")
    assert sim == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Bug 1b: integration-shaped — a drifted title short-circuits run_job BEFORE
# any form fill, returning the new terminal status + logging both titles.
# ---------------------------------------------------------------------------

def _seed_job_row(conn, url, application_url, title):
    conn.execute(
        "INSERT INTO jobs (url, application_url, title, site, fit_score) "
        "VALUES (?,?,?,?,?)",
        (url, application_url, title, "databricks", 8),
    )
    conn.commit()


def test_run_job_short_circuits_on_posting_drift(tmp_path, monkeypatch):
    dbp = tmp_path / "drift.db"
    db.close_connection(dbp)
    conn = db.init_db(dbp)

    URL = "https://databricks.com/company/careers/open-positions/job?gh_jid=8429978002"
    _seed_job_row(conn, URL, URL, "Sr. Product Designer, AI/BI")

    monkeypatch.setattr(launcher, "get_connection", lambda *a, **k: db.get_connection(dbp))
    monkeypatch.setattr(launcher.config, "load_profile",
                        lambda: {"personal": {"city": "San Francisco", "email": "n@x.io"}})
    monkeypatch.setattr(launcher.config, "load_search_config", lambda: {})
    monkeypatch.setattr(launcher.config, "APP_DIR", tmp_path)
    monkeypatch.setattr(launcher, "add_event", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "update_state", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "reset_worker_dir", lambda wid: tmp_path)

    # Prefill loads the page + resolves the URL; return a benign Greenhouse result.
    monkeypatch.setattr(launcher, "prefill_application", lambda **k: {
        "ats": "greenhouse", "fields_filled": [], "error": None,
        "duration_ms": 5, "resolved_url": URL,
    })
    # The live page serves a DIFFERENT (manager) posting.
    monkeypatch.setattr(
        launcher, "_latest_stream_observation",
        lambda *a, **k: BrowserObservation(url=URL, title="Engineering Manager - UI Platform"),
    )

    # Anything past the guard would begin filling/spawning — make it explode so a
    # regression that lets the run continue past the guard fails loudly.
    def _boom(*a, **k):
        raise AssertionError("reached form-fill / LLM spawn past the drift guard")
    monkeypatch.setattr(launcher.config, "find_claude_binary", _boom)
    monkeypatch.setattr(launcher, "_greenhouse_adapter_pass", _boom)
    monkeypatch.setattr(launcher.prompt_mod, "build_prompt", _boom)

    job = {"url": URL, "application_url": URL,
           "title": "Sr. Product Designer, AI/BI", "site": "databricks",
           "location": "Remote"}

    status, ms, prefill = launcher.run_job(
        job, port=0, worker_id=0, dry_run=False,
        browser_stream=object(), identity_id=None, broker=None)

    assert status == "needs_review:posting_drift"
    assert isinstance(ms, int)
    assert job["_run_meta"]["failure_class"] == "expired_posting_drift"

    # Both titles recorded on the row for review (dedicated apply_result_json field).
    row = db.get_connection(dbp).execute(
        "SELECT apply_result_json, last_failure_class FROM jobs WHERE url = ?",
        (URL,),
    ).fetchone()
    assert row["last_failure_class"] == "expired_posting_drift"
    detail = row["apply_result_json"]
    assert "Sr. Product Designer, AI/BI" in detail          # db_title
    assert "Engineering Manager - UI Platform" in detail    # live_title


def test_run_job_missing_live_title_does_not_short_circuit(tmp_path, monkeypatch):
    # Fail-open: an unreadable live title must NOT trip the guard; the run must
    # proceed past it (and here hit our sentinel, proving it moved on).
    dbp = tmp_path / "drift_open.db"
    db.close_connection(dbp)
    conn = db.init_db(dbp)
    URL = "https://boards.greenhouse.io/x/jobs/1"
    _seed_job_row(conn, URL, URL, "Sr. Product Designer, AI/BI")

    monkeypatch.setattr(launcher, "get_connection", lambda *a, **k: db.get_connection(dbp))
    monkeypatch.setattr(launcher.config, "load_profile",
                        lambda: {"personal": {"city": "San Francisco", "email": "n@x.io"}})
    monkeypatch.setattr(launcher.config, "load_search_config", lambda: {})
    monkeypatch.setattr(launcher.config, "APP_DIR", tmp_path)
    monkeypatch.setattr(launcher, "add_event", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "update_state", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "reset_worker_dir", lambda wid: tmp_path)
    monkeypatch.setattr(launcher, "prefill_application", lambda **k: {
        "ats": "greenhouse", "fields_filled": [], "error": None,
        "duration_ms": 5, "resolved_url": URL,
    })
    # No live title -> undecidable -> fail open.
    monkeypatch.setattr(launcher, "_latest_stream_observation",
                        lambda *a, **k: BrowserObservation(url=URL, title=""))

    def _stop(*a, **k):
        raise RuntimeError("proceeded past the guard as expected")
    # build_prompt is the first real work past the guard on this benign path;
    # reaching it proves the guard was skipped (fail open), not tripped.
    monkeypatch.setattr(launcher.prompt_mod, "build_prompt", _stop)

    job = {"url": URL, "application_url": URL,
           "title": "Sr. Product Designer, AI/BI", "site": "x", "location": "Remote"}

    # The guard must be skipped (fail open); the run continues and hits _stop.
    with pytest.raises(RuntimeError, match="past the guard"):
        launcher.run_job(job, port=0, worker_id=0, dry_run=False,
                         browser_stream=object(), identity_id=None, broker=None)
    # Guard did NOT stamp a drift failure class.
    assert job["_run_meta"]["failure_class"] != "expired_posting_drift"


# ---------------------------------------------------------------------------
# Bug 2: --url writeback keying — status lands on the canonical row even when
# launched with the application_url form (aggregator rows: url != application_url).
# ---------------------------------------------------------------------------

LISTING = "https://www.linkedin.com/jobs/view/9999"
APPLY = "https://databricks.com/company/careers/open-positions/job?gh_jid=8429978002"


def test_mark_result_lands_on_canonical_row_via_application_url(tmp_path, monkeypatch):
    dbp = tmp_path / "wb.db"
    db.close_connection(dbp)
    conn = db.init_db(dbp)
    # Aggregator row: canonical key (url) differs from the direct apply link.
    _seed_job_row(conn, LISTING, APPLY, "Sr. Product Designer, AI/BI")
    monkeypatch.setattr(launcher, "get_connection", lambda *a, **k: db.get_connection(dbp))

    # Run launched via --url <application_url> writes back the failure.
    launcher.mark_result(APPLY, "needs_review", error="posting_drift")

    row = db.get_connection(dbp).execute(
        "SELECT apply_status, apply_error FROM jobs WHERE url = ?", (LISTING,)
    ).fetchone()
    assert row["apply_status"] == "needs_review"      # was NULL before the fix
    assert row["apply_error"] == "posting_drift"


def test_mark_job_lands_on_canonical_row_via_application_url(tmp_path, monkeypatch):
    dbp = tmp_path / "wb_mark.db"
    db.close_connection(dbp)
    conn = db.init_db(dbp)
    _seed_job_row(conn, LISTING, APPLY, "Sr. Product Designer, AI/BI")
    monkeypatch.setattr(launcher, "get_connection", lambda *a, **k: db.get_connection(dbp))

    launcher.mark_job(APPLY, "applied")

    row = db.get_connection(dbp).execute(
        "SELECT apply_status, applied_at FROM jobs WHERE url = ?", (LISTING,)
    ).fetchone()
    assert row["apply_status"] == "applied"
    assert row["applied_at"] is not None


def test_writeback_exact_url_still_wins_fast_path(tmp_path, monkeypatch):
    # The auto-apply worker always passes the acquired job['url']; the exact
    # match must remain a no-op fast path (no accidental application_url hop).
    dbp = tmp_path / "wb_exact.db"
    db.close_connection(dbp)
    conn = db.init_db(dbp)
    _seed_job_row(conn, LISTING, APPLY, "Sr. Product Designer, AI/BI")
    monkeypatch.setattr(launcher, "get_connection", lambda *a, **k: db.get_connection(dbp))

    assert launcher._resolve_canonical_url(LISTING) == LISTING
    assert launcher._resolve_canonical_url(APPLY) == LISTING       # app_url -> canonical

    launcher.mark_result(LISTING, "failed", error="boom")
    row = db.get_connection(dbp).execute(
        "SELECT apply_status FROM jobs WHERE url = ?", (LISTING,)
    ).fetchone()
    assert row["apply_status"] == "failed"


def test_writeback_ambiguous_application_url_is_left_unchanged(tmp_path, monkeypatch):
    # Two rows share the same application_url (duplicate aggregator discovery).
    # Resolution is ambiguous -> return input unchanged rather than guess and
    # mismark the wrong row.
    dbp = tmp_path / "wb_ambig.db"
    db.close_connection(dbp)
    conn = db.init_db(dbp)
    _seed_job_row(conn, LISTING, APPLY, "Sr. Product Designer, AI/BI")
    _seed_job_row(conn, "https://www.linkedin.com/jobs/view/8888", APPLY, "Sr. Product Designer")
    monkeypatch.setattr(launcher, "get_connection", lambda *a, **k: db.get_connection(dbp))

    assert launcher._resolve_canonical_url(APPLY) == APPLY   # unresolved -> unchanged
    launcher.mark_result(APPLY, "needs_review", error="x")   # matches no row; no crash
    n = db.get_connection(dbp).execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_status IS NOT NULL"
    ).fetchone()[0]
    assert n == 0
