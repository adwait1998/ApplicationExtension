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
