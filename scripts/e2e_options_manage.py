"""End-to-end coverage for the Settings page's account-management sections:
the skills editor, saved answers, application log, and the legacy-install
409 message on "New profile" -- everything added to options.html/options.js
beyond the résumé-import flow that scripts/e2e_options_resume.py already
covers.

Drives a real Chromium with the unpacked extension and a real local service
on a temp data dir (never E:\\applypilot-data), seeded directly on disk with
a profile (including skills_boundary), an answer bank, and one log entry so
every check exercises a real round trip through the service instead of a
mocked response.

Run:  python scripts/e2e_options_manage.py
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

try:
    sys.stdout.reconfigure(encoding="utf-8")  # the 409 message below has an em dash
except Exception:
    pass

REPO = pathlib.Path(__file__).resolve().parent.parent
EXT = REPO / "extension"
PORT = int(os.environ.get("APC_E2E_PORT", "8804"))
failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(name)


def stage(name):
    print(f"\n-- {name} --")


tmp = pathlib.Path(tempfile.mkdtemp(prefix="apc-opts-manage-"))

# -- seed profile.json: legacy single-profile layout, same as e2e_options_resume.py,
#    so GET /profiles reports legacy=True and POST /profiles 409s (test 4 below). --
PROFILE_SEED = {
    "personal": {"full_name": "Nida Shah", "email": "nida.test@example.com", "password": "KEEP-ME-SECRET"},
    "work_authorization": {},
    "skills_boundary": {"languages": ["Python", "Go"]},
}
(tmp / "profile.json").write_text(json.dumps(PROFILE_SEED), encoding="utf-8")

# -- seed answer_bank.json: one of the operator's own remembered answers, one reused
#    from an earlier run -- list_answers() must show both, forget() only one. --
ANSWERS_SEED = [
    {"q": "Why do you want to work here?", "a": "I admire the team's developer-tools work.",
     "source": "you", "ts": 1755000000},
    {"q": "What is your greatest strength?", "a": "Attention to detail.",
     "source": "earlier run", "ts": 1755000100},
]
(tmp / "answer_bank.json").write_text(json.dumps(ANSWERS_SEED), encoding="utf-8")

# -- seed application_log.jsonl: one prior fill to show in the table and export. --
LOG_ENTRY = {
    "id": "log0001test", "url": "https://boards.greenhouse.io/acme/jobs/999",
    "title": "Senior Product Designer", "company": "Acme Corp",
    "created": 1755000200, "updated": 1755000200, "status": "filled",
    "counts": {"filled": 12, "drafts": 2, "needs_you": 1, "failed": 0, "unreadable": 0}, "fills": 1,
}
(tmp / "application_log.jsonl").write_text(json.dumps(LOG_ENTRY) + "\n", encoding="utf-8")

env = dict(os.environ)
env["APPLYPILOT_DIR"] = str(tmp)
env["APPLYPILOT_ROOT"] = str(tmp)
env.pop("APPLYPILOT_PROFILE", None)
proc = subprocess.Popen(
    [sys.executable, "-m", "applypilot", "serve-extension", "--port", str(PORT)],
    cwd=str(REPO), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

base = f"http://127.0.0.1:{PORT}"
user_dir = tempfile.mkdtemp(prefix="apc-chrome-manage-")

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError  # noqa: E402 — after consts


def wait_true(page, expr, timeout=10000):
    """page.wait_for_function that reports False on timeout instead of raising,
    so one slow/broken thing doesn't abort every check after it.

    `expr` is a bare JS boolean expression (not a function). Wrapped here as an
    explicit arrow function before handing it to Playwright: the extension's MV3
    page CSP is `script-src 'self'` (no unsafe-eval), and Playwright's own
    internal `new Function(...)` wrapping for a bare-expression string trips
    that CSP, even though invoking an actual arrow-function source (exactly
    what page.evaluate() already does elsewhere in this script) does not.
    """
    try:
        page.wait_for_function(f"() => ({expr})", timeout=timeout)
        return True
    except PWTimeoutError:
        return False


def wait_selector(page, selector, timeout=10000):
    try:
        page.wait_for_selector(selector, timeout=timeout)
        return True
    except PWTimeoutError:
        return False


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
            user_dir, headless=False, accept_downloads=True,
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
            page.set_default_timeout(8000)
            # Seed the extension's storage the way the operator would via Options.
            page.goto(f"chrome-extension://{ext_id}/options.html")
            page.evaluate(
                """async ([url, tok]) => {
                    await chrome.storage.local.set({ serviceUrl: url, token: tok });
                }""", [base, token])
            # Warm the service (cold module imports) before the real page load
            # that fires the real GET /profile/full, /answers, /log requests.
            page.evaluate("""async ([url, tok]) => {
                await fetch(url + '/health', { headers: { 'X-ApplyPilot-Token': tok } });
            }""", [base, token])
            page.reload()

            body = page.inner_text("body")
            check("options page renders", len(body) > 100, f"{len(body)} chars")

            # ---------------------------------------------------------------
            stage("1) Skills editor round-trips to profile.json")
            # ---------------------------------------------------------------
            try:
                page.evaluate("document.getElementById('sectionSkills').open = true")
                deadline = time.time() + 20
                ready = False
                while time.time() < deadline:
                    ready = page.evaluate(
                        """() => {
                            const el = document.querySelector('.skills-category-name');
                            return !!(el && el.value === 'languages');
                        }""")
                    if ready:
                        break
                    page.wait_for_timeout(300)
                check("seeded profile/skills loaded into the editor", ready)

                lang_block = page.locator(".skills-category").nth(0)
                check("seeded skill 'Python' renders",
                      lang_block.locator('.skill-chip[data-skill="Python"]').count() == 1)
                check("seeded skill 'Go' renders",
                      lang_block.locator('.skill-chip[data-skill="Go"]').count() == 1)

                lang_block.locator('.skill-chip[data-skill="Go"] .skill-chip-remove').click()
                check("removing a skill drops its chip",
                      lang_block.locator('.skill-chip[data-skill="Go"]').count() == 0)

                lang_block.locator('.row input[type="text"]').fill("Rust")
                lang_block.locator(".row button").click()
                check("adding a skill creates its chip",
                      lang_block.locator('.skill-chip[data-skill="Rust"]').count() == 1)

                page.click("#addSkillCategoryBtn")
                tools_block = page.locator(".skills-category").last
                tools_block.locator(".skills-category-name").fill("tools")
                tools_block.locator('.row input[type="text"]').fill("Git")
                tools_block.locator(".row button").click()
                check("a newly-added category holds its own skill",
                      tools_block.locator('.skill-chip[data-skill="Git"]').count() == 1)

                page.click("#saveProfile")
                saved = wait_true(
                    page, "document.getElementById('profileStatus').textContent.includes('Profile saved')")
                check("Save profile confirms", saved)

                on_disk = json.loads((tmp / "profile.json").read_text(encoding="utf-8"))
                boundary = on_disk.get("skills_boundary") or {}
                check("skills edit round-tripped to profile.json",
                      boundary.get("languages") == ["Python", "Rust"] and boundary.get("tools") == ["Git"],
                      json.dumps(boundary))
                check("password untouched by the skills save",
                      on_disk.get("personal", {}).get("password") == "KEEP-ME-SECRET")
            except Exception as exc:  # noqa: BLE001 — record and keep going
                check("skills editor stage completed without an error", False, repr(exc))

            # ---------------------------------------------------------------
            stage("2) Saved answers: listed, then forgotten")
            # ---------------------------------------------------------------
            try:
                check("answers table renders", wait_selector(page, "#answersTableWrap table"))
                rows = page.locator("#answersTableWrap tbody tr")
                check("both seeded answers are listed", rows.count() == 2, str(rows.count()))

                you_row = page.locator("#answersTableWrap tbody tr", has_text="Why do you want to work here?")
                check("the operator's own answer is badged 'you'",
                      you_row.locator(".who-badge.you").count() == 1)

                other_row = page.locator("#answersTableWrap tbody tr", has_text="What is your greatest strength?")
                check("the reused answer is badged 'earlier run'",
                      other_row.locator(".who-badge").inner_text() == "earlier run")

                you_row.locator("button", has_text="Forget").click()
                forgotten = wait_true(page, "document.querySelectorAll('#answersTableWrap tbody tr').length === 1")
                check("forgetting refreshes the table down to one row", forgotten)

                bank_on_disk = json.loads((tmp / "answer_bank.json").read_text(encoding="utf-8"))
                check("forgotten answer removed from answer_bank.json on disk",
                      all(e.get("q") != "Why do you want to work here?" for e in bank_on_disk),
                      json.dumps(bank_on_disk))
            except Exception as exc:  # noqa: BLE001
                check("saved-answers stage completed without an error", False, repr(exc))

            # ---------------------------------------------------------------
            stage("3) Application log: status change persists, CSV export reflects it")
            # ---------------------------------------------------------------
            try:
                check("log table renders", wait_selector(page, "#logTableWrap table"))
                log_row = page.locator("#logTableWrap tbody tr").first
                row_text = log_row.inner_text()
                check("seeded log entry shows its company", "Acme Corp" in row_text, row_text[:120])

                select = log_row.locator("select")
                check("status select starts at 'filled'", select.input_value() == "filled")

                select.select_option("applied")
                updated = wait_true(page, "document.getElementById('logStatus').textContent.includes('Updated')")
                check("status change confirms", updated)

                log_on_disk = json.loads((tmp / "application_log.jsonl").read_text(encoding="utf-8").strip())
                check("status change persisted to application_log.jsonl",
                      log_on_disk.get("status") == "applied", json.dumps(log_on_disk))

                with page.expect_download() as download_info:
                    page.click("#exportLogBtn")
                download = download_info.value
                csv_text = pathlib.Path(download.path()).read_text(encoding="utf-8")
                check("CSV export contains the company and the updated status",
                      "Acme Corp" in csv_text and "applied" in csv_text, csv_text.strip()[:200])
            except Exception as exc:  # noqa: BLE001
                check("application-log stage completed without an error", False, repr(exc))

            # ---------------------------------------------------------------
            stage("4) Profiles: legacy install's 409 renders verbatim")
            # ---------------------------------------------------------------
            try:
                page.click("#newProfileBtn")
                page.fill("#newProfileId", "second")
                page.click("#createProfileBtn")
                rendered = wait_true(
                    page, "document.getElementById('profileStatus').textContent.includes('profile migrate')")
                check("the 409 response renders in #profileStatus", rendered)

                msg = page.inner_text("#profileStatus")
                check("legacy 409 shown verbatim with the migrate command",
                      "applypilot profile migrate" in msg and "legacy single-profile install" in msg, msg)
                check("legacy 409 is not wrapped in a generic error message",
                      not msg.lower().startswith("could not create profile"), msg)
            except Exception as exc:  # noqa: BLE001
                check("profiles stage completed without an error", False, repr(exc))

        finally:
            ctx.close()
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()

print(f"\n{len(failures)} failure(s): {failures}" if failures else "\nALL OPTIONS/MANAGE CHECKS PASSED")
sys.exit(1 if failures else 0)
