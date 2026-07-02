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
            last_attempted_at TEXT, applied_at TEXT, discovered_at TEXT
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
        launcher._run_seen_sites.clear()
    yield
    with launcher._run_seen_lock:
        launcher._run_seen_urls.clear()
        launcher._run_seen_sites.clear()


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


def _conn_with_hybrid_queue():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE jobs (
            url TEXT PRIMARY KEY, title TEXT, site TEXT, application_url TEXT,
            tailored_resume_path TEXT, fit_score INTEGER, location TEXT,
            full_description TEXT, cover_letter_path TEXT, apply_status TEXT,
            apply_attempts INTEGER DEFAULT 0, agent_id TEXT,
            last_attempted_at TEXT, applied_at TEXT, discovered_at TEXT
        )
    """)
    now = datetime.now(timezone.utc)
    rows = [
        ("https://alpha.test/new", "Senior Product Designer", "alpha", "https://boards.greenhouse.io/alpha/jobs/new", 9, now - timedelta(hours=1)),
        ("https://alpha.test/old", "Staff Product Designer", "alpha", "https://boards.greenhouse.io/alpha/jobs/old", 9, now - timedelta(hours=2)),
        ("https://beta.test/role", "Lead UX Designer", "beta", "https://boards.greenhouse.io/beta/jobs/role", 9, now - timedelta(hours=5)),
        # Newer than every score-9 job, but lower score must not jump the queue.
        ("https://gamma.test/role", "Product Designer", "gamma", "https://boards.greenhouse.io/gamma/jobs/role", 8, now - timedelta(minutes=20)),
    ]
    for url, title, site, app, score, disc in rows:
        conn.execute(
            "INSERT INTO jobs(url,title,site,application_url,fit_score,location,full_description,discovered_at,apply_status,apply_attempts)"
            " VALUES (?,?,?,?,?,'Remote','d',?,NULL,0)",
            (url, title, site, app, score, disc.isoformat()),
        )
    conn.commit()
    return conn


def test_hybrid_queue_prefers_newest_inside_score_and_company_spread(monkeypatch):
    conn = _conn_with_hybrid_queue()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    acquired = []
    for _ in range(3):
        job = launcher.acquire_job(min_score=8, worker_id=0, max_age_hours=24)
        assert job is not None
        acquired.append(job["url"])
        conn.execute("UPDATE jobs SET apply_status='applied' WHERE url=?", (job["url"],))
        conn.commit()

    assert acquired == [
        "https://alpha.test/new",
        "https://beta.test/role",
        "https://alpha.test/old",
    ]


def test_preview_apply_queue_matches_hybrid_order_without_acquiring(monkeypatch):
    conn = _conn_with_hybrid_queue()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    preview = launcher.preview_apply_queue(limit=3, min_score=8, max_age_hours=24)

    assert [job["url"] for job in preview] == [
        "https://alpha.test/new",
        "https://beta.test/role",
        "https://alpha.test/old",
    ]
    assert conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_status = 'in_progress'"
    ).fetchone()[0] == 0


def test_site_contains_filters_preview_and_acquire(monkeypatch):
    conn = _conn_with_hybrid_queue()
    conn.execute(
        "UPDATE jobs SET site = ? WHERE url = ?",
        ("Alpha (TheirStack)", "https://alpha.test/new"),
    )
    conn.execute(
        "UPDATE jobs SET site = ? WHERE url = ?",
        ("Beta (LinkedIn)", "https://beta.test/role"),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    preview = launcher.preview_apply_queue(
        limit=3,
        min_score=8,
        max_age_hours=24,
        site_contains="theirstack",
    )

    assert [job["url"] for job in preview] == ["https://alpha.test/new"]

    job = launcher.acquire_job(
        min_score=8,
        worker_id=0,
        max_age_hours=24,
        site_contains="theirstack",
    )
    assert job is not None
    assert job["url"] == "https://alpha.test/new"
    assert "TheirStack" in job["site"]


def _conn_with_string_null_apply_urls():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE jobs (
            url TEXT PRIMARY KEY, title TEXT, site TEXT, application_url TEXT,
            tailored_resume_path TEXT, fit_score INTEGER, location TEXT,
            full_description TEXT, cover_letter_path TEXT, apply_status TEXT,
            apply_attempts INTEGER DEFAULT 0, apply_error TEXT, agent_id TEXT,
            last_attempted_at TEXT, applied_at TEXT, discovered_at TEXT
        )
    """)
    disc = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    rows = [
        (
            "https://www.linkedin.com/jobs/view/4415704549",
            "Senior Product Designer",
            "linkedin",
            "None",
            9,
        ),
        (
            "https://boards.greenhouse.io/fallback/jobs/1",
            "Staff Product Designer",
            "fallback",
            "None",
            8,
        ),
    ]
    for url, title, site, app, score in rows:
        conn.execute(
            "INSERT INTO jobs(url,title,site,application_url,fit_score,location,full_description,discovered_at,apply_status,apply_attempts)"
            " VALUES (?,?,?,?,?,'Remote','d',?,NULL,0)",
            (url, title, site, app, score, disc),
        )
    conn.commit()
    return conn


