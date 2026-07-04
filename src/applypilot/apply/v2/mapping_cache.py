"""Resolver mapping cache (spec §6.4). Demote-never-archive; bindings, never
values (invariant 3). Every fn takes an explicit sqlite3.Connection so it
composes with get_connection() and in-memory test DBs."""
from __future__ import annotations

from datetime import datetime, timezone

_DEMOTE_AT = 2      # 2 verified failures demote a mapping (spec §6.4)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get(conn, ats: str, field_fp: str) -> dict | None:
    row = conn.execute("SELECT * FROM mapping_cache WHERE ats=? AND field_fp=?",
                       (ats, field_fp)).fetchone()
    return dict(row) if row else None


def get_active(conn, ats: str, field_fp: str) -> dict | None:
    """The binding the resolver should use, or None if absent/demoted."""
    row = conn.execute(
        "SELECT * FROM mapping_cache WHERE ats=? AND field_fp=? AND active=1",
        (ats, field_fp)).fetchone()
    return dict(row) if row else None


def put(conn, ats: str, field_fp: str, *, binding: str,
        widget_driver: str | None = None, locator_tier: str | None = None) -> None:
    """Insert a new active mapping, or (on re-resolution of a demoted/changed
    row) bump version and reactivate. NEVER accepts a literal value."""
    now = _now()
    existing = get(conn, ats, field_fp)
    if existing is None:
        conn.execute(
            "INSERT INTO mapping_cache (ats, field_fp, binding, widget_driver, "
            "locator_tier, version, active, fail_streak, hits, created_at, updated_at) "
            "VALUES (?,?,?,?,?,1,1,0,0,?,?)",
            (ats, field_fp, binding, widget_driver, locator_tier, now, now))
    else:
        conn.execute(
            "UPDATE mapping_cache SET binding=?, widget_driver=?, locator_tier=?, "
            "version=version+1, active=1, fail_streak=0, updated_at=? "
            "WHERE ats=? AND field_fp=?",
            (binding, widget_driver, locator_tier, now, ats, field_fp))
    conn.commit()


def record_success(conn, ats: str, field_fp: str) -> None:
    conn.execute(
        "UPDATE mapping_cache SET hits=hits+1, fail_streak=0, updated_at=? "
        "WHERE ats=? AND field_fp=?", (_now(), ats, field_fp))
    conn.commit()


def record_failure(conn, ats: str, field_fp: str) -> None:
    """A VERIFIED failure (commit read-back said not-committed). Bumps the
    streak; at _DEMOTE_AT it demotes (active=0) but NEVER deletes the row."""
    conn.execute(
        "UPDATE mapping_cache SET fail_streak=fail_streak+1, updated_at=? "
        "WHERE ats=? AND field_fp=?", (_now(), ats, field_fp))
    conn.execute(
        "UPDATE mapping_cache SET active=0 WHERE ats=? AND field_fp=? AND fail_streak>=?",
        (ats, field_fp, _DEMOTE_AT))
    conn.commit()


def record_submit_endpoint(conn, ats: str, company: str, method: str, url_pattern: str) -> None:
    now = _now()
    existing = conn.execute(
        "SELECT seen_count FROM submit_endpoints WHERE ats=? AND company=? AND url_pattern=?",
        (ats, company, url_pattern)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO submit_endpoints (ats, company, method, url_pattern, seen_count, "
            "first_seen, last_seen) VALUES (?,?,?,?,1,?,?)",
            (ats, company, method, url_pattern, now, now))
    else:
        conn.execute(
            "UPDATE submit_endpoints SET seen_count=seen_count+1, last_seen=? "
            "WHERE ats=? AND company=? AND url_pattern=?", (now, ats, company, url_pattern))
    conn.commit()


def get_submit_endpoints(conn, ats: str, company: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM submit_endpoints WHERE ats=? AND company=? ORDER BY seen_count DESC",
        (ats, company)).fetchall()
    return [dict(r) for r in rows]


def stats(conn, ats: str) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) FILTER (WHERE active=1), COALESCE(SUM(hits),0), COUNT(*) "
        "FROM mapping_cache WHERE ats=?", (ats,)).fetchone()
    return {"active_mappings": row[0], "total_hits": row[1], "total_mappings": row[2]}
