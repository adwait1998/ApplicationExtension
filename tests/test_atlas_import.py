from applypilot import database as db
from applypilot.discovery.atlas import boards_repo as repo
from applypilot.discovery.atlas import importer


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


def test_import_seeds_boards_from_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "load_ats_registry", lambda: {
        "greenhouse": ["figma", "notion", "chime"],
        "lever": ["plaid", "spotify"],
        "ashby": ["vercel"],
        "title_keywords": ["product designer"],   # non-ATS keys ignored
    })
    conn = _conn(tmp_path)
    n = importer.import_registry(conn)
    assert n == 6
    rows = repo.get_all(conn)
    assert {(r["ats"], r["token"]) for r in rows} == {
        ("greenhouse", "figma"), ("greenhouse", "notion"), ("greenhouse", "chime"),
        ("lever", "plaid"), ("lever", "spotify"), ("ashby", "vercel"),
    }
    assert all(r["source"] == "yaml_import" and r["status"] == "candidate" for r in rows)


def test_import_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "load_ats_registry", lambda: {
        "greenhouse": ["figma"], "lever": [], "ashby": [],
    })
    conn = _conn(tmp_path)
    assert importer.import_registry(conn) == 1
    assert importer.import_registry(conn) == 0     # second run adds nothing new
    assert len(repo.get_all(conn)) == 1


def test_import_real_registry_has_56_boards(tmp_path):
    # Integration: the shipped package ats_companies.yaml (no user overlay in tmp).
    conn = _conn(tmp_path)
    n = importer.import_registry(conn)
    counts = {}
    for r in repo.get_all(conn):
        counts[r["ats"]] = counts.get(r["ats"], 0) + 1
    # Verified counts at plan authoring: gh 35, lever 3, ashby 18 = 56.
    # A user overlay merged by load_ats_registry can only ADD, so assert >=.
    assert counts.get("greenhouse", 0) >= 35
    assert counts.get("lever", 0) >= 3
    assert counts.get("ashby", 0) >= 18
    assert n >= 56
