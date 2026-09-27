# Friend Installer + On-Device AI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship ApplyPilot Copilot to a friend as a one-time installer (Windows `.exe`, Apple-Silicon Mac `.zip`) that needs no Python, no terminal and no API key, with AI features running on Chrome's built-in on-device model.

**Architecture:** One PyInstaller-frozen executable plays every backend role, chosen by `argv` (Chrome native-messaging host, local FastAPI service, host installer). AI calls in Python keep their existing `client.chat(...)` interface; a new `BridgeClient` queues each call and an extension page answers it with Chrome's `LanguageModel` (Gemini Nano). Installers place the app and the extension folder, then register the native host; the friend does one "Load unpacked".

**Tech Stack:** Python 3.12, FastAPI/uvicorn, PyInstaller 6, Inno Setup 6 (Windows installer), bash + `ditto` (macOS), GitHub Actions (`windows-latest`, `macos-14`), Chrome MV3 extension (plain JS), Chrome Prompt API.

**Design spec:** `docs/superpowers/specs/2026-09-27-friend-installer-design.md` — read it first.

---

## Before you start (read all of this)

**Repo:** `E:\auto-apply-pipeline` (Git Bash path `/e/auto-apply-pipeline`), branch `feat/chrome-extension`. Stay on this branch.

**Python:** use the full path. In Git Bash:

```bash
PY="/c/Users/adwai/AppData/Local/Programs/Python/Python312/python.exe"
```

Every pytest command below assumes you ran that line in the same shell call and are in `/e/auto-apply-pipeline`.

**Commit rules (hard rules):**
- Commit with a one-shot identity, never change git config:
  `git -c user.name="Adwait" -c user.email="adwait1234@gmail.com" commit -m "..."`
- End every commit message with a blank line and `Co-Authored-By: <your model name> <noreply@anthropic.com>` (for example `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`).
- NEVER `git push`. NEVER use `--no-verify`. NEVER `git stash` (other sessions share this repo).
- `git add` only the files the task names. Run `git status --short` before committing and make sure nothing else is staged.

**Data safety (hard rules):**
- Never read from or write to `E:\applypilot-data` in tests or scripts. Tests use `tmp_path`.
- Never edit `profile.json`, `resume.pdf`, `resume.txt`, `.env`, or `.git/config`.
- Never run the real installer (`ApplyPilotCopilot-Setup-*.exe`) or `ApplyPilotCopilot.exe install-host` on this machine. It would repoint the operator's own Chrome native-host registration. If it happens by accident, restore it with `"$PY" -m applypilot extension install-host`.

