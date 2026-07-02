"""Iter-4 regression: successful attempts must never carry a failure_class.

Live data showed dozens of review.jsonl rows with status=applied AND
failure_class=transient_unknown (stale value from earlier in the attempt
loop), which polluted the removable-failure counts that drive the
reliability loop's targeting.
"""

from __future__ import annotations

import json

import pytest

from applypilot.apply import launcher


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.LOG_DIR", tmp_path)
    monkeypatch.setattr("applypilot.config.ensure_dirs", lambda: None)
    return tmp_path


def _last_row(log_dir):
    lines = (log_dir / "review.jsonl").read_text(encoding="utf-8").strip().splitlines()
    return json.loads(lines[-1])


JOB = {"url": "j1", "title": "Designer", "site": "figma (greenhouse)", "fit_score": 9}


def test_applied_row_never_logs_failure_class(log_dir):
    launcher.write_review_log(JOB, "applied", "haiku", 1000, False,
                              failure_class="transient_unknown")
    row = _last_row(log_dir)
    assert row["status"] == "applied"
    assert row["failure_class"] is None


def test_dry_run_applied_row_never_logs_failure_class(log_dir):
    launcher.write_review_log(JOB, "dry_run:applied", "haiku", 1000, True,
                              failure_class="transient_unknown")
    assert _last_row(log_dir)["failure_class"] is None


def test_failed_rows_keep_their_failure_class(log_dir):
    launcher.write_review_log(JOB, "needs_review", "haiku", 1000, False,
                              failure_class="verification_unverified_submission")
    assert _last_row(log_dir)["failure_class"] == "verification_unverified_submission"


def test_failed_status_with_no_class_stays_none(log_dir):
    launcher.write_review_log(JOB, "failed", "haiku", 1000, False)
    assert _last_row(log_dir)["failure_class"] is None
