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


# --------------------------------------------------------------------------- #
#  v2 A/B cutover gate (Task 12, spec §13 Phase 3 exit + §12)                  #
#                                                                             #
#  PURE reporting over what summarize_review already returns/reads. Does NOT  #
#  import applypilot.apply.v2 — the tier labels are just constants here:      #
#  v2 rows carry tier_used="v2_greenhouse" (written by the v2 seam, Task 10), #
#  legacy rows tier_used="legacy_llm" (skill_runner.dispatch_apply OFF path). #
#  No new telemetry: v2 vs legacy fall out of the existing tier_used field.   #
# --------------------------------------------------------------------------- #
_V2_TIER = "v2_greenhouse"
_LEGACY_TIER = "legacy_llm"


def v2_ab_verdict(summary: dict, *, min_rows: int = 100,
                  v2_tier: str = _V2_TIER, legacy_tier: str = _LEGACY_TIER) -> dict:
    """Phase-3 cutover gate, condition §12.1 (pass-rate). Cut Greenhouse over
    to v2 iff v2 matches or beats legacy on >= min_rows LIVE Greenhouse rows.
    Pure over summarize_review's ``by_tier`` — dry-run rows are already
    excluded upstream (summarize_review filters on ``not row["dry_run"]``), so
    this verdict inherits that live-only behaviour for free.

    Shadow go/no-go (see also v2_cutover_gate for the full 3-condition gate):
      * GO (pass-rate leg) when v2 n >= min_rows AND v2 pass_rate >= legacy.
      * HOLD when v2 sample < min_rows, OR v2 pass_rate < legacy.
    Full cutover ALSO requires §12.2 speed (p50 <= 45s) and §12.3 safety
    audit (zero dangling/duplicate INTENTs, zero canary violations) — a lucky
    100 at p50=60s, or a single dangling INTENT, is still a HOLD. Only when
    all three pass is the legacy tier retired for Greenhouse (spec §6.8:
    "retired per-ATS once v2 >= v1 on 100+ live rows").

    Returns: {"go", "reason", "v2", "legacy", "min_rows"}.
    """
    by_tier = summary.get("by_tier", {})
    v2 = by_tier.get(v2_tier, {"n": 0, "applied": 0, "pass_rate": 0.0})
    legacy = by_tier.get(legacy_tier, {"n": 0, "applied": 0, "pass_rate": 0.0})
    if v2["n"] < min_rows:
        reason = f"insufficient v2 sample: {v2['n']} < {min_rows} live rows"
        go = False
    elif v2["pass_rate"] < legacy["pass_rate"]:
        reason = (f"v2 pass_rate {v2['pass_rate']} < legacy {legacy['pass_rate']} "
                  f"(n_v2={v2['n']}, n_legacy={legacy['n']})")
        go = False
    else:
        reason = (f"v2 pass_rate {v2['pass_rate']} >= legacy {legacy['pass_rate']} "
                  f"on {v2['n']} live rows")
        go = True
    return {"go": go, "reason": reason, "v2": v2, "legacy": legacy, "min_rows": min_rows}


def v2_p50_duration_ms(rows: list[dict], *, tier: str = _V2_TIER) -> int | None:
    """Phase-3 cutover gate, condition §12.2 input (speed). p50 (median) of
    ``duration_ms`` over the LIVE ``tier_used == tier`` rows. Pure query over
    the same rows summarize_review reads (dry-run and other tiers excluded);
    no I/O. Returns None when there are no live rows for ``tier`` to measure.

    Account for the react-select sleep tax (Task 6) here — the 45s warm budget
    is wall-clock, so the sleeps between option clicks are already baked into
    duration_ms and this p50 reflects them.
    """
    vals = sorted(
        int(r["duration_ms"]) for r in rows
        if not r.get("dry_run")
        and str(r.get("tier_used") or "") == tier
        and r.get("duration_ms") is not None
    )
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) // 2


