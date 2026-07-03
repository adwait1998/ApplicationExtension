from applypilot.gate.engine import gate_job

PROFILE = {
    "geo": {"remote_ok": True, "remote_scope": "US", "onsite_regions": ["us-ca", "us-wa-seattle", "us-ny-nyc"]},
    "seniority": {"accept_bands": ["mid", "senior"], "ic_only": True},
    "needs_sponsorship": True,
    "workday_accounts": ["adobe"],
}

def test_eligible_greenhouse_remote_us():
    # Location + seniority + automatability all PASS. Because PROFILE needs
    # sponsorship and the committed sponsorship_rule has no positive-signal
    # path (silent description -> UNKNOWN for a visa user, locked by
    # test_gate_rules.test_sponsorship_silent_is_unknown_for_visa_user), the
    # composed verdict is "unknown" (routes to review), NOT "eligible":
    # UNKNOWN never auto-passes. The auto/ats/identity plumbing is still
    # asserted below since those are independent of the sponsorship verdict.
    r = gate_job({"title": "Senior Product Designer", "location": "Remote - US",
                  "full_description": "Design systems work.",
                  "application_url": "https://boards.greenhouse.io/chime/jobs/1"}, PROFILE)
    assert r["gate_result"] == "unknown"
    assert r["automatability"] == "auto"
    assert r["ats"] == "greenhouse" and r["identity_id"] == "greenhouse:chime:1"

def test_ineligible_manager_short_circuits():
    r = gate_job({"title": "Design Manager", "location": "Remote - US",
                  "application_url": "https://boards.greenhouse.io/chime/jobs/2"}, PROFILE)
    assert r["gate_result"] == "ineligible"
    assert any(x["code"] == "seniority_management_track" for x in r["gate_reasons"])

def test_sponsorship_unknown_is_unknown_not_eligible():
    r = gate_job({"title": "Senior Product Designer", "location": "Remote - US",
                  "full_description": "Great pay.",
                  "application_url": "https://boards.greenhouse.io/chime/jobs/3"}, PROFILE)
    assert r["gate_result"] == "unknown"  # visa user + no sponsorship signal -> review, not auto

def test_workday_without_account_is_ineligible():
    r = gate_job({"title": "Senior Product Designer", "location": "Remote - US",
                  "full_description": "x", "application_url": "https://sap.wd3.myworkdayjobs.com/x/job/y_R1"}, PROFILE)
    assert r["automatability"] == "account_required"
    assert r["gate_result"] == "ineligible"

def test_gate_version_stamped():
    from applypilot.gate import GATE_VERSION
    r = gate_job({"title": "Senior Product Designer", "location": "Remote - US", "full_description": "x",
                  "application_url": "https://boards.greenhouse.io/chime/jobs/4"}, PROFILE)
    assert r["gate_version"] == GATE_VERSION and r["gated_at"]

def test_reasons_records_all_rules():
    r = gate_job({"title": "Senior Product Designer", "location": "Remote - US", "full_description": "design",
                  "application_url": "https://boards.greenhouse.io/chime/jobs/5"}, PROFILE)
    codes = {x["rule"] for x in r["gate_reasons"]}
    assert codes == {"automatability", "location", "seniority", "sponsorship"}
