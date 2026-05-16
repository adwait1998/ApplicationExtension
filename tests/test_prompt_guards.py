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
