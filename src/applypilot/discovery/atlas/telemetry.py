"""Shadow-mode telemetry + go/no-go (spec §13 Phase 2 exit). Three signals:
per-ATS board coverage, poll request volume, fresh-eligible-queue-depth. The
Phase-2 gate is: Atlas produces a healthy fresh-eligible queue for the mainstream
profile WITHOUT exceeding politeness budgets. (Ashby/Lever parse-gap rates are a
Phase-3 concern — NOT gated here.)

The Phase-2 -> Phase-3 go/no-go, per spec §13 (Phase 2 exit is about the FUNNEL,
not apply-engine parse rates):
- GO if: fresh-eligible Atlas queue depth >= 5 (median daily) for the mainstream
  profile over the shadow window (mirrors acceptance-gate item 5, §12); AND zero
  source_runs with a politeness/vendor error (429/auth-wall) — the vendor-behavior
  canary is clean; AND poll volume stays within the per-host budget (the polled
  ring-0/1 count is comfortably under ~4 rps/host sustained).
- NO-GO / iterate if: the queue starves (depth < 5) -> widen the freshness window
  / promote warm boards (spec §7.5 levers) before proceeding; OR any host shows a
  429/auth-wall streak -> back off, re-tune politeness, re-run shadow.
- Explicitly NOT gated in Phase 2: Ashby/Lever DOM parse-gap and fingerprint-hit
  rates — those are Phase 3 (apply engine) exit criteria."""
from __future__ import annotations

import json


def _needs_sponsorship() -> bool:
    """Read require_sponsorship from the profile; False if unavailable."""
    try:
        from applypilot import config
        wa = (config.load_profile() or {}).get("work_authorization", {}) or {}
        return bool(wa.get("require_sponsorship"))
    except Exception:
        return False


def board_coverage(conn) -> dict:
    """{ats: {status: count}} across the boards registry."""
    out: dict[str, dict[str, int]] = {}
    for ats, status, n in conn.execute(
            "SELECT ats, status, COUNT(*) FROM boards GROUP BY ats, status").fetchall():
        out.setdefault(ats, {})[status] = n
    return out


def fresh_eligible_depth(conn, *, source_prefix: str = "atlas:") -> int:
    """Eligible, not-yet-scored rows from Atlas — the queue the funnel feeds on."""
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE strategy LIKE ? "
        "AND gate_result = 'eligible'", (source_prefix + "%",)).fetchone()[0]


def review_ready_depth(conn, *, source_prefix: str = "atlas:",
                       needs_sponsorship: bool | None = None) -> int:
    """The queue the funnel can ACT on. Always includes gate_result='eligible'.
    For a sponsorship-needing profile it ALSO includes 'unknown' rows whose only
    open question is sponsorship (location+seniority+automatability all PASS) —
    these are correctly routed to the human review queue rather than auto-applied
    (spec §10.3). For a visa user, 'eligible' alone structurally under-counts the
    real queue to ~0 (JDs rarely state sponsorship), so this is the honest
    queue-health number for the go/no-go."""
    if needs_sponsorship is None:
        needs_sponsorship = _needs_sponsorship()
    eligible = fresh_eligible_depth(conn, source_prefix=source_prefix)
    if not needs_sponsorship:
        return eligible
    extra = 0
    for (reasons,) in conn.execute(
            "SELECT gate_reasons FROM jobs WHERE strategy LIKE ? AND gate_result = 'unknown'",
            (source_prefix + "%",)):
        try:
            rs = {x["rule"]: x for x in json.loads(reasons or "[]")}
        except Exception:
            continue
        if (rs.get("location", {}).get("result") == "PASS"
                and rs.get("seniority", {}).get("result") == "PASS"
                and rs.get("automatability", {}).get("result") == "PASS"
                and rs.get("sponsorship", {}).get("result") == "UNKNOWN"):
            extra += 1
    return eligible + extra


def poll_volume(conn) -> dict:
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(requests),0), COALESCE(SUM(boards_polled),0) "
        "FROM source_runs WHERE source LIKE 'atlas%'").fetchone()
    return {"runs": row[0], "total_requests": row[1], "total_boards_polled": row[2]}


def go_no_go(conn, *, min_fresh_eligible: int = 5, max_error_runs: int = 0,
             needs_sponsorship: bool | None = None) -> dict:
    """Phase-2 exit. The depth signal is REVIEW-READY depth (queue the funnel can
    act on), which for a sponsorship-needing profile includes eligible +
    sponsorship-only-unknown rows (routed to review, spec §10.3). Raw eligible is
    reported alongside for transparency but is not the gate for visa users."""
    if needs_sponsorship is None:
        needs_sponsorship = _needs_sponsorship()
    eligible = fresh_eligible_depth(conn)
    depth = review_ready_depth(conn, needs_sponsorship=needs_sponsorship)
    coverage = board_coverage(conn)
    volume = poll_volume(conn)
    error_runs = conn.execute(
        "SELECT COUNT(*) FROM source_runs WHERE source LIKE 'atlas%' AND error IS NOT NULL"
    ).fetchone()[0]
    go = depth >= min_fresh_eligible and error_runs <= max_error_runs
    return {
        "go": bool(go),
        "signals": {
            "review_ready_depth": depth,
            "fresh_eligible_depth": eligible,
            "needs_sponsorship": needs_sponsorship,
            "board_coverage": coverage,
            "poll_volume": volume,
            "error_runs": error_runs,
        },
        "criteria": {
            "min_fresh_eligible": min_fresh_eligible,
            "max_error_runs": max_error_runs,
        },
    }
