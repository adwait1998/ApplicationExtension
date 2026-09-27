"""The on-device AI bridge: the service queues a chat job, an extension page
answers it. All in-process: no browser, no network."""
from __future__ import annotations

import threading

import pytest

from applypilot.extension import llm_bridge


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _answer_next(bridge, text=None, error=None, delay=0.0):
    """Simulates an extension page: take the next job and answer it."""
    def run():
        job = bridge.next_job(hold_s=5)
        if job is not None:
            bridge.complete(job.id, text=text, error=error)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_status_is_unavailable_until_reported_and_expires():
    clock = FakeClock()
    b = llm_bridge.Bridge(clock=clock)
    assert b.status() == "unavailable" and not b.live()
    b.report("available")
    assert b.status() == "available" and b.live()
    clock.t += llm_bridge.FRESH_S + 1
    assert b.status() == "unavailable" and not b.live()


def test_unknown_status_counts_as_unavailable():
    b = llm_bridge.Bridge()
    b.report("weird")
    assert b.status() == "unavailable"


def test_next_job_returns_none_when_nothing_is_queued():
    b = llm_bridge.Bridge()
    assert b.next_job(hold_s=0.05) is None


def test_job_round_trip():
    b = llm_bridge.Bridge()
    job = b.submit([{"role": "user", "content": "hi"}], temperature=0.3)
    got = b.next_job(hold_s=0.05)
    assert got is job and got.messages == [{"role": "user", "content": "hi"}] and got.temperature == 0.3
    assert b.complete(job.id, text="hello") is True
    assert job.done.is_set() and job.text == "hello"
    assert b.complete(job.id, text="again") is False  # already completed


def test_client_chat_returns_the_page_answer():
    b = llm_bridge.Bridge()
    _answer_next(b, text="drafted answer")
    client = llm_bridge.BridgeClient(bridge=b, pickup_timeout_s=5, answer_timeout_s=5)
    assert client.chat([{"role": "user", "content": "q"}], max_tokens=256, temperature=0.3) == "drafted answer"


def test_client_chat_raises_when_no_page_picks_it_up():
    b = llm_bridge.Bridge()
    client = llm_bridge.BridgeClient(bridge=b, pickup_timeout_s=0.05, answer_timeout_s=5)
    with pytest.raises(RuntimeError, match="didn't respond"):
        client.chat([{"role": "user", "content": "q"}])
    assert b.next_job(hold_s=0.01) is None  # the abandoned job was withdrawn


def test_client_chat_raises_the_page_error():
    b = llm_bridge.Bridge()
    _answer_next(b, error="input too long")
    client = llm_bridge.BridgeClient(bridge=b, pickup_timeout_s=5, answer_timeout_s=5)
    with pytest.raises(RuntimeError, match="input too long"):
        client.chat([{"role": "user", "content": "q"}])


def test_client_ask_is_a_single_user_message():
    b = llm_bridge.Bridge()
    seen = {}

    def run():
        job = b.next_job(hold_s=5)
        seen["messages"] = job.messages
        b.complete(job.id, text="ok")

    threading.Thread(target=run, daemon=True).start()
    assert llm_bridge.BridgeClient(bridge=b, pickup_timeout_s=5, answer_timeout_s=5).ask("hello") == "ok"
    assert seen["messages"] == [{"role": "user", "content": "hello"}]


def test_client_defaults_to_the_module_bridge(monkeypatch):
    fresh = llm_bridge.Bridge()
    monkeypatch.setattr(llm_bridge, "BRIDGE", fresh)
    assert llm_bridge.BridgeClient()._bridge is fresh


# ---------------------------------------------------------------------------
# /llm/next and /llm/result
# ---------------------------------------------------------------------------

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from applypilot.extension.server import create_app, get_or_create_token  # noqa: E402


@pytest.fixture
def bridge_client(tmp_path, monkeypatch):
    fresh = llm_bridge.Bridge()
    monkeypatch.setattr(llm_bridge, "BRIDGE", fresh)
    monkeypatch.setattr(llm_bridge, "POLL_HOLD_S", 0.05)
    token = get_or_create_token(tmp_path)
    app = create_app(app_dir=tmp_path, root=tmp_path, profile={"personal": {}})
    return TestClient(app), {"X-ApplyPilot-Token": token}, fresh


def test_llm_next_needs_the_token(bridge_client):
    client, _headers, _bridge = bridge_client
    assert client.get("/llm/next?status=available").status_code == 401


def test_llm_next_records_status_and_gives_no_job_when_not_ready(bridge_client):
    client, headers, bridge = bridge_client
    bridge.submit([{"role": "user", "content": "q"}], 0.3)
    resp = client.get("/llm/next?status=downloadable", headers=headers)
    assert resp.status_code == 200 and resp.json() == {"job": None}
    assert bridge.status() == "downloadable"


def test_llm_next_hands_out_a_job_and_result_completes_it(bridge_client):
    client, headers, bridge = bridge_client
    job = bridge.submit([{"role": "system", "content": "s"}, {"role": "user", "content": "q"}], 0.3)
    resp = client.get("/llm/next?status=available", headers=headers)
    assert resp.json() == {"job": {"id": job.id, "temperature": 0.3,
                                   "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]}}
    done = client.post("/llm/result", json={"id": job.id, "text": "answer"}, headers=headers)
    assert done.json() == {"ok": True} and job.text == "answer"


def test_llm_next_with_nothing_queued_answers_none(bridge_client):
    client, headers, _bridge = bridge_client
    assert client.get("/llm/next?status=available", headers=headers).json() == {"job": None}


def test_llm_result_for_an_unknown_job(bridge_client):
    client, headers, _bridge = bridge_client
    assert client.post("/llm/result", json={"id": "nope", "error": "x"}, headers=headers).json() == {"ok": False}


def test_end_to_end_chat_through_the_endpoints(bridge_client):
    """BridgeClient.chat waits in one thread while a 'page' polls and answers."""
    client, headers, bridge = bridge_client
    result = {}

    def call():
        result["text"] = llm_bridge.BridgeClient(bridge=bridge, pickup_timeout_s=5, answer_timeout_s=5).chat(
            [{"role": "user", "content": "q"}])

    t = threading.Thread(target=call, daemon=True)
    t.start()
    job = None
    for _ in range(100):
        job = client.get("/llm/next?status=available", headers=headers).json()["job"]
        if job:
            break
    assert job is not None
    client.post("/llm/result", json={"id": job["id"], "text": "from the page"}, headers=headers)
    t.join(timeout=5)
    assert result["text"] == "from the page"