**Writing files that contain backslashes:** in this environment, shell heredocs collapse `\\` into `\`. Create and edit files with the Write/Edit tools, never with `cat <<EOF` or a Python heredoc.

**Existing test suites you must keep green** (full commands are in Task 18):
- pytest: `"$PY" -m pytest -q -p no:cacheprovider` → currently `1774 passed, 2 skipped` (about 6 minutes).
- jsdom self-test: `node extension/selftest.js` with jsdom on `NODE_PATH` → currently `564/564 checks passed`.
- Chrome suites: `scripts/chrome_widgets_test.py`, `scripts/chrome_load_test.py`, `scripts/chrome_panel_test.py` (327 checks, about 7 minutes). Run Chrome suites one at a time; this PC is short on memory.

**Facts you'll rely on (verified in the code on 2026-09-27):**
- Every AI call in the extension service goes through `applypilot.extension.llm_util.get_llm_client()`, which returns an object with `chat(messages, temperature=0.0, max_tokens=4096, response_format=None) -> str`. Callers already treat any `RuntimeError` from it as "no AI available".
- The native host (`src/applypilot/extension/native_host.py`) answers `{"cmd": "hello"}` and `{"cmd": "ensure_server"}` over Chrome's stdio framing (4-byte little-endian length + JSON).
- The service writes its token to `<APPLYPILOT_DIR>/extension_token.txt`.
- The extension's pinned ID is `noooclaijfiejnfgabkemnpabcbdnaac` (derived from the `"key"` in `extension/manifest.json`).

---

## File map

**New files**

| File | Responsibility |
|---|---|
| `src/applypilot/extension/frozen_entry.py` | The frozen app's `main()`: picks the mode from `argv`, sets `APPLYPILOT_DIR`, runs host/service/install/uninstall/self-check. |
| `src/applypilot/extension/llm_bridge.py` | In-memory job queue between the service and extension pages; `BridgeClient` with the `chat()` interface. |
| `extension/llm_bridge.js` | Runs in the side panel and options page: polls `/llm/next`, runs jobs with `LanguageModel`, posts `/llm/result`; `download()` for the first model download. |
| `extension/llm_bridge_selftest.js` | Node-only tests for `llm_bridge.js` (fake `fetch`, `chrome`, `LanguageModel`). |
| `packaging/requirements-friend.txt` | Pinned runtime + build dependencies for the frozen build. |
| `packaging/extension_files.txt` | Allow-list of extension files that ship. |
| `packaging/stage_extension.py` | Copies the allow-listed extension files into a build folder and checks them. |
| `packaging/applypilot_copilot.spec` | PyInstaller spec (onedir, console app `ApplyPilotCopilot`). |
| `packaging/build_windows.ps1` | Windows build: venv → PyInstaller → stage → verify → Inno Setup. |
| `packaging/windows/installer.iss` | Inno Setup script (per-user install, registers the host). |
| `packaging/build_macos.sh` | macOS build: venv → PyInstaller → stage → verify → zip. |
| `packaging/macos/Install ApplyPilot Copilot.command` | Friend's Mac installer script. |
| `packaging/macos/Uninstall ApplyPilot Copilot.command` | Friend's Mac uninstaller script. |
| `packaging/README.md` | Operator guide: how to build and share. |
| `scripts/verify_friend_bundle.py` | Runs a built bundle for real (in temp data dirs) and checks it. |
| `.github/workflows/friend-build.yml` | Manual CI build of both installers. |
| `docs/FRIEND_SETUP.md` | Friend-facing setup guide (shipped as `SETUP.txt`). |
| `tests/test_extension_frozen_entry.py` | Tests for `frozen_entry.py`. |
| `tests/test_extension_llm_bridge.py` | Tests for `llm_bridge.py` and the `/llm/*` endpoints. |
| `tests/test_stage_extension.py` | Tests for `packaging/stage_extension.py`. |

**Modified files**

| File | Change |
|---|---|
| `src/applypilot/extension/native_host.py` | `service_command()` (frozen-aware) and `popen_kwargs()` (new session on macOS). |
| `src/applypilot/extension/native_install.py` | `host_manifest()`, `install_frozen()`, `uninstall_frozen()` (Windows + macOS). |
| `src/applypilot/extension/server.py` | `/llm/next`, `/llm/result`; `/resume/tailor` → 501 when frozen; résumé import off the event loop. |
| `src/applypilot/extension/llm_util.py` | On-device bridge in provider order, label, "local" flag. |
| `extension/options.html`, `extension/options.js` | "On-device AI" row with a download button; start the bridge. |
| `extension/sidepanel.html`, `extension/sidepanel.js` | Load and start the bridge. |
| `extension/README.md` | Document `llm_bridge.js` and on-device AI. |
| `tests/test_extension_native_host.py`, `tests/test_extension_server.py`, `tests/test_extension_llm_util.py` | New tests. |
| `.gitignore` | Ignore `packaging/.venv-*/` and `packaging/.jsdom/`. |

---

## Phase A — Make the Python side work when frozen

### Task 1: Frozen entry point

**Files:**
- Create: `src/applypilot/extension/frozen_entry.py`
- Test: `tests/test_extension_frozen_entry.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_extension_frozen_entry.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_extension_frozen_entry.py -v`
Expected: FAIL — `ImportError: cannot import name 'frozen_entry'`.

- [ ] **Step 3: Write the implementation**

Create `src/applypilot/extension/frozen_entry.py`:

```python
"""Single entry point for the frozen (PyInstaller) friend build.

One executable plays every backend role, chosen by argv:

  chrome-extension://<id>/ [--parent-window=N]   native-messaging host (Chrome launches it)
  serve-extension [--port N]                     the local HTTP service
  install-host --extension-dir PATH              register the native host for this user
  uninstall-host                                 remove that registration
  self-check                                     import everything the app needs, report gaps
  --version                                      print the version

APPLYPILOT_DIR is set BEFORE applypilot.config is imported, in every mode. Chrome
launches the host with no environment of ours, and the host and the service must
agree on one data dir: the token file lives there.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Callable

DEFAULT_PORT = 8787

USAGE = (
    "usage: ApplyPilotCopilot serve-extension [--port N]\n"
    "       ApplyPilotCopilot install-host --extension-dir PATH\n"
    "       ApplyPilotCopilot uninstall-host | self-check | --version\n"
    "(Chrome starts this helper by itself; there is nothing to open.)"
)

# Everything the frozen app imports at some point. A missing one means the
# PyInstaller spec needs a hiddenimport or the build venv lacks a package.
REQUIRED_MODULES = (
    "uvicorn", "fastapi", "pydantic", "python_multipart", "httpx", "dotenv", "yaml",
    "pypdf", "docx",
    "applypilot.extension.server", "applypilot.extension.native_host",
    "applypilot.extension.native_install", "applypilot.extension.resume_import",
    "applypilot.extension.cover_letter", "applypilot.extension.llm_bridge",
)


def default_data_dir() -> Path:
    return Path.home() / ".applypilot"


def parse_mode(argv: list[str]) -> tuple[str, dict]:
    """(mode, options) from argv without the program name. Raises ValueError."""
    if not argv:
        raise ValueError("no mode given")
    first, rest = argv[0], argv[1:]
    if first.startswith("chrome-extension://"):
        return "native-host", {}
    if first == "--version":
        return "version", {}
    if first == "self-check":
        return "self-check", {}
    if first == "serve-extension":
        if not rest:
            return "serve", {"port": DEFAULT_PORT}
        if len(rest) != 2 or rest[0] not in ("--port", "-p"):
            raise ValueError("serve-extension takes only --port N")
        return "serve", {"port": int(rest[1])}
    if first == "install-host":
        if len(rest) != 2 or rest[0] != "--extension-dir":
            raise ValueError("install-host needs --extension-dir PATH")
        return "install", {"extension_dir": Path(rest[1])}
    if first == "uninstall-host":
        return "uninstall", {}
    raise ValueError(f"unknown mode: {first!r}")


def self_check(import_module: Callable[[str], object] = importlib.import_module) -> list[str]:
    """Import every required module; return one line per failure (empty = ok)."""
    failures = []
    for name in REQUIRED_MODULES:
        try:
            import_module(name)
        except Exception as exc:  # noqa: BLE001 -- report every kind of import failure
            failures.append(f"{name}: {exc}")
    return failures


def run_native_host() -> int:
    from applypilot.extension import native_host

    native_host.main()
    return 0


def run_service(port: int) -> int:
    import uvicorn
    from dotenv import load_dotenv

    from applypilot import config
    from applypilot.database import init_db
    from applypilot.extension.server import create_app

    # Only the data dir's own .env. config.load_env() also searches the
    # current and parent folders for a .env, which inside a build tree finds
    # the operator's real keys.
    if config.ENV_PATH.exists():
        load_dotenv(config.ENV_PATH)
    config.ensure_dirs()
    init_db()
    app = create_app(app_dir=config.APP_DIR, host="127.0.0.1", root=config.ROOT)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


def run_install(extension_dir: Path) -> int:
    from applypilot import config
    from applypilot.extension import native_install

    result = native_install.install_frozen(
        extension_dir=Path(extension_dir), app_dir=Path(config.APP_DIR), exe_path=Path(sys.executable))
    print(f"Native host installed. Extension ID: {result['extension_id']}")
    print(f"Host manifest: {result['manifest']}")
    return 0


def run_uninstall() -> int:
    from applypilot import config
    from applypilot.extension import native_install

    native_install.uninstall_frozen(Path(config.APP_DIR))
    print("Native host removed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    os.environ.setdefault("APPLYPILOT_DIR", str(default_data_dir()))
    try:
        mode, opts = parse_mode(argv)
    except ValueError as exc:
        print(f"ApplyPilot Copilot helper: {exc}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    if mode == "version":
        from applypilot import __version__

        print(__version__)
        return 0
    if mode == "self-check":
        failures = self_check()
        for line in failures:
            print(f"missing: {line}", file=sys.stderr)
        if failures:
            return 1
        print("self-check ok")
        return 0
    if mode == "native-host":
        return run_native_host()
    if mode == "serve":
        return run_service(opts["port"])
    if mode == "install":
        return run_install(opts["extension_dir"])
    return run_uninstall()


if __name__ == "__main__":
    sys.exit(main())
```

Note: `applypilot.extension.llm_bridge` in `REQUIRED_MODULES` doesn't exist until Task 5. The unit tests don't import it (they inject `import_module`), so that's fine.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `"$PY" -m pytest tests/test_extension_frozen_entry.py -v`
Expected: all PASS (15 tests).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/extension/frozen_entry.py tests/test_extension_frozen_entry.py
git -c user.name="Adwait" -c user.email="adwait1234@gmail.com" commit -m "feat(friend-build): frozen entry point that picks its role from argv

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: Native host launches the service correctly when frozen (and on macOS)

**Why:** `start_server` runs `sys.executable -m applypilot serve-extension`. In a frozen build `sys.executable` is the app itself, which has no `-m`. And on macOS, Chrome kills the host after each message; without a new session the service can die with it.

**Files:**
- Modify: `src/applypilot/extension/native_host.py` (the `start_server` function)
- Test: `tests/test_extension_native_host.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_extension_native_host.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_extension_native_host.py -v -k "service_command or popen_kwargs or start_server_uses"`
Expected: FAIL — `AttributeError: module 'applypilot.extension.native_host' has no attribute 'service_command'`.

- [ ] **Step 3: Replace `start_server`**

In `src/applypilot/extension/native_host.py`, replace this exact block:

```python
def start_server(port: int, app_dir: Path) -> None:
    """Start the service detached from this short-lived host process, with no
    console window, logging to <app_dir>/logs/extension-service.log."""
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    if windowless.exists():
        exe = windowless
    log_dir = Path(app_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / "extension-service.log", "ab")  # noqa: SIM115 — handed to the child
    flags = 0
    if os.name == "nt":
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    subprocess.Popen([str(exe), "-m", "applypilot", "serve-extension", "--port", str(port)],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                     creationflags=flags, env=dict(os.environ))
```

with:

```python
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
```

- [ ] **Step 4: Run the native host tests**

Run: `"$PY" -m pytest tests/test_extension_native_host.py -v`
Expected: all PASS (the 9 existing tests + 4 new ones).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/extension/native_host.py tests/test_extension_native_host.py
git -c user.name="Adwait" -c user.email="adwait1234@gmail.com" commit -m "fix(native-host): launch the service from a frozen build; keep it alive on macOS

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: Register the frozen app as the native host (Windows + macOS)

**Why:** `native_install.install()` writes a `.bat` launcher that runs `python.exe`, and registers only in the Windows registry. The frozen app is launched by Chrome directly (no launcher), and macOS registration is a JSON file in a fixed folder.

**Files:**
- Modify: `src/applypilot/extension/native_install.py`
- Test: `tests/test_extension_native_host.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_extension_native_host.py`:

```python
def _ext_with_key(tmp_path, key):
    ext = tmp_path / "extension"
    ext.mkdir()
    (ext / "manifest.json").write_text(json.dumps({"manifest_version": 3, "name": "X", "key": key}), encoding="utf-8")
    return ext


def test_install_frozen_on_macos_writes_chrome_host_manifest(tmp_path):
    key = base64.b64encode(b"k" * 40).decode()
    ext = _ext_with_key(tmp_path, key)
    home = tmp_path / "home"
    exe = tmp_path / "app" / "ApplyPilotCopilot"
    r = native_install.install_frozen(ext, tmp_path / "data", exe, platform="darwin", home=home)
    target = home / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts" / "com.applypilot.copilot.json"
    assert r["manifest"] == str(target)
    m = json.loads(target.read_text(encoding="utf-8"))
    assert m["path"] == str(exe) and m["type"] == "stdio" and m["name"] == "com.applypilot.copilot"
    assert m["allowed_origins"] == [f"chrome-extension://{native_install.extension_id_from_key(key)}/"]


def test_install_frozen_on_windows_registers_manifest_pointing_at_the_exe(tmp_path):
    key = base64.b64encode(b"k" * 40).decode()
    ext = _ext_with_key(tmp_path, key)
    reg, deleted = {}, []
    exe = tmp_path / "app" / "ApplyPilotCopilot.exe"
    r = native_install.install_frozen(ext, tmp_path / "data", exe, platform="win32",
                                      reg_set=reg.__setitem__, reg_delete=deleted.append)
    manifest = tmp_path / "data" / "native_host" / "com.applypilot.copilot.json"
    assert r["manifest"] == str(manifest)
    assert reg == {r"Software\Google\Chrome\NativeMessagingHosts\com.applypilot.copilot": str(manifest)}
    assert json.loads(manifest.read_text(encoding="utf-8"))["path"] == str(exe)
    assert not (tmp_path / "data" / "native_host" / "applypilot_host.bat").exists()  # no launcher when frozen
    assert r"Software\Chromium\NativeMessagingHosts\com.applypilot.copilot" in deleted


def test_install_frozen_refuses_a_manifest_without_key(tmp_path):
    ext = tmp_path / "extension"
    ext.mkdir()
    original = json.dumps({"manifest_version": 3, "name": "X"})
    (ext / "manifest.json").write_text(original, encoding="utf-8")
    import pytest
    with pytest.raises(RuntimeError, match="key"):
        native_install.install_frozen(ext, tmp_path / "data", tmp_path / "app.exe", platform="win32",
                                      reg_set=lambda k, v: None, reg_delete=lambda k: None)
    assert (ext / "manifest.json").read_text(encoding="utf-8") == original  # never rewritten


def test_uninstall_frozen_on_macos_removes_the_manifest(tmp_path):
    home = tmp_path / "home"
    target = home / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts" / "com.applypilot.copilot.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    native_install.uninstall_frozen(tmp_path / "data", platform="darwin", home=home)
    assert not target.exists()
    native_install.uninstall_frozen(tmp_path / "data", platform="darwin", home=home)  # idempotent


def test_uninstall_frozen_on_windows_removes_registration(tmp_path):
    deleted = []
    native_install.uninstall_frozen(tmp_path, platform="win32", reg_delete=deleted.append)
    assert len(deleted) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_extension_native_host.py -v -k "frozen"`
Expected: FAIL — `AttributeError: module 'applypilot.extension.native_install' has no attribute 'install_frozen'`.

- [ ] **Step 3: Refactor `write_host_files` to share the manifest builder**

In `src/applypilot/extension/native_install.py`, replace this exact block:

```python
    manifest = host_dir / f"{HOST_NAME}.json"
    manifest.write_text(json.dumps({
        "name": HOST_NAME,
        "description": "ApplyPilot Copilot local service launcher",
        "path": str(launcher),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ext_id}/"],
    }, indent=2), encoding="utf-8")
    return manifest
```

with:

```python
    manifest = host_dir / f"{HOST_NAME}.json"
    manifest.write_text(json.dumps(host_manifest(ext_id, launcher), indent=2), encoding="utf-8")
    return manifest
```

Then add this function directly ABOVE `def write_host_files(`:

```python
def host_manifest(ext_id: str, host_path: Path) -> dict:
    """The native-messaging host manifest Chrome reads. ``host_path`` is what
    Chrome runs: the .bat launcher for a normal install, the app itself for
    the frozen friend build."""
    return {
        "name": HOST_NAME,
        "description": "ApplyPilot Copilot local service launcher",
        "path": str(host_path),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ext_id}/"],
    }
```

- [ ] **Step 4: Add the frozen install/uninstall functions**

Append to the END of `src/applypilot/extension/native_install.py`:

```python
# ---------------------------------------------------------------------------
# Frozen friend build (see applypilot.extension.frozen_entry)
# ---------------------------------------------------------------------------

# Where Chrome on macOS looks for per-user native-messaging host manifests.
MAC_CHROME_HOSTS_DIR = Path("Library") / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts"


def _mac_manifest_path(home: Path | None) -> Path:
    return Path(home or Path.home()) / MAC_CHROME_HOSTS_DIR / f"{HOST_NAME}.json"


def _no_keygen() -> str:
    raise RuntimeError('extension/manifest.json has no "key": the friend build must ship the pinned '
                       "extension ID, or the native host would refuse the extension")


def install_frozen(extension_dir: Path, app_dir: Path, exe_path: Path, *, platform: str | None = None,
                   home: Path | None = None, reg_set: Callable[[str, str], None] = _winreg_set,
                   reg_delete: Callable[[str], None] | None = None) -> dict:
    """Register the frozen app itself as the native host: Chrome runs
    ``exe_path`` directly, so there is no launcher script and no env var.
    Never writes a new "key" into the shipped manifest."""
    platform = platform or sys.platform
    ext_id = ensure_manifest_key(Path(extension_dir) / "manifest.json", keygen=_no_keygen)
    manifest = host_manifest(ext_id, Path(exe_path))
    if platform == "darwin":
        target = _mac_manifest_path(home)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return {"extension_id": ext_id, "manifest": str(target)}
    if platform == "win32":
        host_dir = Path(app_dir) / "native_host"
        host_dir.mkdir(parents=True, exist_ok=True)
        target = host_dir / f"{HOST_NAME}.json"
        target.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        for base in REGISTRY_PATHS:
            reg_set(base + "\\" + HOST_NAME, str(target))
        for base in LEGACY_REGISTRY_PATHS:
            (reg_delete or _winreg_delete)(base + "\\" + HOST_NAME)
        return {"extension_id": ext_id, "manifest": str(target)}
    raise RuntimeError(f"unsupported platform for the friend build: {platform}")


def uninstall_frozen(app_dir: Path, *, platform: str | None = None, home: Path | None = None,
                     reg_delete: Callable[[str], None] = _winreg_delete) -> None:
    platform = platform or sys.platform
    if platform == "darwin":
        _mac_manifest_path(home).unlink(missing_ok=True)
        return
    uninstall(app_dir, reg_delete=reg_delete)
```

- [ ] **Step 5: Run the native host tests**

Run: `"$PY" -m pytest tests/test_extension_native_host.py -v`
Expected: all PASS (the existing tests, including `test_install_pins_the_id_writes_host_files_and_registers`, still pass after the refactor).

- [ ] **Step 6: Commit**

```bash
git add src/applypilot/extension/native_install.py tests/test_extension_native_host.py
git -c user.name="Adwait" -c user.email="adwait1234@gmail.com" commit -m "feat(friend-build): register the frozen app as the native host on Windows and macOS

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: Service fixes for the frozen build

**Why:** (a) `/resume/tailor` renders PDFs with Playwright, which the friend build leaves out; it must answer clearly. (b) `/profile/import-resume` is `async def` but calls the LLM synchronously. With the on-device bridge, that call waits for the extension to answer over HTTP, which a blocked event loop can never serve — a deadlock.

**Files:**
- Modify: `src/applypilot/extension/server.py`
- Test: `tests/test_extension_server.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_extension_server.py`:

```python
def test_tailor_is_unavailable_in_the_frozen_build(client, auth_headers, monkeypatch):
    import sys as _sys
    monkeypatch.setattr(_sys, "frozen", True, raising=False)
    resp = client.post("/resume/tailor", json={"urls": ["https://example.com/job"], "page_text": ""},
                       headers=auth_headers)
    assert resp.status_code == 501
    assert "aren't available" in resp.json()["detail"]


def test_resume_import_runs_off_the_event_loop(client, auth_headers, monkeypatch):
    """The LLM step may wait on the extension (on-device bridge), which the
    event loop must stay free to serve -- so the import runs in a worker thread."""
    import asyncio
    from types import SimpleNamespace

    from applypilot.extension import server as srv

    seen = {}

    def fake_import(**kwargs):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return SimpleNamespace(draft_profile={}, provenance={}, warnings=[],
                               saved_filename="resume.txt", content_type="text/plain")

    monkeypatch.setattr(srv.resume_import, "import_resume", fake_import)
    resp = client.post("/profile/import-resume", files={"file": ("resume.txt", b"Jordan Testperson", "text/plain")},
                       headers=auth_headers)
    assert resp.status_code == 200
    assert seen["on_loop"] is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_extension_server.py -v -k "frozen_build or off_the_event_loop"`
Expected: both FAIL — the first gets a 503/403 instead of 501; the second asserts `seen["on_loop"] is False` but it is `True`.

- [ ] **Step 3: Add the imports**

In `src/applypilot/extension/server.py`, the stdlib imports near the top include `import shutil` followed by `import tempfile`. Add `import sys` between them (keep alphabetical order):

```python
import shutil
import sys
import tempfile
```

Then find these two lines:

```python
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
```

and insert the new import between them:

```python
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
```

- [ ] **Step 4: Run the résumé import in a worker thread**

Replace this exact block (inside `import_resume_endpoint`):

```python
        try:
            result = resume_import.import_resume(
                filename=file.filename or "",
                data=data,
                existing_profile=existing,
                profile_dir=path.parent,
                allow_identity_change=allow_identity_change,
            )
```

with:

```python
        try:
            # In a worker thread: the LLM step can wait on the extension's
            # on-device bridge (GET /llm/next), which this event loop must
            # stay free to serve.
            result = await run_in_threadpool(
                resume_import.import_resume,
                filename=file.filename or "",
                data=data,
                existing_profile=existing,
                profile_dir=path.parent,
                allow_identity_change=allow_identity_change,
            )
```

- [ ] **Step 5: Make `/resume/tailor` answer 501 when frozen**

Replace this exact block:

```python
    @app.post("/resume/tailor")
    def tailor_endpoint(body: CoverLetterIn, _: None = Depends(_require_token)) -> dict:
        ok, _provider = llm_util.llm_available()
```

with:

```python
    @app.post("/resume/tailor")
    def tailor_endpoint(body: CoverLetterIn, _: None = Depends(_require_token)) -> dict:
        if getattr(sys, "frozen", False):
            # The friend build leaves out Playwright + Chromium, which render the PDF.
            raise HTTPException(status_code=501, detail="tailored résumés aren't available in this version "
                                                        "— use your regular résumé")
        ok, _provider = llm_util.llm_available()
```

- [ ] **Step 6: Run the server tests**

Run: `"$PY" -m pytest tests/test_extension_server.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/applypilot/extension/server.py tests/test_extension_server.py
git -c user.name="Adwait" -c user.email="adwait1234@gmail.com" commit -m "fix(server): résumé import off the event loop; tailor answers 501 in the friend build

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Phase B — On-device AI bridge

### Task 5: Python bridge (job queue + BridgeClient)

**Files:**
- Create: `src/applypilot/extension/llm_bridge.py`
- Test: `tests/test_extension_llm_bridge.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_extension_llm_bridge.py`:

```python
"""The on-device AI bridge: the service queues a chat job, an extension page
answers it. All in-process: no browser, no network."""
from __future__ import annotations

import threading

import pytest

from applypilot.extension import llm_bridge


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _answer_next(bridge, text=None, error=None, delay=0.0):
    """Simulates an extension page: take the next job and answer it."""
    def run():
        job = bridge.next_job(hold_s=5)
        if job is not None:
            bridge.complete(job.id, text=text, error=error)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_status_is_unavailable_until_reported_and_expires():
    clock = FakeClock()
    b = llm_bridge.Bridge(clock=clock)
    assert b.status() == "unavailable" and not b.live()
    b.report("available")
    assert b.status() == "available" and b.live()
    clock.t += llm_bridge.FRESH_S + 1
    assert b.status() == "unavailable" and not b.live()


def test_unknown_status_counts_as_unavailable():
    b = llm_bridge.Bridge()
    b.report("weird")
    assert b.status() == "unavailable"


def test_next_job_returns_none_when_nothing_is_queued():
    b = llm_bridge.Bridge()
    assert b.next_job(hold_s=0.05) is None


def test_job_round_trip():
    b = llm_bridge.Bridge()
    job = b.submit([{"role": "user", "content": "hi"}], temperature=0.3)
    got = b.next_job(hold_s=0.05)
    assert got is job and got.messages == [{"role": "user", "content": "hi"}] and got.temperature == 0.3
    assert b.complete(job.id, text="hello") is True
    assert job.done.is_set() and job.text == "hello"
    assert b.complete(job.id, text="again") is False  # already completed


def test_client_chat_returns_the_page_answer():
    b = llm_bridge.Bridge()
    _answer_next(b, text="drafted answer")
    client = llm_bridge.BridgeClient(bridge=b, pickup_timeout_s=5, answer_timeout_s=5)
    assert client.chat([{"role": "user", "content": "q"}], max_tokens=256, temperature=0.3) == "drafted answer"


def test_client_chat_raises_when_no_page_picks_it_up():
    b = llm_bridge.Bridge()
    client = llm_bridge.BridgeClient(bridge=b, pickup_timeout_s=0.05, answer_timeout_s=5)
    with pytest.raises(RuntimeError, match="didn't respond"):
        client.chat([{"role": "user", "content": "q"}])
    assert b.next_job(hold_s=0.01) is None  # the abandoned job was withdrawn


def test_client_chat_raises_the_page_error():
    b = llm_bridge.Bridge()
    _answer_next(b, error="input too long")
    client = llm_bridge.BridgeClient(bridge=b, pickup_timeout_s=5, answer_timeout_s=5)
    with pytest.raises(RuntimeError, match="input too long"):
        client.chat([{"role": "user", "content": "q"}])


def test_client_ask_is_a_single_user_message():
    b = llm_bridge.Bridge()
    seen = {}

    def run():
        job = b.next_job(hold_s=5)
        seen["messages"] = job.messages
        b.complete(job.id, text="ok")

    threading.Thread(target=run, daemon=True).start()
    assert llm_bridge.BridgeClient(bridge=b, pickup_timeout_s=5, answer_timeout_s=5).ask("hello") == "ok"
    assert seen["messages"] == [{"role": "user", "content": "hello"}]


def test_client_defaults_to_the_module_bridge(monkeypatch):
    fresh = llm_bridge.Bridge()
    monkeypatch.setattr(llm_bridge, "BRIDGE", fresh)
    assert llm_bridge.BridgeClient()._bridge is fresh
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_extension_llm_bridge.py -v`
Expected: FAIL — `ImportError: cannot import name 'llm_bridge'`.

- [ ] **Step 3: Write the implementation**

Create `src/applypilot/extension/llm_bridge.py`:

```python
"""On-device AI bridge: the local service asks an extension page to run the model.

