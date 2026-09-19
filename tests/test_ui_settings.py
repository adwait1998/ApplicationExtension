from applypilot.webui import settings as st


def test_defaults_are_safe(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    s = st.load_settings()
    assert s["max_live_applies_per_day"] >= 1
    assert s["batch"]["limit"] >= 1 and s["batch"]["dry_run"] in (True, False)


def test_autopilot_is_not_a_per_profile_setting(tmp_path, monkeypatch):
    """One runner, one wallet: autopilot and the spend cap live ONLY in
    shared_settings. Two sources of truth for autopilot is how a supervisor
    ends up running while the UI reports it off."""
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    assert "autopilot_enabled" not in st.DEFAULT_SETTINGS
    assert "spend_cap_usd_per_day" not in st.DEFAULT_SETTINGS
    assert "autopilot_enabled" not in st.load_settings()


def test_save_then_load_roundtrip_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    st.save_settings({"max_live_applies_per_day": 7,
                      "batch": {"limit": 10, "model": "claude-haiku-4-5-20251001",
                                "workers": 2, "headless": True, "dry_run": False,
                                "min_score": 8, "max_age_hours": 24, "site_contains": None}})
    s2 = st.load_settings()
    assert s2["max_live_applies_per_day"] == 7
    assert s2["batch"]["workers"] == 2
    assert (tmp_path / "ui_settings.json").exists()


def test_partial_update_merges_and_keeps_unknown_safe(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    st.save_settings(st.load_settings())
    merged = st.update_settings({"max_live_applies_per_day": 3})
    assert merged["max_live_applies_per_day"] == 3
    assert merged["batch"]["limit"] == st.DEFAULT_SETTINGS["batch"]["limit"]


def test_corrupt_file_falls_back_to_safe_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    (tmp_path / "ui_settings.json").write_text("{ not json", encoding="utf-8")
    s = st.load_settings()
    assert s["max_live_applies_per_day"] == st.DEFAULT_SETTINGS["max_live_applies_per_day"]
    assert s["batch"]["dry_run"] is True                 # fail-safe: rehearsal


def test_validation_rejects_absurd_caps(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    import pytest
    with pytest.raises(ValueError):
        st.update_settings({"max_live_applies_per_day": -1})
