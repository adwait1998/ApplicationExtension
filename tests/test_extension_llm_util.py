"""Tests for applypilot.extension.llm_util -- the Claude-CLI fallback used
by the extension's draft (answers.py) and résumé-import (resume_import.py)
LLM call sites.

$0, no network, no real subprocess: applypilot.llm.get_client and
applypilot.llm.ClaudeCodeClient are always monkeypatched, and
applypilot.config.find_claude_binary is always monkeypatched too, so these
tests are deterministic regardless of whether the machine running the
suite actually has the Claude Code CLI installed.
"""
from __future__ import annotations

import http.server
import socket
import threading as _threading

import pytest

from applypilot.extension import llm_util


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL", "LLM_PROVIDER",
                "APPLYPILOT_LLM_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------------------
# get_llm_client()
# ---------------------------------------------------------------------------


def test_get_llm_client_returns_the_normal_client_when_a_provider_is_configured(monkeypatch):
    sentinel = object()
    monkeypatch.setattr("applypilot.llm.get_client", lambda: sentinel)

    def _boom():
        raise AssertionError("must not consult find_claude_binary when a provider works")

    monkeypatch.setattr("applypilot.config.find_claude_binary", _boom)
    assert llm_util.get_llm_client() is sentinel


def test_get_llm_client_falls_back_to_claude_cli(monkeypatch):
    def _raise():
        raise RuntimeError("No LLM provider configured.")

    monkeypatch.setattr("applypilot.llm.get_client", _raise)
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: "/fake/claude.cmd")

    calls = []

    class _FakeClaudeClient:
        def __init__(self, model):
            calls.append(model)

    monkeypatch.setattr("applypilot.llm.ClaudeCodeClient", _FakeClaudeClient)

    client = llm_util.get_llm_client()
    assert isinstance(client, _FakeClaudeClient)
    assert calls == ["sonnet"]


def test_get_llm_client_reraises_when_no_provider_and_no_cli(monkeypatch):
    def _raise():
        raise RuntimeError("No LLM provider configured.")

    monkeypatch.setattr("applypilot.llm.get_client", _raise)
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: None)

    with pytest.raises(RuntimeError, match="No LLM provider configured"):
        llm_util.get_llm_client()


def test_get_llm_client_does_not_swallow_non_runtime_errors(monkeypatch):
    def _raise():
        raise ValueError("something else entirely")

    monkeypatch.setattr("applypilot.llm.get_client", _raise)

    def _boom():
        raise AssertionError("a non-RuntimeError must propagate, never reach the CLI fallback")

    monkeypatch.setattr("applypilot.config.find_claude_binary", _boom)
    with pytest.raises(ValueError, match="something else"):
        llm_util.get_llm_client()


# ---------------------------------------------------------------------------
# llm_available()
# ---------------------------------------------------------------------------


def test_llm_available_false_when_nothing_configured(monkeypatch):
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: None)
    assert llm_util.llm_available() == (False, "")


def test_llm_available_true_via_claude_cli_fallback(monkeypatch):
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: "/fake/claude.cmd")
    assert llm_util.llm_available() == (True, "claude-cli")


def test_llm_available_true_via_gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: None)
    assert llm_util.llm_available() == (True, "gemini")


def test_llm_available_true_via_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: None)
    assert llm_util.llm_available() == (True, "openai")


def test_llm_available_true_via_local_url_when_the_endpoint_answers(monkeypatch):
    """Precedence: a set LLM_URL selects the local provider. This used to
    assert True from the env var alone — which is the bug: configured is not
    running. Reachability itself is covered by the probe tests below."""
    monkeypatch.setenv("LLM_URL", "http://localhost:8000")
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: None)
    monkeypatch.setattr(llm_util, "_local_endpoint_up", lambda url: True)
    assert llm_util.llm_available() == (True, "local")


def test_llm_available_explicit_claude_provider_requires_the_binary(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: None)
    assert llm_util.llm_available() == (False, "claude-cli")


def test_llm_available_never_spawns_a_subprocess(monkeypatch):
    """find_claude_binary() is filesystem-only (PATH lookup + glob) -- this
    just documents/locks in that llm_available() never touches
    applypilot.llm.get_client() (which WOULD construct a real client) at
    all, by proving a get_client() call would blow up the test if made."""
    def _boom():
        raise AssertionError("llm_available() must never call get_client()")

    monkeypatch.setattr("applypilot.llm.get_client", _boom)
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: None)
    llm_util.llm_available()


