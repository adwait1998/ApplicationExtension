"""The Atlas TICK: idempotent, resumable, no-daemon catch-up poll (spec §7.2).

Reassign rings for active boards, select due ring-0/1 boards within budget,
poll each (id-set diff -> gate -> store), tally into source_runs. Safe to run
on cron/on-wake or as the 'atlas' discover sub-source.

No daemon (spec §7.2/§14): a single pass over the boards due right now, then
return. Idempotent — re-running only polls whatever is due; resumable — each
poll_board commits + mark_checked on its own, so a mid-tick crash loses no
progress and the next tick picks up the boards not yet checked."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from applypilot.discovery.atlas import USER_AGENT
from applypilot.discovery.atlas import boards_repo as repo
from applypilot.discovery.atlas import rings
from applypilot.discovery.atlas import source_runs as sr
from applypilot.discovery.atlas.poller import poll_board
from applypilot.discovery.atlas.politeness import HostRateLimiter

# Client budget: ring-0/1 only, well inside per-host politeness (spec §7.2).
_DEFAULT_BUDGET = 6000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_tick(conn, policy: dict, *, client: httpx.Client | None = None,
             budget: int = _DEFAULT_BUDGET, force_due: bool = False,
             limiter: HostRateLimiter | None = None) -> dict:
    """One idempotent, resumable Atlas tick. Returns aggregate tallies for this
    tick and writes exactly one source_runs row.

    force_due re-selects every ring-0/1 board regardless of cadence (used to
    prove idempotency: the id-set diff still short-circuits unchanged boards).
    """
    now = _now()
    owns = client is None
    client = client or httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT})
    limiter = limiter or HostRateLimiter(rps=4.0)
    run_id = sr.open_run(conn, source="atlas")
    boards_polled = requests = jobs_seen = jobs_new = jobs_eligible = 0
    error = None
    try:
        # 1. Reassign rings for the boards THIS client owns — active boards that
        #    are unassigned (ring NULL) or already hot/warm. Ring-2 (cold) boards
        #    are a server-side snapshot concern and are left untouched, so the
        #    client never promotes a cold board into its own poll set (spec §14,
        #    invariant #5).
        for b in repo.get_all(conn):
            if b.get("status") != "active" or b.get("ring") == 2:
                continue
            repo.set_ring(conn, b["ats"], b["token"], rings.assign_ring(b, now=now))
        # 2. Select due ring-0/1 boards within budget (hot-first). Ring 2 is a
        #    server-side snapshot concern — the client never polls it (spec §14).
        candidates = repo.get_boards_by_ring(conn, rings=(0, 1))
        if force_due:
            for b in candidates:
                b["last_checked"] = None
        due = rings.select_due(candidates, now=now, budget=budget)
        # 3. Poll each (poll_board commits + mark_checked per board -> resumable).
        #    Per-board try/except so one board's failure never aborts the tick.
        for b in due:
            try:
                res = poll_board(conn, repo.get(conn, b["ats"], b["token"]), policy,
                                 client=client, limiter=limiter)
            except Exception:  # noqa: BLE001 — one bad board never kills the tick
                boards_polled += 1
                requests += 1
                continue
            boards_polled += 1
            requests += 1
            if res.ok:
                jobs_seen += res.total_ids
                jobs_new += res.new_ids
        # Eligible count for this tick's newly-stored atlas rows (gated_at is
        # stamped at gate time, strictly after `now` captured at tick start).
        jobs_eligible = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE strategy LIKE 'atlas:%' "
            "AND gate_result = 'eligible' AND gated_at >= ?", (now,)).fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    finally:
        sr.finish_run(conn, run_id, boards_polled=boards_polled, requests=requests,
                      jobs_seen=jobs_seen, jobs_new=jobs_new, jobs_eligible=jobs_eligible,
                      cost_usd=0.0, error=error)
        if owns:
            client.close()
    return {"run_id": run_id, "boards_polled": boards_polled, "requests": requests,
            "jobs_seen": jobs_seen, "jobs_new": jobs_new, "jobs_eligible": jobs_eligible,
            "error": error}
