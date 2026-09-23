"""End-to-end: real uvicorn socket + the Chrome extension's ACTUAL scanner output.

Uses a synthetic profile in a temp APPLYPILOT_DIR so no real personal data is
touched or printed. Asserts the resolution ladder behaves correctly on a
realistic ATS-shaped form.
"""
import json, os, pathlib, subprocess, sys, tempfile, time, urllib.request, urllib.error

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
    fields = [json.loads(l) for l in
              FIELDS_FIXTURE.read_text(encoding="utf-8").splitlines() if l.strip()]
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

    fills = {f["id"]: f for f in plan["fills"]}
    skips = {s["id"]: s for s in plan["skipped"]}
    print(f"\n[ok] resolved {len(fields)} fields -> {len(fills)} fills, {len(skips)} skips")
    for f in plan["fills"]:
        print(f"   FILL  {f['id']:4} {f['source']:14} {f.get('profile_key',''):28} = {f['value']!r}")
    for s in plan["skipped"]:
        print(f"   SKIP  {s['id']:4} {s['source']:14} {s['reason']}")

    raw = json.dumps(plan)
    failures = []

    def check(name, cond):
        print(("PASS  " if cond else "FAIL  ") + name)
        if not cond:
            failures.append(name)

    print("\n--- assertions ---")
    check("secret never appears anywhere in the response",
          "SUPER-SECRET-SHOULD-NEVER-APPEAR" not in raw)
    check("password field is not filled", "x3" not in fills)
    check("password field skipped by the secret guard",
          skips.get("x3", {}).get("source") == "secret_guard")
    check("full name filled from profile", fills.get("f0", {}).get("value") == "Alex Rivera")
    check("email filled", fills.get("f1", {}).get("value") == "alex@example.test")
    check("work-auth radio answered by the CANARY tier",
          fills.get("f8", {}).get("source") == "canary")
    check("work-auth answer is Yes", fills.get("f8", {}).get("value") == "Yes")
    check("sponsorship answered by the CANARY tier",
          fills.get("x1", {}).get("source") == "canary")
    check("sponsorship answer is No (profile says no sponsorship needed)",
          fills.get("x1", {}).get("value") == "No")
    check("salary NOT guessed (empty in profile)", "x2" not in fills)
    check("free-text 'why do you want to work here' NOT answered", "x4" not in fills)
    check("auth enforced with 401", not bad_auth)
    check("tiers_available reported", isinstance(health.get("tiers_available"), list))
    check("every fill corresponds to a submitted field",
          set(fills) <= {f["id"] for f in fields})

    print(f"\n{len(failures)} failure(s)" if failures else "\nALL E2E ASSERTIONS PASSED")
    sys.exit(1 if failures else 0)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
