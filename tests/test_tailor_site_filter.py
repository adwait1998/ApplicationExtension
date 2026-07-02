from __future__ import annotations

import sqlite3

from applypilot.scoring import tailor


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE jobs (
            url TEXT PRIMARY KEY,
            title TEXT,
            site TEXT,
            full_description TEXT,
            application_url TEXT,
            fit_score INTEGER,
            tailored_resume_path TEXT,
            tailored_at TEXT,
            tailor_attempts INTEGER DEFAULT 0,
            applied_at TEXT,
            apply_status TEXT,
            apply_attempts INTEGER DEFAULT 0,
            discovered_at TEXT
        )
        """
    )
    rows = [
        (
            "https://jobs.example/theirstack",
            "Product Designer",
            "Acme (TheirStack)",
            "desc",
            "https://jobs.example/theirstack/apply",
            9,
            None,
            None,
            0,
        ),
        (
            "https://jobs.example/other",
            "Product Designer",
            "Other",
            "desc",
            "https://jobs.example/other/apply",
            9,
            None,
            None,
            0,
        ),
        (
            "https://jobs.example/manual",
            "Product Designer",
            "Manual (TheirStack)",
            "desc",
            "https://www.linkedin.com/jobs/view/1",
            9,
            None,
            "manual",
            0,
        ),
    ]
    conn.executemany(
        """
        INSERT INTO jobs(
            url, title, site, full_description, application_url, fit_score,
            applied_at, apply_status, apply_attempts, discovered_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-06-02T00:00:00+00:00')
        """,
        rows,
    )
    conn.commit()
    return conn


def test_run_tailoring_filters_by_site_contains(monkeypatch, tmp_path):
    conn = _conn()
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("Base resume", encoding="utf-8")

    monkeypatch.setattr(tailor, "get_connection", lambda: conn)
    monkeypatch.setattr(tailor, "load_profile", lambda: {"personal": {"full_name": "Nida Shah"}})
    monkeypatch.setattr(tailor, "RESUME_PATH", resume_path)
    monkeypatch.setattr(tailor, "TAILORED_DIR", tmp_path / "tailored")
    monkeypatch.setattr(
        tailor,
        "tailor_resume",
        lambda _resume, _job, _profile, validation_mode="normal": (
            "Tailored resume",
            {"status": "approved", "attempts": 1},
        ),
    )

    result = tailor.run_tailoring(min_score=8, limit=10, site_contains="Acme")

    assert result["approved"] == 1
    saved = {
        row["url"]: dict(row)
        for row in conn.execute("SELECT url, tailored_resume_path, tailor_attempts FROM jobs")
    }
    assert saved["https://jobs.example/theirstack"]["tailored_resume_path"]
    assert saved["https://jobs.example/theirstack"]["tailor_attempts"] == 1
    assert saved["https://jobs.example/other"]["tailored_resume_path"] is None
    assert saved["https://jobs.example/other"]["tailor_attempts"] == 0


def test_run_tailoring_applyable_only_skips_manual_source_rows(monkeypatch, tmp_path):
    conn = _conn()
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("Base resume", encoding="utf-8")

    monkeypatch.setattr(tailor, "get_connection", lambda: conn)
    monkeypatch.setattr(tailor, "load_profile", lambda: {"personal": {"full_name": "Nida Shah"}})
    monkeypatch.setattr(tailor, "RESUME_PATH", resume_path)
    monkeypatch.setattr(tailor, "TAILORED_DIR", tmp_path / "tailored")
    monkeypatch.setattr(
        tailor,
        "tailor_resume",
        lambda _resume, _job, _profile, validation_mode="normal": (
            "Tailored resume",
            {"status": "approved", "attempts": 1},
        ),
    )

    result = tailor.run_tailoring(
        min_score=8,
        limit=10,
        site_contains="TheirStack",
        applyable_only=True,
    )

    assert result["approved"] == 1
    saved = {
        row["url"]: dict(row)
        for row in conn.execute("SELECT url, tailored_resume_path, tailor_attempts FROM jobs")
    }
    assert saved["https://jobs.example/theirstack"]["tailored_resume_path"]
    assert saved["https://jobs.example/manual"]["tailored_resume_path"] is None
    assert saved["https://jobs.example/manual"]["tailor_attempts"] == 0