def test_string_none_application_url_falls_back_or_skips_manual(monkeypatch):
    conn = _conn_with_string_null_apply_urls()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(
        "applypilot.config.is_manual_ats",
        lambda url: "linkedin.com/jobs/view" in url,
    )

    job = launcher.acquire_job(min_score=8, worker_id=0, max_age_hours=24)

    assert job is not None
    assert job["url"] == "https://boards.greenhouse.io/fallback/jobs/1"
    assert job["application_url"] == "https://boards.greenhouse.io/fallback/jobs/1"
    assert conn.execute(
        "SELECT apply_status FROM jobs WHERE url = 'https://www.linkedin.com/jobs/view/4415704549'"
    ).fetchone()[0] == "manual"


def test_preview_skips_string_none_manual_apply_urls_without_mutating(monkeypatch):
    conn = _conn_with_string_null_apply_urls()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(
        "applypilot.config.is_manual_ats",
        lambda url: "linkedin.com/jobs/view" in url,
    )

    preview = launcher.preview_apply_queue(limit=2, min_score=8, max_age_hours=24)

    assert [job["url"] for job in preview] == ["https://boards.greenhouse.io/fallback/jobs/1"]
    assert conn.execute(
        "SELECT apply_status FROM jobs WHERE url = 'https://www.linkedin.com/jobs/view/4415704549'"
    ).fetchone()[0] is None