Chrome's built-in model (Gemini Nano, the Prompt API's ``LanguageModel``) lives in
the browser, so Python can't call it. Every AI call in the extension service goes
through ``llm_util.get_llm_client().chat(...)``. ``BridgeClient`` has that same
``chat()`` signature: it queues a job and waits for an extension page (the side
panel or the options page, running ``extension/llm_bridge.js``) to take it from
``GET /llm/next`` and answer through ``POST /llm/result``.

Nothing here touches the network or the disk; the endpoints live in server.py.
"""
from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

PICKUP_TIMEOUT_S = 10.0    # a page must take the job this quickly...
ANSWER_TIMEOUT_S = 180.0   # ...and answer within this (CPU-only machines are slow)
FRESH_S = 60.0             # a page that reported "available" this recently counts as live
POLL_HOLD_S = 20.0         # how long GET /llm/next waits for a job before answering "none"
STATUSES = ("available", "downloadable", "downloading", "unavailable")


@dataclass
class Job:
    id: str
    messages: list
    temperature: float
    picked: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    text: str | None = None
    error: str | None = None


class Bridge:
    """Thread-safe queue of chat jobs plus the model status pages report."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._cond = threading.Condition()
        self._queue: deque[Job] = deque()
        self._jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)
        self._status = "unavailable"
        self._status_at: float | None = None

    def report(self, status: str) -> None:
        """A page's view of LanguageModel.availability()."""
        with self._cond:
            self._status = status if status in STATUSES else "unavailable"
            self._status_at = self._clock()

    def status(self) -> str:
        with self._cond:
            if self._status_at is None or self._clock() - self._status_at > FRESH_S:
                return "unavailable"
            return self._status

    def live(self) -> bool:
        """True when a page reported the model ready within FRESH_S seconds."""
        return self.status() == "available"

    def submit(self, messages: list, temperature: float) -> Job:
        with self._cond:
            job = Job(id=str(next(self._ids)), messages=list(messages), temperature=float(temperature))
            self._queue.append(job)
            self._jobs[job.id] = job
            self._cond.notify_all()
            return job

    def next_job(self, hold_s: float = POLL_HOLD_S) -> Job | None:
        """The oldest queued job, waiting up to ``hold_s`` for one to arrive."""
        deadline = time.monotonic() + hold_s
        with self._cond:
            while not self._queue:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)
            job = self._queue.popleft()
            job.picked.set()
            return job

    def complete(self, job_id: str, text: str | None = None, error: str | None = None) -> bool:
        """Record a page's answer. False when the job is unknown or already done."""
        with self._cond:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        job.text, job.error = text, error
        job.done.set()
        return True

    def withdraw(self, job: Job) -> None:
        """Forget a job its caller gave up on."""
        with self._cond:
            try:
                self._queue.remove(job)
            except ValueError:
                pass
            self._jobs.pop(job.id, None)


# The service's one bridge. Looked up at call time (``llm_bridge.BRIDGE``) so
# tests can swap it with monkeypatch.
BRIDGE = Bridge()


class BridgeClient:
    """Drop-in for ``applypilot.llm.LLMClient``: same ``chat``/``ask``/``close``."""

    def __init__(self, bridge: Bridge | None = None, pickup_timeout_s: float = PICKUP_TIMEOUT_S,
                 answer_timeout_s: float = ANSWER_TIMEOUT_S) -> None:
        self._bridge = bridge if bridge is not None else BRIDGE
        self._pickup = pickup_timeout_s
        self._answer = answer_timeout_s

    def chat(self, messages: list[dict], temperature: float = 0.0, max_tokens: int = 4096,
             response_format: dict | None = None) -> str:
        """``max_tokens`` and ``response_format`` are accepted for compatibility;
        the on-device model has no equivalent."""
        job = self._bridge.submit(messages, temperature)
        if not job.picked.wait(self._pickup):
            self._bridge.withdraw(job)
            raise RuntimeError("the on-device model didn't respond — keep the ApplyPilot side panel "
                               "or Settings page open while it works")
        if not job.done.wait(self._answer):
            self._bridge.withdraw(job)
            raise RuntimeError("the on-device model took too long to answer")
        if job.error:
            raise RuntimeError(f"on-device model error: {job.error}")
        return job.text or ""

    def ask(self, prompt: str, **kwargs) -> str:
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        pass
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `"$PY" -m pytest tests/test_extension_llm_bridge.py -v`
Expected: all PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/extension/llm_bridge.py tests/test_extension_llm_bridge.py
git -c user.name="Adwait" -c user.email="adwait1234@gmail.com" commit -m "feat(ai): on-device model bridge queue and BridgeClient

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 6: `/llm/next` and `/llm/result` endpoints

**Files:**
- Modify: `src/applypilot/extension/server.py`
- Test: `tests/test_extension_llm_bridge.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_extension_llm_bridge.py`:

```python
# ---------------------------------------------------------------------------
# /llm/next and /llm/result
# ---------------------------------------------------------------------------

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from applypilot.extension.server import create_app, get_or_create_token  # noqa: E402


@pytest.fixture
def bridge_client(tmp_path, monkeypatch):
    fresh = llm_bridge.Bridge()
    monkeypatch.setattr(llm_bridge, "BRIDGE", fresh)
    monkeypatch.setattr(llm_bridge, "POLL_HOLD_S", 0.05)
    token = get_or_create_token(tmp_path)
    app = create_app(app_dir=tmp_path, root=tmp_path, profile={"personal": {}})
    return TestClient(app), {"X-ApplyPilot-Token": token}, fresh


def test_llm_next_needs_the_token(bridge_client):
    client, _headers, _bridge = bridge_client
    assert client.get("/llm/next?status=available").status_code == 401


def test_llm_next_records_status_and_gives_no_job_when_not_ready(bridge_client):
    client, headers, bridge = bridge_client
    bridge.submit([{"role": "user", "content": "q"}], 0.3)
    resp = client.get("/llm/next?status=downloadable", headers=headers)
    assert resp.status_code == 200 and resp.json() == {"job": None}
    assert bridge.status() == "downloadable"


def test_llm_next_hands_out_a_job_and_result_completes_it(bridge_client):
    client, headers, bridge = bridge_client
    job = bridge.submit([{"role": "system", "content": "s"}, {"role": "user", "content": "q"}], 0.3)
    resp = client.get("/llm/next?status=available", headers=headers)
    assert resp.json() == {"job": {"id": job.id, "temperature": 0.3,
                                   "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]}}
    done = client.post("/llm/result", json={"id": job.id, "text": "answer"}, headers=headers)
    assert done.json() == {"ok": True} and job.text == "answer"


def test_llm_next_with_nothing_queued_answers_none(bridge_client):
    client, headers, _bridge = bridge_client
    assert client.get("/llm/next?status=available", headers=headers).json() == {"job": None}


def test_llm_result_for_an_unknown_job(bridge_client):
    client, headers, _bridge = bridge_client
    assert client.post("/llm/result", json={"id": "nope", "error": "x"}, headers=headers).json() == {"ok": False}


def test_end_to_end_chat_through_the_endpoints(bridge_client):
    """BridgeClient.chat waits in one thread while a 'page' polls and answers."""
    client, headers, bridge = bridge_client
    result = {}

    def call():
        result["text"] = llm_bridge.BridgeClient(bridge=bridge, pickup_timeout_s=5, answer_timeout_s=5).chat(
            [{"role": "user", "content": "q"}])

    t = threading.Thread(target=call, daemon=True)
    t.start()
    job = None
    for _ in range(100):
        job = client.get("/llm/next?status=available", headers=headers).json()["job"]
        if job:
            break
    assert job is not None
    client.post("/llm/result", json={"id": job["id"], "text": "from the page"}, headers=headers)
    t.join(timeout=5)
    assert result["text"] == "from the page"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_extension_llm_bridge.py -v -k "llm_next or llm_result or end_to_end"`
Expected: FAIL with 404s (the endpoints don't exist).

- [ ] **Step 3: Add the import**

In `src/applypilot/extension/server.py`, find the line that starts with:

```python
from applypilot.extension import (answer_memory, app_log, cover_letter, job_context, llm_util, resolve,
```

In that line, replace `job_context, llm_util, resolve,` with `job_context, llm_bridge, llm_util, resolve,` (keeps the list alphabetical; the rest of the statement is unchanged).

- [ ] **Step 4: Add the request model**

Find this exact line:

```python
class CoverLetterIn(BaseModel):
```

and add directly ABOVE it:

```python
class LlmResultIn(BaseModel):
    """POST /llm/result body: a page's answer to one on-device AI job."""
    id: str
    text: str | None = None
    error: str | None = None


```

- [ ] **Step 5: Add the endpoints**

Find the `/health` endpoint. Its body ends with these exact lines:

```python
            "llm_blocked_reason": llm_util.cloud_block_reason(app_dir),
        }
```

Add directly BELOW them (same indentation as `@app.get("/health")`):

```python

    # ---- on-device AI bridge (see llm_bridge.py and extension/llm_bridge.js) ----
    # Plain `def`: FastAPI runs these in its thread pool, so a poll held open
    # waiting for a job never blocks other requests.

    @app.get("/llm/next")
    def llm_next(status: str = "unavailable", _: None = Depends(_require_token)) -> dict:
        llm_bridge.BRIDGE.report(status)
        if status != "available":
            return {"job": None}
        job = llm_bridge.BRIDGE.next_job(hold_s=llm_bridge.POLL_HOLD_S)
        if job is None:
            return {"job": None}
        return {"job": {"id": job.id, "messages": job.messages, "temperature": job.temperature}}

    @app.post("/llm/result")
    def llm_result(body: LlmResultIn, _: None = Depends(_require_token)) -> dict:
        return {"ok": llm_bridge.BRIDGE.complete(body.id, text=body.text, error=body.error)}
```

- [ ] **Step 6: Run the bridge and server tests**

Run: `"$PY" -m pytest tests/test_extension_llm_bridge.py tests/test_extension_server.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/applypilot/extension/server.py tests/test_extension_llm_bridge.py
git -c user.name="Adwait" -c user.email="adwait1234@gmail.com" commit -m "feat(ai): /llm/next and /llm/result endpoints for the on-device bridge

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 7: Use the bridge as an AI provider

**Provider order after this task:** explicit env provider (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `LLM_URL`, `LLM_PROVIDER=claude`) → on-device bridge (a page reported the model ready within 60 s) → Claude CLI → none. The operator's setup (`LLM_URL` = Ollama) is unchanged.

**Files:**
- Modify: `src/applypilot/extension/llm_util.py`
- Test: `tests/test_extension_llm_util.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_extension_llm_util.py`:

```python
# ---------------------------------------------------------------------------
# On-device bridge (Chrome's built-in model via extension/llm_bridge.js)
# ---------------------------------------------------------------------------

def _no_env_provider(monkeypatch):
    def _raise():
        raise RuntimeError("No LLM provider configured.")
    monkeypatch.setattr("applypilot.llm.get_client", _raise)
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: None)


def _bridge(monkeypatch, status=None):
    from applypilot.extension import llm_bridge
    fresh = llm_bridge.Bridge()
    if status:
        fresh.report(status)
    monkeypatch.setattr(llm_bridge, "BRIDGE", fresh)
    return fresh


def test_bridge_is_used_when_no_env_provider_and_a_page_is_ready(monkeypatch):
    from applypilot.extension import llm_bridge
    _no_env_provider(monkeypatch)
    fresh = _bridge(monkeypatch, "available")
    client = llm_util.get_llm_client()
    assert isinstance(client, llm_bridge.BridgeClient) and client._bridge is fresh


