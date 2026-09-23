"""End-to-end: real uvicorn socket + the Chrome extension's ACTUAL scanner output.

Uses a synthetic profile in a temp APPLYPILOT_DIR so no real personal data is
touched or printed. Asserts the resolution ladder behaves correctly on a
realistic ATS-shaped form.
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
# Real output of the extension's scanner over extension/test-page.html,
# regenerate with:  node extension/selftest.js | grep '^{"id"'
FIELDS_FIXTURE = REPO / "extension" / "fixtures_scanned_fields.jsonl"
PY = r"C:\Users\adwai\AppData\Local\Programs\Python\Python312\python.exe"
PORT = 8799

tmp = pathlib.Path(tempfile.mkdtemp(prefix="apc-e2e-"))
(tmp / "profile.json").write_text(json.dumps({
    "profile_id": "testuser",
    "personal": {
        "full_name": "Alex Rivera", "email": "alex@example.test",
        "phone": "+1-555-0100", "city": "Seattle", "province_state": "WA",
        "country": "United States", "postal_code": "98101",
        "address": "1 Test Street", "linkedin_url": "https://linkedin.com/in/alexrivera",
        "portfolio_url": "https://alexrivera.example",
        "password": "SUPER-SECRET-SHOULD-NEVER-APPEAR",
    },
    "work_authorization": {
        "legally_authorized_to_work": True, "require_sponsorship": False,
    },
    "compensation": {"salary_expectation": "", "salary_currency": "USD"},
    "experience": {"current_job_title": "Staff Designer",
                   "current_company": "Example Co",
                   "years_of_experience_total": "6"},
    "eeo_voluntary": {},
    "work_history": [
        {"title": "Staff Designer", "company": "Example Co",
         "location": "Seattle, WA", "start": "03/2022", "end": "",
         "current": True, "description": "Led design for the analytics suite."},
        {"title": "Product Designer", "company": "Globex Inc",
         "location": "San Jose, CA", "start": "06/2019", "end": "02/2022",
         "current": False, "description": "Design systems and onboarding."},
    ],
}), encoding="utf-8")

env = dict(os.environ)
env["APPLYPILOT_DIR"] = str(tmp)
env.pop("APPLYPILOT_ROOT", None)
env.pop("APPLYPILOT_PROFILE", None)

proc = subprocess.Popen(
    [PY, "-m", "applypilot", "serve-extension", "--port", str(PORT)],
    cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

base = f"http://127.0.0.1:{PORT}"
token = None
tok_file = tmp / "extension_token.txt"


def get(url, tok=None):
    req = urllib.request.Request(url)
    if tok:
        req.add_header("X-ApplyPilot-Token", tok)
    return urllib.request.urlopen(req, timeout=10)


try:
    # wait for liveness
    for _ in range(90):
        if tok_file.exists():
            token = tok_file.read_text(encoding="utf-8").strip()
        try:
            get(f"{base}/health", token)
            break
        except urllib.error.HTTPError:
            break                      # responding (401 is fine) => it is up
        except Exception:
            time.sleep(1)
    else:
        raise SystemExit("service never came up")

    token = tok_file.read_text(encoding="utf-8").strip()
    print(f"[ok] service live on {base}, token {len(token)} chars")

    # --- auth is enforced -------------------------------------------------
    try:
        get(f"{base}/health")
        print("[FAIL] /health served WITHOUT a token")
        bad_auth = True
    except urllib.error.HTTPError as e:
        print(f"[ok] unauthenticated request rejected ({e.code})")
        bad_auth = e.code != 401

    health = json.load(get(f"{base}/health", token))
    print(f"[ok] health: {health}")

    # --- resolve the REAL scanner output ----------------------------------
    fields = [json.loads(line) for line in
              FIELDS_FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    # add the fields a real ATS form has that our mock page lacks
    fields += [
        {"id": "x1", "selector": "#sponsorship", "tag": "input", "type": "text",
         "name": "sponsorship", "autocomplete": "",
         "label": "Will you now or in the future require sponsorship for employment visa status?",
         "placeholder": "", "required": True, "options": []},
        {"id": "x2", "selector": "#salary", "tag": "input", "type": "text",
         "name": "salary", "autocomplete": "", "label": "Desired Salary",
         "placeholder": "", "required": False, "options": []},
        {"id": "x3", "selector": "#pw", "tag": "input", "type": "password",
         "name": "password", "autocomplete": "current-password", "label": "Password",
         "placeholder": "", "required": False, "options": []},
        {"id": "x4", "selector": "#why", "tag": "textarea", "type": "textarea",
         "name": "why_us", "autocomplete": "",
         "label": "Why do you want to work here?", "placeholder": "", "required": True,
         "options": []},
    ]
    body = json.dumps({"url": "https://boards.greenhouse.io/acme/jobs/1",
                       "fields": fields}).encode()
    req = urllib.request.Request(f"{base}/resolve", data=body,
                                 headers={"Content-Type": "application/json",
                                          "X-ApplyPilot-Token": token})
    plan = json.load(urllib.request.urlopen(req, timeout=30))

    by_id = {f["id"]: f for f in fields}

    def _label(entry):
        return (by_id.get(entry["id"], {}).get("label") or "").strip().lower()

    def _section(entry):
        return (by_id.get(entry["id"], {}).get("section") or "").strip().lower()

    # Keyed by label: scanner-assigned ids shift whenever the mock page changes,
    # and an assertion that silently stops matching anything is worse than no
    # assertion at all.
    fills = {_label(f): f for f in plan["fills"]}
    skips = {_label(s_): s_ for s_ in plan["skipped"]}
    fills_by_section = {}
    for f in plan["fills"]:
        fills_by_section.setdefault(_section(f), {})[_label(f)] = f
    print(f"\n[ok] resolved {len(fields)} fields -> {len(fills)} fills, {len(skips)} skips")
    for f in plan["fills"]:
        print(f"   FILL  {_label(f)[:30]:32} {f['source']:12} {str(f.get('profile_key'))[:26]:28} = {str(f['value'])[:34]!r}")
    for s_ in plan["skipped"]:
        print(f"   SKIP  {_label(s_)[:30]:32} {s_['source']:12} {s_['reason'][:44]}")

    raw = json.dumps(plan)
    failures = []

    def check(name, cond):
        print(("PASS  " if cond else "FAIL  ") + name)
        if not cond:
            failures.append(name)

    print("\n--- assertions ---")
    check("secret never appears anywhere in the response",
          "SUPER-SECRET-SHOULD-NEVER-APPEAR" not in raw)
    check("password field is not filled", "password" not in fills)
    check("password field skipped by the secret guard",
          skips.get("password", {}).get("source") == "secret_guard")
    check("full name filled from profile", fills.get("full name", {}).get("value") == "Alex Rivera")
    check("email filled", fills.get("email address", {}).get("value") == "alex@example.test")
    wa = fills.get("are you legally authorized to work in the us?", {})
    check("work-auth answered by the CANARY tier", wa.get("source") == "canary")
    check("work-auth answer is Yes", wa.get("value") == "Yes")
    sp = fills.get("will you now or in the future require sponsorship for employment visa status?", {})
    check("sponsorship answered by the CANARY tier", sp.get("source") == "canary")
    check("sponsorship answer is No (profile says no sponsorship needed)", sp.get("value") == "No")
    check("salary NOT guessed (empty in profile)", "desired salary" not in fills)
    check("free-text 'why do you want to work here' NOT answered",
          "why do you want to work here?" not in fills)

    # --- tier 3: repeating work-experience blocks (the real Workday failure) ---
    b1 = fills_by_section.get("work experience 1", {})
    b2 = fills_by_section.get("work experience 2", {})
    check("Work Experience 1 filled from the MOST RECENT position",
          b1.get("job title", {}).get("value") == "Staff Designer"
          and b1.get("company", {}).get("value") == "Example Co")
    check("Work Experience 2 filled from the PREVIOUS position",
          b2.get("job title", {}).get("value") == "Product Designer"
          and b2.get("company", {}).get("value") == "Globex Inc")
    check("block 1 and block 2 got DIFFERENT employers (no cross-contamination)",
          b1.get("company", {}).get("value") != b2.get("company", {}).get("value"))
    check("structured tier is the source for work-history fields",
          b1.get("job title", {}).get("source") == "structured")
    check("current role leaves the End date blank",
          b1.get("to", {}).get("value", "SENTINEL") == "")
    check("previous role has a real End date", b2.get("to", {}).get("value") == "02/2022")
    third = [s_ for s_ in plan["skipped"]
             if "work history" in s_["reason"] and "never" in s_["reason"]]
    check("3rd block SKIPPED, not wrapped back to position 1", len(third) >= 1)
    check("auth enforced with 401", not bad_auth)
    check("tiers_available reported", isinstance(health.get("tiers_available"), list))
    submitted_ids = {f["id"] for f in fields}
    check("every fill corresponds to a submitted field",
          {f["id"] for f in plan["fills"]} <= submitted_ids)
    check("every skip corresponds to a submitted field",
          {s_["id"] for s_ in plan["skipped"]} <= submitted_ids)
    check("no field is both filled and skipped",
          not ({f["id"] for f in plan["fills"]} & {s_["id"] for s_ in plan["skipped"]}))

    print(f"\n{len(failures)} failure(s)" if failures else "\nALL E2E ASSERTIONS PASSED")
    sys.exit(1 if failures else 0)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
