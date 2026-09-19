import json
import sqlite3
from pathlib import Path

from applypilot import migrate_profiles


def _legacy_root(tmp_path):
    root = tmp_path / "data"
    (root / "logs").mkdir(parents=True)
    (root / "profile.json").write_text(json.dumps({"personal": {"name": "Nida Shah"}}),
                                       encoding="utf-8")
    (root / "resume.pdf").write_bytes(b"%PDF-1.4 fake")
    (root / "searches.yaml").write_text("queries: []\n", encoding="utf-8")
    (root / "logs" / "review.jsonl").write_text('{"x":1}\n', encoding="utf-8")
    db = root / "applypilot.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('http://a','Designer')")
    conn.execute("CREATE TABLE boards (ats TEXT, token TEXT, ring INT)")
    conn.execute("INSERT INTO boards VALUES ('greenhouse','twilio',0)")
    conn.execute("CREATE TABLE source_runs (id INTEGER PRIMARY KEY, source TEXT)")
    conn.execute("CREATE TABLE mapping_cache (ats TEXT, field_fp TEXT, binding TEXT)")
    conn.execute("CREATE TABLE submit_endpoints (ats TEXT, company TEXT)")
    conn.commit()
    conn.close()
    return root


def test_migrate_moves_personal_files_into_profile(tmp_path):
    root = _legacy_root(tmp_path)
    migrate_profiles.migrate(root, profile_id="nida")
    p = root / "profiles" / "nida"
    assert (p / "profile.json").exists()
    assert (p / "resume.pdf").exists()
    assert (p / "searches.yaml").exists()
    assert (p / "logs" / "review.jsonl").exists()
    assert (p / "applypilot.db").exists()
    assert not (root / "profile.json").exists()


def test_migrate_stamps_profile_id(tmp_path):
    root = _legacy_root(tmp_path)
    migrate_profiles.migrate(root, profile_id="nida")
    data = json.loads((root / "profiles" / "nida" / "profile.json").read_text(encoding="utf-8"))
    assert data["profile_id"] == "nida"
    assert data["personal"]["name"] == "Nida Shah"     # preserved


def test_migrate_copies_atlas_tables_and_drops_them_from_profile_db(tmp_path):
    root = _legacy_root(tmp_path)
    migrate_profiles.migrate(root, profile_id="nida")
    atlas = sqlite3.connect(root / "shared" / "atlas.db")
    assert atlas.execute("SELECT token FROM boards").fetchone()[0] == "twilio"
    prof = sqlite3.connect(root / "profiles" / "nida" / "applypilot.db")
    names = {r[0] for r in prof.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "jobs" in names
    assert not ({"boards", "source_runs", "mapping_cache", "submit_endpoints"} & names)


def test_migrate_sets_active_profile(tmp_path):
    root = _legacy_root(tmp_path)
    migrate_profiles.migrate(root, profile_id="nida")
    assert (root / "active_profile").read_text(encoding="utf-8").strip() == "nida"


def test_migrate_is_idempotent(tmp_path):
    root = _legacy_root(tmp_path)
    migrate_profiles.migrate(root, profile_id="nida")
    result = migrate_profiles.migrate(root, profile_id="nida")
    assert result["already_migrated"] is True


def test_migrate_takes_a_backup_first(tmp_path):
    root = _legacy_root(tmp_path)
    result = migrate_profiles.migrate(root, profile_id="nida")
    backup = Path(result["backup"])
    assert backup.is_dir()
    assert (backup / "profile.json").exists()       # the pre-migration state
    assert (backup / "applypilot.db").exists()


def test_migrate_never_moves_root_only_entries_into_profile(tmp_path):
    """profiles/, shared/ and active_profile must never end up inside the
    profile dir, even if they happen to already exist at the root (e.g. a
    previous partial setup, or another profile's scaffold)."""
    root = _legacy_root(tmp_path)
    (root / "shared").mkdir()
    (root / "shared" / "atlas.db").write_bytes(b"")
    (root / "active_profile").write_text("stale", encoding="utf-8")
    migrate_profiles.migrate(root, profile_id="nida")
    p = root / "profiles" / "nida"
    assert not (p / "shared").exists()
    assert not (p / "active_profile").exists()
    assert not (p / "profiles").exists()


def test_atlas_ddl_round_trips_from_real_init_db(tmp_path):
    """The plan's other tests exercise the DDL-rewrite against a hand-written,
    single-line fixture schema (e.g. "CREATE TABLE boards (ats TEXT, token
    TEXT, ring INT)"), which can't catch anything that depends on the real
    DDL's shape (multi-line, inline `--` comments, a composite PRIMARY KEY
    clause). This test proves the rewrite against the actual DDL that
    `database.init_db` emits for the four atlas tables: every column, its
    declared type, and the primary key must be identical before migration
    (main.<table>) and after (atlas.<table>) — only the schema qualifier and
    the added IF NOT EXISTS should differ."""
    from applypilot import database

    root = tmp_path / "data"
    (root / "logs").mkdir(parents=True)
    (root / "profile.json").write_text(
        json.dumps({"personal": {"name": "Nida Shah"}}), encoding="utf-8")

    db_path = root / "applypilot.db"
    conn = database.init_db(db_path)
    # database.get_connection() sets row_factory=sqlite3.Row; normalize to
    # plain tuples so this compares column values, not Row identity.
    before = {
        t: [tuple(r) for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]
        for t in migrate_profiles.ATLAS_TABLES
    }
    database.close_connection(db_path)

    migrate_profiles.migrate(root, profile_id="nida")

    atlas = sqlite3.connect(root / "shared" / "atlas.db")
    try:
        for t in migrate_profiles.ATLAS_TABLES:
            after = [tuple(r) for r in atlas.execute(f"PRAGMA table_info({t})").fetchall()]
            assert after == before[t], f"schema drifted for {t!r}: {before[t]} != {after}"
    finally:
        atlas.close()
