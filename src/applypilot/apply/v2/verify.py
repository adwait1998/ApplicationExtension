"""Verification (spec §6.7). Tier 1 = PASSIVE network evidence (the application
submit POST, read status+url only — never route.fetch, invariant 8); harvests
the endpoint signature on confirmed success. Tier 2 = DOM verdict cores REUSED
unchanged from the v1 verifier. Tier 1 preferred; ambiguity -> needs_review."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from applypilot.apply.v2 import mapping_cache as mc

log = logging.getLogger(__name__)

# Reuse the safety kernel's request-time submit classifier so "the application
# submit" is defined ONCE across the codebase. browser_stream._guard consumes the
# one-shot broker ticket on exactly is_submit_request(method, url); the passive
# success signal below layers a 2xx/3xx status check on the same predicate.
try:
    from applypilot.apply.browser_stream import is_submit_request
except Exception:                                # pragma: no cover - import guard
    _ATS_HOST_RE = re.compile(
        r"(greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com)$", re.I)
    _MUTATION = {"POST", "PUT", "PATCH"}
    _NOT_SUBMIT = re.compile(r"/(resume|cv)/?parse|/validate|analytics|/collect|/track|/log", re.I)
    _SUBMIT_HINT = re.compile(r"/applications?\b|/apply\b|/submit\b", re.I)

    def is_submit_request(method: str, url: str) -> bool:
        if (method or "").upper() not in _MUTATION:
            return False
        m = re.match(r"https?://([^/:]+)", url or "")
        host = (m.group(1) if m else "").lower()
        if not _ATS_HOST_RE.search(host):
            return False
        if _NOT_SUBMIT.search(url or ""):
            return False
        return bool(_SUBMIT_HINT.search(url or ""))


def is_submit_post(method: str, url: str, status: int) -> bool:
    """True only for the language-independent success signal: a mutating request
    to an ATS host that hits the application-submit path (is_submit_request),
    succeeded (2xx/3xx), and is NOT a resume-parse / analytics / validation XHR
    (invariant 8)."""
    try:
        code = int(status)
    except (TypeError, ValueError):
        return False
    if not (200 <= code < 400):                      # rejected/validation != success
        return False
    return is_submit_request(method, url)


class NetworkEvidence:
    """Attach on_response as a PASSIVE context listener (context.on('response',
    ev.on_response)). Reads status+url only; never blocks or refetches."""

    def __init__(self, *, ats: str, company: str):
        self.ats = ats
        self.company = company
        self.submitted = False
        self.submit_url: str | None = None
        self.submit_method: str | None = None

    def on_response(self, response) -> None:
        try:
            method = getattr(response.request, "method", "")
            url = getattr(response, "url", "")
            status = getattr(response, "status", 0)
        except Exception:
            return
        if is_submit_post(method, url, status):
            self.submitted = True
            self.submit_url = url
            self.submit_method = (method or "POST").upper()


@dataclass
class DomSignals:
    has_confirmation: bool = False
    url_changed: bool = False
    submit_gone: bool = False
    submit_disabled: bool = False
    no_validation_errors: bool = False
    required_ok: bool = False


@dataclass
class VerifyResult:
    verified: bool
    tier: int | None = None
    confidence: float = 0.0
    needs_review: bool = False
    submit_url: str | None = None


def _url_pattern(url: str) -> str:
    """Harvest signature = host+path, query/fragment stripped (matches the
    submit_endpoints.url_pattern column semantics)."""
    m = re.match(r"https?://([^?#]+)", url or "")
    return (m.group(1) if m else url or "").rstrip("/")


def verify(evidence: NetworkEvidence, *, conn=None, dom_signals: DomSignals | None,
           verify_threshold: float = 0.75) -> VerifyResult:
    # TIER 1: network evidence (language-independent, strongest). Auto-harvest
    # the observed endpoint — Tier-1-only, since we actually saw the POST.
    # The harvest is a best-effort cache side-effect and MUST NOT sink an
    # already-confirmed verdict: a lost 'applied' result could cascade into a
    # re-apply (double-issue, a Critical invariant). So isolate any cache-write
    # failure (sqlite locked / disk error / schema drift) — log and continue.
    if evidence.submitted and evidence.submit_url:
        if conn is not None:
            try:
                mc.record_submit_endpoint(conn, evidence.ats, evidence.company,
                                          evidence.submit_method or "POST",
                                          _url_pattern(evidence.submit_url))
            except Exception:                        # best-effort harvest only
                log.warning("submit-endpoint harvest failed for %s/%s; verdict "
                            "stands", evidence.ats, evidence.company, exc_info=True)
        return VerifyResult(True, tier=1, confidence=1.0, submit_url=evidence.submit_url)

    # TIER 2: DOM verdict core, REUSED unchanged. No harvest here — no observed
    # POST means no endpoint we can trust.
    if dom_signals is not None:
        from applypilot.apply.launcher import _compute_verification_verdict
        confidence, verified = _compute_verification_verdict(
            has_confirmation=dom_signals.has_confirmation,
            url_changed=dom_signals.url_changed,
            submit_gone=dom_signals.submit_gone,
            submit_disabled=dom_signals.submit_disabled,
            no_validation_errors=dom_signals.no_validation_errors,
            required_ok=dom_signals.required_ok,
            verify_threshold=verify_threshold,
        )
        if verified:
            return VerifyResult(True, tier=2, confidence=confidence)
        return VerifyResult(False, tier=2, confidence=confidence, needs_review=True)

    # Neither tier confirmed: never a silent pass.
    return VerifyResult(False, needs_review=True)
