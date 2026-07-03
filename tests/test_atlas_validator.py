import httpx

from applypilot import database as db
from applypilot.discovery.atlas import boards_repo as repo
from applypilot.discovery.atlas import politeness, validator


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


# --- politeness (pure, fake clock) ---

def test_token_bucket_rate_limits_per_host():
    t = {"now": 0.0}
    slept = []
    b = politeness.HostRateLimiter(rps=4.0, clock=lambda: t["now"],
                                   sleep=lambda s: (slept.append(s), t.__setitem__("now", t["now"] + s)))
    for _ in range(3):
        b.acquire("boards-api.greenhouse.io")
    # 3 requests at 4 rps on one host => ~0.25s min spacing between the 2nd and 3rd
    assert sum(slept) >= 0.25 - 1e-9
    # a different host is not throttled by the first host's budget
    slept.clear()
    b.acquire("api.lever.co")
    assert sum(slept) == 0


def test_backoff_grows_on_repeated_failures():
    b = politeness.Backoff(base=0.5, cap=8.0)
    assert b.delay(0) == 0.5
    assert b.delay(1) == 1.0
    assert b.delay(3) == 4.0
    assert b.delay(10) == 8.0        # capped


# --- validator (mock transport, no network) ---

def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_validate_existing_greenhouse_board_becomes_active(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "newco", source="db_mining")

    def handler(req):
        assert req.url.host == "boards-api.greenhouse.io"
        assert "content=false" in str(req.url)      # cheap existence check
        return httpx.Response(200, json={"jobs": [{"id": 1}, {"id": 2}, {"id": 3}]})

    res = validator.validate_board(conn, "greenhouse", "newco", client=_client(handler))
    assert res.ok is True and res.job_count == 3
    row = repo.get(conn, "greenhouse", "newco")
    assert row["status"] == "active" and row["job_count"] == 3


def test_validator_does_NOT_title_filter(tmp_path):
    # A board with zero design jobs still validates: membership is profile-agnostic.
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "ashby", "warehouseco", source="cc_snapshot")

    def handler(req):
        return httpx.Response(200, json={"jobs": [{"title": "Forklift Operator"}]})

    res = validator.validate_board(conn, "ashby", "warehouseco", client=_client(handler))
    assert res.ok is True and res.job_count == 1     # kept despite no matching titles
    assert repo.get(conn, "ashby", "warehouseco")["status"] == "active"


def test_validate_dead_board_404_marks_dead(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "lever", "goneco", source="db_mining")

    def handler(req):
        return httpx.Response(404, json={})

    res = validator.validate_board(conn, "lever", "goneco", client=_client(handler))
    assert res.ok is False
    row = repo.get(conn, "lever", "goneco")
    assert row["status"] == "dead" and row["error_streak"] == 1


def test_validate_empty_board_is_active_with_zero(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "emptyco", source="db_mining")

    def handler(req):
        return httpx.Response(200, json={"jobs": []})

    res = validator.validate_board(conn, "greenhouse", "emptyco", client=_client(handler))
    # exists but posts nothing right now -> still a member (may post tomorrow)
    assert res.ok is True and res.job_count == 0
    assert repo.get(conn, "greenhouse", "emptyco")["status"] == "active"
