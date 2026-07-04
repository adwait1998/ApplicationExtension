from applypilot import database as db
from applypilot.discovery.atlas import source_runs as sr


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


def test_open_and_finish_run(tmp_path):
    conn = _conn(tmp_path)
    run_id = sr.open_run(conn, source="atlas")
    assert isinstance(run_id, int)
    sr.finish_run(conn, run_id, boards_polled=10, requests=10, jobs_seen=120,
                  jobs_new=8, jobs_eligible=3, cost_usd=0.0)
    row = sr.get_run(conn, run_id)
    assert row["boards_polled"] == 10 and row["jobs_new"] == 8 and row["jobs_eligible"] == 3
    assert row["finished_at"] is not None and row["error"] is None


def test_finish_run_records_error(tmp_path):
    conn = _conn(tmp_path)
    run_id = sr.open_run(conn, source="atlas:lever")
    sr.finish_run(conn, run_id, error="429 storm")
    assert sr.get_run(conn, run_id)["error"] == "429 storm"


def test_recent_runs_ordered_newest_first(tmp_path):
    conn = _conn(tmp_path)
    a = sr.open_run(conn, source="atlas")
    sr.finish_run(conn, a)
    b = sr.open_run(conn, source="atlas")
    sr.finish_run(conn, b)
    runs = sr.recent_runs(conn, source="atlas", limit=5)
    assert [r["run_id"] for r in runs][:2] == [b, a]