def test_bridge_is_not_used_when_no_page_is_ready(monkeypatch):
    _no_env_provider(monkeypatch)
    _bridge(monkeypatch, "downloadable")
    with pytest.raises(RuntimeError):
        llm_util.get_llm_client()


def test_env_provider_still_wins_over_the_bridge(monkeypatch):
    sentinel = object()
    monkeypatch.setattr("applypilot.llm.get_client", lambda: sentinel)
    _bridge(monkeypatch, "available")
    assert llm_util.get_llm_client() is sentinel


def test_bridge_beats_the_claude_cli_fallback(monkeypatch):
    from applypilot.extension import llm_bridge

    def _raise():
        raise RuntimeError("No LLM provider configured.")
    monkeypatch.setattr("applypilot.llm.get_client", _raise)
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: "C:/fake/claude.exe")
    _bridge(monkeypatch, "available")
    assert isinstance(llm_util.get_llm_client(), llm_bridge.BridgeClient)


def test_provider_info_reports_the_bridge_as_local(monkeypatch):
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: None)
    _bridge(monkeypatch, "available")
    info = llm_util.provider_info()
    assert info == {"available": True, "provider": "chrome-on-device",
                    "label": "Chrome's built-in AI on this computer", "local": True}
    assert llm_util.cloud_block_reason() is None


def test_provider_info_without_bridge_is_unchanged(monkeypatch):
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: None)
    _bridge(monkeypatch)
    assert llm_util.provider_info()["available"] is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_extension_llm_util.py -v -k "bridge"`
Expected: FAIL (`get_llm_client` raises instead of returning a `BridgeClient`; `provider_info` has no `chrome-on-device`).

- [ ] **Step 3: Add the bridge to `get_llm_client()`**

In `src/applypilot/extension/llm_util.py`, replace this exact block:

```python
    try:
        return llm_mod.get_client()
    except RuntimeError:
        from applypilot.config import find_claude_binary

        claude_bin = find_claude_binary()
```

with:

```python
    try:
        return llm_mod.get_client()
    except RuntimeError:
        # Chrome's on-device model, answered by an open extension page. Local
        # and free, so it goes before the metered Claude CLI.
        from applypilot.extension import llm_bridge

        if llm_bridge.BRIDGE.live():
            return llm_bridge.BridgeClient()

        from applypilot.config import find_claude_binary

        claude_bin = find_claude_binary()
```

- [ ] **Step 4: Add the bridge to `llm_available()`**

In the same file, inside `llm_available()`, replace this exact block:

```python
    if config.find_claude_binary() is not None:
        return True, "claude-cli"

    return False, ""
```

with:

```python
    from applypilot.extension import llm_bridge

    if llm_bridge.BRIDGE.live():
        return True, "chrome-on-device"

    if config.find_claude_binary() is not None:
        return True, "claude-cli"

    return False, ""
```

- [ ] **Step 5: Label it and mark it local**

Replace this exact block:

```python
    "remote-endpoint": "a remote model endpoint",
    "local": "a model on this computer",
}
```

with:

```python
    "remote-endpoint": "a remote model endpoint",
    "local": "a model on this computer",
    "chrome-on-device": "Chrome's built-in AI on this computer",
}
```

And in `provider_info()`, replace this exact line:

```python
            "local": provider == "local"}
```

with:

```python
            "local": provider in ("local", "chrome-on-device")}
```

- [ ] **Step 6: Run the LLM tests**

Run: `"$PY" -m pytest tests/test_extension_llm_util.py tests/test_extension_cloud_llm.py tests/test_extension_llm_bridge.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/applypilot/extension/llm_util.py tests/test_extension_llm_util.py
git -c user.name="Adwait" -c user.email="adwait1234@gmail.com" commit -m "feat(ai): use Chrome's on-device model when no other provider is set up

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 8: Extension-side bridge (`llm_bridge.js`) + Node tests

**Files:**
- Create: `extension/llm_bridge.js`
- Create: `extension/llm_bridge_selftest.js`

- [ ] **Step 1: Write the failing test file**

Create `extension/llm_bridge_selftest.js`:

```javascript
#!/usr/bin/env node
/*
 * Tests extension/llm_bridge.js in plain Node -- no browser, no npm packages:
 *   node extension/llm_bridge_selftest.js
 * Each test loads the file into a fresh vm context with fake chrome/fetch/LanguageModel.
 * Dev-only: never shipped (not in packaging/extension_files.txt).
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = fs.readFileSync(path.join(__dirname, 'llm_bridge.js'), 'utf8');

function load(globals) {
  const ctx = vm.createContext(Object.assign({ setTimeout: () => 0 }, globals));
  vm.runInContext(SRC, ctx);
  return ctx.ApplyPilotLlmBridge;
}

function fakeChrome(store) {
  return { storage: { local: { get: () => Promise.resolve(Object.assign({}, store)) } } };
}

function jsonResponse(obj, status) {
  const code = status || 200;
  return { ok: code < 400, status: code, json: () => Promise.resolve(obj) };
}

const checks = [];
function expect(name, cond) { checks.push({ name, pass: !!cond }); }

async function main() {
  // ---- toPromptApi ----
  {
    const B = load({});
    const p = B.toPromptApi([
      { role: 'system', content: 'A' }, { role: 'system', content: 'B' },
      { role: 'user', content: 'q1' }, { role: 'assistant', content: 'a1' }, { role: 'user', content: 'q2' }]);
    expect('toPromptApi: every system message merged into one leading system prompt',
      p.initialPrompts[0].role === 'system' && p.initialPrompts[0].content === 'A\n\nB');
    expect('toPromptApi: earlier turns kept in order',
      p.initialPrompts.length === 3 && p.initialPrompts[1].content === 'q1' && p.initialPrompts[2].content === 'a1');
    expect('toPromptApi: the last user message is the input', p.input === 'q2');
    let threw = false;
    try { B.toPromptApi([{ role: 'system', content: 'x' }]); } catch (e) { threw = true; }
    expect('toPromptApi: no user message throws', threw);
  }

  // ---- availability ----
  {
    expect('availability: no LanguageModel -> unavailable', (await load({}).availability()) === 'unavailable');
    const odd = load({ LanguageModel: { availability: () => Promise.resolve('weird') } });
    expect('availability: an unknown value -> unavailable', (await odd.availability()) === 'unavailable');
    const ok = load({ LanguageModel: { availability: () => Promise.resolve('available') } });
    expect('availability: passes "available" through', (await ok.availability()) === 'available');
    const boom = load({ LanguageModel: { availability: () => Promise.reject(new Error('x')) } });
    expect('availability: a throwing API -> unavailable', (await boom.availability()) === 'unavailable');
  }

  // ---- start() is inert without LanguageModel ----
  {
    let fetched = 0;
    const B = load({ chrome: fakeChrome({ token: 't' }),
      fetch: () => { fetched++; return Promise.resolve(jsonResponse({ job: null })); } });
    expect('start: inert where the browser has no LanguageModel', B.start() === false && fetched === 0);
  }

  // ---- pollOnce reports status; nothing runs while the model isn't ready ----
  {
    const calls = [];
    const B = load({
      chrome: fakeChrome({ token: 'tok', serviceUrl: 'http://127.0.0.1:9999/' }),
      LanguageModel: { availability: () => Promise.resolve('downloadable'),
        create: () => { throw new Error('must not run'); } },
      fetch: (url, opts) => { calls.push([url, opts]); return Promise.resolve(jsonResponse({ job: null })); }
    });
    const r = await B.pollOnce();
    expect('pollOnce: reports its status to /llm/next (trailing slash on the URL trimmed)',
      calls.length === 1 && calls[0][0] === 'http://127.0.0.1:9999/llm/next?status=downloadable');
    expect('pollOnce: sends the token', calls[0][1].headers['X-ApplyPilot-Token'] === 'tok');
    expect('pollOnce: model not ready -> waits before the next poll', r.waitMs === B.IDLE_WAIT_MS);
  }

  // ---- no token yet: no request at all ----
  {
    let fetched = 0;
    const B = load({ chrome: fakeChrome({}), LanguageModel: { availability: () => Promise.resolve('available') },
      fetch: () => { fetched++; return Promise.resolve(jsonResponse({ job: null })); } });
    const r = await B.pollOnce();
    expect('pollOnce: no token yet -> no request, idle wait', fetched === 0 && r.waitMs === B.IDLE_WAIT_MS);
  }

  // ---- a job is run and answered ----
  {
    const posts = [];
    const created = [];
    const B = load({
      chrome: fakeChrome({ token: 'tok' }),
      LanguageModel: {
        availability: () => Promise.resolve('available'),
        params: () => Promise.resolve({ defaultTopK: 3, maxTopK: 128, defaultTemperature: 1, maxTemperature: 2 }),
        create: (opts) => {
          created.push(opts);
          return Promise.resolve({ prompt: (input) => Promise.resolve('ANSWER to ' + input), destroy: () => {} });
        }
      },
      fetch: (url, opts) => {
        if (url.indexOf('/llm/next') !== -1) {
          return Promise.resolve(jsonResponse({ job: { id: '7', temperature: 5,
            messages: [{ role: 'system', content: 'S' }, { role: 'user', content: 'Q' }] } }));
        }
        posts.push([url, JSON.parse(opts.body)]);
        return Promise.resolve(jsonResponse({ ok: true }));
      }
    });
    const r = await B.pollOnce();
    expect('job: the answer is posted to /llm/result with its id',
      posts.length === 1 && posts[0][0] === 'http://127.0.0.1:8787/llm/result' &&
      posts[0][1].id === '7' && posts[0][1].text === 'ANSWER to Q');
    expect('job: temperature clamped to the model maximum, sent together with topK',
      created[0].temperature === 2 && created[0].topK === 3);
    expect('job: the system message is the first initial prompt',
      created[0].initialPrompts[0].role === 'system' && created[0].initialPrompts[0].content === 'S');
    expect('job: polls again immediately', r.waitMs === 0);
  }

  // ---- a model error is reported, never swallowed ----
  {
    const posts = [];
    const B = load({
      chrome: fakeChrome({ token: 'tok' }),
      LanguageModel: { availability: () => Promise.resolve('available'),
        create: () => Promise.resolve({ prompt: () => Promise.reject(new Error('input too long')), destroy: () => {} }) },
      fetch: (url, opts) => {
        if (url.indexOf('/llm/next') !== -1) {
          return Promise.resolve(jsonResponse({ job: { id: '8', temperature: 0.3, messages: [{ role: 'user', content: 'Q' }] } }));
        }
        posts.push(JSON.parse(opts.body));
        return Promise.resolve(jsonResponse({ ok: true }));
      }
    });
    await B.pollOnce();
    expect('job error: posted back as {id, error}',
      posts.length === 1 && posts[0].id === '8' && /input too long/.test(posts[0].error) && posts[0].text === undefined);
  }

  // ---- service unreachable ----
  {
    const B = load({ chrome: fakeChrome({ token: 'tok' }), LanguageModel: { availability: () => Promise.resolve('available') },
      fetch: () => Promise.reject(new Error('ECONNREFUSED')) });
    const r = await B.pollOnce();
    expect('pollOnce: service unreachable -> idle wait, never throws', r.waitMs === B.IDLE_WAIT_MS);
  }

  // ---- download() ----
  {
    let msg = '';
    try { await load({}).download(); } catch (e) { msg = e.message; }
    expect('download: no LanguageModel -> a clear error', /built-in AI/.test(msg));

    const progress = [];
    let createdSync = false;
    const B = load({ LanguageModel: {
      create: (opts) => {
        createdSync = true;
        opts.monitor({ addEventListener: (type, fn) => { if (type === 'downloadprogress') { fn({ loaded: 0.5 }); fn({ loaded: 1 }); } } });
        return Promise.resolve({ destroy: () => {} });
      } } });
    const pending = B.download((loaded) => progress.push(loaded));
    expect('download: create() is called synchronously (Chrome needs the click gesture)', createdSync);
    expect('download: resolves "available"', (await pending) === 'available');
    expect('download: reports progress', progress.join(',') === '0.5,1');
  }

  let failed = 0;
  for (const c of checks) {
    console.log(`${c.pass ? 'PASS' : 'FAIL'}  ${c.name}`);
    if (!c.pass) failed++;
  }
  console.log(`\n${checks.length - failed}/${checks.length} checks passed.`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
```

- [ ] **Step 2: Run it to verify it fails**

Run: `node extension/llm_bridge_selftest.js`
Expected: FAIL — `ENOENT: no such file or directory ... llm_bridge.js`.

- [ ] **Step 3: Write the implementation**

Create `extension/llm_bridge.js`:

