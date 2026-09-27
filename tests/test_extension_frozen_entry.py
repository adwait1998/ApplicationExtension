"""The frozen friend build's single entry point: argv -> mode."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from applypilot.extension import frozen_entry


def test_chrome_launch_is_native_host_mode():
    # Chrome launches a native host with the caller's origin as argv[1]
    # (plus --parent-window=N on Windows).
    assert frozen_entry.parse_mode(["chrome-extension://noooclaijfiejnfgabkemnpabcbdnaac/"]) == ("native-host", {})
    assert frozen_entry.parse_mode(["chrome-extension://abc/", "--parent-window=0"]) == ("native-host", {})


def test_serve_extension_default_and_explicit_port():
    assert frozen_entry.parse_mode(["serve-extension"]) == ("serve", {"port": 8787})
    assert frozen_entry.parse_mode(["serve-extension", "--port", "9001"]) == ("serve", {"port": 9001})


def test_install_and_uninstall_modes():
    assert frozen_entry.parse_mode(["install-host", "--extension-dir", "C:/x/extension"]) == (
        "install", {"extension_dir": Path("C:/x/extension")})
    assert frozen_entry.parse_mode(["uninstall-host"]) == ("uninstall", {})


def test_version_and_self_check_modes():
    assert frozen_entry.parse_mode(["--version"]) == ("version", {})
    assert frozen_entry.parse_mode(["self-check"]) == ("self-check", {})


@pytest.mark.parametrize("argv", [
    [], ["bogus"], ["serve-extension", "--port"], ["serve-extension", "--port", "abc"],
    ["install-host"], ["install-host", "--wrong", "x"],
])
def test_bad_argv_is_rejected(argv):
    with pytest.raises(ValueError):
        frozen_entry.parse_mode(argv)


def test_main_sets_data_dir_before_anything_else(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_DIR", raising=False)
    assert frozen_entry.main(["--version"]) == 0
    assert os.environ["APPLYPILOT_DIR"] == str(Path.home() / ".applypilot")


def test_main_keeps_an_explicit_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    assert frozen_entry.main(["--version"]) == 0
    assert os.environ["APPLYPILOT_DIR"] == str(tmp_path)


def test_main_dispatches_each_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(frozen_entry, "run_native_host", lambda: calls.append("host") or 0)
    monkeypatch.setattr(frozen_entry, "run_service", lambda port: calls.append(("serve", port)) or 0)
    monkeypatch.setattr(frozen_entry, "run_install", lambda d: calls.append(("install", d)) or 0)
    monkeypatch.setattr(frozen_entry, "run_uninstall", lambda: calls.append("uninstall") or 0)
    assert frozen_entry.main(["chrome-extension://abc/"]) == 0
    assert frozen_entry.main(["serve-extension", "--port", "9100"]) == 0
    assert frozen_entry.main(["install-host", "--extension-dir", str(tmp_path)]) == 0
    assert frozen_entry.main(["uninstall-host"]) == 0
    assert calls == ["host", ("serve", 9100), ("install", tmp_path), "uninstall"]


def test_main_bad_argv_returns_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    assert frozen_entry.main(["bogus"]) == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_self_check_reports_missing_modules():
    def fake_import(name):
        if name == "pypdf":
            raise ImportError("No module named 'pypdf'")
        return object()

    assert frozen_entry.self_check(import_module=fake_import) == ["pypdf: No module named 'pypdf'"]
    assert frozen_entry.self_check(import_module=lambda name: object()) == []
