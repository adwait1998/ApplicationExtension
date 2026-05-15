"""Regression: the apply freshness gate (acquire_job max_age_hours).

The DB accumulates week-old jobs. The non-destructive fix is a freshness
filter on selection — stale jobs stay in the DB but are never acquired for
apply. Verify: with max_age_hours set, only jobs whose discovered_at is
within the window are returned; None/0 disables the gate (drain everything).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from applypilot.apply import launcher


def _conn_with_jobs():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE jobs (
            url TEXT PRIMARY KEY, title TEXT, site TEXT, application_url TEXT,
            tailored_resume_path TEXT, fit_score INTEGER, location TEXT,
            full_description TEXT, cover_letter_path TEXT, apply_status TEXT,
            apply_attempts INTEGER DEFAULT 0, agent_id TEXT,
            last_attempted_at TEXT, discovered_at TEXT
        )
    """)
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=12)).isoformat()
    old = (now - timedelta(days=7)).isoformat()
    rows = [
        ("https://x.com/fresh", "Senior Product Designer", "freshco", "https://boards.greenhouse.io/freshco/jobs/1", 9, "Remote", "d", fresh),
        ("https://x.com/old",   "Staff Product Designer",  "oldco",   "https://boards.greenhouse.io/oldco/jobs/2",   9, "Remote", "d", old),
    ]
    for url, title, site, app, score, loc, fd, disc in rows:
        conn.execute(
            "INSERT INTO jobs(url,title,site,application_url,fit_score,location,full_description,discovered_at,apply_status,apply_attempts)"
            " VALUES (?,?,?,?,?,?,?,?,NULL,0)",
            (url, title, site, app, score, loc, fd, disc),
        )
    conn.commit()
    return conn


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(launcher, "_load_blocked", lambda: (set(), []))
    monkeypatch.setattr("applypilot.config.is_manual_ats", lambda url: False)
    with launcher._run_seen_lock:
        launcher._run_seen_urls.clear()
    yield
    with launcher._run_seen_lock:
        launcher._run_seen_urls.clear()


def test_freshness_gate_excludes_stale(monkeypatch):
    conn = _conn_with_jobs()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    # 48h window → only the 12h-old job is eligible
    job = launcher.acquire_job(min_score=8, worker_id=0, max_age_hours=48)
    assert job is not None
    assert job["url"] == "https://x.com/fresh"
    # The stale (7-day) job must never be acquired under the gate
    with launcher._run_seen_lock:
        launcher._run_seen_urls.clear()
    conn.execute("UPDATE jobs SET apply_status='applied' WHERE url='https://x.com/fresh'")
    conn.commit()
    assert launcher.acquire_job(min_score=8, worker_id=0, max_age_hours=48) is None


def test_no_gate_drains_everything(monkeypatch):
    conn = _conn_with_jobs()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    # max_age_hours=None → stale job IS eligible (back-compat / drain mode)
    seen = set()
    for _ in range(2):
        j = launcher.acquire_job(min_score=8, worker_id=0, max_age_hours=None)
        if not j:
            break
        seen.add(j["url"])
        conn.execute("UPDATE jobs SET apply_status='applied' WHERE url=?", (j["url"],))
        conn.commit()
    assert "https://x.com/old" in seen and "https://x.com/fresh" in seen


def test_gate_zero_is_disabled(monkeypatch):
    conn = _conn_with_jobs()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    # 0 → treated as disabled (drain mode), same as None
    j = launcher.acquire_job(min_score=8, worker_id=0, max_age_hours=0)
    assert j is not None
