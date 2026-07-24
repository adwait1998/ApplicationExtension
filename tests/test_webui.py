"""Tests for the web dashboard backend (webui/server.py).

All $0: temp SQLite DB + temp review.jsonl, no subprocesses spawned
(the run-launcher safety contract is tested at the arg-builder level).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from applypilot.webui.server import (  # noqa: E402
    ALLOWED_RUN_KINDS,
    _build_run_args,
    create_app,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SCHEMA_COLS = (
    "url TEXT PRIMARY KEY, title TEXT, site TEXT, location TEXT, fit_score INTEGER, "
    "apply_status TEXT, apply_error TEXT, last_failure_class TEXT, discovered_at TEXT, "
    "applied_at TEXT, application_url TEXT, verification_confidence REAL, "
    "apply_attempts INTEGER, skill_used TEXT, score_reasoning TEXT, "
    # Task 12: the "eligible" view/count now flows through queue_policy(), which
    # requires gate_result='eligible' AND automatability='auto'. Fixture rows
    # are gated-eligible+auto so they reflect a real queue-visible row.
    "gate_result TEXT, automatability TEXT, gated_at TEXT, "
    # Operator approve-for-auto override columns (surfaced in the job payload).
    "operator_approved INTEGER DEFAULT 0, approved_at TEXT"
)


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "applypilot.db"
    conn = sqlite3.connect(db)
    conn.execute(f"CREATE TABLE jobs ({_SCHEMA_COLS})")
    jobs = [
        # url, title, site, location, score, status, error, failclass, discovered, applied, app_url
        ("j1", "Product Designer", "figma (greenhouse)", "SF", 9, None, None, None,
         "2026-06-08T00:00:00", None, "https://boards.greenhouse.io/figma/1"),
        ("j2", "Senior Designer", "stripe (greenhouse)", "NYC", 8, "needs_review",
         "unverified", "verification_unverified_submission",
         "2026-05-01T00:00:00", None, "https://boards.greenhouse.io/stripe/2"),
        ("j3", "UX Designer", "chime (greenhouse)", "Remote US", 9, "applied", None, None,
         "2026-05-10T00:00:00", "2026-05-21T00:00:00", "https://boards.greenhouse.io/chime/3"),
        ("j4", "Staff Designer", "linear (ashby)", "Remote", 8, "failed", "captcha",
         "blocker_captcha", "2026-05-15T00:00:00", None, "https://jobs.ashbyhq.com/linear/4"),
    ]
    conn.executemany(
        "INSERT INTO jobs (url,title,site,location,fit_score,apply_status,apply_error,"
        "last_failure_class,discovered_at,applied_at,application_url,"
        "gate_result,automatability,gated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,'eligible','auto','2026-06-01T00:00:00')",
        jobs,
    )
    conn.commit()
    conn.close()

    logs = tmp_path / "logs"
    logs.mkdir()
    attempts = [
        {"ts": "2026-05-21T10:00:00", "site": "chime (greenhouse)", "title": "UX Designer",
         "status": "applied", "cost_usd": 0.9},
        {"ts": "2026-05-21T11:00:00", "site": "linear (ashby)", "title": "Staff Designer",
         "status": "failed", "last_failure_class": "blocker_captcha"},
    ]
    (logs / "review.jsonl").write_text(
        "\n".join(json.dumps(a) for a in attempts), encoding="utf-8"
    )

    app = create_app(db_path=db, app_dir=tmp_path)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Summary / jobs / attempts
# ---------------------------------------------------------------------------


def test_summary_shape(client):
    s = client.get("/api/summary").json()
    assert s["total_jobs"] == 4
    assert s["applied_total"] == 1
    assert s["needs_review"] == 1
    # j1 + j4 are eligible (failed is retryable); j2 needs_review, j3 applied
    assert s["eligible_count"] == 2
    assert sum(s["eligible_freshness"].values()) == 2
    assert s["recent_attempts"]["statuses"]["applied"] == 1
    assert "blocker_captcha" in s["recent_attempts"]["failure_classes"]


def test_jobs_views(client):
    eligible = client.get("/api/jobs?view=eligible").json()
    assert {j["url"] for j in eligible["jobs"]} == {"j1", "j4"}
    triage = client.get("/api/jobs?view=needs_review").json()
    assert [j["url"] for j in triage["jobs"]] == ["j2"]
    applied = client.get("/api/jobs?view=applied").json()
    assert [j["url"] for j in applied["jobs"]] == ["j3"]
    assert client.get("/api/jobs?view=nope").status_code == 400


def test_jobs_search_filter(client):
    out = client.get("/api/jobs?view=all&q=stripe").json()
    assert [j["url"] for j in out["jobs"]] == ["j2"]


def test_attempts_feed_newest_first(client):
    out = client.get("/api/attempts?limit=10").json()
    assert [a["status"] for a in out["attempts"]] == ["failed", "applied"]


# ---------------------------------------------------------------------------
# Job actions
# ---------------------------------------------------------------------------


def test_action_mark_applied(client):
    r = client.post("/api/job/action", json={"url": "j2", "action": "mark_applied"})
    assert r.status_code == 200
    job = client.get("/api/jobs?view=applied").json()["jobs"]
    assert {j["url"] for j in job} == {"j2", "j3"}


def test_action_reset_does_not_touch_applied(client):
    r = client.post("/api/job/action", json={"url": "j3", "action": "reset"})
    assert r.status_code == 404  # guarded — applied rows can't be reset


def test_action_park_and_invalid(client):
    assert client.post("/api/job/action", json={"url": "j1", "action": "park"}).status_code == 200
    assert client.post("/api/job/action", json={"url": "j1", "action": "rm -rf"}).status_code == 400
    assert client.post("/api/job/action", json={"url": "ghost", "action": "park"}).status_code == 404


# ---------------------------------------------------------------------------
# Run launcher safety contract
# ---------------------------------------------------------------------------


def test_run_whitelist_has_no_live_apply():
    assert "apply" not in ALLOWED_RUN_KINDS
    with pytest.raises(ValueError):
        _build_run_args("apply", {})


def test_dryrun_apply_args_always_dry_and_capped():
    args = _build_run_args("dryrun_apply", {"limit": 999})
    assert "--dry-run" in args
    # limit hard-capped at 5
    assert args[args.index("--limit") + 1] == "5"


def test_discover_args(client):
    assert _build_run_args("discover", {"source": "ats_boards"}) == [
        "run", "discover", "enrich", "--source", "ats_boards",
    ]
    assert _build_run_args("prune", {"min_score": 8}) == ["prune-expired", "--min-score", "8"]


def test_run_endpoint_rejects_unknown_kind(client):
    r = client.post("/api/run", json={"kind": "apply"})
    assert r.status_code == 409


def test_run_status_idle(client):
    s = client.get("/api/run/status").json()
    assert s["running"] is False
    assert s["returncode"] is None


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "ApplyPilot" in r.text


# ---------------------------------------------------------------------------
# Operator approve-for-auto override (approve_auto / revoke_auto)
# ---------------------------------------------------------------------------


@pytest.fixture
def approval_client(tmp_path):
    """A DB with one sponsorship-'unknown' row and one 'ineligible' row so the
    approve/revoke actions and the ineligible guard can be exercised."""
    db = tmp_path / "applypilot.db"
    conn = sqlite3.connect(db)
    conn.execute(f"CREATE TABLE jobs ({_SCHEMA_COLS})")
    conn.execute(
        "INSERT INTO jobs (url,title,site,fit_score,gate_result,automatability,"
        "gated_at,discovered_at,application_url,operator_approved) "
        "VALUES ('unk','UX Designer','acme (greenhouse)',9,'unknown','auto',"
        "'2026-07-01T00:00:00','2026-07-20T00:00:00','https://boards.greenhouse.io/acme/1',0)"
    )
    conn.execute(
        "INSERT INTO jobs (url,title,site,fit_score,gate_result,automatability,"
        "gated_at,discovered_at,application_url,operator_approved) "
        "VALUES ('bad','Head of Design','x (linkedin)',9,'ineligible','manual',"
        "'2026-07-01T00:00:00','2026-07-20T00:00:00','https://linkedin.com/jobs/2',0)"
    )
    conn.commit()
    conn.close()
    (tmp_path / "logs").mkdir()
    return TestClient(create_app(db_path=db, app_dir=tmp_path))


def test_approve_auto_promotes_unknown_into_queue(approval_client):
    # Not eligible until approved.
    assert approval_client.get("/api/jobs?view=eligible").json()["count"] == 0
    r = approval_client.post("/api/job/action", json={"url": "unk", "action": "approve_auto"})
    assert r.status_code == 200
    elig = approval_client.get("/api/jobs?view=eligible").json()["jobs"]
    assert {j["url"] for j in elig} == {"unk"}
    assert elig[0]["operator_approved"] == 1
    assert elig[0]["approved_at"] is not None


def test_approve_auto_refuses_ineligible(approval_client):
    # The SQL guard (gate_result='unknown') means an ineligible row matches
    # nothing -> 404 -> never dispatchable. Paramount invariant, UI edition.
    r = approval_client.post("/api/job/action", json={"url": "bad", "action": "approve_auto"})
    assert r.status_code == 404
    assert approval_client.get("/api/jobs?view=eligible").json()["count"] == 0


def test_revoke_auto_clears_approval(approval_client):
    approval_client.post("/api/job/action", json={"url": "unk", "action": "approve_auto"})
    assert approval_client.get("/api/jobs?view=eligible").json()["count"] == 1
    r = approval_client.post("/api/job/action", json={"url": "unk", "action": "revoke_auto"})
    assert r.status_code == 200
    assert approval_client.get("/api/jobs?view=eligible").json()["count"] == 0


def test_job_payload_exposes_approval_fields(client):
    row = client.get("/api/jobs?view=eligible").json()["jobs"][0]
    assert "operator_approved" in row
    assert "gate_result" in row
    assert "approved_at" in row


def test_index_has_approve_button_wired(client):
    html = client.get("/").text
    assert "approve_auto" in html
    assert "revoke_auto" in html
    assert "auto-approved" in html
