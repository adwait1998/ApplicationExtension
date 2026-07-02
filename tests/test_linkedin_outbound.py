from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from applypilot import cli
from applypilot.cli import app
from applypilot.database import ensure_columns
from applypilot.enrichment import detail
from applypilot.enrichment.linkedin_cua_probe import build_initial_cua_payload, build_safety_acknowledgement_payload
from applypilot.enrichment.linkedin_outbound import (
    LinkedInResolution,
    classify_linkedin_html,
    resolve_linkedin_jobs,
    resolve_linkedin_with_page,
    select_linkedin_candidates,
    write_resolution,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE jobs (
            url TEXT PRIMARY KEY,
            title TEXT,
            site TEXT,
            application_url TEXT,
            application_url_source TEXT,
            application_url_resolved_at TEXT,
            application_url_error TEXT,
            full_description TEXT,
            detail_scraped_at TEXT,
            fit_score INTEGER,
            apply_status TEXT,
            apply_error TEXT,
            apply_attempts INTEGER DEFAULT 0,
            applied_at TEXT,
            discovered_at TEXT
        )
        """
    )
    return conn


def _insert(
    conn: sqlite3.Connection,
    url: str,
    *,
    title: str = "Senior Product Designer",
    site: str = "linkedin",
    app_url: str | None = None,
    score: int = 9,
    status: str | None = None,
    error: str | None = None,
    detail_scraped_at: str | None = "2026-05-20T00:00:00+00:00",
    discovered_at: str = "2026-05-20T00:00:00+00:00",
) -> None:
    conn.execute(
        """
        INSERT INTO jobs(
            url, title, site, application_url, full_description, detail_scraped_at,
            fit_score, apply_status, apply_error, apply_attempts, discovered_at
        )
        VALUES (?, ?, ?, ?, 'description', ?, ?, ?, ?, 0, ?)
        """,
        (url, title, site, app_url, detail_scraped_at, score, status, error, discovered_at),
    )
    conn.commit()


def test_schema_migration_adds_linkedin_resolver_metadata_columns():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY)")

    added = ensure_columns(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}

    assert "application_url_source" in added
    assert "application_url_resolved_at" in columns
    assert "application_url_error" in columns


def test_classify_linkedin_html_outbound_apply_link():
    html = """
    <main>
      <a aria-label="Apply to Acme" href="/comm/jobs/view/externalApply?url=https%3A%2F%2Fboards.greenhouse.io%2Facme%2Fjobs%2F123">
        Apply
      </a>
    </main>
    """

    result = classify_linkedin_html(html, "https://www.linkedin.com/jobs/view/1")

    assert result.status == "resolved"
    assert result.outbound_url == "https://boards.greenhouse.io/acme/jobs/123"


def test_classify_linkedin_html_easy_apply_only():
    result = classify_linkedin_html(
        '<button aria-label="Easy Apply to Acme">Easy Apply</button>',
        "https://www.linkedin.com/jobs/view/1",
    )

    assert result.status == "easy_apply_only"
    assert result.outbound_url is None


def test_resolved_result_requires_real_http_url():
    with pytest.raises(ValueError, match="valid outbound_url"):
        LinkedInResolution("resolved", outbound_url="None")


def test_classify_linkedin_html_closed_login_captcha_unknown():
    assert classify_linkedin_html("This job is no longer accepting applications").status == "expired"
    assert classify_linkedin_html("Sign in to LinkedIn to view this job").status == "login_blocked"
    assert classify_linkedin_html("Security verification captcha").status == "captcha"
    assert classify_linkedin_html("<main>No apply control here</main>").status == "unknown"


def test_visible_resolver_waits_for_manual_login_clearance(monkeypatch):
    class FakeResponse:
        status = 200

    class FakePage:
        url = "https://www.linkedin.com/login"
        attempts = 0

        def goto(self, _url, **_kwargs):
            self.url = _url
            return FakeResponse()

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def content(self):
            self.attempts += 1
            if self.attempts == 1:
                return "Sign in to LinkedIn to view this job"
            return '<a aria-label="Apply" href="https://jobs.lever.co/acme/123">Apply</a>'

        def evaluate(self, *_args, **_kwargs):
            return {"sawEasyApply": False, "candidates": []}

    monkeypatch.setattr("applypilot.enrichment.linkedin_outbound.time.sleep", lambda _seconds: None)

    result = resolve_linkedin_with_page(
        FakePage(),
        "https://www.linkedin.com/jobs/view/1",
        manual_unblock_seconds=5,
    )

    assert result.status == "resolved"
    assert result.outbound_url == "https://jobs.lever.co/acme/123"


def test_select_candidates_includes_detail_scraped_manual_linkedin_rows():
    conn = _conn()
    _insert(
        conn,
        "https://www.linkedin.com/jobs/view/4415704549",
        app_url=None,
        status="manual",
        error="manual ATS",
        detail_scraped_at="already",
    )

    rows = select_linkedin_candidates(conn, limit=10, min_score=8, retry_hours=0)

    assert [row["url"] for row in rows] == ["https://www.linkedin.com/jobs/view/4415704549"]


def test_select_candidates_can_scope_to_fresh_theirstack_rows():
    conn = _conn()
    now = datetime.now(timezone.utc)
    _insert(
        conn,
        "https://www.linkedin.com/jobs/view/1",
        site="Semgrep (TheirStack)",
        app_url="https://www.linkedin.com/jobs/view/1",
        discovered_at=now.isoformat(),
    )
    _insert(
        conn,
        "https://www.linkedin.com/jobs/view/2",
        site="linkedin",
        app_url="https://www.linkedin.com/jobs/view/2",
        discovered_at=now.isoformat(),
    )
    _insert(
        conn,
        "https://www.linkedin.com/jobs/view/3",
        site="Old (TheirStack)",
        app_url="https://www.linkedin.com/jobs/view/3",
        discovered_at=(now - timedelta(hours=72)).isoformat(),
    )

    rows = select_linkedin_candidates(
        conn,
        limit=10,
        min_score=8,
        retry_hours=0,
        site_contains="TheirStack",
        max_age_hours=48,
    )

    assert [row["url"] for row in rows] == ["https://www.linkedin.com/jobs/view/1"]


def test_write_resolution_sets_application_url_and_restores_manual_ats(monkeypatch):
    conn = _conn()
    url = "https://www.linkedin.com/jobs/view/4415704549"
    _insert(conn, url, status="manual", error="manual ATS")
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
    monkeypatch.setattr("applypilot.config.is_manual_ats", lambda value: "linkedin.com/jobs" in value)

    summary = write_resolution(
        conn,
        row,
        LinkedInResolution("resolved", outbound_url="https://boards.greenhouse.io/acme/jobs/123"),
        write=True,
        now="now",
    )
    saved = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()

    assert summary["would_clear_manual"] is True
    assert saved["application_url"] == "https://boards.greenhouse.io/acme/jobs/123"
    assert saved["application_url_source"] == "linkedin_outbound"
    assert saved["application_url_resolved_at"] == "now"
    assert saved["apply_status"] is None
    assert saved["apply_error"] is None


def test_unresolved_manual_row_stays_manual():
    conn = _conn()
    url = "https://www.linkedin.com/jobs/view/4415704549"
    _insert(conn, url, status="manual", error="manual ATS")
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()

    write_resolution(conn, row, LinkedInResolution("easy_apply_only", error="easy_apply_only"), write=True, now="now")
    saved = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()

    assert saved["application_url"] is None
    assert saved["apply_status"] == "manual"
    assert saved["application_url_error"] == "easy_apply_only"


def test_resolved_non_resolver_manual_row_is_not_restored():
    conn = _conn()
    url = "https://www.linkedin.com/jobs/view/4415704549"
    _insert(conn, url, status="manual", error="manager_role_skipped")
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()

    summary = write_resolution(
        conn,
        row,
        LinkedInResolution("resolved", outbound_url="https://jobs.lever.co/acme/123"),
        write=True,
        now="now",
    )
    saved = conn.execute("SELECT application_url, apply_status, apply_error FROM jobs WHERE url = ?", (url,)).fetchone()

    assert summary["would_clear_manual"] is False
    assert saved["application_url"] == "https://jobs.lever.co/acme/123"
    assert saved["apply_status"] == "manual"
    assert saved["apply_error"] == "manager_role_skipped"


def test_duplicate_outbound_url_is_marked_duplicate():
    conn = _conn()
    linkedin_url = "https://www.linkedin.com/jobs/view/1"
    direct_url = "https://jobs.ashbyhq.com/acme/abc"
    _insert(conn, linkedin_url, status="manual", error="manual ATS")
    _insert(conn, direct_url, title="Direct", app_url=direct_url, status=None)
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (linkedin_url,)).fetchone()

    summary = write_resolution(
        conn,
        row,
        LinkedInResolution("resolved", outbound_url=direct_url),
        write=True,
        now="now",
    )
    saved = conn.execute("SELECT apply_status, apply_error FROM jobs WHERE url = ?", (linkedin_url,)).fetchone()

    assert summary["duplicate_url"] == direct_url
    assert saved["apply_status"] == "duplicate"
    assert "duplicate resolved application URL" in saved["apply_error"]


def test_resolve_linkedin_jobs_dry_run_vs_write():
    conn = _conn()
    url = "https://www.linkedin.com/jobs/view/1"
    _insert(conn, url, status="manual", error="manual ATS")

    def fake_resolver(_url: str, **_kwargs) -> LinkedInResolution:
        return LinkedInResolution("resolved", outbound_url="https://jobs.lever.co/acme/123")

    dry = resolve_linkedin_jobs(conn, limit=1, min_score=8, write=False, retry_hours=0, resolver=fake_resolver)
    assert dry["resolved"] == 1
    assert conn.execute("SELECT application_url FROM jobs WHERE url = ?", (url,)).fetchone()[0] is None

    written = resolve_linkedin_jobs(conn, limit=1, min_score=8, write=True, retry_hours=0, resolver=fake_resolver)
    assert written["resolved"] == 1
    assert conn.execute("SELECT application_url FROM jobs WHERE url = ?", (url,)).fetchone()[0] == "https://jobs.lever.co/acme/123"


def test_run_enrichment_invokes_linkedin_resolver_before_detail_scraper(monkeypatch):
    conn = _conn()
    calls = []

    monkeypatch.setattr(detail, "init_db", lambda: conn)
    monkeypatch.setattr(detail, "resolve_all_urls", lambda _conn: {"resolved": 0, "already_absolute": 0, "failed": 0})
    monkeypatch.setattr(
        detail,
        "_run_linkedin_outbound_resolver",
        lambda _conn, limit, min_score: calls.append(("linkedin", limit, min_score)) or {"processed": 1, "resolved": 1},
    )
    monkeypatch.setattr(
        detail,
        "_run_detail_scraper",
        lambda _conn, max_per_site, max_total, workers: calls.append(("detail", max_per_site, max_total, workers)) or {"processed": 0, "ok": 0, "partial": 0, "error": 0},
    )

    stats = detail.run_enrichment(limit=5, workers=2, linkedin_limit=3, linkedin_min_score=8)

    assert calls == [("linkedin", 3, 8), ("detail", 5, None, 2)]
    assert stats["linkedin_outbound"]["resolved"] == 1


def test_resolve_linkedin_cli_dry_run(monkeypatch):
    runner = CliRunner()
    calls = []

    monkeypatch.setattr(cli, "_bootstrap", lambda: None)

    def fake_resolve(**kwargs):
        calls.append(kwargs)
        return {
            "candidates": 1,
            "processed": 1,
            "resolved": 1,
            "easy_apply_only": 0,
            "expired": 0,
            "login_blocked": 0,
            "captcha": 0,
            "unknown": 0,
            "error": 0,
            "duplicates": 0,
            "results": [{"status": "resolved", "url": "https://www.linkedin.com/jobs/view/1", "outbound_url": "https://jobs.lever.co/acme/1", "duplicate_url": None}],
        }

    monkeypatch.setattr("applypilot.enrichment.linkedin_outbound.resolve_linkedin_jobs", fake_resolve)

    result = runner.invoke(app, ["resolve-linkedin", "--limit", "1", "--dry-run"])

    assert result.exit_code == 0
    assert calls[0]["write"] is False
    assert calls[0]["manual_unblock_seconds"] == 0
    assert "Dry run: database was not updated" in result.output


def test_resolve_linkedin_cli_visible_passes_login_wait(monkeypatch):
    runner = CliRunner()
    calls = []

    monkeypatch.setattr(cli, "_bootstrap", lambda: None)

    def fake_resolve(**kwargs):
        calls.append(kwargs)
        return {
            "candidates": 0,
            "processed": 0,
            "resolved": 0,
            "easy_apply_only": 0,
            "expired": 0,
            "login_blocked": 0,
            "captcha": 0,
            "unknown": 0,
            "error": 0,
            "duplicates": 0,
            "results": [],
        }

    monkeypatch.setattr("applypilot.enrichment.linkedin_outbound.resolve_linkedin_jobs", fake_resolve)

    result = runner.invoke(app, ["resolve-linkedin", "--limit", "1", "--visible", "--login-wait", "7"])

    assert result.exit_code == 0
    assert calls[0]["headless"] is False
    assert calls[0]["manual_unblock_seconds"] == 7


def test_cua_probe_payload_is_linkedin_only_and_resolver_scoped():
    payload = build_initial_cua_payload("https://www.linkedin.com/jobs/view/1")

    assert payload["model"] == "computer-use-preview"
    assert payload["tools"][0]["type"] == "computer_use_preview"
    text = payload["input"][0]["content"][0]["text"]
    assert "Do not use Easy Apply" in text
    assert "Do not submit anything" in text

    ack = build_safety_acknowledgement_payload(
        previous_response_id="resp_1",
        call_id="call_1",
        pending_safety_checks=[{"id": "safe_1", "code": "malicious_instructions", "message": "review"}],
        screenshot_url="https://example.test/screenshot.png",
    )
    assert ack["previous_response_id"] == "resp_1"
    assert ack["input"][0]["acknowledged_safety_checks"][0]["id"] == "safe_1"
    assert ack["truncation"] == "auto"
