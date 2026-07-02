from applypilot.gate.rules import seniority_rule, sponsorship_rule, automatability_rule

SENIORITY = {"accept_bands": ["mid", "senior"], "ic_only": True}

def test_manager_rejected_for_ic():
    v = seniority_rule("Design Manager", SENIORITY)
    assert v.result == "REJECT" and v.code == "seniority_management_track"

def test_lead_is_ic_accepted():
    assert seniority_rule("Lead Product Designer", SENIORITY).result == "PASS"

def test_intern_rejected():
    v = seniority_rule("Product Design Intern", SENIORITY)
    assert v.result == "REJECT" and v.code == "seniority_early_career"

def test_senior_ic_accepted():
    assert seniority_rule("Senior Product Designer", SENIORITY).result == "PASS"

def test_sponsorship_hard_marker_rejects():
    v = sponsorship_rule("Must be authorized to work in the US without sponsorship.",
                         needs_sponsorship=True)
    assert v.result == "REJECT" and v.code == "sponsorship_blocked"
    assert "without sponsorship" in v.evidence.lower()

def test_sponsorship_silent_is_unknown_for_visa_user():
    v = sponsorship_rule("Great team, competitive pay.", needs_sponsorship=True)
    assert v.result == "UNKNOWN" and v.code == "sponsorship_unknown"

def test_sponsorship_irrelevant_when_not_needed():
    assert sponsorship_rule("US citizenship required.", needs_sponsorship=False).result == "PASS"

def test_workday_without_account_parks():
    v = automatability_rule("https://adobe.wd5.myworkdayjobs.com/x/job/y_R1", workday_accounts=[])
    assert v.result == "REJECT" and v.code == "account_required"

def test_workday_with_account_ok():
    v = automatability_rule("https://adobe.wd5.myworkdayjobs.com/x/job/y_R1", workday_accounts=["adobe"])
    assert v.result == "PASS"

def test_manual_ats_rejected():
    v = automatability_rule("https://www.linkedin.com/jobs/view/123", workday_accounts=[])
    assert v.result == "REJECT" and v.code == "manual_ats"

def test_supported_ats_ok():
    assert automatability_rule("https://boards.greenhouse.io/chime/jobs/1", workday_accounts=[]).result == "PASS"
