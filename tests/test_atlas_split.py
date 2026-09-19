import sqlite3

from applypilot import database

ATLAS_TABLES = {"boards", "source_runs", "mapping_cache", "submit_endpoints"}
PROFILE_TABLES = {"jobs", "submission_ledger", "engine_control"}


def _tables(conn, schema):
    return {r[0] for r in conn.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table'")}


def test_atlas_tables_live_in_attached_db(tmp_path):
    db = tmp_path / "p.db"
    atlas = tmp_path / "shared" / "atlas.db"
    conn = database.init_db(db, atlas_path=atlas)
    assert ATLAS_TABLES <= _tables(conn, "atlas")
    assert PROFILE_TABLES <= _tables(conn, "main")
    assert not (ATLAS_TABLES & _tables(conn, "main"))


def test_unqualified_names_resolve_across_databases(tmp_path):
    """The whole point: existing SQL keeps working unchanged."""
    conn = database.init_db(tmp_path / "p.db", atlas_path=tmp_path / "atlas.db")
    # first_seen is NOT NULL in the (unchanged) boards schema -- must be
    # supplied regardless of where the table lives.
    conn.execute(
        "INSERT INTO boards (ats, token, first_seen) VALUES ('greenhouse','twilio','2026-01-01')")
    conn.commit()
    assert conn.execute("SELECT token FROM boards").fetchone()[0] == "twilio"
    conn.execute("UPDATE boards SET ring=1 WHERE token='twilio'")
    conn.commit()
    assert conn.execute("SELECT ring FROM boards").fetchone()[0] == 1


def test_atlas_is_shared_between_two_profiles(tmp_path):
    atlas = tmp_path / "atlas.db"
    a = database.init_db(tmp_path / "a.db", atlas_path=atlas)
    b = database.init_db(tmp_path / "b.db", atlas_path=atlas)
    # first_seen is NOT NULL in the (unchanged) boards schema.
    a.execute(
        "INSERT INTO boards (ats, token, first_seen) VALUES ('lever','acme','2026-01-01')")
    a.commit()
    assert b.execute("SELECT COUNT(*) FROM boards WHERE token='acme'").fetchone()[0] == 1


def test_jobs_are_NOT_shared_between_two_profiles(tmp_path):
    """The load-bearing isolation property."""
    atlas = tmp_path / "atlas.db"
    a = database.init_db(tmp_path / "a.db", atlas_path=atlas)
    b = database.init_db(tmp_path / "b.db", atlas_path=atlas)
    a.execute("INSERT INTO jobs (url, title) VALUES ('http://x','Designer')")
    a.commit()
    assert b.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_legacy_single_db_creates_all_tables_in_main(tmp_path):
    """No atlas_path => today's behavior exactly. Keeps the existing suite green."""
    conn = database.init_db(tmp_path / "legacy.db")
    assert (ATLAS_TABLES | PROFILE_TABLES) <= _tables(conn, "main")


def test_in_memory_still_works():
    conn = database.init_db(":memory:")
    assert (ATLAS_TABLES | PROFILE_TABLES) <= _tables(conn, "main")
