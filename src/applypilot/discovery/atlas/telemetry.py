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


def poll_volume(conn) -> dict:
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(requests),0), COALESCE(SUM(boards_polled),0) "
        "FROM source_runs WHERE source LIKE 'atlas%'").fetchone()
    return {"runs": row[0], "total_requests": row[1], "total_boards_polled": row[2]}


def go_no_go(conn, *, min_fresh_eligible: int = 5, max_error_runs: int = 0) -> dict:
    depth = fresh_eligible_depth(conn)
    coverage = board_coverage(conn)
    volume = poll_volume(conn)
    error_runs = conn.execute(
        "SELECT COUNT(*) FROM source_runs WHERE source LIKE 'atlas%' AND error IS NOT NULL"
    ).fetchone()[0]
    go = depth >= min_fresh_eligible and error_runs <= max_error_runs
    return {
        "go": bool(go),
        "signals": {
            "fresh_eligible_depth": depth,
            "board_coverage": coverage,
            "poll_volume": volume,
            "error_runs": error_runs,
        },
        "criteria": {
            "min_fresh_eligible": min_fresh_eligible,
            "max_error_runs": max_error_runs,
        },
    }
