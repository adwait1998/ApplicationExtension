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
