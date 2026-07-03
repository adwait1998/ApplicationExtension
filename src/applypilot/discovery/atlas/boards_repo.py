"""CRUD for the `boards` table (created in database.init_db). Every public fn
takes an explicit sqlite3.Connection so it composes with the thread-local
get_connection() and with in-memory test DBs alike."""
from __future__ import annotations

from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_board(conn, ats: str, token: str, *, company_name: str | None = None,
                 source: str | None = None) -> bool:
    """Insert (status='candidate') or update a board row. Returns True if a new
    row was inserted. `first_seen` and `source` are set once (first sighting
    wins); company_name is refreshed on every upsert. ats/token are lowercased
    so the registry never double-counts case variants."""
    ats, token = ats.lower(), token.lower()
    existing = get(conn, ats, token)
    if existing is None:
        conn.execute(
            "INSERT INTO boards (ats, token, company_name, status, first_seen, "
            "error_streak, source) VALUES (?,?,?,?,?,0,?)",
            (ats, token, company_name, "candidate", _now(), source),
        )
        conn.commit()
        return True
    if company_name is not None:
        conn.execute("UPDATE boards SET company_name = ? WHERE ats = ? AND token = ?",
                     (company_name, ats, token))
        conn.commit()
    return False


def get(conn, ats: str, token: str) -> dict | None:
    row = conn.execute("SELECT * FROM boards WHERE ats = ? AND token = ?",
                       (ats.lower(), token.lower())).fetchone()
    return dict(row) if row else None


def get_all(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM boards ORDER BY ats, token").fetchall()]


def get_boards_by_ring(conn, *, rings: tuple[int, ...] = (0, 1)) -> list[dict]:
    """Active boards in the given rings. The client TICK calls this with (0, 1)
    only (spec §7.2/§14: no client ring-2 polling). Excludes 'candidate' (not
    yet validated) and 'dead' boards."""
    placeholders = ",".join("?" for _ in rings)
    rows = conn.execute(
        f"SELECT * FROM boards WHERE status = 'active' AND ring IN ({placeholders}) "
        "ORDER BY ring, last_checked IS NOT NULL, last_checked",
        tuple(rings),
    ).fetchall()
    return [dict(r) for r in rows]


def set_ring(conn, ats: str, token: str, ring: int) -> None:
    conn.execute("UPDATE boards SET ring = ? WHERE ats = ? AND token = ?",
                 (ring, ats.lower(), token.lower()))
    conn.commit()


def set_status(conn, ats: str, token: str, status: str) -> None:
    conn.execute("UPDATE boards SET status = ? WHERE ats = ? AND token = ?",
                 (status, ats.lower(), token.lower()))
    conn.commit()


def mark_checked(conn, ats: str, token: str, *, job_id_set_hash: str,
                 job_count: int, new_last_poll: int, changed: bool) -> None:
    """Record a successful poll/validation. Clears error_streak, revives the
    board to 'active', advances last_checked; advances last_changed only when
    the id-set actually changed."""
    now = _now()
    if changed:
        conn.execute(
            "UPDATE boards SET status='active', error_streak=0, last_checked=?, "
            "last_changed=?, job_id_set_hash=?, job_count=?, new_last_poll=? "
            "WHERE ats=? AND token=?",
            (now, now, job_id_set_hash, job_count, new_last_poll, ats.lower(), token.lower()),
        )
    else:
        conn.execute(
            "UPDATE boards SET status='active', error_streak=0, last_checked=?, "
            "job_id_set_hash=?, job_count=?, new_last_poll=? WHERE ats=? AND token=?",
            (now, job_id_set_hash, job_count, new_last_poll, ats.lower(), token.lower()),
        )
    conn.commit()


def mark_dead(conn, ats: str, token: str) -> None:
    """Bump error_streak and set status='dead'. A later successful mark_checked
    revives it (spec §7.2: zero-yield/failing sources demote automatically)."""
    conn.execute(
        "UPDATE boards SET error_streak = error_streak + 1, status = 'dead' "
        "WHERE ats = ? AND token = ?",
        (ats.lower(), token.lower()),
    )
    conn.commit()