# ---------------------------------------------------------------------------
# A local endpoint being CONFIGURED is not the same as it being UP. Reporting
# "available" from config alone told the Settings page drafts would work while
# every draft silently failed to an empty box.
# ---------------------------------------------------------------------------



def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _clear_probe_cache():
    llm_util._probe_cache.clear()


def test_local_llm_reported_unavailable_when_nothing_is_listening(monkeypatch):
    _clear_probe_cache()
    for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_PROVIDER", "APPLYPILOT_LLM_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_URL", f"http://127.0.0.1:{_free_port()}/v1")
    ok, provider = llm_util.llm_available()
    assert provider == "local"
    assert ok is False, "a configured-but-stopped local LLM must not be reported available"


def test_local_llm_reported_available_when_it_answers(monkeypatch):
    _clear_probe_cache()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data": []}')

        def log_message(self, *a):
            pass

    port = _free_port()
    srv = http.server.HTTPServer(("127.0.0.1", port), H)
    t = _threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_PROVIDER", "APPLYPILOT_LLM_PROVIDER"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("LLM_URL", f"http://127.0.0.1:{port}/v1")
        assert llm_util.llm_available() == (True, "local")
    finally:
        srv.shutdown()


def test_probe_result_is_cached(monkeypatch):
    """/health is polled; the probe must not cost a network round-trip each time."""
    _clear_probe_cache()
    calls = {"n": 0}
    real = llm_util.urllib.request.urlopen

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(llm_util.urllib.request, "urlopen", counting)
    url = f"http://127.0.0.1:{_free_port()}/v1"
    for _ in range(5):
        llm_util._local_endpoint_up(url)
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# On-device bridge (Chrome's built-in model via extension/llm_bridge.js)
# ---------------------------------------------------------------------------

def _no_env_provider(monkeypatch):
    def _raise():
        raise RuntimeError("No LLM provider configured.")
    monkeypatch.setattr("applypilot.llm.get_client", _raise)
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: None)


def _bridge(monkeypatch, status=None):
    from applypilot.extension import llm_bridge
    fresh = llm_bridge.Bridge()
    if status:
        fresh.report(status)
    monkeypatch.setattr(llm_bridge, "BRIDGE", fresh)
    return fresh


def test_bridge_is_used_when_no_env_provider_and_a_page_is_ready(monkeypatch):
    from applypilot.extension import llm_bridge
    _no_env_provider(monkeypatch)
    fresh = _bridge(monkeypatch, "available")
    client = llm_util.get_llm_client()
    assert isinstance(client, llm_bridge.BridgeClient) and client._bridge is fresh


def test_bridge_is_not_used_when_no_page_is_ready(monkeypatch):
    _no_env_provider(monkeypatch)
    _bridge(monkeypatch, "downloadable")
    with pytest.raises(RuntimeError):
        llm_util.get_llm_client()


def test_env_provider_still_wins_over_the_bridge(monkeypatch):
    sentinel = object()
    monkeypatch.setattr("applypilot.llm.get_client", lambda: sentinel)
    _bridge(monkeypatch, "available")
    assert llm_util.get_llm_client() is sentinel


def test_bridge_beats_the_claude_cli_fallback(monkeypatch):
    from applypilot.extension import llm_bridge

    def _raise():
        raise RuntimeError("No LLM provider configured.")
    monkeypatch.setattr("applypilot.llm.get_client", _raise)
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: "C:/fake/claude.exe")
    _bridge(monkeypatch, "available")
    assert isinstance(llm_util.get_llm_client(), llm_bridge.BridgeClient)


def test_provider_info_reports_the_bridge_as_local(monkeypatch):
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: None)
    _bridge(monkeypatch, "available")
    info = llm_util.provider_info()
    assert info == {"available": True, "provider": "chrome-on-device",
                    "label": "Chrome's built-in AI on this computer", "local": True}
    assert llm_util.cloud_block_reason() is None


def test_provider_info_without_bridge_is_unchanged(monkeypatch):
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: None)
    _bridge(monkeypatch)
    assert llm_util.provider_info()["available"] is False
