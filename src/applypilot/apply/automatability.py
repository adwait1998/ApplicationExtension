"""Reliability-v2 Phase E: automatability gate + human-in-the-loop queue.

Some pages are economically NON-automatable: a visible CAPTCHA, an
email-verification wall, an SSO redirect, an anti-bot interstitial.
Spending an LLM apply attempt at one of those just burns money + rate
limit and still fails (the (B)-irreducible class). Detect the blocker
BEFORE spawning the agent, mark the job needs_human, append it to a
review queue the operator can work, and move on.

`classify_blocker` is pure → unit-tested at $0. `scan_page_for_blocker`
is a thin Playwright extractor (synthetic-DOM tested). The launcher
calls `automatability_gate` after prefill, before the LLM spawn —
fail-OPEN: any scanner error means proceed (never block a good apply).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page

log = logging.getLogger(__name__)

# SSO/OAuth hosts we must never try to log into.
_SSO_HOSTS = (
    "accounts.google.com", "login.microsoftonline.com", "login.live.com",
    "okta.com", "auth0.com", "onelogin.com", "pingidentity.com",
    "github.com/login", "appleid.apple.com",
)
_ANTIBOT_TEXT = (
    "checking your browser", "verify you are human", "are you a robot",
    "press & hold", "press and hold", "complete the security check",
    "unusual traffic", "enable javascript and cookies to continue",
    "ddos protection by", "cf-challenge",
)
_EMAILVERIFY_TEXT = (
    "enter the verification code", "we sent a code", "we've sent a code",
    "verification code we sent", "enter the 6-digit", "enter the 8-character",
    "check your email for a code", "confirm your email to continue",
    "a code has been sent to your email", "security code sent to",
)


def classify_blocker(signals: dict) -> str | None:
    """Pure decision over a small DOM-signal dict. Returns one of
    'sso' | 'captcha' | 'email_verification' | 'anti_bot' | 'login_wall',
    or None when the page looks automatable. Priority: SSO and anti-bot
    first (hard walls), then captcha, then email-verification, then a
    bare login wall."""
    if signals.get("sso_host"):
        return "sso"
    if signals.get("anti_bot"):
        return "anti_bot"
    if signals.get("captcha_visible"):
        return "captcha"
    if signals.get("email_verification"):
        return "email_verification"
    if signals.get("login_wall"):
        return "login_wall"
    return None


_SIGNAL_JS = r"""
() => {
  const lower = (document.body ? document.body.innerText : '')
    .replace(/\s+/g, ' ').trim().toLowerCase();
  const has = sel => !!document.querySelector(sel);
  // CAPTCHA: only count if a widget is actually present (invisible
  // recaptcha v3 with no challenge is fine — the agent/submit handles it).
  const recaptchaV2 = !!document.querySelector(
    'iframe[src*="recaptcha/api2/anchor"], iframe[src*="recaptcha/enterprise/anchor"], .g-recaptcha[data-sitekey]');
  const hcaptcha = has('iframe[src*="hcaptcha.com"], .h-captcha');
  const turnstile = has('iframe[src*="challenges.cloudflare.com"], .cf-turnstile');
  const captchaChallengeText = /(select all (images|squares)|i'?m not a robot|verify you are human)/i.test(lower);
  // login wall = a password field AND no file/resume input (i.e. not the
  // application form itself, which often has neither).
  const hasPassword = has('input[type="password"]');
  const hasFile = has('input[type="file"]');
  const hasAppFields = has('#first_name, input[name*="first" i], input[name="resume"], textarea');
  return {
    text: lower.slice(0, 4000),
    url: window.location.href,
    captcha_widget: recaptchaV2 || hcaptcha || turnstile,
    captcha_text: captchaChallengeText,
    has_password: hasPassword,
    has_app_fields: hasFile || hasAppFields,
  };
}
"""


def _signals_from_scan(scan: dict) -> dict:
    text = (scan.get("text") or "")
    url = (scan.get("url") or "").lower()
    return {
        "sso_host": any(h in url for h in _SSO_HOSTS),
        "anti_bot": any(t in text for t in _ANTIBOT_TEXT),
        "captcha_visible": bool(scan.get("captcha_widget")) or bool(scan.get("captcha_text")),
        "email_verification": any(t in text for t in _EMAILVERIFY_TEXT),
        "login_wall": bool(scan.get("has_password")) and not bool(scan.get("has_app_fields")),
    }


def scan_page_for_blocker(page: "Page") -> str | None:
    """Scan the live page (all frames) and classify any hard blocker.
    Fail-OPEN: returns None on any error — never block a good apply."""
    agg = {"text": "", "url": "", "captcha_widget": False,
           "captcha_text": False, "has_password": False, "has_app_fields": False}
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    for fr in frames[:12]:
        try:
            s = fr.evaluate(_SIGNAL_JS)
        except Exception:
            continue
        if not isinstance(s, dict):
            continue
        agg["text"] += " " + (s.get("text") or "")
        agg["captcha_widget"] |= bool(s.get("captcha_widget"))
        agg["captcha_text"] |= bool(s.get("captcha_text"))
        agg["has_password"] |= bool(s.get("has_password"))
        agg["has_app_fields"] |= bool(s.get("has_app_fields"))
    try:
        agg["url"] = page.url
    except Exception:
        pass
    return classify_blocker(_signals_from_scan(agg))


def queue_for_human(job: dict, reason: str, *, path: str | Path) -> None:
    """Append the job to the human-review queue (additive JSONL)."""
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "job_url": job.get("url"),
        "apply_url": job.get("application_url") or job.get("url"),
        "title": job.get("title"),
        "site": job.get("site"),
        "reason": reason,
    }
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        log.exception("failed to write human queue")


def automatability_gate(cdp_port: int, job: dict, *, queue_path: str | Path
                        ) -> str | None:
    """Connect to the live Chrome, scan for a hard blocker. If found:
    queue the job for a human and return the reason (caller short-circuits
    to needs_human WITHOUT spawning the LLM). None → automatable, proceed.
    Fail-OPEN on any connection/scan error."""
    pw = browser = None
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{cdp_port}", timeout=3000)
        pages = [p for ctx in browser.contexts for p in ctx.pages]
        page = pages[-1] if pages else None
        if page is None:
            return None
        reason = scan_page_for_blocker(page)
        if reason:
            queue_for_human(job, reason, path=queue_path)
        return reason
    except Exception as e:
        log.debug("automatability_gate failed-open: %s", e)
        return None
    finally:
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if pw is not None:
                pw.stop()
        except Exception:
            pass
