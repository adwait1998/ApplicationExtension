from applypilot import database as db
from applypilot.discovery.atlas import boards_repo as repo
from applypilot.discovery.atlas import source_runs as sr
from applypilot.discovery.atlas import telemetry


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


def test_board_coverage_by_ats_and_status(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "a", source="x")
    repo.set_status(conn, "greenhouse", "a", "active")
    repo.upsert_board(conn, "greenhouse", "b", source="x")  # candidate
    repo.upsert_board(conn, "lever", "c", source="x")
    repo.set_status(conn, "lever", "c", "dead")
    cov = telemetry.board_coverage(conn)
    assert cov["greenhouse"]["active"] == 1 and cov["greenhouse"]["candidate"] == 1
    assert cov["lever"]["dead"] == 1


def test_fresh_eligible_depth_counts_recent_eligible_atlas_rows(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO jobs (url, strategy, gate_result, gated_at, fit_score) "
                 "VALUES ('u1','atlas:greenhouse','eligible',datetime('now'),NULL)")
    conn.execute("INSERT INTO jobs (url, strategy, gate_result, gated_at, fit_score) "
                 "VALUES ('u2','atlas:ashby','ineligible',datetime('now'),NULL)")
    conn.execute("INSERT INTO jobs (url, strategy, gate_result, gated_at, fit_score) "
                 "VALUES ('u3','ats_api:greenhouse','eligible',datetime('now'),NULL)")
    conn.commit()
    depth = telemetry.fresh_eligible_depth(conn, source_prefix="atlas:")
    assert depth == 1                                    # only u1 (eligible + atlas)


def test_poll_volume_sums_requests(tmp_path):
    conn = _conn(tmp_path)
    r = sr.open_run(conn, source="atlas")
    sr.finish_run(conn, r, requests=120, boards_polled=100)
    r2 = sr.open_run(conn, source="atlas")
    sr.finish_run(conn, r2, requests=80, boards_polled=70)
    vol = telemetry.poll_volume(conn)
    assert vol["total_requests"] == 200 and vol["runs"] == 2


def test_go_no_go_verdict(tmp_path):
    conn = _conn(tmp_path)
    # healthy: eligible depth >= threshold, no run errored, requests modest
    conn.execute("INSERT INTO jobs (url, strategy, gate_result, gated_at) "
                 "VALUES ('u1','atlas:greenhouse','eligible',datetime('now'))")
    for u in ("u2", "u3", "u4", "u5"):
        conn.execute("INSERT INTO jobs (url, strategy, gate_result, gated_at) "
                     f"VALUES ('{u}','atlas:greenhouse','eligible',datetime('now'))")
    conn.commit()
    r = sr.open_run(conn, source="atlas")
    sr.finish_run(conn, r, requests=100, boards_polled=100)
    verdict = telemetry.go_no_go(conn, min_fresh_eligible=5, max_error_runs=0)
    assert verdict["go"] is True
    assert "fresh_eligible_depth" in verdict["signals"]


def _seed_gated(conn, url, gate_result, *, loc="PASS", sen="PASS", auto="PASS", spo="PASS"):
    import json as _j
    reasons = _j.dumps([
        {"rule": "location", "result": loc, "code": "x"},
        {"rule": "seniority", "result": sen, "code": "x"},
        {"rule": "automatability", "result": auto, "code": "x"},
        {"rule": "sponsorship", "result": spo, "code": "x"},
    ])
    conn.execute("INSERT INTO jobs (url, strategy, gate_result, gate_reasons) VALUES (?,?,?,?)",
                 (url, "atlas:greenhouse", gate_result, reasons))
    conn.commit()


def test_review_ready_counts_sponsorship_only_unknown_for_visa(tmp_path):
    from applypilot import database as db
    from applypilot.discovery.atlas import telemetry
    db.init_db(tmp_path / "t.db"); conn = db.get_connection(tmp_path / "t.db")
    _seed_gated(conn, "elig", "eligible")                                   # counts always
    _seed_gated(conn, "spo_only", "unknown", spo="UNKNOWN")                 # visa: review-ready
    _seed_gated(conn, "loc_bad", "ineligible", loc="REJECT", spo="UNKNOWN") # never (hard reject)
    _seed_gated(conn, "sen_bad", "unknown", sen="REJECT", spo="UNKNOWN")    # never (2 opens)
    # visa profile: eligible + sponsorship-only-unknown = 2
    assert telemetry.review_ready_depth(conn, needs_sponsorship=True) == 2
    # non-visa: only eligible = 1
    assert telemetry.review_ready_depth(conn, needs_sponsorship=False) == 1

def test_go_no_go_reports_both_depths(tmp_path):
    from applypilot import database as db
    from applypilot.discovery.atlas import telemetry
    db.init_db(tmp_path / "g.db"); conn = db.get_connection(tmp_path / "g.db")
    for i in range(6):
        _seed_gated(conn, f"s{i}", "unknown", spo="UNKNOWN")
    r = telemetry.go_no_go(conn, needs_sponsorship=True)
    assert r["signals"]["review_ready_depth"] == 6 and r["signals"]["fresh_eligible_depth"] == 0
    assert r["go"] is True                                                  # 6 >= 5, visa review-ready
    r2 = telemetry.go_no_go(conn, needs_sponsorship=False)
    assert r2["go"] is False                                                # 0 eligible for non-visa
