from __future__ import annotations

import sqlite3

from applypilot.database import get_jobs_by_stage, get_stats


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE jobs (
            url TEXT PRIMARY KEY,
            title TEXT,
            site TEXT,
            application_url TEXT,
            full_description TEXT,
            detail_scraped_at TEXT,
            detail_error TEXT,
            fit_score INTEGER,
            tailored_resume_path TEXT,
            tailor_attempts INTEGER DEFAULT 0,
            cover_letter_path TEXT,
            cover_attempts INTEGER DEFAULT 0,
            applied_at TEXT,
            apply_status TEXT,
            apply_attempts INTEGER DEFAULT 0,
            apply_error TEXT,
            discovered_at TEXT,
            -- Task 12: ready_to_apply (via _count_applyable_jobs) and the
            -- "pending_apply" stage both route through queue_policy(), which
            -- requires gate_result='eligible' AND automatability='auto'. Seed
            -- rows are gated-eligible so the automatable-URL intent is tested.
            gate_result TEXT, automatability TEXT, gated_at TEXT,
            operator_approved INTEGER DEFAULT 0, approved_at TEXT
        )
    """)
    rows = [
        ("https://www.linkedin.com/jobs/view/1", "LinkedIn", "linkedin", "None", 9),
        ("https://boards.greenhouse.io/acme/jobs/1", "Direct", "acme", "None", 9),
        ("not-a-url", "Broken", "broken", "None", 9),
    ]
    for url, title, site, app, score in rows:
        conn.execute(
            "INSERT INTO jobs(url,title,site,application_url,full_description,detail_scraped_at,fit_score,apply_status,apply_attempts,gate_result,automatability,gated_at)"
            " VALUES (?,?,?,?, 'desc', 'now', ?, NULL, 0, 'eligible', 'auto', 't')",
            (url, title, site, app, score),
        )
    conn.commit()
    return conn


def test_ready_to_apply_counts_only_automatable_urls(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(
        "applypilot.config.is_manual_ats",
        lambda url: "linkedin.com/jobs/view" in url,
    )

    stats = get_stats(conn)
    pending = get_jobs_by_stage(conn, stage="pending_apply", min_score=8, limit=10)

    assert stats["ready_to_apply"] == 1
    assert [job["url"] for job in pending] == ["https://boards.greenhouse.io/acme/jobs/1"]
