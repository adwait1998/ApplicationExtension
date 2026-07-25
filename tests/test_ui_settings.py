from applypilot.webui import settings as st


def test_defaults_autopilot_off(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    s = st.load_settings()
    assert s["autopilot_enabled"] is False               # constraint 3
    assert s["max_live_applies_per_day"] >= 1
    assert s["spend_cap_usd_per_day"] > 0
    assert s["batch"]["limit"] >= 1 and s["batch"]["dry_run"] in (True, False)


def test_save_then_load_roundtrip_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    st.save_settings({"autopilot_enabled": True, "max_live_applies_per_day": 7,
                      "spend_cap_usd_per_day": 3.5,
                      "batch": {"limit": 10, "model": "claude-haiku-4-5-20251001",
                                "workers": 2, "headless": True, "dry_run": False,
                                "min_score": 8, "max_age_hours": 24, "site_contains": None}})
    s2 = st.load_settings()
    assert s2["autopilot_enabled"] is True and s2["max_live_applies_per_day"] == 7
    assert (tmp_path / "ui_settings.json").exists()


def test_partial_update_merges_and_keeps_unknown_safe(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    st.save_settings(st.load_settings())
    merged = st.update_settings({"autopilot_enabled": True})
    assert merged["autopilot_enabled"] is True
    assert merged["max_live_applies_per_day"] == st.DEFAULT_SETTINGS["max_live_applies_per_day"]


def test_corrupt_file_falls_back_to_safe_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    (tmp_path / "ui_settings.json").write_text("{ not json", encoding="utf-8")
    s = st.load_settings()
    assert s["autopilot_enabled"] is False               # fail-safe (invariant 8)


def test_validation_rejects_absurd_caps(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    import pytest
    with pytest.raises(ValueError):
        st.update_settings({"max_live_applies_per_day": -1})
    with pytest.raises(ValueError):
        st.update_settings({"spend_cap_usd_per_day": 0})