def v2_cutover_gate(rows: list[dict], *, min_rows: int = 100,
                    max_p50_ms: int = 45000, audit_clean: bool | None = None,
                    v2_tier: str = _V2_TIER, legacy_tier: str = _LEGACY_TIER) -> dict:
    """Full Phase-3 -> cutover go/no-go (spec §13 Phase 3 exit + §12 items 1-3).
    THREE conditions, ALL of which must pass before flipping the flag — pass
    rate alone is NOT sufficient:

      §12.1 pass-rate : v2_ab_verdict(summary)["go"] is True (v2 >= legacy on
                        >= min_rows live Greenhouse rows).
      §12.2 speed     : p50 of live v2 duration_ms <= max_p50_ms (45s warm).
      §12.3 safety    : zero dangling/duplicate INTENTs + zero canary-field
                        violations + zero unauthorized submissions attributable
                        to v2. This is NOT derivable from review.jsonl, so it
                        is injected via ``audit_clean`` — the operator runs the
                        query trio (see format_v2_cutover / below) and passes
                        the boolean result. audit_clean=None => not-yet-run =>
                        HOLD (never silently GO on an un-run audit).

    §12.3 query trio (no new telemetry — reads existing durable records):
      1. submission_ledger.SubmissionLedger.dangling_count() == 0  (a
         needs_review:v2_crashed_post_submit leaves a dangling INTENT by
         design — reconcile it, do not count it as a clean submit).
      2. no identity double-submitted: confirmed_count_for_token(token, since)
         <= 1 per identity across the v2 burn-in corpus.
      3. flight-recorder / canary provenance: zero canary-field writes and no
         submit-POST without an open broker ticket.

    Cache-hit rate (mapping_cache.stats) and parse-gap rate must be MEASURED
    and reported alongside (risk §15) before any speed claim is trusted.

    Returns: {"go", "pass_rate", "speed", "audit", "min_rows", "max_p50_ms",
    "reason"}. ``go`` is True iff all three legs are GO.
    """
    summary = summarize_review(rows)
    pass_rate = v2_ab_verdict(summary, min_rows=min_rows,
                              v2_tier=v2_tier, legacy_tier=legacy_tier)

    p50 = v2_p50_duration_ms(rows, tier=v2_tier)
    if p50 is None:
        speed = {"go": False, "p50_ms": None, "max_p50_ms": max_p50_ms,
                 "reason": f"no live {v2_tier} rows to measure p50"}
    elif p50 > max_p50_ms:
        speed = {"go": False, "p50_ms": p50, "max_p50_ms": max_p50_ms,
                 "reason": f"v2 p50 {p50}ms > {max_p50_ms}ms warm budget"}
    else:
        speed = {"go": True, "p50_ms": p50, "max_p50_ms": max_p50_ms,
                 "reason": f"v2 p50 {p50}ms <= {max_p50_ms}ms warm budget"}

    if audit_clean is None:
        audit = {"go": None, "reason": (
            "safety audit not run — query submission_ledger for zero dangling/"
            "duplicate INTENTs and the flight recorder for zero canary "
            "violations, then re-run with audit_clean")}
    elif audit_clean:
        audit = {"go": True,
                 "reason": "zero dangling/duplicate INTENTs + zero canary violations"}
    else:
        audit = {"go": False,
                 "reason": "canary/duplicate/dangling-INTENT violation attributable to v2"}

    go = bool(pass_rate["go"]) and bool(speed["go"]) and audit["go"] is True
    reason = "; ".join([
        "PASS-RATE ok" if pass_rate["go"] else f"PASS-RATE hold ({pass_rate['reason']})",
        "SPEED ok" if speed["go"] else f"SPEED hold ({speed['reason']})",
        "AUDIT ok" if audit["go"] is True else f"AUDIT hold ({audit['reason']})",
    ])
    return {"go": go, "pass_rate": pass_rate, "speed": speed, "audit": audit,
            "min_rows": min_rows, "max_p50_ms": max_p50_ms, "reason": reason}


