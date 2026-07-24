"""Operator approve-for-auto-apply path.

database.queue_policy dispatches only gate_result='eligible' AND
automatability='auto' rows. For a visa profile essentially no posting
explicitly promises sponsorship, so the live queue is STRUCTURALLY EMPTY
(zero 'eligible' rows). This adds a sanctioned, logged operator override:
gate_result='unknown' rows the operator explicitly approves
(operator_approved=1) become queue-visible — review-first stays the default.

PARAMOUNT INVARIANT (tested here in the SQL itself, not just the CLI):
approval must NEVER make gate_result='ineligible' rows dispatchable.

All $0: temp SQLite DBs via init_db; the live E:\\applypilot-data DB is never
touched.
"""

from __future__ import annotations

import sqlite3

from typer.testing import CliRunner

from applypilot import config, database as db
from applypilot.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INSERT_COLS = (
    "url, fit_score, gate_result, automatability, gated_at, application_url, "
    "apply_status, applied_at, apply_attempts, operator_approved, approved_at, "
    "discovered_at"
)


def _seed(conn, rows):
    conn.executemany(
        f"INSERT INTO jobs ({_INSERT_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    conn.commit()


def _row(url, gate_result, *, score=9, automatability="auto", app_url=None,
         apply_status=None, applied_at=None, apply_attempts=0,
         operator_approved=0, approved_at=None, discovered_at="2026-07-20T00:00:00"):
    return (url, score, gate_result, automatability, "2026-07-01T00:00:00",
            app_url or f"https://boards.greenhouse.io/x/jobs/{url}", apply_status,
            applied_at, apply_attempts, operator_approved, approved_at, discovered_at)


def _selected(conn, min_score=8, max_age_hours=None):
    frag, params = db.queue_policy(min_score=min_score, max_age_hours=max_age_hours)
    return {r[0] for r in conn.execute(f"SELECT url FROM jobs WHERE {frag}", params).fetchall()}


def _redirect_db(tmp_path, monkeypatch):
    """Point the CLI's _bootstrap()/get_connection() at a throwaway tmp DB."""
    db_path = tmp_path / "applypilot.db"
    db.close_connection(db_path)
    db.close_connection()
    monkeypatch.setattr(db, "DB_PATH", db_path)
    return db_path


# ---------------------------------------------------------------------------
# queue_policy SQL — the override honored, the invariant enforced
# ---------------------------------------------------------------------------

def test_fragment_still_advertises_eligible_and_auto():
    # Existing contract stays intact; the OR-branch is additive.
    frag, params = db.queue_policy(min_score=8)
    assert "gate_result = 'eligible'" in frag
    assert "automatability = 'auto'" in frag
    assert "operator_approved = 1" in frag
    assert "gate_result = 'unknown'" in frag
    assert 8 in params


def test_approved_unknown_is_selected(tmp_path):
    db.init_db(tmp_path / "a.db")
    conn = db.get_connection(tmp_path / "a.db")
    _seed(conn, [_row("appr", "unknown", operator_approved=1, approved_at="2026-07-22T00:00:00")])
    assert _selected(conn) == {"appr"}


def test_unknown_without_approval_is_not_selected(tmp_path):
    db.init_db(tmp_path / "b.db")
    conn = db.get_connection(tmp_path / "b.db")
    _seed(conn, [_row("noappr", "unknown", operator_approved=0)])
    assert _selected(conn) == set()


def test_ineligible_even_when_approved_is_never_selected(tmp_path):
    # THE paramount invariant — hard rejections stay rejected, approved or not.
    db.init_db(tmp_path / "c.db")
    conn = db.get_connection(tmp_path / "c.db")
    _seed(conn, [_row("inelig", "ineligible", operator_approved=1, approved_at="2026-07-22T00:00:00")])
    assert _selected(conn) == set()


def test_eligible_leg_unchanged(tmp_path):
    db.init_db(tmp_path / "d.db")
    conn = db.get_connection(tmp_path / "d.db")
    _seed(conn, [_row("elig", "eligible", operator_approved=0)])
    assert _selected(conn) == {"elig"}


def test_null_operator_approved_is_not_approved(tmp_path):
    # Legacy rows read NULL (or 0) for operator_approved — must behave as unapproved.
    db.init_db(tmp_path / "e.db")
    conn = db.get_connection(tmp_path / "e.db")
    _seed(conn, [_row("legacy", "unknown", operator_approved=None)])
    assert _selected(conn) == set()


def test_mixed_population(tmp_path):
    db.init_db(tmp_path / "mix.db")
    conn = db.get_connection(tmp_path / "mix.db")
    _seed(conn, [
        _row("u_ok", "eligible"),                                   # in (eligible leg)
        _row("u_inelig", "ineligible", operator_approved=1),        # out (hard reject)
        _row("u_unk_appr", "unknown", operator_approved=1),         # in (override)
        _row("u_unk_noappr", "unknown", operator_approved=0),       # out (review-first)
        _row("u_unk_null", "unknown", operator_approved=None),      # out (NULL == unapproved)
    ])
    assert _selected(conn) == {"u_ok", "u_unk_appr"}


def test_approved_unknown_still_respects_attempt_cap(tmp_path):
    db.init_db(tmp_path / "cap.db")
    conn = db.get_connection(tmp_path / "cap.db")
    cap = config.DEFAULTS["max_apply_attempts"]
    _seed(conn, [
        _row("under", "unknown", operator_approved=1, apply_attempts=cap - 1),
        _row("capped", "unknown", operator_approved=1, apply_status="failed", apply_attempts=cap),
        _row("applied", "unknown", operator_approved=1, apply_status="applied", applied_at="2026-07-10"),
    ])
    assert _selected(conn) == {"under"}


def test_expired_twin_does_not_hide_live_sibling_from_policy(tmp_path):
    # queue_policy is the single authority: a confirmed-EXPIRED row is blocked
    # by its own apply_status leg, but it must NOT hide a LIVE same-title twin.
    # (The launcher's durable-dedup carve-out enforces the same rule so the
    # preview never diverges from dispatch — see test_apply_freshness_gate.)
    db.init_db(tmp_path / "twin.db")
    conn = db.get_connection(tmp_path / "twin.db")
    _seed(conn, [
        _row("live", "unknown", operator_approved=1, apply_status=None,
             app_url="https://job-boards.greenhouse.io/twilio/jobs/7985808"),
        _row("dead", "unknown", operator_approved=1, apply_status="expired",
             app_url="https://job-boards.greenhouse.io/twilio/jobs/1111111"),
    ])
    # Live IN, expired OUT — the expired row is blocked, the live one survives.
    assert _selected(conn) == {"live"}


def test_approved_unknown_still_respects_age_and_score(tmp_path):
    db.init_db(tmp_path / "age.db")
    conn = db.get_connection(tmp_path / "age.db")
    from datetime import datetime, timezone, timedelta
    fresh = datetime.now(timezone.utc).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(hours=200)).isoformat()
    _seed(conn, [
        _row("fresh", "unknown", operator_approved=1, discovered_at=fresh),
        _row("stale", "unknown", operator_approved=1, discovered_at=stale),
        _row("lowscore", "unknown", operator_approved=1, score=5, discovered_at=fresh),
    ])
    assert _selected(conn, min_score=8, max_age_hours=48) == {"fresh"}


