# tests/test_shared_settings.py
from applypilot.webui import shared_settings as ss


def test_defaults_are_safe(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.SHARED_DIR", tmp_path)
    s = ss.load_shared_settings()
    assert s["autopilot_enabled"] is False
    assert s["autopilot_profiles"] == []
    assert s["spend_cap_usd_per_day"] == 5.0


def test_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.SHARED_DIR", tmp_path)
    ss.update_shared_settings({"spend_cap_usd_per_day": 9.0,
                               "autopilot_profiles": ["nida", "adwait"]})
    s = ss.load_shared_settings()
    assert s["spend_cap_usd_per_day"] == 9.0
    assert s["autopilot_profiles"] == ["nida", "adwait"]
    assert s["autopilot_enabled"] is False


def test_corrupt_file_resolves_to_safe_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.SHARED_DIR", tmp_path)
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    s = ss.load_shared_settings()
    assert s["autopilot_enabled"] is False


def test_rejects_nonpositive_spend_cap(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr("applypilot.config.SHARED_DIR", tmp_path)
    with pytest.raises(ValueError):
        ss.update_shared_settings({"spend_cap_usd_per_day": 0})