def v2_audit_clean(*, ledger, flight_dir=None, tokens=None) -> dict:
    """Compute the §12.3 safety-audit leg from EXISTING durable records (no new
    telemetry). Three conditions, ALL required for go=True:
      1. zero dangling INTENTs (ledger.dangling_count() == 0) — a
         needs_review:v2_crashed_post_submit leaves one by design; it must be
         reconciled, not counted as clean.
      2. no identity double-submitted: confirmed_count_for_token(token) <= 1 for
         every board token in the v2 burn-in corpus. DISCLOSURE: review.jsonl
         rows carry no board_token today, so with an empty ``tokens`` set this
         leg does NOT run — it is a no-op (not a proof of zero duplicates), and
         the reason text says so ("dup-check skipped: no board_token ...").
      3. zero canary-field violations in the flight-recorder bundles. A field is
         a VIOLATION only when it is a canary key (ir.is_canary_key), was
         COMMITTED, AND its provenance is a non-profile/policy SOURCE
         ('oracle'/'answer:*') — a submitted canary answer that did not come
         from an exact profile/policy path breaks invariant 7. A PARKED canary
         (provenance 'parked', never committed) is the SAFE invariant-7 outcome
         ("canary + no data -> park, never guess", resolver.py:139) and is NOT a
         violation; 4 of the 6 canary keys (citizenship/salary/address/dob) have
         no profile path and are ALWAYS parked, so requiring ``committed`` is
         what stops this leg chronically false-HOLDing on routine safe traffic.
         DISCLOSURE: flight bundles are written for NON-applied outcomes only
         (the orchestrator commits nothing on 'applied'), so this leg covers
         non-applied SHADOW traffic only — it cannot see successful submissions.
    Returns {"go", "dangling", "duplicates", "canary_violations", "reason"}."""
    from pathlib import Path
    import json as _json
    from applypilot.apply.v2.ir import is_canary_key

    checked_tokens = list(tokens or [])
    dangling = int(ledger.dangling_count()) if ledger is not None else 0
    duplicates = 0
    for tok in checked_tokens:
        try:
            if ledger.confirmed_count_for_token(tok, "") > 1:
                duplicates += 1
        except Exception:
            continue
    canary_violations = 0
    fdir = Path(flight_dir) if flight_dir else None
    if fdir and fdir.exists():
        for p in fdir.glob("*.json"):
            try:
                bundle = _json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            for f in bundle.get("fields", []):
                sk = f.get("semantic_key")
                prov = (f.get("provenance") or "")
                committed = bool(f.get("committed"))
                # Violation = a COMMITTED canary written from a non-profile/policy
                # source. A parked canary is never committed and is SAFE, so the
                # committed gate excludes it; the provenance check is kept so a
                # committed profile.*/policy.* canary also stays clean.
                if (is_canary_key(sk) and committed
                        and not (prov.startswith("profile.") or prov.startswith("policy."))):
                    canary_violations += 1
    go = dangling == 0 and duplicates == 0 and canary_violations == 0
    dup_txt = (f"{duplicates} duplicate" if checked_tokens
               else "dup-check skipped: no board_token in review log")
    canary_note = "canary leg covers non-applied shadow traffic only"
    if go:
        reason = f"clean: {dangling} dangling, {dup_txt}, {canary_violations} canary ({canary_note})"
    else:
        reason = (f"HOLD: dangling={dangling} {dup_txt} "
                  f"canary={canary_violations} ({canary_note})")
    return {"go": go, "dangling": dangling, "duplicates": duplicates,
            "canary_violations": canary_violations, "reason": reason}


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
        "  Cost / apply       : "
        + (f"${s['cost_per_apply_usd']:.3f}" if s["cost_per_apply_usd"] is not None else "n/a"),
        "  Cache hit rate     : "
        + (f"{s['cache_hit_rate']:.0%}" if s["cache_hit_rate"] is not None else "n/a"),
        "-" * 56,
        "  Pass rate by ATS:",
    ]
    for ats, d in s["by_ats"].items():
        lines.append(f"    {ats[:28]:28s} {d['applied']:>3}/{d['n']:<3} ({d['pass_rate']:.0%})")
    lines.append("  Pass rate by tier:")
    for t, d in s["by_tier"].items():
        lines.append(f"    {t[:28]:28s} {d['applied']:>3}/{d['n']:<3} ({d['pass_rate']:.0%})")
    if _V2_TIER in s.get("by_tier", {}):
        v = v2_ab_verdict(s)
        tag = "CUTOVER-READY" if v["go"] else "HOLD"
        lines.append(f"  v2 A/B (pass-rate) : {tag} — {v['reason']}")
    lines.append("=" * 56)
    return "\n".join(lines)


def format_v2_cutover(rows: list[dict], *, audit_clean: bool | None = None) -> str:
    """Operator-facing render of the full 3-condition v2 cutover gate
    (spec §13 Phase 3 / §12). Wraps v2_cutover_gate. §12.3 is a MANUAL audit
    (not in review.jsonl); when audit_clean is None the query trio is printed
    for the operator to run and then re-invoke with the result."""
    gate = v2_cutover_gate(rows, audit_clean=audit_clean)
    pr, sp, au = gate["pass_rate"], gate["speed"], gate["audit"]

    def _mark(g: bool | None) -> str:
        return "GO  " if g is True else ("HOLD" if g is False else "????")

    lines = [
        "=" * 56,
        "  v2 -> Greenhouse cutover gate (spec §13 Phase 3 / §12)",
        "=" * 56,
        f"  [{_mark(pr['go'])}] §12.1 pass-rate : {pr['reason']}",
        f"  [{_mark(sp['go'])}] §12.2 speed p50 : {sp['reason']}",
        f"  [{_mark(au['go'])}] §12.3 safety    : {au['reason']}",
        "-" * 56,
    ]
    if au["go"] is None:
        lines += [
            "  §12.3 is a MANUAL audit (not in review.jsonl). Run the trio:",
            "    1. submission_ledger.dangling_count() == 0  (reconcile any",
            "       needs_review:v2_crashed_post_submit dangling INTENT first)",
            "    2. no identity double-submitted:",
            "       confirmed_count_for_token(token, since) <= 1 per identity",
            "    3. flight-recorder / canary: zero canary-field writes, no",
            "       submit-POST without an open broker ticket",
            "  Then re-run report --v2-cutover with the audit result to finalize.",
            "-" * 56,
        ]
    lines.append(f"  VERDICT: {'CUTOVER-READY' if gate['go'] else 'HOLD'}")
    lines.append("=" * 56)
    return "\n".join(lines)
