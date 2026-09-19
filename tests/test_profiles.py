import pytest

from applypilot import profiles


def test_valid_and_invalid_ids():
    assert profiles.is_valid_id("nida")
    assert profiles.is_valid_id("adwait_2")
    assert profiles.is_valid_id("a-b")
    for bad in ("", "Nida", "a b", "../etc", "a/b", "a.b", "x" * 65):
        assert not profiles.is_valid_id(bad), bad


def test_data_root_prefers_explicit_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ROOT", str(tmp_path))
    monkeypatch.delenv("APPLYPILOT_DIR", raising=False)
    assert profiles.data_root() == tmp_path


def test_data_root_falls_back_to_appdir_env(tmp_path, monkeypatch):
    monkeypatch.delenv("APPLYPILOT_ROOT", raising=False)
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    assert profiles.data_root() == tmp_path


def test_legacy_layout_detected_when_profile_json_present(tmp_path):
    (tmp_path / "profile.json").write_text("{}", encoding="utf-8")
    assert profiles.is_legacy_layout(tmp_path)


def test_multi_layout_when_profiles_dir_present(tmp_path):
    (tmp_path / "profiles" / "nida").mkdir(parents=True)
    assert not profiles.is_legacy_layout(tmp_path)


def test_list_and_dir(tmp_path):
    for p in ("nida", "adwait"):
        (tmp_path / "profiles" / p).mkdir(parents=True)
    assert profiles.list_profiles(tmp_path) == ["adwait", "nida"]   # sorted
    assert profiles.profile_dir(tmp_path, "nida") == tmp_path / "profiles" / "nida"


def test_profile_dir_rejects_traversal(tmp_path):
    with pytest.raises(ValueError):
        profiles.profile_dir(tmp_path, "../escape")


def test_active_profile_roundtrip(tmp_path):
    (tmp_path / "profiles" / "nida").mkdir(parents=True)
    assert profiles.get_active(tmp_path) is None
    profiles.set_active(tmp_path, "nida")
    assert profiles.get_active(tmp_path) == "nida"


def test_set_active_rejects_unknown(tmp_path):
    with pytest.raises(ValueError):
        profiles.set_active(tmp_path, "ghost")


def test_resolve_precedence_flag_beats_env(tmp_path, monkeypatch):
    for p in ("nida", "adwait"):
        (tmp_path / "profiles" / p).mkdir(parents=True)
    monkeypatch.setenv("APPLYPILOT_PROFILE", "nida")
    assert profiles.resolve(tmp_path, argv=["apply", "--profile", "adwait"]) == "adwait"


def test_resolve_env_beats_active_file(tmp_path, monkeypatch):
    for p in ("nida", "adwait"):
        (tmp_path / "profiles" / p).mkdir(parents=True)
    profiles.set_active(tmp_path, "nida")
    monkeypatch.setenv("APPLYPILOT_PROFILE", "adwait")
    assert profiles.resolve(tmp_path, argv=["apply"]) == "adwait"


def test_resolve_active_file_beats_nothing(tmp_path, monkeypatch):
    for p in ("nida", "adwait"):
        (tmp_path / "profiles" / p).mkdir(parents=True)
    monkeypatch.delenv("APPLYPILOT_PROFILE", raising=False)
    profiles.set_active(tmp_path, "adwait")
    assert profiles.resolve(tmp_path, argv=["apply"]) == "adwait"


def test_resolve_sole_profile_needs_no_config(tmp_path, monkeypatch):
    (tmp_path / "profiles" / "nida").mkdir(parents=True)
    monkeypatch.delenv("APPLYPILOT_PROFILE", raising=False)
    assert profiles.resolve(tmp_path, argv=["apply"]) == "nida"


def test_resolve_ambiguous_raises_listing_choices(tmp_path, monkeypatch):
    for p in ("nida", "adwait"):
        (tmp_path / "profiles" / p).mkdir(parents=True)
    monkeypatch.delenv("APPLYPILOT_PROFILE", raising=False)
    with pytest.raises(profiles.ProfileError) as e:
        profiles.resolve(tmp_path, argv=["apply"])
    assert "adwait" in str(e.value) and "nida" in str(e.value)


def test_resolve_unknown_profile_raises(tmp_path):
    (tmp_path / "profiles" / "nida").mkdir(parents=True)
    with pytest.raises(profiles.ProfileError):
        profiles.resolve(tmp_path, argv=["apply", "--profile", "ghost"])


@pytest.mark.parametrize("argv,expected", [
    (["apply", "--profile", "nida"], "nida"),
    (["apply", "--profile=nida"], "nida"),
    (["--profile", "nida", "apply"], "nida"),
    (["apply"], None),
    (["apply", "--profile"], None),          # dangling flag, no value
])
def test_profile_from_argv(argv, expected):
    assert profiles.profile_from_argv(argv) == expected


def test_config_exposes_shared_paths(tmp_path, monkeypatch):
    """SHARED_DIR/ATLAS_DB_PATH derive from the ROOT, not from APP_DIR."""
    import importlib

    monkeypatch.setenv("APPLYPILOT_ROOT", str(tmp_path))
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path / "profiles" / "nida"))
    from applypilot import config as _c
    config = importlib.reload(_c)
    try:
        assert config.ROOT == tmp_path
        assert config.SHARED_DIR == tmp_path / "shared"
        assert config.ATLAS_DB_PATH == tmp_path / "shared" / "atlas.db"
        assert config.APP_DIR == tmp_path / "profiles" / "nida"
    finally:
        monkeypatch.undo()
        importlib.reload(_c)
