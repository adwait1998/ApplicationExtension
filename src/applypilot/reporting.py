"""Reliability-v2 Phase A: cost/telemetry summarizer over logs/review.jsonl.

`summarize_review` is a PURE function (list[dict] -> dict) so it unit-tests
at $0 with synthetic rows. The CLI `applypilot report` is a thin wrapper.

Core question it answers: of everything that failed, how much was
(A) REMOVABLE — our stochastic agent re-solving a stable form badly
    (transient_*, validation_*, skill_flow_*, no_result_line, stuck, ...)
vs (B) IRREDUCIBLE — environment defenses / eligibility we can't "fix"
    (CAPTCHA, email-verification, SSO, expired, not-eligible, policy_*).
Plus $/apply, cache hit-rate, and pass-rate by ATS — so reliability work
is driven by data, not anecdote.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Failure-class / reason substrings that are environment-or-eligibility,
# i.e. NOT a pipeline bug we can engineer away. Everything else that failed
# is treated as (A) removable.
_IRREDUCIBLE_MARKERS = (
    "captcha",
    "email_verification",
    "sso_required",
    "not_eligible",
    "not_a_job_application",
    "account_required",
    "expired",
    "site_blocked",
    "cloudflare",
    "blocked_by",
    "policy_",
    "unsafe_verification",
    "possible_duplicate_guard",
)

_SUCCESS_STATUSES = ("applied",)


def _is_success(row: dict) -> bool:
    s = str(row.get("status") or "")
    return s == "applied" or s.startswith("dry_run:applied")


def _is_terminal_nonfailure(row: dict) -> bool:
    """Rows that aren't a 'failure' for pass-rate purposes (skipped/paused)."""
    s = str(row.get("status") or "")
    return s in ("skipped", "paused") or s.startswith("dry_run:")


def classify_failure_bucket(row: dict) -> str | None:
    """Return 'A' (removable), 'B' (irreducible), or None (not a failure)."""
    if _is_success(row) or _is_terminal_nonfailure(row):
        return None
    blob = " ".join(
        str(row.get(k) or "").lower()
        for k in ("failure_class", "error", "status")
    )
    if not blob.strip():
        return None
    if any(m in blob for m in _IRREDUCIBLE_MARKERS):
        return "B"
    return "A"


def summarize_review(rows: list[dict]) -> dict[str, Any]:
    """Pure summary over review.jsonl rows. No I/O."""
    live = [r for r in rows if not r.get("dry_run")]
    n = len(live)
    applied = sum(1 for r in live if _is_success(r))
    a_fail = sum(1 for r in live if classify_failure_bucket(r) == "A")
    b_fail = sum(1 for r in live if classify_failure_bucket(r) == "B")

    cost = sum(float(r.get("cost_usd") or 0) for r in live)
    cost_rows = [r for r in live if r.get("cost_usd") is not None]
    cr = sum(int(r.get("cache_read") or 0) for r in live)
    cc = sum(int(r.get("cache_create") or 0) for r in live)
    it = sum(int(r.get("input_tokens") or 0) for r in live)
    cache_denom = cr + cc + it

    by_ats: dict[str, dict[str, int]] = {}
    for r in live:
        ats = str(r.get("prefill_ats") or r.get("site") or "unknown")
        d = by_ats.setdefault(ats, {"n": 0, "applied": 0})
        d["n"] += 1
        if _is_success(r):
            d["applied"] += 1

    by_tier: dict[str, dict[str, int]] = {}
    for r in live:
        t = str(r.get("tier_used") or "unknown")
        d = by_tier.setdefault(t, {"n": 0, "applied": 0})
        d["n"] += 1
        if _is_success(r):
            d["applied"] += 1

    return {
        "live_attempts": n,
        "applied": applied,
        "pass_rate": round(applied / n, 3) if n else 0.0,
        "fail_A_removable": a_fail,
        "fail_B_irreducible": b_fail,
        "removable_share": round(a_fail / (a_fail + b_fail), 3) if (a_fail + b_fail) else 0.0,
        "total_cost_usd": round(cost, 2),
        "cost_per_apply_usd": round(cost / len(cost_rows), 3) if cost_rows else None,
        "cost_rows": len(cost_rows),
        "cache_hit_rate": round(cr / cache_denom, 3) if cache_denom else None,
        "by_ats": {k: {**v, "pass_rate": round(v["applied"] / v["n"], 3) if v["n"] else 0.0}
                   for k, v in sorted(by_ats.items())},
        "by_tier": {k: {**v, "pass_rate": round(v["applied"] / v["n"], 3) if v["n"] else 0.0}
                    for k, v in sorted(by_tier.items())},
    }


def load_review_rows(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def format_report(summary: dict[str, Any]) -> str:
    s = summary
    lines = [
        "=" * 56,
        "  ApplyPilot — reliability / cost report",
        "=" * 56,
        f"  Live attempts      : {s['live_attempts']}",
        f"  Applied            : {s['applied']}  (pass rate {s['pass_rate']:.0%})",
        f"  Failures (A) removable  : {s['fail_A_removable']}  <- engineer these away",
        f"  Failures (B) irreducible: {s['fail_B_irreducible']}  <- route to human/skip",
        f"  Removable share of fails: {s['removable_share']:.0%}",
        "-" * 56,
        f"  Total cost         : ${s['total_cost_usd']:.2f}"
        + ("" if s["cost_rows"] else "   (NO cost telemetry in rows yet)"),
        f"  Cost / apply       : "
        + (f"${s['cost_per_apply_usd']:.3f}" if s["cost_per_apply_usd"] is not None else "n/a"),
        f"  Cache hit rate     : "
        + (f"{s['cache_hit_rate']:.0%}" if s["cache_hit_rate"] is not None else "n/a"),
        "-" * 56,
        "  Pass rate by ATS:",
    ]
    for ats, d in s["by_ats"].items():
        lines.append(f"    {ats[:28]:28s} {d['applied']:>3}/{d['n']:<3} ({d['pass_rate']:.0%})")
    lines.append("  Pass rate by tier:")
    for t, d in s["by_tier"].items():
        lines.append(f"    {t[:28]:28s} {d['applied']:>3}/{d['n']:<3} ({d['pass_rate']:.0%})")
    lines.append("=" * 56)
    return "\n".join(lines)
