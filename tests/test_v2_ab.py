"""Task 12: A/B measurement + shadow cutover go/no-go.

PURE tests over synthetic review rows ($0). Exercises three helpers that
sit on top of what `summarize_review` already returns / reads:

- v2_ab_verdict(summary)      -> §12.1 pass-rate gate (v2 >= legacy, >=100 rows)
- v2_p50_duration_ms(rows)    -> §12.2 speed gate input (p50 of live v2 rows)
- v2_cutover_gate(rows, ...)  -> assembles §12.1 + §12.2 + injected §12.3 audit

None of these import applypilot.apply.v2 — the tier strings are constants.
"""
from applypilot.reporting import (
    summarize_review,
    v2_ab_verdict,
    v2_p50_duration_ms,
    v2_cutover_gate,
)


def _rows(v2_applied, v2_n, legacy_applied, legacy_n, *, dry_run=False,
          v2_ms=40000, legacy_ms=90000):
    rows = []
    for i in range(v2_n):
        rows.append({"status": "applied" if i < v2_applied else "failed:x",
                     "tier_used": "v2_greenhouse", "duration_ms": v2_ms,
                     "dry_run": dry_run, "site": "greenhouse"})
    for i in range(legacy_n):
        rows.append({"status": "applied" if i < legacy_applied else "failed:x",
                     "tier_used": "legacy_llm", "duration_ms": legacy_ms,
                     "dry_run": dry_run, "site": "greenhouse"})
    return rows


# ---------------------------------------------------------------- §12.1 pass-rate

def test_verdict_no_go_below_100_rows():
    summary = summarize_review(_rows(v2_applied=40, v2_n=50, legacy_applied=35, legacy_n=50))
    v = v2_ab_verdict(summary)
    assert v["go"] is False
    assert v["reason"] and "100" in v["reason"]        # not enough rows yet
    assert v["v2"]["n"] == 50 and v["legacy"]["n"] == 50


def test_verdict_go_when_v2_ge_v1_and_100plus_rows():
    # v2 pass_rate 0.90 (108/120), legacy 0.80 (80/100) -> GO
    summary = summarize_review(_rows(v2_applied=108, v2_n=120, legacy_applied=80, legacy_n=100))
    v = v2_ab_verdict(summary)
    assert v["v2"]["n"] >= 100
    assert v["v2"]["pass_rate"] >= v["legacy"]["pass_rate"]
    assert v["go"] is True


def test_verdict_no_go_when_v2_worse():
    summary = summarize_review(_rows(v2_applied=70, v2_n=110, legacy_applied=95, legacy_n=110))
    v = v2_ab_verdict(summary)
    assert v["go"] is False                            # v2 pass_rate < legacy
    assert "pass_rate" in v["reason"]


def test_verdict_ignores_dry_run_rows():
    # summarize_review already excludes dry-run from `live`; verdict inherits that.
    summary = summarize_review(_rows(108, 120, 80, 100, dry_run=True))
    v = v2_ab_verdict(summary)
    assert v["v2"]["n"] == 0 and v["go"] is False       # nothing live to judge


def test_verdict_go_on_exact_tie_at_min_rows():
    # v2 == legacy pass_rate, exactly 100 v2 rows -> GO (">=" both ways).
    summary = summarize_review(_rows(v2_applied=80, v2_n=100, legacy_applied=80, legacy_n=100))
    v = v2_ab_verdict(summary)
    assert v["v2"]["n"] == 100
    assert v["v2"]["pass_rate"] == v["legacy"]["pass_rate"]
    assert v["go"] is True


def test_verdict_no_go_at_99_rows_boundary():
    summary = summarize_review(_rows(v2_applied=99, v2_n=99, legacy_applied=50, legacy_n=100))
    v = v2_ab_verdict(summary)
    assert v["go"] is False
    assert "99" in v["reason"] and "100" in v["reason"]


def test_verdict_missing_tiers_is_hold_not_crash():
    # No v2/legacy rows at all -> defaults, HOLD, no KeyError.
    summary = summarize_review([{"status": "applied", "tier_used": "skill_record",
                                 "site": "greenhouse"}])
    v = v2_ab_verdict(summary)
    assert v["go"] is False
    assert v["v2"]["n"] == 0 and v["legacy"]["n"] == 0


# ---------------------------------------------------------------- §12.2 speed p50

def test_p50_over_live_v2_rows_only():
    # v2 rows 40000ms, legacy 90000ms -> p50 must reflect ONLY v2.
    rows = _rows(108, 120, 80, 100)
    assert v2_p50_duration_ms(rows) == 40000


def test_p50_ignores_dry_run_rows():
    rows = _rows(108, 120, 80, 100, dry_run=True)
    assert v2_p50_duration_ms(rows) is None            # nothing live


def test_p50_median_of_mixed_durations():
    rows = [
        {"tier_used": "v2_greenhouse", "duration_ms": 10000, "status": "applied"},
        {"tier_used": "v2_greenhouse", "duration_ms": 30000, "status": "applied"},
        {"tier_used": "v2_greenhouse", "duration_ms": 50000, "status": "applied"},
    ]
    assert v2_p50_duration_ms(rows) == 30000           # middle value


def test_p50_none_when_no_v2_rows():
    rows = [{"tier_used": "legacy_llm", "duration_ms": 90000, "status": "applied"}]
    assert v2_p50_duration_ms(rows) is None


# ---------------------------------------------------------------- full cutover gate

def test_cutover_gate_go_when_all_three_pass():
    rows = _rows(108, 120, 80, 100, v2_ms=40000)       # pass GO, p50 40s <=45s
    gate = v2_cutover_gate(rows, audit_clean=True)
    assert gate["pass_rate"]["go"] is True
    assert gate["speed"]["go"] is True and gate["speed"]["p50_ms"] == 40000
    assert gate["audit"]["go"] is True
    assert gate["go"] is True


def test_cutover_gate_hold_when_speed_too_slow():
    # pass-rate GO but p50 60s > 45s -> HOLD (a lucky 100 at 60s is a HOLD).
    rows = _rows(108, 120, 80, 100, v2_ms=60000)
    gate = v2_cutover_gate(rows, audit_clean=True)
    assert gate["pass_rate"]["go"] is True
    assert gate["speed"]["go"] is False
    assert gate["go"] is False


def test_cutover_gate_hold_when_audit_not_run():
    # audit_clean=None (default): §12.3 not confirmed -> HOLD even if 1+2 pass.
    rows = _rows(108, 120, 80, 100, v2_ms=40000)
    gate = v2_cutover_gate(rows)
    assert gate["pass_rate"]["go"] is True
    assert gate["speed"]["go"] is True
    assert gate["audit"]["go"] is None
    assert gate["go"] is False


def test_cutover_gate_hold_when_audit_dirty():
    # a single dangling INTENT / canary violation -> HOLD.
    rows = _rows(108, 120, 80, 100, v2_ms=40000)
    gate = v2_cutover_gate(rows, audit_clean=False)
    assert gate["audit"]["go"] is False
    assert gate["go"] is False


def test_cutover_gate_hold_when_pass_rate_fails():
    rows = _rows(70, 110, 95, 110, v2_ms=40000)        # v2 worse
    gate = v2_cutover_gate(rows, audit_clean=True)
    assert gate["pass_rate"]["go"] is False
    assert gate["go"] is False
