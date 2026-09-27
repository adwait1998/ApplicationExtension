"""Install / uninstall the Chrome native-messaging host (no admin needed).

What ``applypilot extension install-host`` does:
1. pins the unpacked extension's ID by giving extension/manifest.json a
   "key" (a public key; Chrome derives the ID from it, so the ID no longer
   depends on the folder the extension was loaded from) — only if missing;
2. writes <app_dir>/native_host/applypilot_host.bat, a launcher that runs
   ``python -m applypilot.extension.native_host`` with this install's data
   directory, and com.applypilot.copilot.json, the host manifest naming the
   launcher and allowing ONLY that extension ID;
3. registers the manifest for Google Chrome (only) under
   HKCU\\Software\\<browser>\\NativeMessagingHosts\\com.applypilot.copilot.

Everything is per-user and reversible with ``uninstall-host``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Callable

from applypilot.extension.native_host import HOST_NAME

# Google Chrome only. Registering for Chromium too let the test suite's own
# (Playwright) Chromium find the operator's real host and talk to the real,
# live service; nobody runs the extension in plain Chromium day to day.
REGISTRY_PATHS = (
    r"Software\Google\Chrome\NativeMessagingHosts",
)
# Registered by the first version of this installer; removed on (re)install
# and uninstall.
LEGACY_REGISTRY_PATHS = (
    r"Software\Chromium\NativeMessagingHosts",
)


def extension_id_from_key(b64_key: str) -> str:
    """Chrome's ID for an extension with this manifest "key": the first 128
    bits of SHA-256 of the DER public key, hex digits 0-f mapped to a-p."""
    digest = hashlib.sha256(base64.b64decode(b64_key)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def _new_public_key_b64() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = key.public_key().public_bytes(serialization.Encoding.DER,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(der).decode("ascii")


def ensure_manifest_key(manifest_path: Path, keygen: Callable[[], str] = _new_public_key_b64) -> str:
    """The extension ID, adding a "key" to manifest.json first if it has none."""
    manifest_path = Path(manifest_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not data.get("key"):
        data["key"] = keygen()
        manifest_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return extension_id_from_key(data["key"])


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


def write_host_files(host_dir: Path, ext_id: str, python_exe: str, env: dict[str, str]) -> Path:
    """Write the launcher and the host manifest; return the manifest path."""
    host_dir = Path(host_dir)
    host_dir.mkdir(parents=True, exist_ok=True)
    launcher = host_dir / "applypilot_host.bat"
    lines = ["@echo off"]
    for k in ("APPLYPILOT_DIR", "APPLYPILOT_ROOT", "APPLYPILOT_PROFILE", "APPLYPILOT_EXTENSION_PORT"):
        if env.get(k):
            lines.append(f'set "{k}={env[k]}"')
    lines.append(f'"{python_exe}" -m applypilot.extension.native_host %*')
    launcher.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    manifest = host_dir / f"{HOST_NAME}.json"
    manifest.write_text(json.dumps(host_manifest(ext_id, launcher), indent=2), encoding="utf-8")
    return manifest


def _winreg_set(subkey: str, value: str) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, subkey) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, value)


def _winreg_delete(subkey: str) -> None:
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)
    except FileNotFoundError:
        pass


def install(extension_dir: Path, app_dir: Path, *, python_exe: str | None = None,
            env: dict[str, str] | None = None,
            reg_set: Callable[[str, str], None] = _winreg_set,
            reg_delete: Callable[[str], None] | None = None,
            keygen: Callable[[], str] = _new_public_key_b64) -> dict:
    ext_id = ensure_manifest_key(Path(extension_dir) / "manifest.json", keygen=keygen)
    env = dict(os.environ if env is None else env)
    env.setdefault("APPLYPILOT_DIR", str(app_dir))
    manifest = write_host_files(Path(app_dir) / "native_host", ext_id, python_exe or sys.executable, env)
    for base in REGISTRY_PATHS:
        reg_set(base + "\\" + HOST_NAME, str(manifest))
    for base in LEGACY_REGISTRY_PATHS:
        (reg_delete or _winreg_delete)(base + "\\" + HOST_NAME)
    return {"extension_id": ext_id, "manifest": str(manifest)}


def uninstall(app_dir: Path, *, reg_delete: Callable[[str], None] = _winreg_delete) -> None:
    for base in REGISTRY_PATHS + LEGACY_REGISTRY_PATHS:
        reg_delete(base + "\\" + HOST_NAME)
    host_dir = Path(app_dir) / "native_host"
    for name in ("applypilot_host.bat", f"{HOST_NAME}.json"):
        try:
            (host_dir / name).unlink()
        except FileNotFoundError:
            pass


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
