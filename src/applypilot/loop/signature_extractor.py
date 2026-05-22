"""Deterministic signature extraction from a failed apply row.

Same root cause across runs → same signature. This is the key the loop
uses to gate Branch D (patching) at signature_counts >= 3.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


# Map URL host substrings to a stable ATS shorthand.
_HOST_PATTERNS: tuple[tuple[str, str], ...] = (
    ("boards.greenhouse.io", "greenhouse"),
    ("greenhouse.io", "greenhouse"),
    ("jobs.lever.co", "lever"),
    ("lever.co", "lever"),
    ("jobs.ashbyhq.com", "ashby"),
    ("ashbyhq.com", "ashby"),
    ("myworkdaysite.com", "workday"),
    ("workday.com", "workday"),
    ("linkedin.com", "linkedin"),
    ("indeed.com", "indeed"),
)


def _ats_token(row: dict[str, Any]) -> str:
    """Extract a stable ATS / host token from the row's URL or site."""
    url = row.get("url") or ""
    host = ""
    if url:
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            host = ""
    for pattern, token in _HOST_PATTERNS:
        if pattern in host:
            return token
    # Fall back to site prefix (e.g. "greenhouse_figma" → "greenhouse").
    site = (row.get("site") or "").lower()
    if "_" in site:
        return site.split("_", 1)[0]
    return site or "unknown"


def _company_from_site(row: dict[str, Any]) -> str:
    """Best-effort company slug from `site`."""
    site = (row.get("site") or "").lower()
    if "_" in site:
        return site.split("_", 1)[1]
    return site or "unknown"


def _model_short(row: dict[str, Any]) -> str:
    model = (row.get("model") or "").lower()
    if "haiku" in model:
        return "haiku"
    if "sonnet" in model:
        return "sonnet"
    if "opus" in model:
        return "opus"
    return "unknown"


def _classify_transient_phase(row: dict[str, Any]) -> str:
    err = (row.get("apply_error") or "").lower()
    if "prefill" in err and "goto" in err:
        return "prefill_goto"
    if "page.goto" in err or "page_load" in err:
        return "page_load"
    if "agent" in err or "claude" in err:
        return "agent_turn"
    return "unknown"


def extract(
    review_row: dict[str, Any],
    transcript_path: Path | None,
    verify_json_path: Path | None,
) -> str | None:
    """Return a deterministic signature string for a failed attempt.

    Returns None for status == 'applied'. Otherwise returns a slug like
    'transient_timeout:prefill_goto:greenhouse' that the loop uses to
    accumulate evidence and decide whether to patch.

    transcript_path and verify_json_path are accepted for forward
    compatibility but not yet consulted — the row's `last_failure_class`
    and `apply_error` are sufficient for the current taxonomy.
    """
    status = review_row.get("apply_status") or ""
    if status == "applied":
        return None

    cls = (review_row.get("last_failure_class") or "").strip()
    if not cls:
        return None  # Cannot classify — loop will skip.

    ats = _ats_token(review_row)

    if cls == "transient_timeout":
        phase = _classify_transient_phase(review_row)
        return f"transient_timeout:{phase}:{ats}"

    if cls == "no_result_line":
        return f"no_result_line:{_model_short(review_row)}"

    if cls == "unverified_submission":
        return f"unverified_submission:{ats}"

    if cls == "validation_react_select" or "react_select" in cls:
        err = review_row.get("apply_error") or ""
        m = re.search(r"react_select\w*:([\w_]+)", err)
        field = m.group(1) if m else "unknown"
        return f"react_select_clobber:{field}:{ats}"

    # Irreducible classes — pass through with company suffix; the loop's
    # routing layer treats them as (B)-irreducible and skips patching anyway.
    if cls in {"captcha", "email_verification", "sso_required", "expired",
               "not_eligible", "cloudflare", "account_required"}:
        company = _company_from_site(review_row)
        return f"{cls}:{company}"

    # Unknown class — return verbatim with ATS suffix.
    return f"{cls}:{ats}"
