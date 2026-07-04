"""Ring assignment + per-tick selection/budget math (pure; spec §7.2/§7.5).
Ring 0 = hot (poll ~hourly), 1 = warm (~6-12h), 2 = cold (snapshot-only; the
CLIENT never polls ring 2 — spec §14). Adaptive: a board that just posted is
hot; a long-dormant one decays to cold; a freshly-validated board with no
change history gets a warm chance rather than being buried cold."""
from __future__ import annotations

from datetime import datetime, timezone

# Minimum spacing (hours) before a ring is due again. v1 uses the low end of
# the spec bands (30 min hot, 6h warm) so the queue stays fresh.
_CADENCE_H = {0: 0.5, 1: 6.0, 2: 24.0}
_HOT_RECENCY_H = 72        # changed within 3 days -> hot
_COLD_DORMANCY_H = 24 * 45  # no change in 45 days AND empty -> cold


def _parse(ts: str | None):
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def _hours_since(ts: str | None, now: str) -> float | None:
    dt = _parse(ts)
    if dt is None:
        return None
    return (_parse(now) - dt).total_seconds() / 3600.0


def assign_ring(board: dict, *, now: str) -> int:
    changed_ago = _hours_since(board.get("last_changed"), now)
    posted_recently = (board.get("new_last_poll") or 0) > 0
    if posted_recently or (changed_ago is not None and changed_ago <= _HOT_RECENCY_H):
        return 0
    if changed_ago is None:
        return 1                                  # never-changed-yet: warm chance
    if changed_ago >= _COLD_DORMANCY_H and (board.get("job_count") or 0) == 0:
        return 2
    return 1


def is_due(board: dict, *, now: str) -> bool:
    ring = board.get("ring")
    if ring is None:
        return True
    since = _hours_since(board.get("last_checked"), now)
    if since is None:
        return True
    return since >= _CADENCE_H.get(ring, 6.0)


def select_due(boards: list[dict], *, now: str, budget: int) -> list[dict]:
    """Due boards, hot-first, capped at `budget` requests this tick."""
    due = [b for b in boards if is_due(b, now=now)]
    due.sort(key=lambda b: (b.get("ring", 9),
                            b.get("last_checked") or ""))    # oldest-checked first within ring
    return due[:budget]


def estimate_daily_requests(*, hot: int, warm: int) -> int:
    """Rough daily request volume for a ring-0/1 subset (one req/poll thanks to
    id-set diffing). Hot ~hourly (24/day), warm ~2/day."""
    return hot * 24 + warm * 2
