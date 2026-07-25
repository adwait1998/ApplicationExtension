from applypilot.apply.v2 import preflight as pf
from applypilot.apply.browser_stream import BrowserObservation, ControlObservation


def _obs(**kw):
    return BrowserObservation(**kw)


def test_classifies_ats_kind_from_url():
    r = pf.classify(intended_url="https://boards.greenhouse.io/acme/jobs/1",
                    landed_url="https://boards.greenhouse.io/acme/jobs/1",
                    obs=_obs(page_text_sample="Apply for this job", submit_buttons=[ControlObservation()]))
    assert r.ats_kind == "greenhouse"
    assert not r.job_expired and not r.login_wall
    assert r.terminal is None                     # clean -> proceed to parse


def test_expired_redirect_is_a_named_terminal():
    r = pf.classify(intended_url="https://boards.greenhouse.io/acme/jobs/1",
                    landed_url="https://job-boards.greenhouse.io/acme?error=true",
                    obs=_obs(page_text_sample="Open roles at Acme"))
    assert r.job_expired is True
    assert r.terminal == "failed:expired"         # bucket B, one named family


def test_login_wall_detected():
    obs = _obs(page_text_sample="Sign in to continue to your application",
               controls=[ControlObservation(control_type="password")])
    r = pf.classify(intended_url="u", landed_url="u", obs=obs)
    assert r.login_wall is True and r.terminal == "login_issue"


def test_captcha_detected():
    obs = _obs(page_text_sample="Please verify you are human",
               controls=[ControlObservation(selector='iframe[src*="recaptcha"]')])
    r = pf.classify(intended_url="u", landed_url="u", obs=obs)
    assert r.captcha_present is True and r.terminal == "captcha"


def test_sso_gate_detected():
    obs = _obs(page_text_sample="Continue with Google  Continue with Okta single sign-on")
    r = pf.classify(intended_url="u", landed_url="u", obs=obs)
    assert r.sso_gate is True                       # sso is a login-family terminal
    assert r.terminal == "login_issue"


def test_form_frame_path_from_iframe_embed():
    obs = _obs(submit_buttons=[ControlObservation(frame_index=1, frame_url="https://boards.greenhouse.io/embed/job_app?token=abc")],
               controls=[ControlObservation(frame_index=1, frame_url="https://boards.greenhouse.io/embed/job_app?token=abc")])
    r = pf.classify(intended_url="u", landed_url="u", obs=obs)
    assert r.form_frame_path == ("https://boards.greenhouse.io/embed/job_app",)  # query stripped


def test_probe_live_wraps_collect(monkeypatch):
    # probe(page, ...) collects an observation then classifies; a collect failure
    # returns a benign result (terminal=None) so the caller falls through to parse
    # (which owns its own fail-open) rather than the probe hard-failing.
    class _P:
        url = "https://boards.greenhouse.io/acme/jobs/1"
    monkeypatch.setattr(pf, "collect_browser_observation",
                        lambda page, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = pf.probe(_P(), intended_url=_P.url)
    assert r.terminal is None and r.ats_kind == "greenhouse"
