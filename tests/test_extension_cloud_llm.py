"""AI features only send text off this computer with explicit opt-in."""
import json

import pytest
from fastapi.testclient import TestClient

from applypilot.extension import answers, llm_util
from applypilot.extension import settings as ext_settings
from applypilot.extension.schema import FieldDescriptor as F, SkipResult
from applypilot.extension.server import create_app


@pytest.fixture
def cloud_env(monkeypatch):
    for k in ("LLM_URL", "OPENAI_API_KEY", "LLM_PROVIDER", "APPLYPILOT_LLM_PROVIDER",
              ext_settings.CLOUD_LLM_ENV, "APPLYPILOT_DRAFTS", "APPLYPILOT_ANSWERS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-key")


def test_localhost_endpoint_is_local_and_remote_one_is_not(monkeypatch, cloud_env):
    monkeypatch.setenv("LLM_URL", "http://localhost:11434/v1")
    monkeypatch.setattr(llm_util, "_local_endpoint_up", lambda url: True)
    assert llm_util.provider_info()["local"] is True
    assert llm_util.cloud_block_reason(None) is None
    monkeypatch.setenv("LLM_URL", "https://llm.example.com/v1")
    info = llm_util.provider_info()
    assert info["local"] is False and info["provider"] == "remote-endpoint"


def test_cloud_provider_is_blocked_until_allowed(tmp_path, cloud_env):
    reason = llm_util.cloud_block_reason(tmp_path)
    assert reason and "Google Gemini" in reason
    ext_settings.save_settings(tmp_path, {"cloud_llm_allowed": True})
    assert llm_util.cloud_block_reason(tmp_path) is None


def test_env_can_allow_cloud(tmp_path, cloud_env, monkeypatch):
    monkeypatch.setenv(ext_settings.CLOUD_LLM_ENV, "1")
    assert llm_util.cloud_block_reason(tmp_path) is None


def test_blocked_drafts_never_call_a_model_and_say_why(tmp_path, cloud_env, monkeypatch):
    monkeypatch.setattr(llm_util, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(answers, "drafts_enabled", lambda app_dir=None: True)
    monkeypatch.setattr(llm_util, "get_llm_client",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("model called")))
    r = answers.match(F(id="t", label="Describe a project you are proud of", tag="textarea"), {},
                      app_dir=tmp_path)
    assert isinstance(r, SkipResult) and "off this computer" in r.reason


def _client(tmp_path):
    (tmp_path / "profile.json").write_text(json.dumps({"personal": {"full_name": ""}}), encoding="utf-8")
    app = create_app(app_dir=tmp_path, root=tmp_path)
    return TestClient(app), {"X-ApplyPilot-Token": app.state.token}


def test_endpoints_report_and_enforce(tmp_path, cloud_env, monkeypatch):
    c, h = _client(tmp_path)
    health = c.get("/health", headers=h).json()
    assert health["llm_provider"] == "gemini" and health["llm_local"] is False
    assert health["cloud_llm_allowed"] is False and health["llm_blocked_reason"]
    assert c.post("/cover-letter", headers=h, json={"urls": [], "page_text": "x" * 300}).status_code == 403

    called = []

    class Recorder:
        def chat(self, messages, **kw):
            called.append(1)
            return "{}"

    monkeypatch.setattr(llm_util, "get_llm_client", lambda *a, **k: Recorder())
    r = c.post("/profile/import-resume", headers=h,
               files={"file": ("resume.txt", b"Taylor Morgan\ntaylor@example.com\n\nSKILLS\nFigma", "text/plain")})
    assert r.status_code == 200 and called == []
    assert any("off this computer" in w for w in r.json()["warnings"])
    assert r.json()["profile"]["personal"]["email"] == "taylor@example.com"   # deterministic still imports

    s = c.post("/settings", headers=h, json={"cloud_llm_allowed": True}).json()
    assert s["cloud_llm_allowed"] is True and s["llm"]["provider"] == "gemini"
    assert c.get("/health", headers=h).json()["llm_blocked_reason"] is None
