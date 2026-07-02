"""Guard the apply-prompt invariants that cost real money/time when they
regress. $0 — just builds the prompt string.

- Anti-keystroke rule (iter-13): browser_type one-char-at-a-time on text
  fields timed out whole applies (sofi, PayPal). The prompt must forbid it.
- GREENHOUSE FAST PATH must stay present (trust prefill / no scroll-loop).
"""
from __future__ import annotations

import pytest

from applypilot import config
from applypilot.apply import prompt as P


@pytest.fixture
def job():
    return {
        "title": "Senior Product Designer",
        "site": "figma (greenhouse)",
        "url": "https://boards.greenhouse.io/figma/jobs/1",
        "application_url": "https://boards.greenhouse.io/figma/jobs/1",
        "full_description": "Design role.",
        "fit_score": 9,
        "tailored_resume_path": str(config.RESUME_PDF_PATH),
    }


@pytest.mark.parametrize("dry_run", [False, True])
def test_prompt_forbids_keystroke_typing(job, dry_run):
    s = P.build_prompt(job=job, tailored_resume="Nida Shah resume " * 30,
                       dry_run=dry_run,
                       prefill_status={"ats": "greenhouse",
                                       "fields_filled": ["email"],
                                       "error": None, "duration_ms": 1})
    low = s.lower()
    assert "never use browser_type to enter a text-field value" in low
    assert "browser_fill_form" in s
    assert "budget tripwire" in low
    # The proven Greenhouse recipe must stay in the prompt.
    assert "GREENHOUSE FAST PATH" in s


def test_prompt_makes_stream_executor_mandatory_for_visible_controls(job):
    s = P.build_prompt(
        job=job,
        tailored_resume="Nida Shah resume " * 30,
        dry_run=False,
        prefill_status={
            "ats": "greenhouse",
            "fields_filled": ["email"],
            "error": None,
            "duration_ms": 1,
        },
        browser_observation_summary=(
            "Visible controls:\n"
            "- f0:control:1:abc | Email: nida@example.com (required, ok)"
        ),
    )

    assert "APPLYPILOT STREAM TOOLS (MANDATORY FAST PATH)" in s
    assert "MUST use mcp__applypilot_stream__stream_execute" in s
    assert "fill/select/upload/check/click/type/press" in s
    assert "STREAM FALLBACK:" in s
    assert "browser_snapshot is not the normal page-read path" in s
    assert "RESULT:FAILED:stream_snapshot_needed" in s
    assert "RESULT:FAILED:apply_button_not_found" in s
    assert "do NOT invent URL patterns" in s
    assert "ONE browser_snapshot" not in s
    assert "NEXT browser_snapshot" not in s
    assert "Starting now: **First action is browser_snapshot**" not in s
    assert "snapshot each new page" not in s


def test_prompt_forbids_candidate_profile_only_as_applied(job):
    s = P.build_prompt(
        job={**job, "site": "logitech (workday)", "application_url": "https://example.com/apply"},
        tailored_resume="Nida Shah resume " * 30,
        dry_run=False,
    )
    low = s.lower()

    assert "candidate profile" in low
    assert "introduce yourself" in low
    assert "not a completed job application" in low
    assert "RESULT:FAILED:candidate_profile_only" in s
    assert "profile submission is not success" in low