def _conn_with_dupes():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE jobs (
            url TEXT PRIMARY KEY, title TEXT, site TEXT, application_url TEXT,
            tailored_resume_path TEXT, fit_score INTEGER, location TEXT,
            full_description TEXT, cover_letter_path TEXT, apply_status TEXT,
            apply_attempts INTEGER DEFAULT 0, agent_id TEXT,
            last_attempted_at TEXT, applied_at TEXT, discovered_at TEXT
        )
    """)
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=6)).isoformat()
    fresher = (now - timedelta(hours=2)).isoformat()
    # Same site + exact title, 3 URLs: an aggregator one + a canonical
    # Greenhouse one + an older aggregator. Dedup must keep exactly one and
    # prefer the canonical-ATS URL.
    rows = [
        ("https://indeed.com/v/aaa", "Senior UX Designer, Alexa", "indeed", "https://indeed.com/apply/aaa", 9, fresh),
        ("https://boards.greenhouse.io/x/jobs/1", "Senior UX Designer, Alexa", "indeed", "https://boards.greenhouse.io/x/jobs/1", 9, fresher),
        ("https://indeed.com/v/bbb", "Senior UX Designer, Alexa", "indeed", "https://indeed.com/apply/bbb", 9, fresh),
        # A genuinely different role at the same site — must NOT be collapsed.
        ("https://indeed.com/v/ccc", "Staff Product Designer, Maps", "indeed", "https://indeed.com/apply/ccc", 9, fresh),
    ]
    for url, title, site, app, score, disc in rows:
        conn.execute(
            "INSERT INTO jobs(url,title,site,application_url,fit_score,location,full_description,discovered_at,apply_status,apply_attempts)"
            " VALUES (?,?,?,?,?,'Remote','d',?,NULL,0)",
            (url, title, site, app, score, disc),
        )
    conn.commit()
    return conn


def test_dedup_collapses_same_site_title_prefers_canonical(monkeypatch):
    conn = _conn_with_dupes()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    acquired = []
    for _ in range(5):
        j = launcher.acquire_job(min_score=8, worker_id=0, max_age_hours=None)
        if not j:
            break
        acquired.append(j)
        conn.execute("UPDATE jobs SET apply_status='applied' WHERE url=?", (j["url"],))
        conn.commit()
        with launcher._run_seen_lock:
            launcher._run_seen_urls.clear()

    titles = sorted(j["title"] for j in acquired)
    # The 3 "Senior UX Designer, Alexa" dupes collapse to ONE; the distinct
    # "Staff Product Designer, Maps" survives → exactly 2 acquired total.
    assert titles == ["Senior UX Designer, Alexa", "Staff Product Designer, Maps"], titles
    # And the one kept for the dup group is the canonical Greenhouse URL,
    # not an indeed aggregator link.
    alexa = [j for j in acquired if j["title"] == "Senior UX Designer, Alexa"][0]
    assert "greenhouse.io" in alexa["application_url"], alexa["application_url"]


def test_dedup_durable_even_when_first_twin_failed(monkeypatch):
    """The exact 2026-05-15 bug: brex/harvey same (company,title) attempted
    twice because the first ended needs_review/failed (not applied), so the
    durable guard didn't fire. Now ANY prior attempt of a same-(site,title)
    twin must exclude the others — one application per role, win or lose."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE jobs (
            url TEXT PRIMARY KEY, title TEXT, site TEXT, application_url TEXT,
            tailored_resume_path TEXT, fit_score INTEGER, location TEXT,
            full_description TEXT, cover_letter_path TEXT, apply_status TEXT,
            apply_attempts INTEGER DEFAULT 0, agent_id TEXT,
            last_attempted_at TEXT, applied_at TEXT, discovered_at TEXT
        )
    """)
    disc = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    # Twin A already attempted and ended needs_review. Twin B (different url,
    # same site+title) is still untried. Plus a distinct role as a control.
    conn.execute("INSERT INTO jobs VALUES "
        "('https://www.brex.com/careers/1?gh_jid=1','Staff Product Designer','brex (greenhouse)',"
        "'https://www.brex.com/careers/1?gh_jid=1',NULL,8,'Remote','d',NULL,'needs_review',1,NULL,?,NULL,?)",
        (None, disc))
    conn.execute("INSERT INTO jobs VALUES "
        "('https://www.brex.com/careers/2?gh_jid=2','Staff Product Designer','brex (greenhouse)',"
        "'https://www.brex.com/careers/2?gh_jid=2',NULL,8,'Remote','d',NULL,NULL,0,NULL,?,NULL,?)",
        (None, disc))
    conn.execute("INSERT INTO jobs VALUES "
        "('https://boards.greenhouse.io/brex/jobs/9','Senior Designer, Brand','brex (greenhouse)',"
        "'https://boards.greenhouse.io/brex/jobs/9',NULL,8,'Remote','d',NULL,NULL,0,NULL,?,NULL,?)",
        (None, disc))
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    acquired = []
    for _ in range(5):
        j = launcher.acquire_job(min_score=8, worker_id=0, max_age_hours=None)
        if not j:
            break
        acquired.append(j["title"])
        conn.execute("UPDATE jobs SET apply_status='applied' WHERE url=?", (j["url"],))
        conn.commit()
        with launcher._run_seen_lock:
            launcher._run_seen_urls.clear()

    # Twin B must NOT be acquired (its sibling already hit needs_review).
    # Only the distinct "Senior Designer, Brand" role is applyable.
    assert acquired == ["Senior Designer, Brand"], acquired
