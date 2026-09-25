"""Install / uninstall the Chrome native-messaging host (no admin needed).

What ``applypilot extension install-host`` does:
1. pins the unpacked extension's ID by giving extension/manifest.json a
   "key" (a public key; Chrome derives the ID from it, so the ID no longer
   depends on the folder the extension was loaded from) — only if missing;
2. writes <app_dir>/native_host/applypilot_host.bat, a launcher that runs
   ``python -m applypilot.extension.native_host`` with this install's data
   directory, and com.applypilot.copilot.json, the host manifest naming the
   launcher and allowing ONLY that extension ID;
3. registers the manifest for Chrome (and Chromium) under
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

REGISTRY_PATHS = (
    r"Software\Google\Chrome\NativeMessagingHosts",
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
    manifest.write_text(json.dumps({
        "name": HOST_NAME,
        "description": "ApplyPilot Copilot local service launcher",
        "path": str(launcher),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ext_id}/"],
    }, indent=2), encoding="utf-8")
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
            keygen: Callable[[], str] = _new_public_key_b64) -> dict:
    ext_id = ensure_manifest_key(Path(extension_dir) / "manifest.json", keygen=keygen)
    env = dict(os.environ if env is None else env)
    env.setdefault("APPLYPILOT_DIR", str(app_dir))
    manifest = write_host_files(Path(app_dir) / "native_host", ext_id, python_exe or sys.executable, env)
    for base in REGISTRY_PATHS:
        reg_set(base + "\\" + HOST_NAME, str(manifest))
    return {"extension_id": ext_id, "manifest": str(manifest)}


def uninstall(app_dir: Path, *, reg_delete: Callable[[str], None] = _winreg_delete) -> None:
    for base in REGISTRY_PATHS:
        reg_delete(base + "\\" + HOST_NAME)
    host_dir = Path(app_dir) / "native_host"
    for name in ("applypilot_host.bat", f"{HOST_NAME}.json"):
        try:
            (host_dir / name).unlink()
        except FileNotFoundError:
            pass
