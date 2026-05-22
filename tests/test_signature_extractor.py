"""Tests for applypilot.loop.signature_extractor.

Same input → same signature, always. Different signatures should differ.
"""
from __future__ import annotations

from applypilot.loop import signature_extractor as sig


def test_applied_row_returns_none():
    row = {"apply_status": "applied", "last_failure_class": None, "site": "figma"}
    assert sig.extract(row, None, None) is None


def test_irreducible_failure_class_passes_through():
    row = {
        "apply_status": "needs_review:captcha",
        "last_failure_class": "captcha",
        "site": "lever_acme",
        "url": "https://acme.com/x",
    }
    assert sig.extract(row, None, None) == "captcha:acme"


def test_transient_timeout_prefill_signature():
    row = {
        "apply_status": "needs_review:timeout",
        "last_failure_class": "transient_timeout",
        "site": "greenhouse_figma",
        "url": "https://boards.greenhouse.io/figma/jobs/123",
        "apply_error": "prefill Page.goto timed out after 10s",
    }
    assert sig.extract(row, None, None) == "transient_timeout:prefill_goto:greenhouse"


def test_no_result_line_signature_includes_model():
    row = {
        "apply_status": "needs_review:no_result_line",
        "last_failure_class": "no_result_line",
        "site": "greenhouse_figma",
        "url": "https://boards.greenhouse.io/figma/jobs/123",
        "model": "claude-haiku-4-5-20251001",
    }
    assert sig.extract(row, None, None) == "no_result_line:haiku"


def test_unverified_submission_signature():
    row = {
        "apply_status": "needs_review:unverified_submission",
        "last_failure_class": "unverified_submission",
        "site": "lever_acme",
        "url": "https://jobs.lever.co/acme/abc",
    }
    assert sig.extract(row, None, None) == "unverified_submission:lever"


def test_react_select_clobber_extracts_field():
    row = {
        "apply_status": "needs_review:validation",
        "last_failure_class": "react_select_clobber",
        "site": "greenhouse_figma",
        "url": "https://boards.greenhouse.io/figma/jobs/123",
        "apply_error": "react_select_clobber:work_authorization on field 7",
    }
    assert sig.extract(row, None, None) == "react_select_clobber:work_authorization:greenhouse"


def test_same_inputs_produce_same_signature():
    row = {
        "apply_status": "needs_review:timeout",
        "last_failure_class": "transient_timeout",
        "site": "greenhouse_figma",
        "url": "https://boards.greenhouse.io/figma/jobs/123",
        "apply_error": "prefill Page.goto timed out after 10s",
    }
    assert sig.extract(row, None, None) == sig.extract(row, None, None)


def test_unknown_class_falls_back_to_class_plus_ats():
    row = {
        "apply_status": "needs_review:weird",
        "last_failure_class": "weird_new_class",
        "site": "greenhouse_acme",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
    }
    assert sig.extract(row, None, None) == "weird_new_class:greenhouse"


def test_missing_class_returns_none():
    row = {
        "apply_status": "needs_review:unknown",
        "last_failure_class": None,
        "site": "figma",
    }
    assert sig.extract(row, None, None) is None


def test_ats_token_falls_back_to_site_prefix():
    # URL doesn't match any known host; site prefix is used.
    row = {
        "apply_status": "needs_review:timeout",
        "last_failure_class": "transient_timeout",
        "site": "ashby_someco",
        "url": "https://someco.example/apply",
        "apply_error": "prefill goto failed",
    }
    assert sig.extract(row, None, None) == "transient_timeout:prefill_goto:ashby"
