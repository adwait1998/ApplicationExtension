from applypilot.gate.rules import location_rule
from applypilot.gate import Verdict

# geo policy: remote-US ok; onsite only in CA / Seattle / NYC
POLICY = {"remote_ok": True, "remote_scope": "US",
          "onsite_regions": ["us-ca", "us-wa-seattle", "us-ny-nyc"]}


def test_ca_substring_bug_is_dead():
    # "ca" must NOT match "appliCAtions" — word-boundary only
    v = location_rule("Applications Engineer role", POLICY, is_title_field=False)
    assert v.code != "location_ok"  # not spuriously accepted via substring


def test_remote_us_accepts():
    assert location_rule("Remote - United States", POLICY).result == "PASS"


def test_onsite_allowed_metro():
    assert location_rule("San Francisco, CA", POLICY).result == "PASS"


def test_onsite_rejected_metro():
    v = location_rule("Austin, TX", POLICY)
    assert v.result == "REJECT" and v.code == "location_onsite_out_of_region"


def test_non_us_remote_rejected():
    v = location_rule("Remote - EMEA", POLICY)
    assert v.result == "REJECT" and v.code == "location_remote_scope"


def test_unknown_location_is_unknown_not_pass():
    v = location_rule("", POLICY)
    assert v.result == "UNKNOWN"   # never silently PASS (the 55-parked-jobs bug)


def test_description_carveout_downgrades_to_unknown():
    v = location_rule("Remote (US)", POLICY, description="Not available to residents of CA or NY.")
    assert v.result == "UNKNOWN" and "carveout" in v.code


def test_no_substring_patterns_leak():
    from applypilot.gate import gazetteer as gz
    assert not gz.word_match("ca", "applications")
    assert not gz.word_match("or", "coordinator")
