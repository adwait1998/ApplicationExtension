"""Reliability-v2 Phase A: report summarizer is a pure function — test at $0
with synthetic review.jsonl rows. No network, no LLM, no DB.
"""
from __future__ import annotations

from applypilot.reporting import (
    classify_failure_bucket,
    summarize_review,
    format_report,
    load_review_rows,
)


def _row(**kw):
    base = {"dry_run": False, "status": "applied", "prefill_ats": "greenhouse",
            "tier_used": "legacy_llm", "cost_usd": 1.0, "cache_read": 900,
            "cache_create": 50, "input_tokens": 50}
    base.update(kw)
    return base


def test_classify_removable_vs_irreducible():
    # (B) irreducible — environment/eligibility
    assert classify_failure_bucket(_row(status="failed", failure_class="blocker_captcha")) == "B"
    assert classify_failure_bucket(_row(status="needs_review", failure_class="blocker_email_verification_required")) == "B"
    assert classify_failure_bucket(_row(status="failed", failure_class="policy_sso_required")) == "B"
    assert classify_failure_bucket(_row(status="expired", failure_class=None, error="expired")) == "B"
    # (A) removable — our stochastic agent / pipeline
    assert classify_failure_bucket(_row(status="needs_review", failure_class="transient_timeout")) == "A"
    assert classify_failure_bucket(_row(status="failed", failure_class="validation_skill_flow_attributeerror")) == "A"
    assert classify_failure_bucket(_row(status="needs_review", failure_class="transient_no_result_line")) == "A"
    # successes / non-failures → None
    assert classify_failure_bucket(_row(status="applied")) is None
    assert classify_failure_bucket(_row(status="skipped")) is None
    assert classify_failure_bucket(_row(status="dry_run:applied", dry_run=True)) is None


def test_summary_core_metrics():
    rows = [
        _row(status="applied", cost_usd=1.20),
        _row(status="applied", cost_usd=0.80, prefill_ats="ashby", tier_used="skill_replay"),
        _row(status="needs_review", failure_class="transient_timeout", cost_usd=1.50),
        _row(status="failed", failure_class="blocker_captcha", cost_usd=0.10),
        _row(status="failed", failure_class="validation_skill_flow_attributeerror", cost_usd=0.0),
        _row(status="dry_run:applied", dry_run=True, cost_usd=0.0),  # excluded
    ]
    s = summarize_review(rows)
    assert s["live_attempts"] == 5            # dry_run excluded
    assert s["applied"] == 2
    assert s["pass_rate"] == 0.4
    assert s["fail_A_removable"] == 2          # timeout + skill_flow_attributeerror
    assert s["fail_B_irreducible"] == 1        # captcha
    assert s["removable_share"] == round(2 / 3, 3)
    assert s["total_cost_usd"] == 3.60
    assert s["cost_per_apply_usd"] == round(3.60 / 5, 3)
    # cache hit rate = cr / (cr+cc+it) summed
    assert s["cache_hit_rate"] is not None and 0 < s["cache_hit_rate"] < 1
    assert s["by_ats"]["greenhouse"]["n"] == 4
    assert s["by_ats"]["ashby"]["applied"] == 1
    assert s["by_tier"]["skill_replay"]["applied"] == 1


def test_summary_handles_missing_telemetry_gracefully():
    # Old rows with no cost/cache fields must not crash; cost_rows=0.
    rows = [{"dry_run": False, "status": "applied", "site": "figma (greenhouse)"},
            {"dry_run": False, "status": "needs_review", "failure_class": "transient_stuck"}]
    s = summarize_review(rows)
    assert s["live_attempts"] == 2
    assert s["cost_per_apply_usd"] is None
    assert s["cache_hit_rate"] is None
    assert s["fail_A_removable"] == 1
    # format must render without throwing even with n/a values
    assert "NO cost telemetry" in format_report(s)


def test_empty_and_loader(tmp_path):
    assert load_review_rows(tmp_path / "missing.jsonl") == []
    p = tmp_path / "review.jsonl"
    p.write_text('{"status":"applied","dry_run":false}\n\nnot-json\n', encoding="utf-8")
    rows = load_review_rows(p)
    assert len(rows) == 1  # blank + bad-json skipped
    s = summarize_review(rows)
    assert s["applied"] == 1


def test_adapter_slice_isolated_for_acceptance_judging():
    """Acceptance #6 must be judged on adapter-path jobs ONLY (the mixed
    Phase-F average was Workday-polluted). adapter_slice = tier_used
    starting 'greenhouse_adapter' only."""
    rows = [
        # adapter-path
        {"dry_run": False, "status": "applied", "tier_used": "greenhouse_adapter", "cost_usd": 0.90},
        {"dry_run": False, "status": "applied", "tier_used": "greenhouse_adapter_submit", "cost_usd": 0.05},
        {"dry_run": False, "status": "needs_review:timeout",
         "failure_class": "transient_timeout", "tier_used": "greenhouse_adapter"},
        # NOT adapter-path (Workday/indeed/LLM) — must be excluded from the slice
        {"dry_run": False, "status": "applied", "tier_used": "skill_record", "cost_usd": 3.50},
        {"dry_run": False, "status": "applied", "tier_used": "legacy_llm", "cost_usd": 4.20},
    ]
    s = summarize_review(rows)
    a = s["adapter_slice"]
    assert a["n"] == 3                       # only the 3 greenhouse_adapter* rows
    assert a["applied"] == 2
    assert a["fail_A_removable"] == 1        # the timeout
    # $/apply on the slice = (0.90 + 0.05) / 2 applied = 0.475 — NOT
    # dragged up by the $3.50/$4.20 Workday/LLM jobs
    assert a["cost_per_apply_usd"] == 0.475
    assert "Adapter-path (v2 acc#6)" in format_report(s)
