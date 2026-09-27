"""Chrome native-messaging host: how the extension reaches the local service
without a terminal or a pasted token.

Chrome starts this process (through the launcher script written by
``applypilot extension install-host``) when the extension calls
``chrome.runtime.sendNativeMessage("com.applypilot.copilot", ...)``. Only the
extension ID listed in the host manifest's ``allowed_origins`` can connect —
Chrome enforces that, so the token this hands out never reaches a web page or
another extension.

Commands (one JSON object in, one out):
  {"cmd": "hello"}          -> {"ok": true, "version": ...}
  {"cmd": "ensure_server"}  -> starts ``applypilot serve-extension`` detached
                               when nothing answers on the port, waits for
                               /health, and returns {"ok": true, "port", "token"}

Wire format (Chrome's): a 4-byte native-endian length, then UTF-8 JSON.
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import BinaryIO, Callable

HOST_NAME = "com.applypilot.copilot"
DEFAULT_PORT = 8787
START_TIMEOUT_S = 45.0
VERSION = "1"


def read_message(stream: BinaryIO) -> dict | None:
    raw_len = stream.read(4)
    if len(raw_len) < 4:
        return None
    (length,) = struct.unpack("=I", raw_len)
    if length > 1024 * 1024:
        return None
    data = stream.read(length)
    try:
        msg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return msg if isinstance(msg, dict) else {}


def write_message(stream: BinaryIO, obj: dict) -> None:
    data = json.dumps(obj).encode("utf-8")
    stream.write(struct.pack("=I", len(data)))
    stream.write(data)
    stream.flush()


def server_up(port: int, token: str) -> bool:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/health", headers={"X-ApplyPilot-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 — refused, timeout, or another service (401/404) on the port
        return False


def service_command(port: int) -> list[str]:
    """How to launch the service. The frozen friend build re-runs its own
    executable in serve mode (it has no ``-m``); a normal install runs
    ``python -m applypilot serve-extension``, preferring pythonw.exe on
    Windows so no console window opens."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "serve-extension", "--port", str(port)]
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    if windowless.exists():
        exe = windowless
    return [str(exe), "-m", "applypilot", "serve-extension", "--port", str(port)]


def popen_kwargs(is_windows: bool) -> dict:
    """Detach the service from this short-lived host. On Windows: no console,
    own process group. On macOS/Linux: a new session, because Chrome ends the
    host process after every one-shot message and must not take the service
    down with it."""
    if is_windows:
        return {"creationflags": (getattr(subprocess, "DETACHED_PROCESS", 0)
                                  | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                  | getattr(subprocess, "CREATE_NO_WINDOW", 0))}
    return {"start_new_session": True}


def start_server(port: int, app_dir: Path) -> None:
    """Start the service detached from this short-lived host process, logging
    to <app_dir>/logs/extension-service.log."""
    log_dir = Path(app_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / "extension-service.log", "ab")  # noqa: SIM115 — handed to the child
    subprocess.Popen(service_command(port), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     close_fds=True, env=dict(os.environ), **popen_kwargs(os.name == "nt"))


def handle(msg: dict, *, app_dir: Path, port: int = DEFAULT_PORT,
           prober: Callable[[int, str], bool] = server_up,
           starter: Callable[[int, Path], None] = start_server,
           sleep: Callable[[float], None] = time.sleep,
           timeout_s: float = START_TIMEOUT_S) -> dict:
    cmd = (msg or {}).get("cmd")
    if cmd == "hello":
        return {"ok": True, "version": VERSION}
    if cmd != "ensure_server":
        return {"ok": False, "error": f"unknown command: {cmd!r}"}

    from applypilot.extension.server import get_or_create_token

    token = get_or_create_token(Path(app_dir))
    if prober(port, token):
        return {"ok": True, "port": port, "token": token, "started": False}
    try:
        starter(port, Path(app_dir))
    except Exception as exc:  # noqa: BLE001 — reported to the extension, never raised
        return {"ok": False, "error": f"could not start the service: {str(exc)[:200]}"}
    waited = 0.0
    while waited < timeout_s:
        sleep(0.5)
        waited += 0.5
        if prober(port, token):
            return {"ok": True, "port": port, "token": token, "started": True}
    return {"ok": False, "error": f"the service did not come up on port {port} within {int(timeout_s)} s "
                                  f"(see {Path(app_dir) / 'logs' / 'extension-service.log'})"}


def main() -> None:
    from applypilot import config

    app_dir = Path(config.APP_DIR)
    port = int(os.environ.get("APPLYPILOT_EXTENSION_PORT", DEFAULT_PORT))
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    while True:
        msg = read_message(stdin)
        if msg is None:
            return
        write_message(stdout, handle(msg, app_dir=app_dir, port=port))


if __name__ == "__main__":
    main()
