"""Durable two-phase submission record. INTENT is written immediately before a
submit is attempted; the same identity transitions to CONFIRMED (verified) or
FAILED (rejected/unverified). A dangling INTENT (process died between the two)
is visible across restarts and must be reconciled by a human — never blindly
retried. This is what makes the Reddit-double-submit class structurally dead."""
from __future__ import annotations

from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SubmissionLedger:
    def __init__(self, conn):
        self.conn = conn

    def record_intent(self, identity_id: str, *, worker_id: int) -> None:
        now = _now()
        self.conn.execute(
            "INSERT INTO submission_ledger (identity_id, state, worker_id, created_at, updated_at) "
            "VALUES (?, 'intent', ?, ?, ?)", (identity_id, worker_id, now, now))
        self.conn.commit()

    def _resolve_open(self, identity_id: str, state: str, *, reason=None, confidence=None) -> None:
        self.conn.execute(
            "UPDATE submission_ledger SET state=?, reason=?, confidence=?, updated_at=? "
            "WHERE identity_id=? AND state='intent'",
            (state, reason, confidence, _now(), identity_id))
        self.conn.commit()

    def confirm(self, identity_id: str, *, confidence: float) -> None:
        self._resolve_open(identity_id, "confirmed", confidence=confidence)

    def fail(self, identity_id: str, *, reason: str) -> None:
        self._resolve_open(identity_id, "failed", reason=reason)

    def has_open_intent(self, identity_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM submission_ledger WHERE identity_id=? AND state='intent' LIMIT 1",
            (identity_id,)).fetchone() is not None

    def has_confirmed(self, identity_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM submission_ledger WHERE identity_id=? AND state='confirmed' LIMIT 1",
            (identity_id,)).fetchone() is not None

    def confirmed_count_for_token(self, board_token: str, since_iso: str) -> int:
        # cooldown: count confirmed applies to this board within a window.
        # identity_id is 'ats:token:job_id', so match the ':token:' segment.
        return self.conn.execute(
            "SELECT COUNT(*) FROM submission_ledger WHERE state='confirmed' "
            "AND updated_at >= ? AND identity_id LIKE ?",
            (since_iso, f"%:{board_token}:%")).fetchone()[0]

    def dangling_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM submission_ledger WHERE state='intent'").fetchone()[0]
