"""source_runs accounting (spec §7.2). open_run at tick start, finish_run at
end with the tallies. Cost is 0.0 in Phase 2 (Atlas polling is $0 public JSON);
the column exists so down-funnel scoring cost can be joined later."""
from __future__ import annotations

from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_run(conn, *, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO source_runs (source, started_at) VALUES (?, ?)",
        (source, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn, run_id: int, *, boards_polled: int = 0, requests: int = 0,
               jobs_seen: int = 0, jobs_new: int = 0, jobs_eligible: int = 0,
               cost_usd: float = 0.0, error: str | None = None) -> None:
    conn.execute(
        "UPDATE source_runs SET finished_at=?, boards_polled=?, requests=?, "
        "jobs_seen=?, jobs_new=?, jobs_eligible=?, cost_usd=?, error=? WHERE run_id=?",
        (_now(), boards_polled, requests, jobs_seen, jobs_new, jobs_eligible,
         cost_usd, error, run_id),
    )
    conn.commit()


def get_run(conn, run_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM source_runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def recent_runs(conn, *, source: str | None = None, limit: int = 20) -> list[dict]:
    if source:
        rows = conn.execute(
            "SELECT * FROM source_runs WHERE source = ? ORDER BY run_id DESC LIMIT ?",
            (source, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM source_runs ORDER BY run_id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
