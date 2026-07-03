from applypilot import database as db
from applypilot.discovery.atlas import boards_repo as repo


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


def test_upsert_inserts_then_updates(tmp_path):
    conn = _conn(tmp_path)
    assert repo.upsert_board(conn, "greenhouse", "chime", company_name="Chime", source="yaml_import") is True
    # second upsert of same PK is an update, not a new row
    assert repo.upsert_board(conn, "greenhouse", "chime", company_name="Chime Inc", source="db_mining") is False
    rows = repo.get_all(conn)
    assert len(rows) == 1
    assert rows[0]["company_name"] == "Chime Inc"        # updated
    assert rows[0]["source"] == "yaml_import"            # source is NOT overwritten (first-seen wins)
    assert rows[0]["status"] == "candidate"
    assert rows[0]["first_seen"]


def test_upsert_is_case_normalized(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "Greenhouse", "Chime", source="yaml_import")
    repo.upsert_board(conn, "greenhouse", "chime", source="db_mining")
    assert len(repo.get_all(conn)) == 1                  # folded to one row


def test_get_boards_by_ring_filters(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "a", source="x")
    repo.upsert_board(conn, "lever", "b", source="x")
    repo.upsert_board(conn, "ashby", "c", source="x")
    repo.set_ring(conn, "greenhouse", "a", 0)
    repo.set_ring(conn, "lever", "b", 1)
    repo.set_ring(conn, "ashby", "c", 2)
    repo.set_status(conn, "greenhouse", "a", "active")
    repo.set_status(conn, "lever", "b", "active")
    repo.set_status(conn, "ashby", "c", "active")
    got = repo.get_boards_by_ring(conn, rings=(0, 1))
    tokens = {(r["ats"], r["token"]) for r in got}
    assert tokens == {("greenhouse", "a"), ("lever", "b")}   # ring 2 excluded


def test_get_boards_by_ring_excludes_dead_and_candidate(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "live", source="x")
    repo.upsert_board(conn, "greenhouse", "dead", source="x")
    repo.upsert_board(conn, "greenhouse", "unval", source="x")
    for t in ("live", "dead", "unval"):
        repo.set_ring(conn, "greenhouse", t, 0)
    repo.set_status(conn, "greenhouse", "live", "active")
    repo.set_status(conn, "greenhouse", "dead", "dead")
    # 'unval' stays 'candidate'
    got = {r["token"] for r in repo.get_boards_by_ring(conn, rings=(0, 1))}
    assert got == {"live"}


def test_mark_checked_records_diff_and_clears_error(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "a", source="x")
    repo.mark_dead(conn, "greenhouse", "a")              # bumps error_streak, sets status dead
    repo.mark_checked(conn, "greenhouse", "a",
                      job_id_set_hash="h1", job_count=10, new_last_poll=10, changed=True)
    row = repo.get(conn, "greenhouse", "a")
    assert row["job_id_set_hash"] == "h1"
    assert row["job_count"] == 10 and row["new_last_poll"] == 10
    assert row["error_streak"] == 0                      # a successful check clears the streak
    assert row["status"] == "active"                     # revived
    assert row["last_checked"] and row["last_changed"]


def test_mark_checked_unchanged_leaves_last_changed(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "a", source="x")
    repo.mark_checked(conn, "greenhouse", "a", job_id_set_hash="h1", job_count=5, new_last_poll=5, changed=True)
    first_changed = repo.get(conn, "greenhouse", "a")["last_changed"]
    repo.mark_checked(conn, "greenhouse", "a", job_id_set_hash="h1", job_count=5, new_last_poll=0, changed=False)
    row = repo.get(conn, "greenhouse", "a")
    assert row["last_changed"] == first_changed          # unchanged poll does not move last_changed
    assert row["last_checked"] >= first_changed          # but last_checked advances


def test_mark_dead_after_streak(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "a", source="x")
    repo.mark_dead(conn, "greenhouse", "a")
    repo.mark_dead(conn, "greenhouse", "a")
    row = repo.get(conn, "greenhouse", "a")
    assert row["error_streak"] == 2 and row["status"] == "dead"
