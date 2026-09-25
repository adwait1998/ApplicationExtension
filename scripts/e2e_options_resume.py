"""The v3 headline flow, end to end in a real browser: upload a resume in the
extension's options page and watch the profile fill itself.

This is the one thing no unit test or jsdom check can prove. It drives a real
Chromium with the unpacked extension, a real local service, and a real file
picker (Playwright's set_input_files), then asserts the parsed draft actually
lands in the form — and, just as importantly, that work authorisation does NOT,
because a resume is not evidence of visa status.

Needs an LLM for the structured pass; without one the deterministic pass still
runs and the contact-detail assertions below still hold. The test reports which
mode it ran in rather than silently asserting less.

Run:  python scripts/e2e_options_resume.py
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
EXT = REPO / "extension"
PORT = int(os.environ.get("APC_E2E_PORT", "8803"))
failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(name)


RESUME_TEXT = """Nida Shah
Senior Product Designer
Seattle, WA | nida.test@example.com | (555) 010-0100
linkedin.com/in/nidashah14 | github.com/nidashah

Authorized to work in the US. No sponsorship required.

EXPERIENCE
Senior Product Designer, Acme Corp - Seattle, WA (03/2022 - Present)
- Led design for the analytics suite.

Product Designer, Globex - San Jose, CA (06/2019 - 02/2022)
- Design systems and onboarding.

