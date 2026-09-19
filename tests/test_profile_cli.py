import json

from typer.testing import CliRunner

from applypilot.cli import app

runner = CliRunner()


def _mk(root, pid):
    d = root / "profiles" / pid
    d.mkdir(parents=True)
    (d / "profile.json").write_text(
        json.dumps({"profile_id": pid, "personal": {"name": pid.title()}}),
        encoding="utf-8")
    return d


def test_profile_list_shows_ids_and_active(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ROOT", str(tmp_path))
    _mk(tmp_path, "nida"); _mk(tmp_path, "adwait")
    (tmp_path / "active_profile").write_text("nida", encoding="utf-8")
    res = runner.invoke(app, ["profile", "list"])
    assert res.exit_code == 0
    assert "nida" in res.stdout and "adwait" in res.stdout


def test_profile_use_sets_active(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ROOT", str(tmp_path))
    _mk(tmp_path, "nida"); _mk(tmp_path, "adwait")
    res = runner.invoke(app, ["profile", "use", "adwait"])
    assert res.exit_code == 0
    assert (tmp_path / "active_profile").read_text(encoding="utf-8").strip() == "adwait"


def test_profile_use_rejects_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ROOT", str(tmp_path))
    _mk(tmp_path, "nida")
    res = runner.invoke(app, ["profile", "use", "ghost"])
    assert res.exit_code != 0


def test_profile_add_scaffolds_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ROOT", str(tmp_path))
    res = runner.invoke(app, ["profile", "add", "adwait", "--no-wizard"])
    assert res.exit_code == 0
    assert (tmp_path / "profiles" / "adwait").is_dir()


def test_profile_add_rejects_invalid_id(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ROOT", str(tmp_path))
    res = runner.invoke(app, ["profile", "add", "Bad Id"])
    assert res.exit_code != 0