# ---------------------------------------------------------------------------
# Migration — ensure_columns adds the new columns to an old-schema DB
# ---------------------------------------------------------------------------

def test_ensure_columns_migrates_old_schema(tmp_path):
    dbfile = tmp_path / "old.db"
    conn = sqlite3.connect(dbfile)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE jobs (url TEXT PRIMARY KEY, fit_score INTEGER, "
        "gate_result TEXT, automatability TEXT, apply_status TEXT, "
        "applied_at TEXT, apply_attempts INTEGER, discovered_at TEXT)"
    )
    conn.commit()

    added = db.ensure_columns(conn)
    assert "operator_approved" in added
    assert "approved_at" in added

    # insert + query with the newly migrated columns works
    conn.execute(
        "INSERT INTO jobs (url, fit_score, gate_result, automatability, "
        "operator_approved) VALUES ('u', 9, 'unknown', 'auto', 1)"
    )
    conn.commit()
    frag, params = db.queue_policy(min_score=8)
    urls = {r[0] for r in conn.execute(f"SELECT url FROM jobs WHERE {frag}", params).fetchall()}
    assert urls == {"u"}
    conn.close()


# ---------------------------------------------------------------------------
# CLI approve / --list / --top / --revoke
# ---------------------------------------------------------------------------

def _approved_flag(conn, url):
    row = conn.execute(
        "SELECT operator_approved, approved_at FROM jobs WHERE url=?", (url,)
    ).fetchone()
    return (row[0], row[1])


def test_cli_approve_by_url_sets_flag_and_timestamp(tmp_path, monkeypatch):
    _redirect_db(tmp_path, monkeypatch)
    db.init_db()
    conn = db.get_connection()
    _seed(conn, [_row("https://boards.greenhouse.io/x/jobs/u1", "unknown", operator_approved=0)])

    result = runner.invoke(app, ["approve", "https://boards.greenhouse.io/x/jobs/u1"])
    assert result.exit_code == 0, result.stdout

    approved, approved_at = _approved_flag(conn, "https://boards.greenhouse.io/x/jobs/u1")
    assert approved == 1
    assert approved_at is not None
    # now queue-visible
    assert "https://boards.greenhouse.io/x/jobs/u1" in _selected(conn)