```javascript
/**
 * ApplyPilot Copilot — on-device AI bridge.
 *
 * Chrome's built-in model (Gemini Nano, the Prompt API's `LanguageModel`) runs
 * inside the browser, so the local Python service can't call it. While the side
 * panel or the options page is open, this loop asks the service for work
 * (GET /llm/next, held up to ~20 s), runs each job with LanguageModel, and posts
 * the answer back (POST /llm/result). The service's llm_bridge.BridgeClient
 * waits for that answer, so every AI feature (drafted answers, cover letters,
 * résumé import) works unchanged.
 *
 * Inert where the browser has no LanguageModel at all (older Chrome, test
 * browsers). Reads the service URL and token from chrome.storage.local — the
 * same keys background.js's getConfig() uses. Never runs in the service worker:
 * its lifetime is too short for a long-poll loop.
 */
(function (root) {
  'use strict';

  var DEFAULT_SERVICE_URL = 'http://127.0.0.1:8787';
  var IDLE_WAIT_MS = 5000;
  var STATUSES = ['available', 'downloadable', 'downloading', 'unavailable'];
  var running = false;

  function model() { return root.LanguageModel; }

  function destroy(session) {
    try { if (session && typeof session.destroy === 'function') session.destroy(); } catch (e) { /* ignore */ }
  }

  /** 'available' | 'downloadable' | 'downloading' | 'unavailable' — never rejects. */
  function availability() {
    var LM = model();
    if (!LM || typeof LM.availability !== 'function') return Promise.resolve('unavailable');
    return Promise.resolve().then(function () { return LM.availability(); }).then(function (a) {
      return STATUSES.indexOf(a) === -1 ? 'unavailable' : a;
    }, function () { return 'unavailable'; });
  }

  function getConfig() {
    return root.chrome.storage.local.get(['serviceUrl', 'token']).then(function (d) {
      return {
        serviceUrl: String(d.serviceUrl || DEFAULT_SERVICE_URL).replace(/\/+$/, ''),
        token: d.token || ''
      };
    });
  }

  /**
   * Python chat messages -> { initialPrompts, input } for the Prompt API. Every
   * system message is merged into ONE leading system prompt (the API accepts a
   * system role only first); the last user message is the input; the turns in
   * between stay in order.
   */
  function toPromptApi(messages) {
    var list = (messages || []).filter(function (m) { return m && typeof m.content === 'string'; });
    var last = -1;
    for (var i = list.length - 1; i >= 0; i--) {
      if (list[i].role === 'user') { last = i; break; }
    }
    if (last === -1) throw new Error('no user message to answer');
    var systemParts = [];
    var turns = [];
    for (var j = 0; j < last; j++) {
      var m = list[j];
      if (m.role === 'system') systemParts.push(m.content);
      else turns.push({ role: m.role === 'assistant' ? 'assistant' : 'user', content: m.content });
    }
    var initialPrompts = systemParts.length ? [{ role: 'system', content: systemParts.join('\n\n') }] : [];
    return { initialPrompts: initialPrompts.concat(turns), input: list[last].content };
  }

  /** Runs one job with the on-device model; resolves to its text. */
  function runJob(job) {
    var LM = model();
    return Promise.resolve().then(function () {
      var p = toPromptApi(job.messages);
      var paramsP = typeof LM.params === 'function'
        ? Promise.resolve().then(function () { return LM.params(); }).catch(function () { return null; })
        : Promise.resolve(null);
      return paramsP.then(function (params) {
        var opts = { initialPrompts: p.initialPrompts };
        if (params && typeof job.temperature === 'number') {
          // The API takes temperature and topK together, or neither.
          opts.temperature = Math.max(0, Math.min(job.temperature, params.maxTemperature));
          opts.topK = params.defaultTopK;
        }
        return LM.create(opts);
      }).then(function (session) {
        return Promise.resolve(session.prompt(p.input)).then(function (text) {
          destroy(session);
          return String(text == null ? '' : text);
        }, function (err) {
          destroy(session);
          throw err;
        });
      });
    });
  }

  function postResult(cfg, body) {
    return root.fetch(cfg.serviceUrl + '/llm/result', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-ApplyPilot-Token': cfg.token },
      body: JSON.stringify(body)
    });
  }

  /** One poll: report status, take at most one job, answer it. Resolves { waitMs }; never rejects. */
  function pollOnce() {
    return Promise.all([getConfig(), availability()]).then(function (r) {
      var cfg = r[0];
      var status = r[1];
      if (!cfg.token) return { waitMs: IDLE_WAIT_MS };
      return root.fetch(cfg.serviceUrl + '/llm/next?status=' + encodeURIComponent(status), {
        headers: { 'X-ApplyPilot-Token': cfg.token }
      }).then(function (resp) {
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.json();
      }).then(function (data) {
        var job = data && data.job;
        if (!job) return { waitMs: status === 'available' ? 0 : IDLE_WAIT_MS };
        return runJob(job).then(function (text) {
          return postResult(cfg, { id: job.id, text: text });
        }, function (err) {
          return postResult(cfg, { id: job.id, error: String((err && err.message) || err).slice(0, 300) });
        }).then(function () { return { waitMs: 0 }; });
      });
    }).catch(function () { return { waitMs: IDLE_WAIT_MS }; });
  }

  /** Starts the loop, once per page. Returns false (and does nothing) without LanguageModel. */
  function start() {
    if (running || !model()) return false;
    running = true;
    (function loop() {
      pollOnce().then(function (r) { root.setTimeout(loop, r.waitMs); });
    })();
    return true;
  }

  /**
   * Downloads the on-device model. MUST be called straight from a click handler:
   * Chrome only starts the download with a user gesture, so create() is called
   * before anything is awaited.
   */
  function download(onProgress) {
    var LM = model();
    if (!LM || typeof LM.create !== 'function') {
      return Promise.reject(new Error("this version of Chrome doesn't include built-in AI"));
    }
    var created = LM.create({
      monitor: function (m) {
        m.addEventListener('downloadprogress', function (e) { if (onProgress) onProgress(e.loaded); });
      }
    });
    return Promise.resolve(created).then(function (session) {
      destroy(session);
      return 'available';
    });
  }

  root.ApplyPilotLlmBridge = {
    start: start,
    availability: availability,
    download: download,
    toPromptApi: toPromptApi,
    runJob: runJob,
    pollOnce: pollOnce,
    IDLE_WAIT_MS: IDLE_WAIT_MS
  };
})(typeof globalThis !== 'undefined' ? globalThis : this);
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node extension/llm_bridge_selftest.js`
Expected: `23/23 checks passed.` and exit code 0.

- [ ] **Step 5: Commit**

```bash
git add extension/llm_bridge.js extension/llm_bridge_selftest.js
git -c user.name="Adwait" -c user.email="adwait1234@gmail.com" commit -m "feat(ai): extension bridge that answers the service with Chrome's on-device model

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 9: Wire the bridge into the side panel and Settings

**Files:**
- Modify: `extension/sidepanel.html`, `extension/sidepanel.js`
- Modify: `extension/options.html`, `extension/options.js`

- [ ] **Step 1: Load the bridge in both pages**

In `extension/sidepanel.html`, replace this exact line:

```html
  <script src="sidepanel.js"></script>
```

with:

```html
  <script src="llm_bridge.js"></script>
  <script src="sidepanel.js"></script>
```

In `extension/options.html`, replace this exact line:

```html
  <script src="options.js"></script>
```

with:

```html
  <script src="llm_bridge.js"></script>
  <script src="options.js"></script>
```

- [ ] **Step 2: Start the bridge when each page loads**

In `extension/sidepanel.js`, the file ends with:

```javascript
  init();
})();
```

Replace those two lines with:

```javascript
  init();
  // Answer on-device AI jobs from the service while this panel is open.
  if (window.ApplyPilotLlmBridge) window.ApplyPilotLlmBridge.start();
})();
```

In `extension/options.js`, the file ends with:

```javascript
  load();
})();
```

Replace those two lines with:

```javascript
  load();
  // Answer on-device AI jobs from the service while Settings is open (résumé import).
  if (window.ApplyPilotLlmBridge) window.ApplyPilotLlmBridge.start();
})();
```

- [ ] **Step 3: Add the "On-device AI" row to Settings → Smart fill**

In `extension/options.html`, replace this exact line:

```html
    <div class="hint" id="smartFillModelLine" style="margin-top:12px;">AI model: checking…</div>
```

with:

```html
    <div class="hint" id="smartFillModelLine" style="margin-top:12px;">AI model: checking…</div>
    <div id="onDeviceAiRow" style="margin-top:12px;">
      <div class="hint" id="onDeviceAiLine">On-device AI: checking…</div>
      <button type="button" id="onDeviceAiDownload" style="display:none;margin-top:8px;">Download on-device model</button>
    </div>
```

- [ ] **Step 4: Drive the row from options.js**

In `extension/options.js`, find this exact function:

```javascript
  function loadLlmAvailability() {
    chrome.runtime.sendMessage({ type: 'HEALTH' }).then(function (resp) {
      applyHealthData(resp && resp.ok ? resp.data : null);
    }, function () {
      applyHealthData(null);
    });
  }
```

and add directly BELOW it:

```javascript

  // ---- On-device AI (Chrome's built-in model; see llm_bridge.js) ----
  var onDeviceAiLineEl = document.getElementById('onDeviceAiLine');
  var onDeviceAiDownloadEl = document.getElementById('onDeviceAiDownload');
  var ON_DEVICE_AI_TEXT = {
    available: 'On-device AI: ready. Chrome\'s built-in model runs on this computer; nothing is sent anywhere.',
    downloadable: 'On-device AI: works on this computer but isn\'t downloaded yet (a one-time download of a few GB).',
    downloading: 'On-device AI: downloading…',
    unavailable: 'On-device AI: not available here. It needs Chrome 138 or newer, about 22 GB of free disk space, ' +
      'and a graphics card with more than 4 GB of memory or 16 GB of RAM. Autofill works without it.'
  };

  function refreshOnDeviceAi() {
    var bridge = window.ApplyPilotLlmBridge;
    if (!bridge) {
      onDeviceAiLineEl.textContent = ON_DEVICE_AI_TEXT.unavailable;
      onDeviceAiDownloadEl.style.display = 'none';
      return;
    }
    bridge.availability().then(function (a) {
      onDeviceAiLineEl.textContent = ON_DEVICE_AI_TEXT[a] || ON_DEVICE_AI_TEXT.unavailable;
      onDeviceAiDownloadEl.style.display = a === 'downloadable' ? '' : 'none';
    });
  }

  onDeviceAiDownloadEl.addEventListener('click', function () {
    // download() calls LanguageModel.create() synchronously: Chrome only starts
    // the model download inside this click's user gesture.
    onDeviceAiDownloadEl.disabled = true;
    onDeviceAiLineEl.textContent = ON_DEVICE_AI_TEXT.downloading;
    window.ApplyPilotLlmBridge.download(function (loaded) {
      onDeviceAiLineEl.textContent = 'On-device AI: downloading… ' + Math.round(loaded * 100) + '%';
    }).then(function () {
      onDeviceAiDownloadEl.disabled = false;
      refreshOnDeviceAi();
      // The service learns the model is ready on the bridge's next poll.
      setTimeout(loadLlmAvailability, 3000);
    }, function (err) {
      onDeviceAiDownloadEl.disabled = false;
      onDeviceAiLineEl.textContent = 'On-device AI: download failed — ' + ((err && err.message) || err);
    });
  });
```

Then find this exact block inside `load()`:

```javascript
      loadSmartFillSettings();
      loadLlmAvailability();
      loadAnswers();
```

and replace it with:

```javascript
      loadSmartFillSettings();
      loadLlmAvailability();
      refreshOnDeviceAi();
      loadAnswers();
```

- [ ] **Step 5: Syntax-check and run the bridge tests**

Run:
```bash
node --check extension/options.js && node --check extension/sidepanel.js && node --check extension/llm_bridge.js && echo SYNTAX-OK
node extension/llm_bridge_selftest.js
```
Expected: `SYNTAX-OK`, then `23/23 checks passed.`

- [ ] **Step 6: Commit**

```bash
git add extension/sidepanel.html extension/sidepanel.js extension/options.html extension/options.js
git -c user.name="Adwait" -c user.email="adwait1234@gmail.com" commit -m "feat(ai): on-device AI row in Settings; side panel and Settings run the bridge

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 10: Document the bridge + regression check

**Files:**
- Modify: `extension/README.md`

- [ ] **Step 1: Add the file-map row**

