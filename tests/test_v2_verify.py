from applypilot import database as db
from applypilot.apply.v2 import verify
from applypilot.apply.v2 import mapping_cache as mc


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


class _Resp:
    def __init__(self, method, url, status):
        self.request = type("R", (), {"method": method})()
        self.url = url
        self.status = status


def test_submit_post_classified_over_analytics_and_resume_parse():
    # The application-submit POST is the success signal; analytics / resume-parse
    # / validation XHR are NOT (invariant 8).
    assert verify.is_submit_post("POST", "https://boards.greenhouse.io/acme/applications", 200)
    assert not verify.is_submit_post("POST", "https://www.google-analytics.com/collect", 200)
    assert not verify.is_submit_post("POST", "https://boards.greenhouse.io/acme/resume/parse", 200)
    assert not verify.is_submit_post("GET", "https://boards.greenhouse.io/acme/applications", 200)
    assert not verify.is_submit_post("POST", "https://boards.greenhouse.io/acme/applications", 422)  # rejected


def test_network_recorder_captures_submit_and_ignores_noise():
    rec = verify.NetworkEvidence(ats="greenhouse", company="acme")
    rec.on_response(_Resp("POST", "https://www.google-analytics.com/collect", 200))
    rec.on_response(_Resp("POST", "https://boards.greenhouse.io/acme/resume/parse", 200))
    assert rec.submitted is False
    rec.on_response(_Resp("POST", "https://boards.greenhouse.io/acme/applications", 200))
    assert rec.submitted is True
    assert rec.submit_url.endswith("/acme/applications")


def test_tier1_success_harvests_endpoint(tmp_path):
    conn = _conn(tmp_path)
    rec = verify.NetworkEvidence(ats="greenhouse", company="acme")
    rec.on_response(_Resp("POST", "https://boards.greenhouse.io/acme/applications", 201))
    v = verify.verify(rec, conn=conn, dom_signals=None)
    assert v.verified is True and v.tier == 1
    eps = mc.get_submit_endpoints(conn, "greenhouse", "acme")
    assert eps and "applications" in eps[0]["url_pattern"]   # auto-harvested


def test_tier2_dom_fallback_when_no_network_evidence(tmp_path):
    conn = _conn(tmp_path)
    rec = verify.NetworkEvidence(ats="greenhouse", company="acme")   # never saw a submit POST
    # DOM says: confirmation present, url changed, submit gone, no errors, required ok.
    dom = verify.DomSignals(has_confirmation=True, url_changed=True, submit_gone=True,
                            submit_disabled=False, no_validation_errors=True, required_ok=True)
    v = verify.verify(rec, conn=conn, dom_signals=dom, verify_threshold=0.75)
    assert v.verified is True and v.tier == 2
    assert v.confidence >= 0.75                              # from _compute_verification_verdict
    assert mc.get_submit_endpoints(conn, "greenhouse", "acme") == []  # no harvest without net evidence


def test_ambiguous_is_needs_review(tmp_path):
    conn = _conn(tmp_path)
    rec = verify.NetworkEvidence(ats="greenhouse", company="acme")
    dom = verify.DomSignals(has_confirmation=False, url_changed=False, submit_gone=False,
                            submit_disabled=False, no_validation_errors=False, required_ok=True)
    v = verify.verify(rec, conn=conn, dom_signals=dom, verify_threshold=0.75)
    assert v.verified is False and v.needs_review is True    # neither tier confirmed
