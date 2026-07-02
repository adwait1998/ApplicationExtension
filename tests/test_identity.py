from applypilot.identity import parse_ats_url, AtsRef

def test_greenhouse_canonical():
    assert parse_ats_url("https://boards.greenhouse.io/chime/jobs/8141068002?gh_jid=8141068002") == \
        AtsRef("greenhouse", "chime", "8141068002", confident=True)

def test_greenhouse_job_boards_host():
    assert parse_ats_url("https://job-boards.greenhouse.io/affirm/jobs/7546216003") == \
        AtsRef("greenhouse", "affirm", "7546216003", confident=True)

def test_greenhouse_vanity_gh_jid():
    r = parse_ats_url("https://careers.airbnb.com/positions/6153760?gh_jid=6153760")
    assert r.ats == "greenhouse" and r.token == "airbnb" and r.job_id == "6153760"
    assert r.confident is False  # token guessed from host label

def test_greenhouse_vanity_dotcareers():
    r = parse_ats_url("https://instacart.careers/job/?gh_jid=123456")
    assert r.ats == "greenhouse" and r.token == "instacart" and r.job_id == "123456"

def test_lever():
    assert parse_ats_url("https://jobs.lever.co/palantir/15f01f3a-922d-4cff-b093-888333d88628") == \
        AtsRef("lever", "palantir", "15f01f3a-922d-4cff-b093-888333d88628", confident=True)

def test_lever_apply_suffix():
    r = parse_ats_url("https://jobs.lever.co/palantir/15f01f3a-922d-4cff-b093-888333d88628/apply")
    assert r.job_id == "15f01f3a-922d-4cff-b093-888333d88628"

def test_lever_substring_false_positive_guarded():
    # 'cleverhealth' contains 'lever' but is not jobs.lever.co
    assert parse_ats_url("https://cleverhealth.com/careers/x") is None

def test_ashby():
    assert parse_ats_url("https://jobs.ashbyhq.com/baseten/126d54b4-a7bc-4456-bf4d-5d224e4f5d63") == \
        AtsRef("ashby", "baseten", "126d54b4-a7bc-4456-bf4d-5d224e4f5d63", confident=True)

def test_workday():
    r = parse_ats_url("https://adobe.wd5.myworkdayjobs.com/external_experienced/job/New-York/Digital-Strategist_R168109")
    assert r.ats == "workday" and r.token == "adobe" and r.job_id == "R168109"

def test_unsupported():
    assert parse_ats_url("https://example.com/careers/123") is None
    assert parse_ats_url("") is None

from applypilot.identity import identity_id

def test_identity_id_from_ats_ref():
    a = identity_id("https://boards.greenhouse.io/chime/jobs/8141068002?gh_jid=8141068002")
    b = identity_id("https://boards.greenhouse.io/chime/jobs/8141068002?utm_source=li")
    assert a == b == "greenhouse:chime:8141068002"   # tracking params collapse

def test_identity_id_cross_source_same_posting():
    # aggregator vanity URL and canonical URL for the same gh job -> same identity
    canonical = identity_id("https://boards.greenhouse.io/airbnb/jobs/6153760")
    vanity = identity_id("https://careers.airbnb.com/positions/6153760?gh_jid=6153760")
    assert canonical == vanity == "greenhouse:airbnb:6153760"

def test_identity_id_falls_back_to_normalized_url():
    # unparseable ATS -> deterministic fallback on normalized (company,title,location) not raw url
    key = identity_id("https://example.com/careers/x", company="Acme", title="Product Designer", location="Remote US")
    assert key.startswith("norm:")
    assert key == identity_id("https://other.example.com/y", company="acme", title="product designer", location="remote us")

def test_workday_board_slug_not_treated_as_job_id():
    r = parse_ats_url("https://adobe.wd5.myworkdayjobs.com/external_experienced")
    assert r.ats == "workday" and r.token == "adobe" and r.job_id is None

def test_vanity_ats_vendor_name_not_used_as_token():
    assert parse_ats_url("https://careers.lever.co/x?gh_jid=5") is None
    assert parse_ats_url("https://jobs.greenhouse.io/x?gh_jid=7") is None

def test_ats_token_two_segment_only_when_no_other_fields():
    # bare board ref with distinguishing fields -> norm (don't fold distinct postings)
    a = identity_id("https://jobs.lever.co/palantir", title="Staff Engineer", location="NYC")
    b = identity_id("https://jobs.lever.co/palantir", title="Product Designer", location="SF")
    assert a.startswith("norm:") and b.startswith("norm:") and a != b
    # degenerate: no other fields -> 2-segment token form
    assert identity_id("https://jobs.lever.co/palantir") == "lever:palantir"

def test_uppercase_host():
    r = parse_ats_url("https://BOARDS.GREENHOUSE.IO/chime/jobs/123")
    assert r == AtsRef("greenhouse", "chime", "123", confident=True)

def test_no_colon_injection_ever():
    for u in ("https://boards.greenhouse.io/chime/jobs/123",
              "https://jobs.lever.co/palantir/15f01f3a-922d-4cff-b093-888333d88628",
              "https://adobe.wd5.myworkdayjobs.com/x/job/y/Role_R1"):
        assert identity_id(u).count(":") == 2  # ats:token:job_id, never more
