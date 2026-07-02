"""Iter-6 regression: Workday tenants without registered accounts are parked
pre-spawn instead of burning 350-430s + LLM cost on a login wall.

Evidence: all 4 blocker_login_issue rows (2026-05-20..22) were Workday
tenants (salesforce pre-account, rakuten, thomsonreuters) or an eQuest
redirect — each spent 216-432s before failing on sign-in.
"""

from __future__ import annotations

import pytest

from applypilot.apply import launcher

CFG = {"workday_accounts": ["motorolasolutions", "Salesforce", "adobe"]}
PROFILE = {"personal": {"city": "San Jose"}}


def _job(url):
    return {"title": "Designer", "location": "Remote, US",
            "application_url": url, "full_description": "Remote role."}


def test_unregistered_tenant_is_rejected():
    job = _job("https://rakuten.wd1.myworkdayjobs.com/RakutenRewards/job/x")
    assert launcher._workday_account_reject(job, CFG) == "workday_account_required"


def test_registered_tenant_passes_case_insensitive():
    job = _job("https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/job/x")
    assert launcher._workday_account_reject(job, CFG) is None


def test_non_workday_url_passes():
    job = _job("https://boards.greenhouse.io/figma/jobs/123")
    assert launcher._workday_account_reject(job, CFG) is None


def test_no_config_list_means_gate_disabled():
    job = _job("https://rakuten.wd1.myworkdayjobs.com/RakutenRewards/job/x")
    assert launcher._workday_account_reject(job, {}) is None
    assert launcher._workday_account_reject(job, {"workday_accounts": []}) is None


def test_combined_reject_reason_prefers_cheapest_check():
    job = _job("https://rakuten.wd1.myworkdayjobs.com/RakutenRewards/job/x")
    assert launcher._preapply_reject_reason(job, PROFILE, CFG) == "workday_account_required"


def test_combined_reject_reason_still_catches_location():
    job = {"title": "Designer", "location": "Mexico City, Mexico",
           "application_url": "https://boards.greenhouse.io/x/jobs/1",
           "full_description": "On-site role based in Mexico City."}
    assert launcher._preapply_reject_reason(job, PROFILE, CFG) == "not_eligible_location"


def test_classifier_maps_to_blocker():
    assert launcher._classify_failure_class(
        "failed:workday_account_required", "workday_account_required"
    ) == "blocker_workday_account_required"
