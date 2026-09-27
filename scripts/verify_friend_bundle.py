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