In `extension/README.md`, find the line that starts with `` | `selftest.js` | `` (it's in the "Architecture / file map" table). Insert these two rows directly AFTER that line:

```markdown
| `llm_bridge.js` | On-device AI bridge, loaded by `sidepanel.html` and `options.html` only. While either page is open it long-polls the service's `GET /llm/next`, runs each job with Chrome's built-in model (`LanguageModel`, Gemini Nano), and posts the answer to `POST /llm/result`; the service's `llm_bridge.BridgeClient` waits for it. Reads `serviceUrl`/`token` from `chrome.storage.local` (the same keys `background.js` uses). Inert when the browser has no `LanguageModel`. `download()` starts the one-time model download and must be called from a click. |
| `llm_bridge_selftest.js` | Node-only tests for `llm_bridge.js` (`node extension/llm_bridge_selftest.js`). Dev-only, never shipped. |
```

- [ ] **Step 2: Add a section at the end of the README**

Append to the end of `extension/README.md`:

```markdown

## On-device AI (Chrome's built-in model)

When no other AI provider is configured (no `GEMINI_API_KEY`, `OPENAI_API_KEY` or `LLM_URL`), the service uses Chrome's own on-device model through the extension. The friend build relies on this; your own setup (a local Ollama via `LLM_URL`) is unchanged, because an explicit provider always wins.

- **Requirements** (Chrome, 2026-09): Chrome 138+, Windows 10/11 or macOS 13+, at least 22 GB free on the drive holding the Chrome profile, and a GPU with more than 4 GB of video memory or 16 GB of RAM with 4+ CPU cores. Otherwise Settings says it's unavailable and AI features are skipped; autofill works regardless.
- **First use:** Settings → Smart fill → "Download on-device model" (a one-time download of a few GB).
- **Keep a page open:** the model runs in the side panel or the Settings page. Fill started from the Alt+Shift+G shortcut with the panel closed gets no drafted answers.
- **Privacy:** nothing leaves the computer; the service reports this provider as local, so the "allow cloud AI" setting doesn't apply to it.
```

- [ ] **Step 3: Run the Python and JS suites touched so far**

Run:
```bash
"$PY" -m pytest tests/test_extension_frozen_entry.py tests/test_extension_native_host.py tests/test_extension_server.py tests/test_extension_llm_bridge.py tests/test_extension_llm_util.py tests/test_extension_cloud_llm.py -q -p no:cacheprovider
node extension/llm_bridge_selftest.js
```
Expected: all pytest tests pass; `23/23 checks passed.`

- [ ] **Step 4: Commit**

```bash
git add extension/README.md
git -c user.name="Adwait" -c user.email="adwait1234@gmail.com" commit -m "docs(extension): on-device AI bridge

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Phase C — Packaging

### Task 11: Build inputs + extension staging

**Files:**
- Create: `packaging/requirements-friend.txt`
- Create: `packaging/extension_files.txt`
- Create: `packaging/stage_extension.py`
- Test: `tests/test_stage_extension.py`
- Modify: `.gitignore`

- [ ] **Step 1: Create the requirements file**

Create `packaging/requirements-friend.txt`:

```text
# Runtime + build dependencies for the frozen friend build (PyInstaller).
# Installed into packaging/.venv-win or packaging/.venv-mac by the build scripts.
# The operator's own install uses pyproject.toml, not this file.
fastapi>=0.110
uvicorn>=0.27
pydantic>=2.0
python-multipart>=0.0.18
httpx>=0.24
python-dotenv>=1.0
pyyaml>=6.0
rich>=13.0
pypdf>=4.0
python-docx>=1.0
pyinstaller>=6.6
```

- [ ] **Step 2: Create the extension allow-list**

Create `packaging/extension_files.txt`:

```text
# Extension files that ship to friends. Anything not listed stays home
# (selftest.js, test-page.html, fixtures, llm_bridge_selftest.js, README.md).
# A trailing slash copies a whole folder.
manifest.json
background.js
content.js
scanner.js
capture.js
llm_bridge.js
sidepanel.html
sidepanel.js
sidepanel.css
options.html
options.js
icons/
```

- [ ] **Step 3: Write the failing tests**

Create `tests/test_stage_extension.py`:

```python
"""packaging/stage_extension.py: what ships in the friend's extension folder."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("stage_extension", ROOT / "packaging" / "stage_extension.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_real_allow_list_stages_cleanly(tmp_path):
    stage = _load()
    dest = tmp_path / "extension"
    assert stage.stage(dest) == []
    assert (dest / "manifest.json").exists() and (dest / "llm_bridge.js").exists() and (dest / "icons").is_dir()
    for dev_only in ("selftest.js", "test-page.html", "fixtures_scanned_fields.jsonl", "llm_bridge_selftest.js", "README.md"):
        assert not (dest / dev_only).exists(), dev_only


def _fake_ext(tmp_path, manifest, pages=None, extra=()):
    src = tmp_path / "src"
    src.mkdir()
    (src / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for name, html in (pages or {}).items():
        (src / name).write_text(html, encoding="utf-8")
    for name in extra:
        (src / name).write_text("x", encoding="utf-8")
    return src


def test_missing_listed_file_is_reported(tmp_path):
    stage = _load()
    src = _fake_ext(tmp_path, {"key": "k"})
    problems = stage.stage(tmp_path / "out", src=src, entries=["manifest.json", "gone.js"])
    assert problems == ["missing: gone.js"]


def test_manifest_without_key_is_reported(tmp_path):
    stage = _load()
    src = _fake_ext(tmp_path, {"name": "x"})
    assert any("key" in p for p in stage.stage(tmp_path / "out", src=src, entries=["manifest.json"]))


def test_page_referencing_an_unstaged_script_is_reported(tmp_path):
    stage = _load()
    src = _fake_ext(tmp_path, {"key": "k"}, pages={"p.html": '<script src="a.js"></script><script src="b.js"></script>'},
                    extra=["a.js"])
    problems = stage.stage(tmp_path / "out", src=src, entries=["manifest.json", "p.html", "a.js"])
    assert problems == ["p.html references b.js, which isn't staged"]


def test_manifest_referencing_an_unstaged_file_is_reported(tmp_path):
    stage = _load()
    src = _fake_ext(tmp_path, {"key": "k", "background": {"service_worker": "bg.js"}, "options_page": "o.html"},
                    extra=["o.html"])
    problems = stage.stage(tmp_path / "out", src=src, entries=["manifest.json", "o.html"])
    assert problems == ["manifest.json references bg.js, which isn't staged"]
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_stage_extension.py -v`
Expected: FAIL — `FileNotFoundError` for `packaging/stage_extension.py`.

- [ ] **Step 5: Write the staging script**

Create `packaging/stage_extension.py`:

```python
"""Copies the shippable extension files (packaging/extension_files.txt) into a build folder.

    python packaging/stage_extension.py --dest build/friend/windows/extension

Exit 1 when a listed file is missing, when manifest.json has no "key" (a friend's
unpacked copy must keep the pinned extension ID the native host allows), or when
a staged HTML page or the manifest references a file that isn't staged.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
LIST = Path(__file__).resolve().parent / "extension_files.txt"
REF_RE = re.compile(r'<(?:script|link)\b[^>]*\b(?:src|href)="([^"]+)"', re.I)


def read_list(path: Path = LIST) -> list[str]:
    lines = (ln.strip() for ln in path.read_text(encoding="utf-8").splitlines())
    return [ln for ln in lines if ln and not ln.startswith("#")]


def _manifest_refs(manifest: dict) -> list[str]:
    refs = []
    if isinstance(manifest.get("background"), dict) and manifest["background"].get("service_worker"):
        refs.append(manifest["background"]["service_worker"])
    if isinstance(manifest.get("side_panel"), dict) and manifest["side_panel"].get("default_path"):
        refs.append(manifest["side_panel"]["default_path"])
    if manifest.get("options_page"):
        refs.append(manifest["options_page"])
    refs.extend((manifest.get("icons") or {}).values())
    action = manifest.get("action") or {}
    if isinstance(action.get("default_icon"), dict):
        refs.extend(action["default_icon"].values())
    return refs


def stage(dest: Path, src: Path = EXT, entries: list[str] | None = None) -> list[str]:
    """Copy ``entries`` from ``src`` into a fresh ``dest``. Returns problems (empty = ok)."""
    entries = read_list() if entries is None else entries
    problems: list[str] = []
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for entry in entries:
        name = entry.rstrip("/")
        source = Path(src) / name
        if not source.exists():
            problems.append(f"missing: {entry}")
            continue
        if source.is_dir():
            shutil.copytree(source, dest / name)
        else:
            shutil.copy2(source, dest / name)
    manifest_path = dest / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not manifest.get("key"):
            problems.append('manifest.json has no "key" (the pinned extension ID)')
        for ref in _manifest_refs(manifest):
            if not (dest / ref).exists():
                problems.append(f"manifest.json references {ref}, which isn't staged")
    for page in sorted(dest.glob("*.html")):
        for ref in REF_RE.findall(page.read_text(encoding="utf-8")):
            if "://" not in ref and not (dest / ref).exists():
                problems.append(f"{page.name} references {ref}, which isn't staged")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dest", required=True, type=Path)
    args = ap.parse_args(argv)
    problems = stage(args.dest)
    for p in problems:
        print(f"stage_extension: {p}", file=sys.stderr)
    if not problems:
        print(f"staged extension -> {args.dest}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `"$PY" -m pytest tests/test_stage_extension.py -v`
Expected: all PASS (5 tests).

- [ ] **Step 7: Ignore build-only folders**

Append to `.gitignore`:

```text

# Friend build (packaging/): build venvs and a local jsdom install for the self-test
packaging/.venv-*/
packaging/.jsdom/
```

(`build/` and `dist/` are already ignored.)

- [ ] **Step 8: Commit**

```bash
git add packaging/requirements-friend.txt packaging/extension_files.txt packaging/stage_extension.py tests/test_stage_extension.py .gitignore
git -c user.name="Adwait" -c user.email="adwait1234@gmail.com" commit -m "build(friend): dependency list, extension allow-list and staging check

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 12: PyInstaller spec

**Files:**
- Create: `packaging/applypilot_copilot.spec`

- [ ] **Step 1: Write the spec**

Create `packaging/applypilot_copilot.spec`:

```python
# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the friend build: ONE executable that is the Chrome
# native-messaging host, the local service and the host installer
# (see src/applypilot/extension/frozen_entry.py).
#
# Build through packaging/build_windows.ps1 or packaging/build_macos.sh, which
# pass --distpath/--workpath. Console app on purpose: the native host talks to
# Chrome over stdin/stdout.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")

# uvicorn loads its protocol/loop implementations by name at runtime.
hiddenimports = collect_submodules("uvicorn") + collect_submodules("applypilot.extension")

# python-docx ships its default document template as package data.
datas = collect_data_files("docx")

# Heavy packages other parts of applypilot use that the friend build never needs.
# (Tailored-résumé PDF rendering needs playwright; server.py answers 501 when frozen.)
excludes = ["playwright", "pandas", "numpy", "torch", "laya", "mcp", "typer", "bs4",
            "tkinter", "cryptography", "matplotlib", "IPython"]

a = Analysis(
    [os.path.join(SRC, "applypilot", "extension", "frozen_entry.py")],
    pathex=[SRC],
    hiddenimports=hiddenimports,
    datas=datas,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ApplyPilotCopilot",
    console=True,
    upx=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ApplyPilotCopilot")
```

- [ ] **Step 2: Commit** (it's exercised by the build in Task 14)

```bash
git add packaging/applypilot_copilot.spec
git -c user.name="Adwait" -c user.email="adwait1234@gmail.com" commit -m "build(friend): PyInstaller spec for the single-executable backend

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 13: Bundle verification script

Runs a built bundle for real, in throwaway data folders, with every AI-provider env var removed.

**Files:**
- Create: `scripts/verify_friend_bundle.py`

- [ ] **Step 1: Write the script**

Create `scripts/verify_friend_bundle.py`:

```python
"""Checks a built friend bundle before it goes to anyone.

    python scripts/verify_friend_bundle.py --bundle build/friend/windows
    python3 scripts/verify_friend_bundle.py --bundle build/friend/macos/ApplyPilotCopilot-mac

The bundle folder holds ApplyPilotCopilot/ (the PyInstaller app), extension/ and
SETUP.txt. The app is run for real, in throwaway data folders, with every
AI-provider env var removed, so it can never reach a real model or the
operator's data. Exit 0 = every check passed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

PINNED_ID = "noooclaijfiejnfgabkemnpabcbdnaac"
FORBIDDEN = {"profile.json", "answer_bank.json", ".env", "extension_token.txt", "applypilot.db",
             "extension_settings.json", "application_log.jsonl"}
RESUME_RE = re.compile(r"resume\.(pdf|txt|docx)", re.I)
DEV_ONLY = ("selftest.js", "test-page.html", "fixtures_scanned_fields.jsonl", "llm_bridge_selftest.js", "README.md")
SCRUB = ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_URL", "LLM_MODEL", "LLM_API_KEY",
         "LLM_PROVIDER", "APPLYPILOT_LLM_PROVIDER", "APPLYPILOT_DIR", "APPLYPILOT_ROOT", "APPLYPILOT_PROFILE",
         "APPLYPILOT_EXTENSION_PORT", "APPLYPILOT_LAYA", "APPLYPILOT_ANSWERS", "APPLYPILOT_DRAFTS")
# Providers that can only come from env vars/.env. Seeing one means something leaked in.
ENV_ONLY_PROVIDERS = {"gemini", "openai", "local", "remote-endpoint"}


def expected_version() -> str:
    text = (ROOT / "src" / "applypilot" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def exe_path(bundle: Path) -> Path:
    name = "ApplyPilotCopilot.exe" if os.name == "nt" else "ApplyPilotCopilot"
    return bundle / "ApplyPilotCopilot" / name


def clean_env(data_dir: Path, port: int | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    env["APPLYPILOT_DIR"] = str(data_dir)
    if port is not None:
        env["APPLYPILOT_EXTENSION_PORT"] = str(port)
    return env


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def frame(obj) -> bytes:
    data = json.dumps(obj).encode()
    return struct.pack("=I", len(data)) + data


def read_frame(stream):
    header = stream.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("=I", header)
    return json.loads(stream.read(length))


def http(method: str, url: str, token: str, body: bytes | None = None, content_type: str = "application/json"):
    headers = {"X-ApplyPilot-Token": token}
    if body is not None:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, method=method, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        return exc.code, None


def wait_health(port: int, token: str, timeout: float = 90) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, data = http("GET", f"http://127.0.0.1:{port}/health", token)
            if status == 200:
                return data
        except OSError:
            pass
        time.sleep(1)
    return None


def kill_bundle_processes(exe: Path) -> None:
    """Stop any copy of the bundle's app still running (the service the host starts)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/IM", exe.name], capture_output=True)
    else:
        subprocess.run(["pkill", "-f", str(exe)], capture_output=True)


def post_resume(port: int, token: str):
    boundary = "----applypilotverify"
    text = b"Jordan Testperson\njordan.testperson@example.com\n(206) 555-0147\nSeattle, WA\n"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"resume.txt\"\r\n"
            f"Content-Type: text/plain\r\n\r\n").encode() + text + f"\r\n--{boundary}--\r\n".encode()
    return http("POST", f"http://127.0.0.1:{port}/profile/import-resume", token, body,
                content_type=f"multipart/form-data; boundary={boundary}")


# ---------------------------------------------------------------------------
# checks: each returns a list of problems (empty = pass)
# ---------------------------------------------------------------------------

def check_files(bundle: Path) -> list[str]:
    from applypilot.extension.native_install import extension_id_from_key

    problems = []
    for path in bundle.rglob("*"):
        if path.is_file() and (path.name in FORBIDDEN or RESUME_RE.fullmatch(path.name)):
            problems.append(f"personal-data file in the bundle: {path.relative_to(bundle)}")
    ext = bundle / "extension"
    for name in DEV_ONLY:
        if (ext / name).exists():
            problems.append(f"dev-only file shipped: extension/{name}")
    manifest = ext / "manifest.json"
    if not manifest.exists():
        return problems + ["extension/manifest.json is missing"]
    key = json.loads(manifest.read_text(encoding="utf-8")).get("key", "")
    if not key or extension_id_from_key(key) != PINNED_ID:
        problems.append("the extension's ID isn't the pinned one")
    if not exe_path(bundle).exists():
        problems.append(f"the app is missing: {exe_path(bundle)}")
    if not (bundle / "SETUP.txt").exists():
        problems.append("SETUP.txt is missing")
    return problems


def check_version(exe: Path) -> list[str]:
    out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=120)
    got = out.stdout.strip()
    return [] if out.returncode == 0 and got == expected_version() else [f"--version printed {got!r}"]


def check_self(exe: Path) -> list[str]:
    out = subprocess.run([str(exe), "self-check"], capture_output=True, text=True, timeout=120)
    return [] if out.returncode == 0 and "self-check ok" in out.stdout else [f"self-check failed: {out.stderr.strip()}"]


def check_native_host_hello(exe: Path) -> list[str]:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        proc = subprocess.Popen([str(exe), f"chrome-extension://{PINNED_ID}/"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=clean_env(Path(d)), cwd=d)
        try:
            proc.stdin.write(frame({"cmd": "hello"}))
            proc.stdin.flush()
            reply = read_frame(proc.stdout)
            proc.stdin.close()
            proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
    return [] if reply == {"ok": True, "version": "1"} else [f"native host hello: {reply!r}"]


def check_service(exe: Path) -> list[str]:
    problems = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        port = free_port()
        proc = subprocess.Popen([str(exe), "serve-extension", "--port", str(port)], env=clean_env(Path(d)), cwd=d,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            token_file = Path(d) / "extension_token.txt"
            deadline = time.time() + 90
            while not token_file.exists() and time.time() < deadline:
                time.sleep(0.5)
            if not token_file.exists():
                return ["the service never wrote its token file"]
            token = token_file.read_text(encoding="utf-8").strip()
            health = wait_health(port, token)
            if not health or health.get("status") != "ok":
                return [f"/health: {health!r}"]
            if health.get("llm_provider") in ENV_ONLY_PROVIDERS:
                problems.append(f"an env-configured AI provider leaked in: {health.get('llm_provider')}")
            status, data = http("GET", f"http://127.0.0.1:{port}/llm/next?status=unavailable", token)
            if status != 200 or data != {"job": None}:
                problems.append(f"/llm/next: {status} {data!r}")
            status, _ = http("POST", f"http://127.0.0.1:{port}/resume/tailor", token,
                             json.dumps({"urls": [], "page_text": ""}).encode())
            if status != 501:
                problems.append(f"/resume/tailor should answer 501 in the friend build, got {status}")
            status, data = post_resume(port, token)
            if status != 200 or "profile" not in (data or {}):
                problems.append(f"résumé import: {status} {data!r}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
    return problems


def check_host_starts_service(exe: Path) -> list[str]:
    """The real auto-connect path: the host starts the service, exits, and the
    service keeps running (Chrome ends the host after every message)."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        port = free_port()
        proc = subprocess.Popen([str(exe), f"chrome-extension://{PINNED_ID}/"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=clean_env(Path(d), port=port), cwd=d)
        try:
            proc.stdin.write(frame({"cmd": "ensure_server"}))
            proc.stdin.flush()
            reply = read_frame(proc.stdout)
            proc.stdin.close()
            proc.wait(timeout=60)
            if not reply or not reply.get("ok") or reply.get("port") != port:
                return [f"ensure_server: {reply!r}"]
            if not wait_health(port, reply["token"], timeout=30):
                return ["the service the host started isn't answering after the host exited"]
            return []
        finally:
            if proc.poll() is None:
                proc.kill()
            kill_bundle_processes(exe)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bundle", required=True, type=Path)
    args = ap.parse_args(argv)
    bundle = args.bundle.resolve()
    exe = exe_path(bundle)
    checks = [
        ("files: no personal data, no dev files, pinned ID", lambda: check_files(bundle)),
        ("--version", lambda: check_version(exe)),
        ("self-check (every module imports)", lambda: check_self(exe)),
        ("native host answers hello", lambda: check_native_host_hello(exe)),
        ("service: health, bridge, tailor 501, résumé import", lambda: check_service(exe)),
        ("native host starts a service that outlives it", lambda: check_host_starts_service(exe)),
    ]
    failed = 0
    for name, run in checks:
        try:
            problems = run()
        except Exception as exc:  # noqa: BLE001 -- a crashing check is a failed check
            problems = [f"crashed: {exc!r}"]
        print(f"{'PASS' if not problems else 'FAIL'}  {name}")
        for p in problems:
            print(f"      {p}")
        failed += bool(problems)
    print(f"\n{len(checks) - failed}/{len(checks)} bundle checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Syntax-check it**

Run: `"$PY" -m py_compile scripts/verify_friend_bundle.py && echo OK`
Expected: `OK`. (It's run for real in Task 14.)

- [ ] **Step 3: Commit**

```bash
git add scripts/verify_friend_bundle.py
git -c user.name="Adwait" -c user.email="adwait1234@gmail.com" commit -m "build(friend): bundle verification that runs the app for real in throwaway dirs

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 14: Windows build (script + installer) — build and verify here

**Files:**
- Create: `packaging/build_windows.ps1`
- Create: `packaging/windows/installer.iss`

- [ ] **Step 1: Write the build script**

Create `packaging/build_windows.ps1`:

```powershell
# Builds the Windows friend installer.
#   build\friend\windows\              ApplyPilotCopilot\ (app), extension\, SETUP.txt
#   dist\ApplyPilotCopilot-Setup-<version>.exe   (only when Inno Setup 6 is installed)
#
# Usage (from the repo root):
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 -Python C:\path\to\python.exe
param([string]$Python = '')

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root

function Invoke-Checked([string]$Exe, [string[]]$ArgList) {
    & $Exe @ArgList
    if ($LASTEXITCODE -ne 0) { throw "failed ($LASTEXITCODE): $Exe $($ArgList -join ' ')" }
}

if (-not $Python) {
    $known = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    if (Test-Path $known) { $Python = $known } else { $Python = 'python' }
}

$Version = (Select-String -Path 'src\applypilot\__init__.py' -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Write-Host "ApplyPilot Copilot friend build $Version"

$Venv = Join-Path $Root 'packaging\.venv-win'
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $VenvPy)) { Invoke-Checked $Python @('-m', 'venv', $Venv) }
Invoke-Checked $VenvPy @('-m', 'pip', 'install', '--upgrade', 'pip')
Invoke-Checked $VenvPy @('-m', 'pip', 'install', '-r', 'packaging\requirements-friend.txt')

$Out = Join-Path $Root 'build\friend\windows'
if (Test-Path $Out) { Remove-Item -Recurse -Force $Out }
Invoke-Checked $VenvPy @('-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', $Out,
    '--workpath', (Join-Path $Root 'build\pyinstaller-win'), 'packaging\applypilot_copilot.spec')
Invoke-Checked $VenvPy @('packaging\stage_extension.py', '--dest', (Join-Path $Out 'extension'))
Copy-Item 'docs\FRIEND_SETUP.md' (Join-Path $Out 'SETUP.txt')

Invoke-Checked $VenvPy @('scripts\verify_friend_bundle.py', '--bundle', $Out)

$Iscc = $null
foreach ($candidate in @(
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'))) {
    if ($candidate -and (Test-Path $candidate)) { $Iscc = $candidate; break }
}
if (-not $Iscc) {
    Write-Warning "Inno Setup 6.3+ not found: the app is built and verified in $Out, but no installer .exe was made. Install Inno Setup 6.3 or newer (https://jrsoftware.org/isdl.php) or build through the GitHub workflow."
    exit 0
}
$env:APC_VERSION = $Version
New-Item -ItemType Directory -Force (Join-Path $Root 'dist') | Out-Null
Invoke-Checked $Iscc @('packaging\windows\installer.iss')
Write-Host "Built dist\ApplyPilotCopilot-Setup-$Version.exe"
```

- [ ] **Step 2: Write the installer script**

Create `packaging/windows/installer.iss`:

```ini
; Inno Setup 6 script for the ApplyPilot Copilot friend installer.
; Built by packaging\build_windows.ps1 (which sets APC_VERSION); paths are
; relative to this file. Per-user install: no admin rights needed.

#define AppName "ApplyPilot Copilot"
#define AppVersion GetEnv("APC_VERSION")

[Setup]
AppId={{6E1C3B9A-4F2D-4C8E-9B7A-3D5F1A2C8E40}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=ApplyPilot
DefaultDirName={localappdata}\Programs\ApplyPilotCopilot
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=ApplyPilotCopilot-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
; The service may be running from these files during an upgrade or uninstall.
CloseApplications=force
RestartApplications=no

[Files]
Source: "..\..\build\friend\windows\ApplyPilotCopilot\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\..\build\friend\windows\extension\*"; DestDir: "{app}\extension"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\..\build\friend\windows\SETUP.txt"; DestDir: "{app}"; Flags: ignoreversion

[Run]
Filename: "{app}\ApplyPilotCopilot.exe"; Parameters: "install-host --extension-dir ""{app}\extension"""; Flags: runhidden waituntilterminated; StatusMsg: "Connecting ApplyPilot Copilot to Chrome..."
Filename: "{app}\SETUP.txt"; Description: "Show the next steps (Load the extension in Chrome)"; Flags: postinstall shellexec skipifsilent
Filename: "{app}\extension"; Description: "Open the extension folder"; Flags: postinstall shellexec skipifsilent

[UninstallRun]
Filename: "{app}\ApplyPilotCopilot.exe"; Parameters: "uninstall-host"; Flags: runhidden waituntilterminated; RunOnceId: "UninstallHost"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM ApplyPilotCopilot.exe"; Flags: runhidden waituntilterminated; RunOnceId: "StopService"
```

- [ ] **Step 3: Write a placeholder guide so the build can run** (Task 17 writes the real one)

If `docs/FRIEND_SETUP.md` doesn't exist yet, create it with one line:

```markdown
ApplyPilot Copilot setup guide (full text added in Task 17).
```

- [ ] **Step 4: Build and verify**

This builds the app into `build\friend\windows` and runs `scripts/verify_friend_bundle.py` against it. It does NOT install anything or touch the registry. Check memory first; PyInstaller and the checks need about 2 GB free.

Run (PowerShell tool):
```powershell
$os = Get-CimInstance Win32_OperatingSystem; '{0:N1} GB free' -f ($os.FreePhysicalMemory/1MB)
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```
Expected (last lines):
```
PASS  files: no personal data, no dev files, pinned ID
PASS  --version
PASS  self-check (every module imports)
PASS  native host answers hello
PASS  service: health, bridge, tailor 501, résumé import
PASS  native host starts a service that outlives it

6/6 bundle checks passed.
WARNING: Inno Setup 6.3+ not found: ...
```
(If Inno Setup 6.3+ is installed, the last line is instead `Built dist\ApplyPilotCopilot-Setup-0.3.0.exe`.)

If `self-check` fails with a missing module, add that module name to `hiddenimports` in `packaging/applypilot_copilot.spec` (or add the package to `packaging/requirements-friend.txt`) and re-run. If the service check fails, read `<temp>/logs/extension-service.log`; the verify script prints the failing check.

- [ ] **Step 5: Make sure nothing personal or built got staged in git**

Run: `git status --short`
Expected: only `packaging/build_windows.ps1`, `packaging/windows/installer.iss`, and possibly `docs/FRIEND_SETUP.md`. `build/`, `dist/` and `packaging/.venv-win/` must NOT appear (they're ignored).

- [ ] **Step 6: Commit**

```bash
git add packaging/build_windows.ps1 packaging/windows/installer.iss docs/FRIEND_SETUP.md
git -c user.name="Adwait" -c user.email="adwait1234@gmail.com" commit -m "build(friend): Windows build script and per-user Inno Setup installer

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 15: macOS build + install scripts

These can't run on this Windows PC. They're built and verified by the GitHub workflow (Task 16).

**Files:**
- Create: `packaging/build_macos.sh`
- Create: `packaging/macos/Install ApplyPilot Copilot.command`
- Create: `packaging/macos/Uninstall ApplyPilot Copilot.command`

- [ ] **Step 1: Write the build script**

Create `packaging/build_macos.sh`:

```bash
#!/usr/bin/env bash
# Builds the macOS (Apple Silicon) friend bundle.
#   build/friend/macos/ApplyPilotCopilot-mac/   ApplyPilotCopilot/ (app), extension/, SETUP.txt, *.command
#   dist/ApplyPilotCopilot-macOS-arm64-<version>.zip
# Usage (on an arm64 Mac, from the repo root):  PYTHON=python3 bash packaging/build_macos.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

[ "$(uname -s)" = "Darwin" ] || { echo "build_macos.sh must run on macOS" >&2; exit 1; }
[ "$(uname -m)" = "arm64" ] || { echo "this build targets Apple Silicon; run it on an arm64 Mac or runner" >&2; exit 1; }

PYTHON="${PYTHON:-python3}"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/applypilot/__init__.py)"
echo "ApplyPilot Copilot friend build $VERSION (macOS arm64)"

VENV="$ROOT/packaging/.venv-mac"
[ -x "$VENV/bin/python" ] || "$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -r packaging/requirements-friend.txt

OUT="$ROOT/build/friend/macos/ApplyPilotCopilot-mac"
rm -rf "$ROOT/build/friend/macos"
mkdir -p "$OUT"
"$VENV/bin/python" -m PyInstaller --noconfirm --clean --distpath "$OUT" \
  --workpath "$ROOT/build/pyinstaller-mac" packaging/applypilot_copilot.spec
"$VENV/bin/python" packaging/stage_extension.py --dest "$OUT/extension"
cp docs/FRIEND_SETUP.md "$OUT/SETUP.txt"
cp "packaging/macos/Install ApplyPilot Copilot.command" "packaging/macos/Uninstall ApplyPilot Copilot.command" "$OUT/"
chmod +x "$OUT"/*.command

# PyInstaller ad-hoc signs the app; an unsigned arm64 binary won't run at all.
codesign --verify --verbose "$OUT/ApplyPilotCopilot/ApplyPilotCopilot"

"$VENV/bin/python" scripts/verify_friend_bundle.py --bundle "$OUT"

mkdir -p "$ROOT/dist"
ZIP="$ROOT/dist/ApplyPilotCopilot-macOS-arm64-$VERSION.zip"
rm -f "$ZIP"
ditto -c -k --keepParent "$OUT" "$ZIP"
echo "Built $ZIP"
```

- [ ] **Step 2: Write the friend's Mac installer**

Create `packaging/macos/Install ApplyPilot Copilot.command` (the file name contains spaces; use the Write tool with the exact path):

```bash
#!/bin/bash
# Installs ApplyPilot Copilot for this Mac user.
# First time: right-click this file -> Open -> Open (macOS asks because it isn't from the App Store).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Applications/ApplyPilot Copilot"

echo "Installing ApplyPilot Copilot to: $DEST"
pkill -f "$DEST/app/ApplyPilotCopilot" 2>/dev/null || true   # stop an older copy that's running
rm -rf "$DEST/app" "$DEST/extension"
mkdir -p "$DEST"
ditto "$HERE/ApplyPilotCopilot" "$DEST/app"
ditto "$HERE/extension" "$DEST/extension"
cp "$HERE/SETUP.txt" "$DEST/SETUP.txt"
cp "$HERE/Uninstall ApplyPilot Copilot.command" "$DEST/"
chmod +x "$DEST/Uninstall ApplyPilot Copilot.command"

# Downloaded files carry a quarantine flag. Chrome starts the helper directly and
# macOS would silently refuse to run it, so clear the flag on these files only.
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true

"$DEST/app/ApplyPilotCopilot" install-host --extension-dir "$DEST/extension"

echo
echo "Done. Next, in Chrome:"
echo "  1. Open chrome://extensions and turn on Developer mode (top right)."
echo "  2. Click 'Load unpacked' and choose this folder:"
echo "       $DEST/extension"
echo "     (in the file picker: Home > Applications > ApplyPilot Copilot > extension)"
echo "The setup guide is opening now."
open "$DEST"
open -e "$DEST/SETUP.txt" || true
echo
read -n 1 -s -r -p "Press any key to close this window."
```

- [ ] **Step 3: Write the friend's Mac uninstaller**

Create `packaging/macos/Uninstall ApplyPilot Copilot.command`:

```bash
#!/bin/bash
# Removes ApplyPilot Copilot for this Mac user. Your profile and answers in ~/.applypilot are kept.
set -euo pipefail
DEST="$HOME/Applications/ApplyPilot Copilot"

if [ -x "$DEST/app/ApplyPilotCopilot" ]; then
  "$DEST/app/ApplyPilotCopilot" uninstall-host || true
fi
pkill -f "$DEST/app/ApplyPilotCopilot" 2>/dev/null || true
rm -rf "$DEST"

echo "ApplyPilot Copilot is removed. Also remove the extension in chrome://extensions."
echo "Your data (profile, answers) is still in ~/.applypilot. Delete that folder to remove it too."
read -n 1 -s -r -p "Press any key to close this window."
```

- [ ] **Step 4: Mark the scripts executable in git** (Windows can't set the bit on disk)

Run:
```bash
git add packaging/build_macos.sh "packaging/macos/Install ApplyPilot Copilot.command" "packaging/macos/Uninstall ApplyPilot Copilot.command"
git update-index --chmod=+x packaging/build_macos.sh "packaging/macos/Install ApplyPilot Copilot.command" "packaging/macos/Uninstall ApplyPilot Copilot.command"
git ls-files -s packaging/build_macos.sh packaging/macos/
```
Expected: each line starts with `100755`.

Also make sure the scripts have LF line endings (a CRLF shebang breaks on macOS):

Run: `file packaging/build_macos.sh "packaging/macos/Install ApplyPilot Copilot.command" "packaging/macos/Uninstall ApplyPilot Copilot.command"`
Expected: no `CRLF` in the output. If you see `CRLF`, convert with `sed -i 's/\r$//' <file>` and `git add` again.

- [ ] **Step 5: Keep them LF forever**

Append to `.gitattributes` (create the file if it doesn't exist):

```text
*.sh text eol=lf
*.command text eol=lf
```

- [ ] **Step 6: Commit**

```bash
git add .gitattributes
git -c user.name="Adwait" -c user.email="adwait1234@gmail.com" commit -m "build(friend): macOS build script and double-click install/uninstall scripts

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 16: GitHub Actions workflow

**Files:**
- Create: `.github/workflows/friend-build.yml`

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/friend-build.yml`:

```yaml
# Builds the friend installers. Manual only: Actions tab -> friend-build -> Run workflow.
# The operator runs this in their OWN GitHub repo (see packaging/README.md).
name: friend-build

on:
  workflow_dispatch:

jobs:
  windows:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install Inno Setup
        run: choco install innosetup --no-progress -y
      - name: Build and verify
        shell: pwsh
        run: ./packaging/build_windows.ps1 -Python python
      - uses: actions/upload-artifact@v4
        with:
          name: ApplyPilotCopilot-Windows
          path: dist/ApplyPilotCopilot-Setup-*.exe
          if-no-files-found: error

  macos:
    runs-on: macos-14   # Apple Silicon (arm64)
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Build and verify
        run: PYTHON=python bash packaging/build_macos.sh
      - uses: actions/upload-artifact@v4
        with:
          name: ApplyPilotCopilot-macOS-arm64
          path: dist/ApplyPilotCopilot-macOS-arm64-*.zip
          if-no-files-found: error
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/friend-build.yml
git -c user.name="Adwait" -c user.email="adwait1234@gmail.com" commit -m "ci(friend): manual workflow that builds and verifies both installers

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Phase D — Docs

### Task 17: Friend guide + operator guide

**Files:**
- Create or replace: `docs/FRIEND_SETUP.md` (shipped as `SETUP.txt`, so keep it readable as plain text: no tables, no HTML)
- Create: `packaging/README.md`

- [ ] **Step 1: Write the friend guide**

Write `docs/FRIEND_SETUP.md` with exactly this content:

```markdown
ApplyPilot Copilot - setup
==========================

ApplyPilot Copilot fills job applications in Chrome for you. You review every
answer and press Submit yourself; it never submits anything.
Everything stays on your computer.

You need: Google Chrome (version 138 or newer).


1. Run the installer
--------------------

Windows: double-click ApplyPilotCopilot-Setup.exe.
  Windows may say "Windows protected your PC". Click "More info", then
  "Run anyway". (It says this because the app isn't signed by a big company.)

Mac: unzip the download, then right-click "Install ApplyPilot Copilot.command"
  and choose Open, then Open again. (macOS asks because it isn't from the App
  Store.) A Terminal window shows the progress; close it when it says Done.


2. Add the extension to Chrome (one time)
-----------------------------------------

1. In Chrome, go to:  chrome://extensions
2. Turn on "Developer mode" (switch at the top right).
3. Click "Load unpacked" and choose the extension folder:
   Windows: in the folder box, paste  %LOCALAPPDATA%\Programs\ApplyPilotCopilot\extension
            and press Enter, then click "Select Folder".
   Mac:     Home > Applications > ApplyPilot Copilot > extension, then "Select".
4. Click the puzzle-piece icon in Chrome's toolbar and pin ApplyPilot Copilot.

Chrome may show a banner about developer-mode extensions. That's expected;
you can dismiss it.


3. Set up your profile
----------------------

1. Right-click the ApplyPilot icon > Options.
2. Under "Import your résumé", choose your résumé (PDF, Word or text file).
3. Check what it found, fill anything missing, and save.


4. Optional: turn on on-device AI
---------------------------------

AI drafts answers to open-ended questions and reads your work history from
your résumé. It runs on your own computer using Chrome's built-in AI, so
nothing is sent anywhere.

In Options > Smart fill, click "Download on-device model" (a one-time download
of a few GB). If it says "not available here", your computer doesn't meet
Chrome's requirements (about 22 GB of free disk space, and a graphics card
with more than 4 GB of memory or 16 GB of RAM). Everything else still works.

Keep the ApplyPilot side panel open while it fills, so the AI can answer.


5. Use it
---------

1. Open a job application page.
2. Click the ApplyPilot icon: the side panel opens.
3. Click "Fill this page". The first time on each website, Chrome asks for
   permission: click Allow.
4. Review every field (green = filled, amber = left for you), then submit it
   yourself.


Not in this version
-------------------

- Tailored résumés (the Tailor button says it isn't available).


Removing it
-----------

Windows: Settings > Apps > ApplyPilot Copilot > Uninstall.
Mac: open Home > Applications > ApplyPilot Copilot and double-click
  "Uninstall ApplyPilot Copilot.command" (it's also in the download folder).
Then remove the extension in chrome://extensions.
Your profile and saved answers stay in the ".applypilot" folder in your home
folder; delete that folder to remove them too.
```

- [ ] **Step 2: Write the operator guide**

Create `packaging/README.md`:

```markdown
# Friend build: how to build and share ApplyPilot Copilot

The friend build packs the backend (native host + local service) into one executable per OS, so a friend needs no Python, terminal or API key. Design: `docs/superpowers/specs/2026-09-27-friend-installer-design.md`.

## What your friend gets

- Windows: `ApplyPilotCopilot-Setup-<version>.exe` (per-user install, no admin).
- Mac (Apple Silicon only): `ApplyPilotCopilot-macOS-arm64-<version>.zip`.
- The guide `SETUP.txt` (source: `docs/FRIEND_SETUP.md`) is inside both.

## Build Windows on this PC

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

This builds `build\friend\windows\`, runs `scripts/verify_friend_bundle.py` (6 checks, all in throwaway folders), then makes `dist\ApplyPilotCopilot-Setup-<version>.exe` if Inno Setup 6.3 or newer is installed (https://jrsoftware.org/isdl.php). Without it, use the GitHub workflow below.

**Never run the installer on this PC.** It re-registers the `com.applypilot.copilot` native host to the frozen app, replacing your dev launcher. If it happens, uninstall it (Settings > Apps) and restore yours with `python -m applypilot extension install-host`.

## Build both installers on GitHub (needed for the Mac one)

1. Create a new **private** repository on github.com under your account (for example `applypilot-friend`). Leave it empty (no README).
2. Add it as a second remote and push this branch as its first branch (so it becomes the default branch, which the Run workflow button needs):
   ```bash
   git remote add mine https://github.com/<your-user>/applypilot-friend.git
   git push mine feat/chrome-extension
   ```
   Checked 2026-09-27: `.env` is ignored and no API keys appear anywhere in the history.
3. On GitHub: Actions tab > **friend-build** > Run workflow.
4. When both jobs are green, download the two artifacts from the run's summary page.

Private repos get 2,000 free Actions minutes a month; macOS minutes count 10x, and one build uses roughly 10 macOS minutes (about 100 of your minutes).

## Sending it

Send the installer for their OS (email, Drive, etc.). Both OSes warn once because the builds aren't code-signed; `SETUP.txt` tells them what to click.

## What doesn't work in the friend build

- Tailored-résumé PDFs (the service answers 501; needs Playwright + Chromium).
- AI features on computers that don't meet Chrome's on-device model requirements (Chrome 138+, ~22 GB free disk, >4 GB VRAM or 16 GB RAM with 4+ cores). Autofill works regardless.
- Intel Macs.
```

- [ ] **Step 3: Rebuild the Windows bundle so SETUP.txt has the real guide**

Run (PowerShell tool): `powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1`
Expected: `6/6 bundle checks passed.`

- [ ] **Step 4: Commit**

```bash
git add docs/FRIEND_SETUP.md packaging/README.md
git -c user.name="Adwait" -c user.email="adwait1234@gmail.com" commit -m "docs(friend): setup guide for friends and a build/share guide for the operator

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Phase E — Final verification and handoff

### Task 18: Full regression

- [ ] **Step 1: Full pytest**

Run: `PYTHONIOENCODING=utf-8 "$PY" -m pytest -q -p no:cacheprovider`
Expected: `1826 passed, 2 skipped` (the 1774 from before this plan + 52 new tests: Task 1 adds 15, Task 2 adds 4, Task 3 adds 5, Task 4 adds 2, Task 5 adds 9, Task 6 adds 6, Task 7 adds 6, Task 11 adds 5). If the baseline had already moved before you started, expect baseline + 52, and 0 failed either way.

- [ ] **Step 2: jsdom self-test**

jsdom must be on `NODE_PATH`. If `packaging/.jsdom/node_modules/jsdom` doesn't exist, install it there once:

```bash
mkdir -p packaging/.jsdom && (cd packaging/.jsdom && npm init -y >/dev/null && npm install jsdom >/dev/null)
```

Run: `NODE_PATH="packaging/.jsdom/node_modules" node extension/selftest.js | tail -1`
Expected: `564/564 checks passed.`

- [ ] **Step 3: Bridge self-test**

Run: `node extension/llm_bridge_selftest.js | tail -1`
Expected: `23/23 checks passed.`

- [ ] **Step 4: Chrome suites, one at a time**

Check free memory first (need about 2 GB):
`powershell.exe -NoProfile -Command "\$os = Get-CimInstance Win32_OperatingSystem; '{0:N1} GB free' -f (\$os.FreePhysicalMemory/1MB)"`

Run each and wait for it to finish before the next:
```bash
PYTHONIOENCODING=utf-8 "$PY" scripts/chrome_widgets_test.py | tail -1
PYTHONIOENCODING=utf-8 "$PY" scripts/chrome_load_test.py | tail -1
PYTHONIOENCODING=utf-8 "$PY" scripts/chrome_panel_test.py | tail -1
PYTHONIOENCODING=utf-8 "$PY" scripts/e2e_options_manage.py | tail -1
PYTHONIOENCODING=utf-8 "$PY" scripts/e2e_options_resume.py | tail -1
```
Expected: `ALL CHROME WIDGET CHECKS PASSED`, `ALL CHROME CHECKS PASSED`, `ALL PANEL CHECKS PASSED`, and passing output from both e2e scripts. The side panel and options page now load `llm_bridge.js`; it stays inert in test browsers without `LanguageModel`, and any poll to a stub gets a 404 that it ignores.

- [ ] **Step 5: Windows bundle one last time**

Run (PowerShell tool): `powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1`
Expected: `6/6 bundle checks passed.`

- [ ] **Step 6: Confirm nothing personal was committed**

Run:
```bash
git log --oneline feat/chrome-extension -25
git ls-files | grep -iE "(^|/)(profile\.json|answer_bank\.json|\.env|extension_token\.txt|resume\.(pdf|txt|docx))$" || echo "no personal files tracked"
```
Expected: the task commits are listed and `no personal files tracked`.

- [ ] **Step 7: Hand off to the operator**

Tell the operator:
- What was built and that every suite passed (quote the numbers).
- The Windows installer (if Inno Setup was present) is at `dist\ApplyPilotCopilot-Setup-<version>.exe`; otherwise use the workflow.
- For the Mac installer: follow `packaging/README.md` → "Build both installers on GitHub". Claude never pushes; the operator does.
- Not verified on this machine: Chrome's on-device model (this PC doesn't meet its requirements) and the Mac installer by hand (no Mac). The friend's first run is the real test; ask them for a screenshot of Options → Smart fill.
- Never run the installer on the dev PC (see `packaging/README.md`).
```
