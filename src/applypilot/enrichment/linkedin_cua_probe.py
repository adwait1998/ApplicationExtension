"""Experimental CUA probe for LinkedIn outbound URL resolution.

This is intentionally isolated from live apply and the default resolver. It
builds a Responses API computer-use request for resolver-only navigation, with
a narrow LinkedIn allowlist and no form submission flow.

Reference: https://platform.openai.com/docs/guides/tools-computer-use
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import urlparse

import httpx

from applypilot.enrichment.linkedin_outbound import is_linkedin_job_url

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
CUA_MODEL = "computer-use-preview"


@dataclass(frozen=True)
class CUAProbeConfig:
    display_width: int = 1024
    display_height: int = 768
    max_steps: int = 8
    allow_domains: tuple[str, ...] = ("linkedin.com", "www.linkedin.com")


def _domain_allowed(url: str, allow_domains: tuple[str, ...]) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in allow_domains)


def build_initial_cua_payload(url: str, config: CUAProbeConfig | None = None) -> dict:
    """Build the initial Responses API payload for an outbound-link-only probe."""
    cfg = config or CUAProbeConfig()
    if not is_linkedin_job_url(url):
        raise ValueError("CUA LinkedIn probe only accepts linkedin.com/jobs URLs")
    if not _domain_allowed(url, cfg.allow_domains):
        raise ValueError("CUA LinkedIn probe URL is outside the allowlist")

    return {
        "model": CUA_MODEL,
        "tools": [
            {
                "type": "computer_use_preview",
                "display_width": cfg.display_width,
                "display_height": cfg.display_height,
                "environment": "browser",
            }
        ],
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Resolver-only task. Open this LinkedIn job URL and identify whether the "
                            "main Apply control leads to a non-LinkedIn company or ATS URL. "
                            "Do not use Easy Apply. Do not fill forms. Do not submit anything. "
                            "Stop after finding the outbound URL or deciding it is Easy Apply only, "
                            f"expired, login blocked, captcha blocked, or unknown. URL: {url}"
                        ),
                    }
                ],
            }
        ],
        "reasoning": {"summary": "concise"},
        "truncation": "auto",
    }


def build_safety_acknowledgement_payload(
    *,
    previous_response_id: str,
    call_id: str,
    pending_safety_checks: list[dict],
    screenshot_url: str,
    config: CUAProbeConfig | None = None,
) -> dict:
    """Build a follow-up payload that acknowledges CUA safety checks.

    The caller still owns the human-in-the-loop decision. This helper only
    shapes the API request after the operator has reviewed the warning.
    """
    cfg = config or CUAProbeConfig()
    return {
        "model": CUA_MODEL,
        "previous_response_id": previous_response_id,
        "tools": [
            {
                "type": "computer_use_preview",
                "display_width": cfg.display_width,
                "display_height": cfg.display_height,
                "environment": "browser",
            }
        ],
        "input": [
            {
                "type": "computer_call_output",
                "call_id": call_id,
                "acknowledged_safety_checks": pending_safety_checks,
                "output": {
                    "type": "computer_screenshot",
                    "image_url": screenshot_url,
                },
            }
        ],
        "truncation": "auto",
    }


def start_cua_probe(url: str, *, api_key: str | None = None, config: CUAProbeConfig | None = None) -> dict:
    """Start the isolated CUA probe.

    This does not execute returned computer actions. A future Ralph iteration
    can compare yield/cost/latency by adding a sandbox browser action loop.
    """
    if os.environ.get("APPLYPILOT_ENABLE_CUA_SPIKE") != "1":
        raise RuntimeError("Set APPLYPILOT_ENABLE_CUA_SPIKE=1 to run the experimental CUA probe.")
    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required for the experimental CUA probe.")

    payload = build_initial_cua_payload(url, config=config)
    with httpx.Client(timeout=60) as client:
        response = client.post(
            OPENAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        return response.json()
