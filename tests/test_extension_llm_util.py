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


def test_llm_available_true_via_local_url(monkeypatch):
    monkeypatch.setenv("LLM_URL", "http://localhost:8000")
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: None)
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