def test_cli_approve_refuses_ineligible(tmp_path, monkeypatch):
    _redirect_db(tmp_path, monkeypatch)
    db.init_db()
    conn = db.get_connection()
    _seed(conn, [_row("https://linkedin.com/jobs/bad", "ineligible", operator_approved=0)])

    result = runner.invoke(app, ["approve", "https://linkedin.com/jobs/bad"])
    # command completes but the row is refused, per-row, and never flagged.
    approved, approved_at = _approved_flag(conn, "https://linkedin.com/jobs/bad")
    assert approved == 0
    assert approved_at is None
    assert "ineligible" in result.stdout.lower() or "refus" in result.stdout.lower() or "skip" in result.stdout.lower()
    assert "https://linkedin.com/jobs/bad" not in _selected(conn)


def test_cli_approve_revoke_clears_flag(tmp_path, monkeypatch):
    _redirect_db(tmp_path, monkeypatch)
    db.init_db()
    conn = db.get_connection()
    _seed(conn, [_row("https://boards.greenhouse.io/x/jobs/r1", "unknown",
                      operator_approved=1, approved_at="2026-07-22T00:00:00")])
    assert "https://boards.greenhouse.io/x/jobs/r1" in _selected(conn)

    result = runner.invoke(app, ["approve", "--revoke", "https://boards.greenhouse.io/x/jobs/r1"])
    assert result.exit_code == 0, result.stdout

    approved, _ = _approved_flag(conn, "https://boards.greenhouse.io/x/jobs/r1")
    assert approved == 0
    assert "https://boards.greenhouse.io/x/jobs/r1" not in _selected(conn)


def test_cli_approve_list_shows_only_candidates(tmp_path, monkeypatch):
    _redirect_db(tmp_path, monkeypatch)
    db.init_db()
    conn = db.get_connection()
    _seed(conn, [
        _row("https://boards.greenhouse.io/x/jobs/cand9", "unknown", score=9),
        _row("https://boards.greenhouse.io/x/jobs/cand8", "unknown", score=8),
        _row("https://linkedin.com/jobs/hardno", "ineligible", score=9),
        _row("https://boards.greenhouse.io/x/jobs/alreadyelig", "eligible", score=9),
        _row("https://boards.greenhouse.io/x/jobs/lowscore", "unknown", score=5),
    ])
    result = runner.invoke(app, ["approve", "--list", "--min-score", "8"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "cand9" in out and "cand8" in out
    assert "hardno" not in out          # ineligible never a candidate
    assert "alreadyelig" not in out     # eligible already dispatchable, not a candidate
    assert "lowscore" not in out        # below min-score
    # --list must not mutate anything
    assert _selected(conn) == {"https://boards.greenhouse.io/x/jobs/alreadyelig"}


def test_cli_approve_top_n_approves_ranked_head(tmp_path, monkeypatch):
    _redirect_db(tmp_path, monkeypatch)
    db.init_db()
    conn = db.get_connection()
    _seed(conn, [
        _row("https://boards.greenhouse.io/x/jobs/hi", "unknown", score=9),
        _row("https://boards.greenhouse.io/x/jobs/lo", "unknown", score=8),
        _row("https://linkedin.com/jobs/never", "ineligible", score=9),
    ])
    result = runner.invoke(app, ["approve", "--top", "1", "--min-score", "8"])
    assert result.exit_code == 0, result.stdout

    assert _approved_flag(conn, "https://boards.greenhouse.io/x/jobs/hi")[0] == 1
    assert _approved_flag(conn, "https://boards.greenhouse.io/x/jobs/lo")[0] == 0
    # ineligible untouched and never dispatchable
    assert _approved_flag(conn, "https://linkedin.com/jobs/never")[0] == 0
    assert _selected(conn) == {"https://boards.greenhouse.io/x/jobs/hi"}


def test_cli_approve_top_respects_ats_filter(tmp_path, monkeypatch):
    _redirect_db(tmp_path, monkeypatch)
    db.init_db()
    conn = db.get_connection()
    conn.executemany(
        f"INSERT INTO jobs ({_INSERT_COLS}, ats) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            _row("https://boards.greenhouse.io/x/jobs/gh", "unknown", score=9) + ("greenhouse",),
            _row("https://jobs.lever.co/y/lv", "unknown", score=9) + ("lever",),
        ],
    )
    conn.commit()
    result = runner.invoke(app, ["approve", "--top", "5", "--ats", "greenhouse", "--min-score", "8"])
    assert result.exit_code == 0, result.stdout
    assert _approved_flag(conn, "https://boards.greenhouse.io/x/jobs/gh")[0] == 1
    assert _approved_flag(conn, "https://jobs.lever.co/y/lv")[0] == 0
