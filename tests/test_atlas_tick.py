import httpx

from applypilot import database as db
from applypilot.discovery.atlas import boards_repo as repo
from applypilot.discovery.atlas import source_runs as sr
from applypilot.discovery.atlas import tick


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


POLICY = {"geo": {"remote_ok": True, "remote_scope": "US", "onsite_regions": ["us-ca"]},
          "seniority": {"accept_bands": ["senior"], "ic_only": True},
          "needs_sponsorship": False, "workday_accounts": []}


def _gh(ids):
    return {"jobs": [{"id": i, "title": "Senior Product Designer",
                      "absolute_url": f"https://boards.greenhouse.io/tickco/jobs/{i}",
                      "location": {"name": "Remote - US"}, "content": "design",
                      "first_published": "2026-07-01T00:00:00Z"} for i in ids]}


def test_tick_polls_only_ring01_and_records_source_run(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "tickco", source="x")
    repo.set_status(conn, "greenhouse", "tickco", "active")
    repo.set_ring(conn, "greenhouse", "tickco", 0)
    # a cold ring-2 board must be ignored by the client tick
    repo.upsert_board(conn, "greenhouse", "coldco", source="x")
    repo.set_status(conn, "greenhouse", "coldco", "active")
    repo.set_ring(conn, "greenhouse", "coldco", 2)

    polled = []

    def handler(req):
        polled.append(str(req.url))
        return httpx.Response(200, json=_gh([1, 2]))

    res = tick.run_tick(conn, POLICY, client=_client(handler), budget=100)
    assert res["boards_polled"] == 1                    # only tickco (ring 0)
    assert all("tickco" in u for u in polled)           # coldco never fetched
    assert res["jobs_new"] == 2
    runs = sr.recent_runs(conn, source="atlas", limit=1)
    assert runs and runs[0]["boards_polled"] == 1 and runs[0]["finished_at"]


def test_tick_is_idempotent_second_run_no_new_jobs(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "tickco", source="x")
    repo.set_status(conn, "greenhouse", "tickco", "active")
    repo.set_ring(conn, "greenhouse", "tickco", 0)

    def handler(req):
        return httpx.Response(200, json=_gh([1]))

    tick.run_tick(conn, POLICY, client=_client(handler), budget=100)
    before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    # second tick: board reassigned; unchanged id-set -> no new rows.
    tick.run_tick(conn, POLICY, client=_client(handler), budget=100, force_due=True)
    after = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert before == after


def test_tick_reassigns_rings_before_selecting(tmp_path):
    conn = _conn(tmp_path)
    # board with no ring yet must get assigned and (being fresh) become warm/hot -> polled
    repo.upsert_board(conn, "greenhouse", "newco", source="x")
    repo.set_status(conn, "greenhouse", "newco", "active")   # ring is NULL

    def handler(req):
        return httpx.Response(200, json=_gh([1]))

    res = tick.run_tick(conn, POLICY, client=_client(handler), budget=100)
    assert res["boards_polled"] == 1
    assert repo.get(conn, "greenhouse", "newco")["ring"] in (0, 1)
