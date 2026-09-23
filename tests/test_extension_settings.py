"""Tests for applypilot.extension.settings -- persisted tier-toggle
settings (answer bank / draft / max-drafts) plus env-var override
precedence over the persisted file.

$0, no network. Every disk-backed test lives entirely under tmp_path; the
zero-arg (app_dir=None) paths are asserted to do no disk I/O at all by
construction (load_settings returns early before touching Path()).
"""
from __future__ import annotations

import json

import pytest

from applypilot.extension import settings as ext_settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_ANSWERS", raising=False)
    monkeypatch.delenv("APPLYPILOT_DRAFTS", raising=False)
    monkeypatch.delenv("APPLYPILOT_MAX_DRAFTS", raising=False)
    yield


# ---------------------------------------------------------------------------
# load_settings: defaults, app_dir=None never touches disk, corrupt file
# ---------------------------------------------------------------------------


def test_load_settings_with_no_app_dir_returns_defaults():
    assert ext_settings.load_settings(None) == ext_settings.DEFAULT_SETTINGS


def test_load_settings_missing_file_returns_defaults(tmp_path):
    assert ext_settings.load_settings(tmp_path) == ext_settings.DEFAULT_SETTINGS


def test_load_settings_reads_persisted_values(tmp_path):
    (tmp_path / "extension_settings.json").write_text(
        json.dumps({"answers_enabled": False, "drafts_enabled": True, "max_drafts": 3}),
        encoding="utf-8",
    )
    assert ext_settings.load_settings(tmp_path) == {
        "answers_enabled": False, "drafts_enabled": True, "max_drafts": 3,
    }


def test_load_settings_corrupt_file_degrades_to_defaults(tmp_path):
    (tmp_path / "extension_settings.json").write_text("not json{{{", encoding="utf-8")
    assert ext_settings.load_settings(tmp_path) == ext_settings.DEFAULT_SETTINGS


def test_load_settings_repairs_missing_or_invalid_keys(tmp_path):
    (tmp_path / "extension_settings.json").write_text(
        json.dumps({"answers_enabled": "not a bool", "max_drafts": -5}), encoding="utf-8"
    )
    out = ext_settings.load_settings(tmp_path)
    assert out == ext_settings.DEFAULT_SETTINGS


def test_load_settings_non_dict_json_degrades_to_defaults(tmp_path):
    (tmp_path / "extension_settings.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert ext_settings.load_settings(tmp_path) == ext_settings.DEFAULT_SETTINGS


# ---------------------------------------------------------------------------
# save_settings: validation, atomic write, merge over existing
# ---------------------------------------------------------------------------


def test_save_settings_writes_and_returns_merged_dict(tmp_path):
    out = ext_settings.save_settings(tmp_path, {"drafts_enabled": True})
    assert out == {"answers_enabled": True, "drafts_enabled": True, "max_drafts": 5}
    on_disk = json.loads((tmp_path / "extension_settings.json").read_text(encoding="utf-8"))
    assert on_disk == out


def test_save_settings_merges_over_previous_save(tmp_path):
    ext_settings.save_settings(tmp_path, {"answers_enabled": False})
    out = ext_settings.save_settings(tmp_path, {"max_drafts": 9})
    assert out == {"answers_enabled": False, "drafts_enabled": False, "max_drafts": 9}


@pytest.mark.parametrize("updates", [
    {"answers_enabled": "yes"},
    {"drafts_enabled": 1},
    {"max_drafts": "five"},
    {"max_drafts": -1},
    {"max_drafts": True},  # bool is technically an int -- must be rejected
])
def test_save_settings_rejects_invalid_values(tmp_path, updates):
    with pytest.raises(ext_settings.InvalidSettings):
        ext_settings.save_settings(tmp_path, updates)


def test_save_settings_rejects_non_dict_body(tmp_path):
    with pytest.raises(ext_settings.InvalidSettings):
        ext_settings.save_settings(tmp_path, ["not", "a", "dict"])  # type: ignore[arg-type]


def test_save_settings_leaves_no_temp_file_behind(tmp_path):
    ext_settings.save_settings(tmp_path, {"drafts_enabled": True})
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_settings_creates_app_dir_if_missing(tmp_path):
    app_dir = tmp_path / "nested" / "dir"
    ext_settings.save_settings(app_dir, {"max_drafts": 1})
    assert (app_dir / "extension_settings.json").exists()


# ---------------------------------------------------------------------------
# effective_settings: env override precedence over the persisted file
# ---------------------------------------------------------------------------


def test_effective_settings_no_app_dir_no_env_is_pure_defaults():
    assert ext_settings.effective_settings(None) == ext_settings.DEFAULT_SETTINGS


def test_effective_settings_uses_persisted_file_when_no_env(tmp_path):
    ext_settings.save_settings(tmp_path, {"answers_enabled": False, "max_drafts": 2})
    assert ext_settings.effective_settings(tmp_path) == {
        "answers_enabled": False, "drafts_enabled": False, "max_drafts": 2,
    }


def test_effective_settings_env_overrides_persisted_file(tmp_path, monkeypatch):
    ext_settings.save_settings(tmp_path, {"answers_enabled": False})
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")
    assert ext_settings.effective_settings(tmp_path)["answers_enabled"] is True


@pytest.mark.parametrize("val,expected", [("0", False), ("false", False), ("no", False),
                                           ("off", False), ("1", True), ("yes", True)])
def test_effective_settings_answers_env_truthy_falsy(monkeypatch, val, expected):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", val)
    assert ext_settings.effective_settings(None)["answers_enabled"] is expected


def test_effective_settings_max_drafts_env_override(monkeypatch, tmp_path):
    ext_settings.save_settings(tmp_path, {"max_drafts": 5})
    monkeypatch.setenv("APPLYPILOT_MAX_DRAFTS", "7")
    assert ext_settings.effective_settings(tmp_path)["max_drafts"] == 7


def test_effective_settings_invalid_max_drafts_env_falls_back_to_settings(monkeypatch, tmp_path):
    ext_settings.save_settings(tmp_path, {"max_drafts": 4})
    monkeypatch.setenv("APPLYPILOT_MAX_DRAFTS", "not-a-number")
    assert ext_settings.effective_settings(tmp_path)["max_drafts"] == 4


def test_effective_settings_drafts_require_answers_even_from_persisted_file(tmp_path):
    ext_settings.save_settings(tmp_path, {"answers_enabled": False, "drafts_enabled": True})
    out = ext_settings.effective_settings(tmp_path)
    assert out["drafts_enabled"] is False  # answers off wins regardless of drafts_enabled


def test_effective_settings_drafts_require_answers_when_env_disables_answers(monkeypatch, tmp_path):
    ext_settings.save_settings(tmp_path, {"drafts_enabled": True})
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "0")
    out = ext_settings.effective_settings(tmp_path)
    assert out["answers_enabled"] is False
    assert out["drafts_enabled"] is False


# ---------------------------------------------------------------------------
# env_override_* helpers: None means "no opinion", not False
# ---------------------------------------------------------------------------


def test_env_override_helpers_return_none_when_unset():
    assert ext_settings.env_override_answers() is None
    assert ext_settings.env_override_drafts() is None
    assert ext_settings.env_override_max_drafts() is None


def test_env_override_helpers_report_explicit_values(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "0")
    monkeypatch.setenv("APPLYPILOT_DRAFTS", "1")
    monkeypatch.setenv("APPLYPILOT_MAX_DRAFTS", "3")
    assert ext_settings.env_override_answers() is False
    assert ext_settings.env_override_drafts() is True
    assert ext_settings.env_override_max_drafts() == 3
