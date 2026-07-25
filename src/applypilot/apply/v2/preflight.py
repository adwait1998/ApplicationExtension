"""Pre-flight probe (spec §6.2): classify a navigated page BEFORE any parse work
into {ats_kind, login_wall, captcha_present, sso_gate, job_expired,
form_frame_path}. Pure over a BrowserObservation + the intended/landed URLs; the
live wrapper collect-then-classifies. Terminal states map to the SAME status
vocabulary the launcher already promotes (failed:expired / captcha /
login_issue). The expired-redirect guard (freshness.is_greenhouse_expired_redirect)
is folded in as ONE member of the named terminal family — the probe is the single
'don't even parse this' authority."""
from __future__ import annotations

import re
from dataclasses import dataclass

from applypilot.apply.browser_stream import collect_browser_observation
from applypilot.apply.prefill import _detect_ats
from applypilot.freshness import is_greenhouse_expired_redirect

_CAPTCHA_RE = re.compile(r"recaptcha|hcaptcha|are you human|verify you are|cf-challenge|turnstile", re.I)
_LOGIN_RE = re.compile(r"\bsign in\b|\blog in\b|\blogin\b|create an account|password", re.I)
_SSO_RE = re.compile(r"single sign-on|continue with (google|okta|microsoft|azure|sso)|saml", re.I)


@dataclass
class ProbeResult:
    ats_kind: str = "unsupported"
    login_wall: bool = False
    captcha_present: bool = False
    sso_gate: bool = False
    job_expired: bool = False
    form_frame_path: tuple[str, ...] = ()
    terminal: str | None = None          # failed:expired | captcha | login_issue | None


def _stable_frame(url: str) -> str:
    return (url or "").split("?", 1)[0].split("#", 1)[0]


def classify(*, intended_url: str, landed_url: str, obs) -> ProbeResult:
    r = ProbeResult(ats_kind=_detect_ats(landed_url or intended_url or ""))
    text = (getattr(obs, "page_text_sample", "") or "")
    controls = list(getattr(obs, "controls", []) or [])
    submits = list(getattr(obs, "submit_buttons", []) or [])

    # (1) expired: the conservative soft-302 signature (bucket B, irreducible).
    if is_greenhouse_expired_redirect(intended_url, landed_url):
        r.job_expired = True
        r.terminal = "failed:expired"
        return r                              # nothing else matters — the req is dead

    # (2) captcha: text markers OR a captcha iframe among the controls.
    if _CAPTCHA_RE.search(text) or any(_CAPTCHA_RE.search(c.selector or "") for c in controls):
        r.captcha_present = True
        r.terminal = "captcha"
        return r

    # (3) sso gate: an explicit SSO prompt (a login-family terminal).
    if _SSO_RE.search(text):
        r.sso_gate = True
        r.terminal = "login_issue"
        return r

    # (4) login wall: a password field OR a sign-in prompt with NO application form.
    has_password = any((c.control_type or "").lower() == "password" for c in controls)
    if has_password or (_LOGIN_RE.search(text) and not submits):
        r.login_wall = True
        r.terminal = "login_issue"
        return r

    # (5) form frame path: the (stable) frame the submit/controls live in, if embedded.
    for c in submits + controls:
        if getattr(c, "frame_index", 0) and getattr(c, "frame_url", ""):
            r.form_frame_path = (_stable_frame(c.frame_url),)
            break
    return r                                  # terminal is None -> proceed to parse


def probe(page, *, intended_url: str | None = None) -> ProbeResult:
    """Live wrapper: collect an observation, then classify. A collect failure is
    benign — return a non-terminal result so the caller falls through to parse
    (which owns its own fail-open, invariant 2). ~2s budget: this is one
    observation collect, no dropdown opens, no fill."""
    landed = getattr(page, "url", "") or (intended_url or "")
    try:
        obs = collect_browser_observation(page)
    except Exception:                          # noqa: BLE001 — never hard-fail the probe
        return ProbeResult(ats_kind=_detect_ats(landed))
    return classify(intended_url=intended_url or landed, landed_url=landed, obs=obs)
