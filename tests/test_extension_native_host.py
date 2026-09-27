"""Native-messaging host + installer: no terminal, no pasted token."""
import base64
import hashlib
import io
import json
import struct

from applypilot.extension import native_host, native_install


def _frame(obj) -> bytes:
    data = json.dumps(obj).encode()
    return struct.pack("=I", len(data)) + data


def test_message_framing_round_trip():
    out = io.BytesIO()
    native_host.write_message(out, {"ok": True, "n": 1})
    assert native_host.read_message(io.BytesIO(out.getvalue())) == {"ok": True, "n": 1}
    assert native_host.read_message(io.BytesIO(b"")) is None          # EOF -> stop
    assert native_host.read_message(io.BytesIO(_frame([1, 2]))) == {}  # not an object


def test_hello_and_unknown_command(tmp_path):
    assert native_host.handle({"cmd": "hello"}, app_dir=tmp_path)["ok"] is True
    assert native_host.handle({"cmd": "rm -rf"}, app_dir=tmp_path)["ok"] is False


def test_server_already_up_returns_token_without_starting(tmp_path):
    started = []
    r = native_host.handle({"cmd": "ensure_server"}, app_dir=tmp_path, port=8799,
                           prober=lambda port, tok: True, starter=lambda p, d: started.append(p))
    assert r["ok"] and r["port"] == 8799 and r["started"] is False and started == []
    assert r["token"] == (tmp_path / "extension_token.txt").read_text(encoding="utf-8").strip()


def test_server_down_is_started_then_polled(tmp_path):
    state = {"up": False}
    probes = []

    def prober(port, tok):
        probes.append(port)
        return state["up"]

    def starter(port, app_dir):
        state["up"] = True

    r = native_host.handle({"cmd": "ensure_server"}, app_dir=tmp_path, prober=prober, starter=starter,
                           sleep=lambda s: None)
    assert r["ok"] and r["started"] is True and len(probes) == 2


def test_server_that_never_comes_up_is_an_honest_error(tmp_path):
    r = native_host.handle({"cmd": "ensure_server"}, app_dir=tmp_path, prober=lambda p, t: False,
                           starter=lambda p, d: None, sleep=lambda s: None, timeout_s=2)
    assert r["ok"] is False and "did not come up" in r["error"]


def test_service_command_when_frozen(monkeypatch):
    import sys as _sys
    monkeypatch.setattr(_sys, "frozen", True, raising=False)
    monkeypatch.setattr(_sys, "executable", r"C:\App\ApplyPilotCopilot.exe")
    assert native_host.service_command(8787) == [r"C:\App\ApplyPilotCopilot.exe", "serve-extension", "--port", "8787"]


def test_service_command_from_python(monkeypatch, tmp_path):
    import sys as _sys
    monkeypatch.delattr(_sys, "frozen", raising=False)
    python = tmp_path / "python.exe"
    python.write_text("")
    monkeypatch.setattr(_sys, "executable", str(python))
    assert native_host.service_command(9000) == [str(python), "-m", "applypilot", "serve-extension", "--port", "9000"]
    (tmp_path / "pythonw.exe").write_text("")  # windowless interpreter next to it wins
    assert native_host.service_command(9000)[0] == str(tmp_path / "pythonw.exe")


import os as _os  # noqa: E402

import pytest as _pytest  # noqa: E402


@_pytest.mark.skipif(_os.name != "nt", reason="the Windows process flags only exist on Windows")
def test_popen_kwargs_detach_on_windows_and_new_session_on_posix():
    import subprocess as _sp
    win = native_host.popen_kwargs(is_windows=True)
    assert win["creationflags"] & _sp.DETACHED_PROCESS and "start_new_session" not in win
    posix = native_host.popen_kwargs(is_windows=False)
    assert posix == {"start_new_session": True}


def test_start_server_uses_service_command_and_kwargs(monkeypatch, tmp_path):
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return object()

    monkeypatch.setattr(native_host.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(native_host, "service_command", lambda port: ["APP", "serve-extension", "--port", str(port)])
    native_host.start_server(8799, tmp_path)
    assert seen["cmd"] == ["APP", "serve-extension", "--port", "8799"]
    assert seen["kwargs"]["stdin"] is native_host.subprocess.DEVNULL
    assert (tmp_path / "logs").is_dir()


def test_extension_id_derivation():
    key = base64.b64encode(b"not-a-real-key").decode()
    digest = hashlib.sha256(b"not-a-real-key").hexdigest()[:32]
    want = "".join("abcdefghijklmnop"[int(c, 16)] for c in digest)
    assert native_install.extension_id_from_key(key) == want
    assert len(want) == 32 and set(want) <= set("abcdefghijklmnop")


def test_install_pins_the_id_writes_host_files_and_registers(tmp_path):
    ext = tmp_path / "extension"
    ext.mkdir()
    (ext / "manifest.json").write_text(json.dumps({"manifest_version": 3, "name": "X"}), encoding="utf-8")
    key = base64.b64encode(b"k" * 40).decode()
    reg = {}
    deleted = []  # never the real registry in a test
    r = native_install.install(ext, tmp_path / "data", python_exe=r"C:\Py\python.exe",
                               env={"APPLYPILOT_DIR": r"E:\data"}, reg_set=reg.__setitem__,
                               reg_delete=deleted.append, keygen=lambda: key)
    assert json.loads((ext / "manifest.json").read_text(encoding="utf-8"))["key"] == key
    assert r["extension_id"] == native_install.extension_id_from_key(key)
    host = json.loads((tmp_path / "data" / "native_host" / "com.applypilot.copilot.json").read_text())
    assert host["allowed_origins"] == [f"chrome-extension://{r['extension_id']}/"]
    bat = (tmp_path / "data" / "native_host" / "applypilot_host.bat").read_text()
    assert r'set "APPLYPILOT_DIR=E:\data"' in bat and "applypilot.extension.native_host" in bat
    assert set(reg) == {base + r"\com.applypilot.copilot" for base in native_install.REGISTRY_PATHS}
    assert set(reg) == {r"Software\Google\Chrome\NativeMessagingHosts\com.applypilot.copilot"}  # Chrome only
    assert r"Software\Chromium\NativeMessagingHosts\com.applypilot.copilot" in deleted  # legacy entry removed
    # A second install keeps the same key (and so the same ID).
    native_install.install(ext, tmp_path / "data", python_exe="py", env={}, reg_set=reg.__setitem__,
                           reg_delete=deleted.append, keygen=lambda: "different")
    assert json.loads((ext / "manifest.json").read_text(encoding="utf-8"))["key"] == key


def test_uninstall_removes_registration_and_files(tmp_path):
    host_dir = tmp_path / "native_host"
    host_dir.mkdir()
    (host_dir / "applypilot_host.bat").write_text("x")
    (host_dir / "com.applypilot.copilot.json").write_text("{}")
    deleted = []
    native_install.uninstall(tmp_path, reg_delete=deleted.append)
    assert len(deleted) == 2 and not any(host_dir.iterdir())


def test_real_keygen_produces_a_valid_public_key():
    from cryptography.hazmat.primitives.serialization import load_der_public_key
    key = native_install._new_public_key_b64()
    load_der_public_key(base64.b64decode(key))   # raises if not a real SubjectPublicKeyInfo
