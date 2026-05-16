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


def is_needs_human(row: dict) -> bool:
    """Phase E: the automatability gate deferred this to a human BEFORE
    spending an LLM (status needs_review:needs_human_*, or a blocker_*
    failure class). Counted separately — it's a correct deferral, not a
    pipeline failure."""
    blob = " ".join(str(row.get(k) or "").lower()
                     for k in ("status", "failure_class"))
    return "needs_human" in blob


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

    needs_human = sum(1 for r in live if is_needs_human(r))

    by_ats: dict[str, dict[str, int]] = {}
    for r in live:
        ats = str(r.get("prefill_ats") or r.get("site") or "unknown")
        d = by_ats.setdefault(ats, {"n": 0, "applied": 0, "needs_human": 0,
                                    "a_fail": 0, "b_fail": 0})
        d["n"] += 1
        if _is_success(r):
            d["applied"] += 1
        if is_needs_human(r):
            d["needs_human"] += 1
        bucket = classify_failure_bucket(r)
        if bucket == "A":
            d["a_fail"] += 1
        elif bucket == "B":
            d["b_fail"] += 1

    by_tier: dict[str, dict[str, int]] = {}
    for r in live:
        t = str(r.get("tier_used") or "unknown")
        d = by_tier.setdefault(t, {"n": 0, "applied": 0})
        d["n"] += 1
        if _is_success(r):
            d["applied"] += 1

    # Adapter-path slice: the FAIR measure for Reliability-v2 acceptance #6
    # ($/apply + (A)-failures) — judged ONLY on jobs the Greenhouse adapter
    # actually drove, excluding Workday/indeed/Ashby the adapter never
    # targets (those polluted the mixed Phase-F average).
    adp = [r for r in live
           if str(r.get("tier_used") or "").startswith("greenhouse_adapter")]
    adp_applied = sum(1 for r in adp if _is_success(r))
    adp_a = sum(1 for r in adp if classify_failure_bucket(r) == "A")
    adp_cost_rows = [r for r in adp if r.get("cost_usd") is not None]
    adp_cost = sum(float(r.get("cost_usd") or 0) for r in adp)
    adapter_slice = {
        "n": len(adp),
        "applied": adp_applied,
        "pass_rate": round(adp_applied / len(adp), 3) if adp else 0.0,
        "fail_A_removable": adp_a,
        "cost_per_apply_usd": (round(adp_cost / adp_applied, 3)
                               if adp_applied else None),
    }

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
        "adapter_slice": adapter_slice,
        "needs_human": needs_human,
        # automatability = of the jobs that WERE automatable (not env-blocked
        # / deferred to a human), how often did we actually apply.
        "automatability": (
            round(applied / max(1, n - b_fail - needs_human), 3) if n else 0.0
        ),
        "by_ats": {k: {**v,
                       "pass_rate": round(v["applied"] / v["n"], 3) if v["n"] else 0.0,
                       "automatability": round(
                           v["applied"] / max(1, v["n"] - v["b_fail"] - v["needs_human"]),
                           3) if v["n"] else 0.0}
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


def _fmt_adapter_slice(a: dict | None) -> str:
    a = a or {"applied": 0, "n": 0, "pass_rate": 0.0,
              "fail_A_removable": 0, "cost_per_apply_usd": None}
    cpa = a.get("cost_per_apply_usd")
    cpa_s = "n/a" if cpa is None else f"${cpa:.3f}"
    return (f"  Adapter-path (v2 acc#6) : {a['applied']}/{a['n']} "
            f"({a['pass_rate']:.0%}), (A)-fails={a['fail_A_removable']}, "
            f"$/apply={cpa_s}")


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
        _fmt_adapter_slice(s.get("adapter_slice")),
        f"  Needs-human (gated, $0) : {s.get('needs_human', 0)}  <- CAPTCHA/email-verif/SSO, no LLM spent",
        f"  Automatability          : {s.get('automatability', 0.0):.0%}  <- applied / (automatable jobs)",
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
