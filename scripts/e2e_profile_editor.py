"""End-to-end for the extension's profile editor, against a real socket.

The subtle bug this exists to catch: GET /profile/full strips
personal.password, so a naive load-edit-save round-trip from the options page
would silently WIPE the operator's stored password. The write path has to
merge back secrets it deliberately never showed the client.

Also proves the loop that makes the feature useful at all — work history saved
from the editor is usable by the structured tier on the very next fill, with
no service restart.

Run:  python scripts/e2e_profile_editor.py
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable
PORT = int(os.environ.get("APC_E2E_PORT", "8802"))
SECRET = "PASSWORD-MUST-SURVIVE-THE-ROUNDTRIP"

tmp = pathlib.Path(tempfile.mkdtemp(prefix="apc-prof-"))
(tmp / "profile.json").write_text(json.dumps({
    "personal": {"full_name": "Alex Rivera", "email": "a@b.test", "password": SECRET},
    "work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": False},
    "eeo_voluntary": {"gender": "Prefer not to say"},
    "resume_facts": {"preserved_companies": ["Acme Corp"]},
}), encoding="utf-8")

env = dict(os.environ)
env["APPLYPILOT_DIR"] = str(tmp)
env["APPLYPILOT_ROOT"] = str(tmp)
env.pop("APPLYPILOT_PROFILE", None)
proc = subprocess.Popen([PY, "-m", "applypilot", "serve-extension", "--port", str(PORT)],
                        cwd=str(REPO), env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
base = f"http://127.0.0.1:{PORT}"
fails = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def call(path, data=None, tok=None):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", "X-ApplyPilot-Token": tok or ""})
    return json.load(urllib.request.urlopen(req, timeout=15))


try:
    tokf = tmp / "extension_token.txt"
    for _ in range(60):
        if tokf.exists():
            try:
                call("/health", tok=tokf.read_text(encoding="utf-8").strip())
                break
            except urllib.error.HTTPError:
                break
            except Exception:
                pass
        time.sleep(1)
    else:
        raise SystemExit("service never came up")
    tok = tokf.read_text(encoding="utf-8").strip()

    prof = call("/profile/full", tok=tok)
    blob = json.dumps(prof)
    check("GET /profile/full returns values", "Alex Rivera" in blob)
    check("password value NOT exposed", SECRET not in blob)
    check("password key absent entirely", "password" not in blob)

    prof["personal"]["full_name"] = "Alex R. Rivera"
    prof["work_history"] = [{"title": "Staff Designer", "company": "Acme Corp",
                             "location": "Seattle, WA", "start": "03/2022",
                             "end": "", "current": True, "description": "d"}]
    call("/profile", data=prof, tok=tok)

    d = json.loads((tmp / "profile.json").read_text(encoding="utf-8"))
    check("edit saved", d["personal"]["full_name"] == "Alex R. Rivera")
    check("PASSWORD SURVIVED the round-trip", d["personal"].get("password") == SECRET)
    check("work_history persisted", d.get("work_history", [{}])[0].get("company") == "Acme Corp")
    check("sections the editor never rendered are preserved",
          d.get("eeo_voluntary", {}).get("gender") == "Prefer not to say"
          and d.get("resume_facts", {}).get("preserved_companies") == ["Acme Corp"])
    check("backup written before overwrite", (tmp / "profile.json.bak").exists())

    fill = call("/resolve", data={"url": "x", "fields": [
        {"id": "a", "selector": "#a", "tag": "input", "type": "text", "name": "",
         "autocomplete": "", "label": "Job Title", "placeholder": "", "required": False,
         "options": [], "section": "Work Experience 1", "section_index": 1}]}, tok=tok)
    check("work history saved via the editor is IMMEDIATELY usable (no restart)",
          (fill["fills"] or [{}])[0].get("value") == "Staff Designer",
          json.dumps(fill["fills"])[:70])

    print(f"\n{len(fails)} failure(s): {fails}" if fails else "\nALL PROFILE-EDITOR CHECKS PASSED")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()

sys.exit(1 if fails else 0)