EDUCATION
Arizona State University, BS Design, 2015 - 2019
"""

tmp = pathlib.Path(tempfile.mkdtemp(prefix="apc-opts-"))
(tmp / "profile.json").write_text(json.dumps({
    "personal": {"full_name": "", "password": "KEEP-ME-SECRET"},
    "work_authorization": {},
}), encoding="utf-8")
resume_path = tmp / "resume.txt"
resume_path.write_text(RESUME_TEXT, encoding="utf-8")

env = dict(os.environ)
env["APPLYPILOT_DIR"] = str(tmp)
env["APPLYPILOT_ROOT"] = str(tmp)
env.pop("APPLYPILOT_PROFILE", None)
proc = subprocess.Popen(
    [sys.executable, "-m", "applypilot", "serve-extension", "--port", str(PORT)],
    cwd=str(REPO), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

base = f"http://127.0.0.1:{PORT}"
user_dir = tempfile.mkdtemp(prefix="apc-chrome-opts-")

from playwright.sync_api import sync_playwright  # noqa: E402 — after consts

try:
    tokf = tmp / "extension_token.txt"
    for _ in range(60):
        if tokf.exists():
            try:
                req = urllib.request.Request(
                    f"{base}/health",
                    headers={"X-ApplyPilot-Token": tokf.read_text(encoding="utf-8").strip()})
                urllib.request.urlopen(req, timeout=5)
                break
            except urllib.error.HTTPError:
                break
            except Exception:
                pass
        time.sleep(1)
    else:
        raise SystemExit("service never came up")
    token = tokf.read_text(encoding="utf-8").strip()
    print(f"[ok] service live on {base}")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_dir, headless=False,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}",
                  "--no-first-run", "--no-default-browser-check"])
        try:
            sw = None
            for _ in range(30):
                if ctx.service_workers:
                    sw = ctx.service_workers[0]
                    break
                try:
                    sw = ctx.wait_for_event("serviceworker", timeout=1000)
                    break
                except Exception:
                    pass
            check("extension loaded (MV3 service worker registered)", sw is not None)
            ext_id = sw.url.split("/")[2]
            # Never the operator's REAL installed native host (it would auto-connect this test
            # to the real service and real profile): point the extension at a host name no one
            # registers, before any extension page loads.
            sw.evaluate("(h) => chrome.storage.local.set({ nativeHostNameOverride: h })",
                        "com.applypilot.copilot.test_absent")

            page = ctx.new_page()
            # Seed the extension's storage the way the operator would via Options.
            page.goto(f"chrome-extension://{ext_id}/options.html")
            page.evaluate(
                """async ([url, tok]) => {
                    await chrome.storage.local.set({ serviceUrl: url, token: tok });
                }""", [base, token])
            page.reload()
            page.wait_for_timeout(1500)

            body = page.inner_text("body")
            check("options page renders", len(body) > 100, f"{len(body)} chars")
            check("onboarding is the first thing shown",
                  "résumé" in body.lower() or "resume" in body.lower(), body[:80].replace("\n", " "))

            file_input = page.locator('input[type="file"]').first
            check("a résumé file input exists on the options page",
                  file_input.count() > 0)

            # Warm the service first: the very first request after startup pays
            # cold module imports (~17s), steady state is ~2s. A fixed sleep here
            # lands in that gap and reads an empty form, which looks exactly like
            # a parsing failure — so poll for the real completion signal instead.
            page.evaluate("""async ([url, tok]) => {
                await fetch(url + '/health', { headers: { 'X-ApplyPilot-Token': tok } });
            }""", [base, token])

            file_input.set_input_files(str(resume_path))
            deadline = time.time() + 90
            while time.time() < deadline:
                done = page.evaluate(
                    """() => {
                        const el = document.querySelector('[data-path="personal.email"]');
                        return !!(el && el.value);
                    }""")
                if done:
                    break
                page.wait_for_timeout(500)
            page.wait_for_timeout(500)

            def val(sel):
                loc = page.locator(sel)
                return loc.input_value() if loc.count() else None

            email = val('[data-path="personal.email"]')
            phone = val('[data-path="personal.phone"]')
            name = val('[data-path="personal.full_name"]')
            print(f"\n  parsed -> name={name!r} email={email!r} phone={phone!r}")

            check("email parsed from the résumé", email == "nida.test@example.com", str(email))
            check("name parsed from the résumé", (name or "").strip() == "Nida Shah", str(name))
            check("phone parsed from the résumé", bool(phone and "010-0100" in phone), str(phone))

            rows = page.locator('[data-field="company"]')
            n_rows = rows.count()
            llm_mode = n_rows > 0
            print(f"  work-history rows rendered: {n_rows} "
                  f"({'LLM pass ran' if llm_mode else 'deterministic only — no LLM configured'})")
            if llm_mode:
                companies = [rows.nth(i).input_value() for i in range(n_rows)]
                check("work history populated from the résumé",
                      any("Acme" in (c or "") for c in companies), str(companies))

            # THE rule: a résumé saying "authorized, no sponsorship" must not set it.
            wa = page.evaluate(
                """() => Array.from(document.querySelectorAll('input[type=radio]'))
                        .filter(r => (r.name || '').toLowerCase().includes('auth')
                                  || (r.name || '').toLowerCase().includes('sponsor'))
                        .filter(r => r.checked).map(r => r.name + '=' + r.value)""")
            # The UI models "not answered" as an explicitly-checked `unset` pill,
            # so an empty list is NOT the pass condition — a checked Yes/No is the
            # failure. Asserting `wa == []` would have passed vacuously if the
            # radios ever stopped rendering at all.
            decided = [x for x in wa if not x.endswith("=unset")]
            check("work authorisation left explicitly Not-set by the résumé import "
                  "(a résumé is not evidence of visa status)",
                  wa != [] and decided == [], str(wa))

            on_disk = json.loads((tmp / "profile.json").read_text(encoding="utf-8"))
            check("nothing auto-saved — the draft awaits review",
                  not on_disk.get("personal", {}).get("email"), json.dumps(on_disk)[:90])
            check("password untouched on disk",
                  on_disk["personal"].get("password") == "KEEP-ME-SECRET")

            page.screenshot(path=str(REPO / "docs" / "options-after-import.png"), full_page=True)
            print(f"\n  screenshot: {REPO / 'docs' / 'options-after-import.png'}")
        finally:
            ctx.close()
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()

print(f"\n{len(failures)} failure(s): {failures}" if failures else "\nALL OPTIONS/RESUME CHECKS PASSED")
sys.exit(1 if failures else 0)
