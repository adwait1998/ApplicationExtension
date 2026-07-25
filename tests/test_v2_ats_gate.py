"""Task 7: the per-ATS v2 dispatch gate (invariant 11) + the Lever _detect_ats
branch.

`_v2_supported_ats(job)` widens the old Greenhouse-only `_is_greenhouse` gate:
the master flag APPLYPILOT_V2_ENGINE must be on, then APPLYPILOT_V2_ATS (a
fresh-read comma allowlist) scopes WHICH ATSes v2 handles. Unset allowlist =>
'greenhouse' only (the Phase-3 shadow, byte-for-byte). An ATS with no front-end
(parser_for is None) is never supported."""
from applypilot.apply import launcher
from applypilot.apply.prefill import _detect_ats


def test_detect_ats_now_recognizes_lever():
    assert _detect_ats("https://jobs.lever.co/acme/1234") == "lever"
    assert _detect_ats("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert _detect_ats("https://jobs.ashbyhq.com/acme/app") == "ashby"


def test_detect_ats_lever_vanity_host():
    # A bare lever.co host (some tenants use jobs.lever.co, some proxy lever.co).
    assert _detect_ats("https://lever.co/acme/1234") == "lever"


def test_v2_supported_ats_default_is_greenhouse_only(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    monkeypatch.delenv("APPLYPILOT_V2_ATS", raising=False)
    assert launcher._v2_supported_ats({"application_url": "https://boards.greenhouse.io/a/jobs/1"})
    assert not launcher._v2_supported_ats({"application_url": "https://jobs.ashbyhq.com/a/app"})
    assert not launcher._v2_supported_ats({"application_url": "https://jobs.lever.co/a/1"})


def test_v2_supported_ats_allowlist_scopes_independently(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    monkeypatch.setenv("APPLYPILOT_V2_ATS", "greenhouse,ashby")
    assert launcher._v2_supported_ats({"application_url": "https://jobs.ashbyhq.com/a/app"})
    assert not launcher._v2_supported_ats({"application_url": "https://jobs.lever.co/a/1"})   # lever not in list


def test_v2_supported_ats_lever_in_allowlist(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    monkeypatch.setenv("APPLYPILOT_V2_ATS", "greenhouse,ashby,lever")
    assert launcher._v2_supported_ats({"application_url": "https://jobs.lever.co/a/1"})


def test_v2_supported_ats_off_when_engine_disabled(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_V2_ENGINE", raising=False)
    monkeypatch.setenv("APPLYPILOT_V2_ATS", "greenhouse,ashby,lever")
    assert not launcher._v2_supported_ats({"application_url": "https://jobs.ashbyhq.com/a/app"})


def test_v2_supported_ats_unknown_ats_never_supported(monkeypatch):
    # An ATS with no front-end (parser_for None) is never supported even if the
    # operator lists it — routing must never claim an ATS v2 cannot parse.
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    monkeypatch.setenv("APPLYPILOT_V2_ATS", "greenhouse,workday")
    assert not launcher._v2_supported_ats({"application_url": "https://acme.myworkdayjobs.com/x"})


def test_v2_supported_ats_whitespace_and_empty_allowlist(monkeypatch):
    # A whitespace/blank allowlist collapses to the greenhouse-only default.
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    monkeypatch.setenv("APPLYPILOT_V2_ATS", "  ,  ")
    assert launcher._v2_supported_ats({"application_url": "https://boards.greenhouse.io/a/jobs/1"})
    assert not launcher._v2_supported_ats({"application_url": "https://jobs.lever.co/a/1"})
