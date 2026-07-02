from __future__ import annotations

import subprocess

import pytest

from applypilot import llm


@pytest.fixture(autouse=True)
def _reset_llm_singleton(monkeypatch):
    llm._instance = None
    for key in (
        "APPLYPILOT_LLM_PROVIDER",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "CLAUDE_MODEL",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "LLM_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    llm._instance = None


def test_claude_provider_override_uses_claude_code_client(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.setenv("LLM_MODEL", "sonnet")
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: "claude")

    client = llm.get_client()

    assert isinstance(client, llm.ClaudeCodeClient)
    assert client.model == "sonnet"


def test_claude_code_client_invokes_cli_with_plain_prompt(monkeypatch):
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: "claude")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout='{"title":"Designer"}\n', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = llm.ClaudeCodeClient("claude-haiku-4-5-20251001")

    response = client.chat([
        {"role": "system", "content": "Return JSON only."},
        {"role": "user", "content": "Tailor this resume."},
    ])

    assert response == '{"title":"Designer"}'
    cmd, kwargs = calls[0]
    assert cmd[:4] == ["claude", "--model", "claude-haiku-4-5-20251001", "-p"]
    assert "--output-format" in cmd
    assert "SYSTEM INSTRUCTIONS:" in kwargs["input"]
    assert "USER REQUEST:" in kwargs["input"]
