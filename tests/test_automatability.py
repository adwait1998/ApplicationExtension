"""Reliability-v2 Phase E done-when: automatability gate + human queue.

- classify_blocker decision table (pure)
- scan_page_for_blocker on synthetic pages (captcha widget / email-verif
  text / clean form) — $0 Playwright
- queue_for_human appends a JSONL row
- reporting surfaces needs_human + per-ATS automatability
"""
from __future__ import annotations

import json

import pytest

from applypilot.apply.automatability import (
    classify_blocker,
    scan_page_for_blocker,
    queue_for_human,
)
from applypilot.reporting import summarize_review, format_report, is_needs_human


# ---- pure decision table ----

def test_classify_priority_and_none():
    assert classify_blocker({"sso_host": True, "captcha_visible": True}) == "sso"
    assert classify_blocker({"anti_bot": True}) == "anti_bot"
    assert classify_blocker({"captcha_visible": True}) == "captcha"
    assert classify_blocker({"email_verification": True}) == "email_verification"
    assert classify_blocker({"login_wall": True}) == "login_wall"
    assert classify_blocker({}) is None
    # an automatable form: no blocking signals
    assert classify_blocker({"captcha_visible": False, "login_wall": False}) is None


# ---- synthetic page scans ----

@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_context().new_page()
        yield p
        b.close()


def test_scan_detects_visible_captcha(page):
    page.set_content("""
      <html><body>
        <form><input id="first_name"><input type="file"></form>
        <div class="h-captcha" data-sitekey="x"></div>
        <iframe src="https://hcaptcha.com/captcha/v1"></iframe>
      </body></html>""")
    assert scan_page_for_blocker(page) == "captcha"


def test_scan_detects_email_verification(page):
    page.set_content("""
      <html><body>
        <h2>Verify your email</h2>
        <p>We sent a code to your email. Enter the verification code below.</p>
        <input id="code">
      </body></html>""")
    assert scan_page_for_blocker(page) == "email_verification"


def test_scan_detects_anti_bot(page):
    page.set_content("<html><body><h1>Checking your browser before you "
                     "continue. Verify you are human.</h1></body></html>")
    assert scan_page_for_blocker(page) == "anti_bot"


def test_scan_clean_application_form_is_automatable(page):
    page.set_content("""
      <html><body><form>
        <label>First name</label><input id="first_name" name="first_name">
        <label>Resume</label><input type="file" name="resume">
        <button>Submit application</button>
      </form></body></html>""")
    assert scan_page_for_blocker(page) is None


def test_scan_fails_open_on_blank(page):
    page.set_content("<html><body></body></html>")
    assert scan_page_for_blocker(page) is None   # never blocks a good apply


# ---- human queue ----

def test_queue_for_human_appends_jsonl(tmp_path):
    qp = tmp_path / "human_queue.jsonl"
    job = {"url": "u1", "application_url": "a1", "title": "Designer",
           "site": "linear (ashby)"}
    queue_for_human(job, "captcha", path=qp)
    queue_for_human({"url": "u2", "title": "X", "site": "y"}, "sso", path=qp)
    rows = [json.loads(l) for l in qp.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["reason"] == "captcha" and rows[0]["job_url"] == "u1"
    assert rows[1]["reason"] == "sso"


# ---- reporting integration ----

def test_report_counts_needs_human_and_automatability():
    rows = [
        {"dry_run": False, "status": "applied", "prefill_ats": "greenhouse", "cost_usd": 1.0},
        {"dry_run": False, "status": "applied", "prefill_ats": "greenhouse", "cost_usd": 1.0},
        {"dry_run": False, "status": "needs_review:needs_human_captcha",
         "failure_class": "blocker_captcha", "prefill_ats": "ashby"},
        {"dry_run": False, "status": "needs_review:timeout",
         "failure_class": "transient_timeout", "prefill_ats": "greenhouse"},
    ]
    assert is_needs_human(rows[2]) is True
    assert is_needs_human(rows[0]) is False
    s = summarize_review(rows)
    assert s["needs_human"] == 1
    # automatable jobs = 4 - b_fail(captcha=1) - needs_human(1) = ... captcha
    # is BOTH b_fail and needs_human; automatability = applied / (n - b - nh)
    # = 2 / (4 - 1 - 1) = 2/2 = 1.0  (the timeout is (A), still automatable)
    assert s["automatability"] == 1.0
    gh = s["by_ats"]["greenhouse"]
    assert gh["applied"] == 2 and gh["n"] == 3
    assert "Needs-human" in format_report(s)
    assert "Automatability" in format_report(s)
