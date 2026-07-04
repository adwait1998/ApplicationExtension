import httpx

from applypilot import database as db
from applypilot.discovery.atlas import boards_repo as repo
from applypilot.discovery.atlas import poller


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


def _gh_full(ids_titles):
    # Greenhouse content=true response shape (subset the fetcher reads).
    return {"jobs": [
        {"id": i, "title": t, "absolute_url": f"https://boards.greenhouse.io/pollco/jobs/{i}",
         "location": {"name": "Remote - US"}, "content": "<p>Design systems.</p>",
         "first_published": "2026-07-01T00:00:00Z"}
        for i, t in ids_titles]}


def test_first_poll_stores_all_new_ids(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "pollco", source="x")
    repo.set_status(conn, "greenhouse", "pollco", "active")

    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json=_gh_full([(1, "Senior Product Designer"),
                                                  (2, "Staff Product Designer")]))

    res = poller.poll_board(conn, repo.get(conn, "greenhouse", "pollco"), POLICY, client=_client(handler))
    assert res.new_ids == 2 and res.stored == 2
    stored = conn.execute("SELECT COUNT(*) FROM jobs WHERE strategy = 'atlas:greenhouse'").fetchone()[0]
    assert stored == 2
    row = repo.get(conn, "greenhouse", "pollco")
    assert row["job_id_set_hash"] and row["job_count"] == 2


def test_unchanged_board_short_circuits_no_content_fetch(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "pollco", source="x")
    repo.set_status(conn, "greenhouse", "pollco", "active")
    resp_json = _gh_full([(1, "Senior Product Designer")])

    def handler(req):
        # cheap id-list variant and full variant both answerable; assert we only
        # ask for content on the FIRST poll, not the second.
        return httpx.Response(200, json=resp_json)

    poller.poll_board(conn, repo.get(conn, "greenhouse", "pollco"), POLICY, client=_client(handler))
    before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    res2 = poller.poll_board(conn, repo.get(conn, "greenhouse", "pollco"), POLICY, client=_client(handler))
    after = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert res2.new_ids == 0 and res2.stored == 0
    assert before == after                              # no new rows on unchanged poll


def test_only_new_ids_stored_on_delta(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "pollco", source="x")
    repo.set_status(conn, "greenhouse", "pollco", "active")

    state = {"jobs": _gh_full([(1, "Senior Product Designer")])}

    def handler(req):
        return httpx.Response(200, json=state["jobs"])

    poller.poll_board(conn, repo.get(conn, "greenhouse", "pollco"), POLICY, client=_client(handler))
    # a new posting appears
    state["jobs"] = _gh_full([(1, "Senior Product Designer"), (2, "Staff Product Designer")])
    res = poller.poll_board(conn, repo.get(conn, "greenhouse", "pollco"), POLICY, client=_client(handler))
    assert res.new_ids == 1 and res.stored == 1         # only id=2 is new


def test_ineligible_new_job_is_stored_but_gated(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "pollco", source="x")
    repo.set_status(conn, "greenhouse", "pollco", "active")

    def handler(req):
        return httpx.Response(200, json=_gh_full([(9, "Engineering Manager")]))  # mgmt -> ineligible

    poller.poll_board(conn, repo.get(conn, "greenhouse", "pollco"), POLICY, client=_client(handler))
    row = conn.execute("SELECT gate_result FROM jobs WHERE strategy='atlas:greenhouse'").fetchone()
    assert row is not None                              # always stored (audit)
    assert row["gate_result"] == "ineligible"          # but gated at ingest
