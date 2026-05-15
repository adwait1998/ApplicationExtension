"""Regression: vanity Greenhouse URL canonicalization (prefill).

Companies host the same Greenhouse form on vanity domains. Prefill must
rewrite them to boards.greenhouse.io/<slug>/jobs/<gh_jid> so the form is
detected (otherwise prefill finds no form, the agent hallucinates
RESULT:APPLIED, and the verifier correctly rejects it as conf~0.2 →
needs_review — observed live on brex 2026-05-15).
"""
from __future__ import annotations

import pytest

from applypilot.apply.prefill import _canonicalize_greenhouse_url as canon


@pytest.mark.parametrize("url,expected", [
    # iter-13 new pattern: www.<co>.<tld>/careers/...?gh_jid=  (brex, stripe)
    ("https://www.brex.com/careers/8124973002?gh_jid=8124973002",
     "https://boards.greenhouse.io/brex/jobs/8124973002"),
    ("https://stripe.com/jobs/search?gh_jid=7655023",
     "https://boards.greenhouse.io/stripe/jobs/7655023"),
    # iter-5 patterns still work
    ("https://careers.airbnb.com/positions/123?gh_jid=456",
     "https://boards.greenhouse.io/airbnb/jobs/456"),
    ("https://careers.duolingo.com/jobs/x?gh_jid=999",
     "https://boards.greenhouse.io/duolingo/jobs/999"),
    ("https://instacart.careers/job/?gh_jid=789",
     "https://boards.greenhouse.io/instacart/jobs/789"),
])
def test_vanity_rewritten(url, expected):
    assert canon(url) == expected


@pytest.mark.parametrize("url", [
    # Already canonical — untouched
    "https://boards.greenhouse.io/figma/jobs/111",
    "https://job-boards.greenhouse.io/reddit/jobs/222",
    # Different ATS — untouched
    "https://jobs.lever.co/plaid/abc",
    "https://jobs.ashbyhq.com/openai/xyz",
    # No gh_jid — can't be a Greenhouse job, untouched
    "https://www.brex.com/careers/8124973002",
    # gh_jid present but NOT a careers/jobs path — must NOT be rewritten
    # (guard against slugifying unrelated marketing links)
    "https://www.brex.com/about?gh_jid=999",
    "https://www.brex.com/blog/post?gh_jid=1",
    # Empty / junk
    "",
])
def test_left_unchanged(url):
    assert canon(url) == url
