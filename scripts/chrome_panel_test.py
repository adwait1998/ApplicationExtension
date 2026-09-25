"""Does the side panel actually replace the popup, and does a fill survive the panel closing?

Modeled on scripts/chrome_load_test.py (same real-Chromium-via-Playwright approach, same
--load-extension / service-worker-lookup technique, same PASS/FAIL check() harness) — read that
file first if this one is confusing. Where chrome_load_test.py proves scanner.js's DOM logic
against a real layout engine, this file proves the NEW plumbing this build adds, which cannot be
exercised any other way: manifest side-panel wiring, per-tab chrome.storage.session state
written by background.js on content.js's behalf, live progress messages, Cancel, the per-field
timeout, and that none of it ever submits the mock form.

No live ApplyPilot service is required or started: a tiny stub HTTP server (stdlib only, bound
to an OS-assigned free port so it can never collide with a real `applypilot serve-extension`
that might be running on 127.0.0.1:8787 on this machine right now) stands in for /health,
/profile/counts and /resolve. It fills whatever fields the real scanner.js actually found on the
real page — it never has to guess field ids up front, since it reads them from the real
/resolve request body.

The timeout and Cancel tests need a field whose fill takes a controlled, known amount of time
(one that never resolves at all, and one that resolves slowly). scanner.js's own Workday-widget
timing depends on markup this file must not assume too much about, so instead this file
monkeypatches `ApplyPilotScanner.applyFill` from OUTSIDE the extension's isolated world, using
chrome.scripting.executeScript's own `func` form targeting the SAME tab/frame with the default
isolated world — which is the same world content.js already runs in, so this patches the exact
function content.js calls, without editing a single line of scanner.js or test-page.html.
"""
import base64
import hashlib
import json
import pathlib
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Resolved relative to this script's own location (repo_root/scripts/.. -> repo_root/extension)
# rather than a hardcoded absolute path: this repo is worked on from multiple git worktrees at
# once, each with its own extension/ copy, and this test must exercise WHICHEVER copy sits next
# to it (i.e. the worktree it's actually run from) so it proves out that worktree's own changes
# instead of some other checkout's — and so two worktrees running this concurrently never race
# on the same files.
EXT = pathlib.Path(__file__).resolve().parent.parent / "extension"
MANIFEST_PATH = EXT / "manifest.json"
TEST_PAGE_BYTES = (EXT / "test-page.html").read_bytes()

from playwright.sync_api import sync_playwright  # noqa: E402 — after EXT/PAGE consts

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------------------
# 0. manifest checks — no browser needed, so these can never be skipped by a browser
#    launch failure elsewhere in this file.
# ---------------------------------------------------------------------------
manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
check("manifest has no action.default_popup (side panel replaces the popup)",
      "default_popup" not in (manifest.get("action") or {}))
check("manifest declares side_panel.default_path = sidepanel.html",
      (manifest.get("side_panel") or {}).get("default_path") == "sidepanel.html")
check("manifest requests the sidePanel permission",
      "sidePanel" in (manifest.get("permissions") or []))
host_perms = manifest.get("host_permissions") or []
optional_host_perms = manifest.get("optional_host_permissions") or []
check("manifest keeps the original 127.0.0.1 host permissions",
      "http://127.0.0.1/*" in host_perms and "http://127.0.0.1:*/*" in host_perms, str(host_perms))
check("manifest host_permissions has NO static broad http(s)/all-urls grant (least privilege)",
      not any(p in host_perms for p in ("https://*/*", "http://*/*", "<all_urls>")), str(host_perms))
check("manifest declares https://*/* and http://*/* as OPTIONAL host permissions instead",
      "https://*/*" in optional_host_perms and "http://*/*" in optional_host_perms, str(optional_host_perms))
check("manifest requests the webNavigation permission (needed to enumerate a tab's frames)",
      "webNavigation" in (manifest.get("permissions") or []))
check("minimum_chrome_version bumped to 116 (chrome.sidePanel.setPanelBehavior needs it)",
      manifest.get("minimum_chrome_version") == "116", manifest.get("minimum_chrome_version"))

# --- item 1: auto-connect via native messaging ---
check("manifest requests the nativeMessaging permission (auto-connect)",
      "nativeMessaging" in (manifest.get("permissions") or []))


def _chrome_extension_id_from_key(b64_key: str) -> str:
    """Same algorithm as applypilot.extension.native_install.extension_id_from_key, duplicated
    here (stdlib only, no dependency on the applypilot package being importable from wherever
    this script runs) so this file can prove manifest.json's pinned "key" really does derive to
    the id the native host's allowed_origins names, independently."""
    digest = hashlib.sha256(base64.b64decode(b64_key)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


EXPECTED_EXTENSION_ID = "noooclaijfiejnfgabkemnpabcbdnaac"
check("manifest has a pinned \"key\" that derives to the native host's allowed extension id",
      bool(manifest.get("key")) and _chrome_extension_id_from_key(manifest["key"]) == EXPECTED_EXTENSION_ID,
      (manifest.get("key") or "")[:40] + "...")

# --- item 7: keyboard shortcuts ---
commands = manifest.get("commands") or {}
check("manifest declares _execute_action with the Alt+Shift+F suggested key (opens the panel)",
      (commands.get("_execute_action") or {}).get("suggested_key", {}).get("default") == "Alt+Shift+F",
      json.dumps(commands.get("_execute_action")))
check("manifest declares a fill-page command with the Alt+Shift+G suggested key",
      (commands.get("fill-page") or {}).get("suggested_key", {}).get("default") == "Alt+Shift+G",
      json.dumps(commands.get("fill-page")))


# ---------------------------------------------------------------------------
# 1. a tiny stub of the local ApplyPilot service — stdlib only, ephemeral port.
# ---------------------------------------------------------------------------
resolve_calls = []  # (url, [field names requested]) — lets a check below prove RESOLVE ran
cover_letter_calls = []  # every /cover-letter request body — item 2
answers_learn_calls = []  # every /answers/learn request's `items` list — item 3
log_calls = []  # every /log request body — item 4
log_status_calls = []  # every (entry_id, status) POSTed to /log/{id}/status — item 4


def value_for_field(f):
    options = f.get("options") or []
    if options:
        return options[0]
    tag = f.get("tag")
    ftype = (f.get("type") or "").lower()
    if tag == "textarea":
        return "Test answer for " + str(f.get("name") or f.get("label") or f["id"])
    if ftype == "checkbox":
        return "true"
    if ftype == "email":
        return "ada@example.com"
    return "Test Value " + str(f["id"])


def build_fills(fields):
    fills, skipped = [], []
    for f in fields:
        widget = f.get("widget") or ""
        ftype = (f.get("type") or "").lower()
        name = f.get("name") or ""
        # Keep the stub's own logic trivial: leave Workday's own popup/prompt widgets and
        # anything unfillable to the "needs you" pile. The timeout/cancel tabs below control
        # timing by patching applyFill() directly, not by relying on any particular widget's
        # real timing, so nothing here needs to touch wd-dropdown/wd-prompt at all. "password" is
        # skipped the same way file/hidden/etc. always were (matches content.js's own "remember my
        # answers" exclusion — see item 3). Anything named "remember_test_*" is ALSO always left
        # for the human regardless of type — the REMEMBER MY ANSWERS fixture below relies on this
        # to get plain text fields into the "needs you" pile on demand.
        if (widget in ("wd-prompt", "wd-dropdown")
                or ftype in ("hidden", "file", "submit", "button", "image", "reset", "password")
                or name.startswith("remember_test_")):
            skipped.append({"id": f["id"], "reason": "test stub: left for you"})
            continue
        fills.append({
            "id": f["id"],
            "auto_fill": True,
            "value": value_for_field(f),
            "reason": "test stub match",
            "profile_key": "test." + str(f.get("name") or f.get("label") or f["id"]),
            "source": "profile",
            "draft": False,
        })
    return fills, skipped


# ---------------------------------------------------------------------------
# 1b. a SECOND, separate origin (a different 127.0.0.1 PORT) serving a plain copy of the same
#     mock form — stands in for a cross-origin ATS iframe (e.g. a company careers page on
#     sofi.com embedding job-boards.greenhouse.io/embed/job_app). A different port on 127.0.0.1
#     is still cross-origin per the browser's same-origin policy (scheme+host+port), which is
#     exactly the property that makes a normal content script unable to read into it without
#     being separately injected — see the TAB 5 test below. Both ports are still 127.0.0.1, so
#     both are covered by the manifest's STATIC host_permissions and neither one triggers a
#     real chrome.permissions.request() prompt in this test (see the manifest checks above).
# ---------------------------------------------------------------------------
class InnerFormHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep test output readable

    def do_GET(self):
        if self.path.startswith("/inner-form.html"):
            body = TEST_PAGE_BYTES
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


inner_server = ThreadingHTTPServer(("127.0.0.1", 0), InnerFormHandler)
inner_port = inner_server.server_address[1]
inner_thread = threading.Thread(target=inner_server.serve_forever, daemon=True)
inner_thread.start()
INNER_URL = f"http://127.0.0.1:{inner_port}/inner-form.html"

# The "outer" (top-frame) page: one field of its own, plus the cross-origin iframe. Its own
# tiny submit trap mirrors test-page.html's (see that file's header) so this test can prove the
# TOP frame was never submitted either, not just the iframe.
WRAPPER_HTML_BYTES = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Acme Corp Careers (embed wrapper)</title></head>
<body>
<h1>Acme Corp Careers</h1>
<form id="outer-form">
  <label for="outer_only_field">How did you hear about us?</label>
  <input type="text" id="outer_only_field" name="outer_only_field" autocomplete="off">
</form>
<script>
  window.__FORM_SUBMITTED__ = false;
  document.getElementById('outer-form').addEventListener('submit', function (e) {{
    e.preventDefault();
    window.__FORM_SUBMITTED__ = true;
  }});
</script>
<iframe id="embedded" title="Job application form" src="{INNER_URL}"
        style="width:900px;height:2200px;border:1px solid #ccc;"></iframe>
</body></html>
""".encode("utf-8")

# A tiny fixture for item 2 (Draft cover letter): visible job-posting-shaped copy (proves the
# extracted page text excludes form-field text and includes real body copy) plus one
# textarea whose label says "cover letter" (proves the panel's Insert button/flow). Which
# /cover-letter response the stub below returns is picked by a marker in the URL's OWN hash
# fragment (never sent over the wire by the browser, but still part of the JSON `urls` this
# test's own JS sends as DATA) — see StubHandler.do_POST — so this one page can drive the
# success AND the 403/422 paths without extra fixture files.
COVER_LETTER_PAGE_BYTES = b"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Great Company Careers</title></head>
<body>
<h1>Great Company Careers</h1>
<p>We are looking for a fantastic engineer to join our team and build great things every day.</p>
<form id="cl-form">
  <label for="cl">Cover Letter (optional)</label>
  <textarea id="cl" name="cover_letter"></textarea>
</form>
<script>
  window.__FORM_SUBMITTED__ = false;
  document.getElementById('cl-form').addEventListener('submit', function (e) {
    e.preventDefault();
    window.__FORM_SUBMITTED__ = true;
  });
</script>
</body></html>
"""

# A tiny fixture for item 3 (Remember my answers): three fields the stub's build_fills() always
# leaves as "needs you" (name prefix "remember_test_", plus the password field via its real
# type) regardless of what's typed into them — so this test can simulate the operator answering
# a fill's leftover questions themselves, then click "Remember my answers", without depending on
# any particular widget being left unfilled for real reasons elsewhere on test-page.html.
REMEMBER_ANSWERS_PAGE_BYTES = b"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Remember Answers Fixture</title></head>
<body>
<form id="ra-form">
  <label for="notice_period">What is your notice period?</label>
  <input type="text" id="notice_period" name="remember_test_skip_notice">
  <label for="salary_expect">Desired salary</label>
  <input type="text" id="salary_expect" name="remember_test_skip_salary" required>
  <label for="fake_password">Set a password for this portal (optional)</label>
  <input type="password" id="fake_password" name="fake_password">
</form>
<script>
  window.__FORM_SUBMITTED__ = false;
  document.getElementById('ra-form').addEventListener('submit', function (e) {
    e.preventDefault();
    window.__FORM_SUBMITTED__ = true;
  });
</script>
</body></html>
"""

# A tiny fixture for item 8 (multi-step continuation): step 1's field lives in #step-root;
# window.__goToStep2()/__goToStep3() replace #step-root's own markup (standing in for a
# Workday-style SPA step transition this test drives directly — the extension itself must never
# click Next, see README "The one rule that matters") AND push a new URL, so both of
# runContinuationCheck()'s signals (form root replaced, url changed) fire together.
MULTI_STEP_PAGE_BYTES = b"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Multi-Step Fixture</title></head>
<body>
<div id="step-root">
  <h2>Step 1</h2>
  <label for="s1_name">Full Name</label>
  <input type="text" id="s1_name" name="s1_name">
</div>
<script>
  window.__FORM_SUBMITTED__ = false;
  window.__goToStep2 = function () {
    document.getElementById('step-root').innerHTML =
      '<h2>Step 2</h2><label for="s2_email">Email</label>' +
      '<input type="text" id="s2_email" name="s2_email">';
    history.pushState({}, '', location.pathname + '#step2');
  };
  window.__goToStep3 = function () {
    document.getElementById('step-root').innerHTML =
      '<h2>Step 3</h2><label for="s3_phone">Phone</label>' +
      '<input type="text" id="s3_phone" name="s3_phone">';
    history.pushState({}, '', location.pathname + '#step3');
  };
</script>
</body></html>
"""


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep test output readable

    def _json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/test-page.html"):
            # Served over http://127.0.0.1 rather than file:// (unlike chrome_load_test.py,
            # which never uses chrome.* extension APIs against the page at all): this test
            # drives the REAL extension pipeline — chrome.scripting.executeScript and
            # chrome.tabs.sendMessage against these tabs, and the panel's own canScript() gate
            # (http/https only, same rule the old popup used) — and file:// pages need a
            # separate "Allow access to file URLs" toggle that a fresh --load-extension profile
            # does not have, and that Playwright has no launch flag for. Serving over plain HTTP
            # sidesteps that entirely and matches how a real job-application page looks anyway.
            body = TEST_PAGE_BYTES
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/embed-wrapper.html"):
            body = WRAPPER_HTML_BYTES
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/cover-letter-page.html"):
            body = COVER_LETTER_PAGE_BYTES
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/remember-answers-page.html"):
            body = REMEMBER_ANSWERS_PAGE_BYTES
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/multi-step-page.html"):
            body = MULTI_STEP_PAGE_BYTES
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            self._json(200, {"tiers_available": ["test-stub"]})
        elif self.path == "/profile/counts":
            # 0/0 -> expandSections() skips "Add Another" entirely; this test doesn't need it.
            self._json(200, {"work_history": 0, "education": 0})
        elif self.path in ("/resume/info", "/resume"):
            self._json(404, {"detail": "no resume stored (test stub)"})
        else:
            self._json(404, {"detail": "not found"})

    def do_POST(self):
        if self.path == "/resolve":
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            fields = body.get("fields") or []
            resolve_calls.append((body.get("url"), [f.get("name") for f in fields]))
            fills, skipped = build_fills(fields)
            self._json(200, {"fills": fills, "skipped": skipped})
        elif self.path == "/cover-letter":
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            cover_letter_calls.append(body)
            # Which response to give is picked by a marker in the first url's OWN hash fragment
            # (see COVER_LETTER_PAGE_BYTES's comment above) rather than needing separate fixture
            # pages for the success/403/422 paths.
            first_url = (body.get("urls") or [""])[0]
            if "cl403" in first_url:
                self._json(403, {"detail": "cloud model not allowed on this computer yet"})
            elif "cl422" in first_url:
                self._json(422, {"detail": "couldn't find this job's description to write a letter against"})
            else:
                self._json(200, {
                    "text": "Dear Hiring Manager,\n\nI am excited to apply.\n\nSincerely,\nTest Applicant",
                    "warnings": ["mentions a specific salary figure"],
                    "draft": True,
                    "provider": "test-stub-llm",
                    "job": {"title": "Test Engineer", "company": "Great Company", "source": "page text"},
                })
        elif self.path == "/answers/learn":
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            items = body.get("items") or []
            answers_learn_calls.append(items)
            # Trivial stand-in for the real canary/screening rules (see answer_memory.py) —
            # just enough to prove BOTH a saved and a skipped item render distinctly, with the
            # skipped one's reason shown. Real classification is that Python module's job, not
            # this stub's — see docs/... / tests/test_extension_answer_memory.py for that.
            saved, skipped = [], []
            for it in items:
                q = it.get("question", "")
                if "salary" in q.lower():
                    skipped.append({"question": q, "reason": "test stub: comes from your profile"})
                else:
                    saved.append(q)
            self._json(200, {"saved": saved, "skipped": skipped})
        elif self.path == "/log":
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            log_calls.append(body)
            entry_id = f"log-{len(log_calls)}"
            self._json(200, {
                "id": entry_id, "url": body.get("url"), "title": body.get("title"),
                "company": body.get("company"), "status": "filled",
                "counts": body.get("counts"), "fills": 1,
            })
        elif self.path.startswith("/log/") and self.path.endswith("/status"):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            entry_id = self.path.split("/")[2]
            log_status_calls.append((entry_id, body.get("status")))
            self._json(200, {"ok": True})
        else:
            self._json(404, {"detail": "not found"})


stub_server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
stub_port = stub_server.server_address[1]
stub_thread = threading.Thread(target=stub_server.serve_forever, daemon=True)
stub_thread.start()
SERVICE_URL = f"http://127.0.0.1:{stub_port}"
PAGE_BASE = f"{SERVICE_URL}/test-page.html"
WRAPPER_URL = f"{SERVICE_URL}/embed-wrapper.html"


# ---------------------------------------------------------------------------
# helpers shared by the browser-driven checks below
# ---------------------------------------------------------------------------
def find_tab_id(helper_page, marker, timeout_s=5):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        tabs = helper_page.evaluate("async () => { const t = await chrome.tabs.query({}); return t.map(x => ({id: x.id, url: x.url})); }")
        for t in tabs:
            if t.get("url") and marker in t["url"]:
                return t["id"]
        time.sleep(0.1)
    return None


def get_state(helper_page, tab_id):
    key = f"fillState_{tab_id}"
    result = helper_page.evaluate("(k) => chrome.storage.session.get(k)", key)
    return result.get(key)


def wait_for_done(helper_page, tab_id, timeout_s=20, sample_progress=None):
    """Polls chrome.storage.session for this tab until status leaves 'running' (or the
    timeout elapses, which is itself a FAILURE this function reports via None — a broken
    per-field timeout/budget would otherwise hang this poll, and therefore this whole test
    script, forever). If `sample_progress` is a list, every distinct (current,total) pair seen
    while status == 'running' is appended to it, proving progress messages actually arrived."""
    deadline = time.time() + timeout_s
    last_progress = None
    while time.time() < deadline:
        state = get_state(helper_page, tab_id)
        if state and state.get("status") == "running":
            p = state.get("progress")
            if p and sample_progress is not None:
                key = (p.get("current"), p.get("total"), p.get("label"))
                if key != last_progress:
                    sample_progress.append(key)
                    last_progress = key
        elif state:
            return state
        time.sleep(0.05)
    return None


def inject_extension_files(helper_page, tab_id):
    helper_page.evaluate(
        "(tabId) => chrome.scripting.executeScript({ target: { tabId }, files: ['scanner.js', 'capture.js', 'content.js'] })",
        tab_id,
    )


def patch_hang_field(helper_page, tab_id, field_name):
    """Makes ApplyPilotScanner.applyFill() never resolve for one specific field (matched by its
    DOM name/id), leaving every other field's real behavior untouched. Runs in the SAME
    isolated world content.js uses (default `world` for executeScript), so this really does
    intercept the exact call content.js makes — it is not a separate copy of scanner.js."""
    helper_page.evaluate(
        """(args) => chrome.scripting.executeScript({
            target: { tabId: args.tabId },
            func: (fieldName) => {
                var orig = window.ApplyPilotScanner.applyFill;
                window.ApplyPilotScanner.applyFill = function (entry, value) {
                    var el = entry.el || entry.input || entry.button;
                    var name = el && (el.name || el.id);
                    if (name === fieldName) return new Promise(function () {});
                    return orig(entry, value);
                };
            },
            args: [args.fieldName],
        })""",
        {"tabId": tab_id, "fieldName": field_name},
    )


def patch_delay_every_field(helper_page, tab_id, delay_ms):
    """Slows down EVERY field's fill by a fixed amount (still eventually resolving, using the
    real applyFill underneath) so a Cancel sent shortly after START_FILL has a wide, reliable
    window to land mid-fill instead of racing a fill that might finish in a few milliseconds."""
    helper_page.evaluate(
        """(args) => chrome.scripting.executeScript({
            target: { tabId: args.tabId },
            func: (delayMs) => {
                var orig = window.ApplyPilotScanner.applyFill;
                window.ApplyPilotScanner.applyFill = function (entry, value) {
                    return new Promise(function (resolve, reject) {
                        setTimeout(function () {
                            Promise.resolve(orig(entry, value)).then(resolve, reject);
                        }, delayMs);
                    });
                };
            },
            args: [args.delayMs],
        })""",
        {"tabId": tab_id, "delayMs": delay_ms},
    )


def patch_resume_with_ats_prefill(helper_page, tab_id, field_name, prefill_value):
    """Simulates an ATS (Lever/Ashby/Workday) parsing an uploaded résumé and repopulating a
    field a moment after upload — exactly the scenario the résumé-first ordering creates a risk
    for (see content.js's isProtectedByPriorUserActivity() doc comment). Monkeypatches
    ApplyPilotScanner.attachResumeFile to write `prefillValue` into `fieldName` as a side effect
    (mirroring what the ATS's own JS would do) and report a normal successful attach; also
    monkeypatches chrome.runtime.sendMessage so GET_RESUME resolves with a fake résumé instead of
    the stub's normal 404, since attachResumeFile is never even called without one. Both patches
    apply only to this one tab's default isolated world — the same world content.js runs in."""
    helper_page.evaluate(
        """(args) => chrome.scripting.executeScript({
            target: { tabId: args.tabId },
            func: (fieldName, prefillValue) => {
                var origSend = chrome.runtime.sendMessage.bind(chrome.runtime);
                chrome.runtime.sendMessage = function (msg) {
                    if (msg && msg.type === 'GET_RESUME') {
                        return Promise.resolve({ ok: true, data: {
                            filename: 'resume.pdf', contentType: 'application/pdf', size: 4,
                            base64: btoa('PDF!')
                        } });
                    }
                    return origSend.apply(null, arguments);
                };
                window.ApplyPilotScanner.attachResumeFile = function (doc) {
                    var el = doc.querySelector('[name="' + fieldName + '"]');
                    if (el) {
                        var desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
                        if (desc && desc.set) desc.set.call(el, prefillValue); else el.value = prefillValue;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    return Promise.resolve({ attempted: true, attached: true, filename: 'resume.pdf' });
                };
            },
            args: [args.fieldName, args.prefillValue],
        })""",
        {"tabId": tab_id, "fieldName": field_name, "prefillValue": prefill_value},
    )


def patch_revert_after_fill(helper_page, tab_id, field_name, delay_ms):
    """Makes ONE specific field silently revert to empty shortly after being filled — simulating
    a controlled-input re-render or a combobox clearing its own search text — so the post-fill
    verify step (content.js's verifyAppliedFills(), ~500ms after the apply loop finishes) has
    something real to catch. Every other field fills normally through the real applyFill."""
    helper_page.evaluate(
        """(args) => chrome.scripting.executeScript({
            target: { tabId: args.tabId },
            func: (fieldName, delayMs) => {
                var orig = window.ApplyPilotScanner.applyFill;
                window.ApplyPilotScanner.applyFill = function (entry, value) {
                    var ok = orig(entry, value);
                    var el = entry.el;
                    var name = el && (el.name || el.id);
                    if (name === fieldName && el) {
                        setTimeout(function () {
                            var desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
                            if (desc && desc.set) desc.set.call(el, ''); else el.value = '';
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                        }, delayMs);
                    }
                    return ok;
                };
            },
            args: [args.fieldName, args.delayMs],
        })""",
        {"tabId": tab_id, "fieldName": field_name, "delayMs": delay_ms},
    )


def start_fill_via_message(helper_page, tab_id, field_timeout_ms=None, budget_ms=None):
    # Goes to background.js (RUN_FILL), not directly to content.js (START_FILL no longer
    # exists) — background.js is the cross-frame coordinator now: it enumerates the tab's
    # frames itself via chrome.webNavigation, so this only needs the tabId. See
    # background.js's runFillForTab() and its file header.
    return helper_page.evaluate(
        """(args) => chrome.tabs.get(args.tabId).then(tab => chrome.runtime.sendMessage({
            type: 'RUN_FILL', tabId: args.tabId, url: tab.url,
            fieldTimeoutMs: args.fieldTimeoutMs, budgetMs: args.budgetMs
        }))""",
        {"tabId": tab_id, "fieldTimeoutMs": field_timeout_ms, "budgetMs": budget_ms},
    )


def submission_counters(page):
    return page.evaluate("() => ({ form: !!window.__FORM_SUBMITTED__, wd: window.__WD_SUBMIT_COUNT__ || 0, url: location.href })")


# ---------------------------------------------------------------------------
# 2. the real browser
# ---------------------------------------------------------------------------
user_dir = tempfile.mkdtemp(prefix="apc-panel-")
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_dir,
        headless=False,
        args=[
            f"--disable-extensions-except={EXT}",
            f"--load-extension={EXT}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )
    try:
        # service worker lookup — same technique as chrome_load_test.py.
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
        check("MV3 service worker registered (manifest accepted by Chrome)", sw is not None,
              sw.url if sw else "none")
        if not sw:
            raise SystemExit("cannot continue without the extension's service worker")
        ext_id = sw.url.split("/")[2]
        panel_url = f"chrome-extension://{ext_id}/sidepanel.html"

        # --- helper: an ordinary (unpinned) panel page used purely to drive chrome.* API
        #     calls this script needs (tab lookups, storage reads, direct messaging) — using
        #     the SAME extension surface real users see, not a privileged test-only API.
        helper = ctx.new_page()
        helper.goto(panel_url)
        helper.wait_for_function("() => window.__applyPilotPanelReady === true", timeout=5000)
        check("the panel page loads and its init() completes", True)
        check("panel page has the expected controls (Fill/Cancel/Undo/Report)",
              helper.eval_on_selector_all(
                  "#scanBtn, #cancelBtn, #undoBtn, #reportBtn", "els => els.length") == 4)
        check("panel title is ApplyPilot Copilot", helper.title() == "ApplyPilot Copilot", helper.title())

        # =====================================================================
        # AUTO-CONNECT (item 1), cold start: nothing configured yet, and no
        # `applypilot extension install-host` native host is registered on this machine for
        # com.applypilot.copilot. That second fact was confirmed by hand against the real Windows
        # registry before this test was written (HKCU\...\NativeMessagingHosts\com.applypilot.copilot
        # is absent by default), so chrome.runtime.sendNativeMessage below gives Chrome's REAL
        # "native messaging host not found" answer — this proves the fallback path end to end
        # rather than mocking chrome.runtime.sendNativeMessage. serviceUrl is pointed at a port
        # nothing is listening on (grabbed, then immediately closed) instead of the default 8787,
        # so this can never accidentally reach a real `applypilot serve-extension` a developer
        # happens to have running on this machine.
        # =====================================================================
        def _unused_local_port():
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            return port

        UNREACHABLE_URL = f"http://127.0.0.1:{_unused_local_port()}"
        helper.evaluate(
            "(cfg) => chrome.storage.local.set(cfg).then(() => "
            "chrome.storage.local.remove(['token', 'serviceConnection']))",
            {"serviceUrl": UNREACHABLE_URL},
        )

        panel0 = ctx.new_page()
        panel0.goto(panel_url)
        panel0.wait_for_function("() => window.__applyPilotPanelReady === true", timeout=5000)
        try:
            panel0.wait_for_function(
                "() => (document.getElementById('tiersLine').textContent || "
                "'').toLowerCase().includes('install-host')",
                timeout=8000)
            tip_shown = True
        except Exception:
            tip_shown = False
        tip_text = panel0.eval_on_selector("#tiersLine", "el => el.textContent")
        check("the panel shows the install-host tip when nothing is configured and the native "
              "host is missing", tip_shown, repr(tip_text))

        conn = helper.evaluate("() => chrome.storage.local.get('serviceConnection')")
        conn_val = (conn or {}).get("serviceConnection") or {}
        check("a missing native host is recorded honestly (mode manual, ok false) in "
              "chrome.storage.local.serviceConnection",
              conn_val.get("mode") == "manual" and conn_val.get("ok") is False, json.dumps(conn_val))
        check("the recorded error names Chrome's own native-messaging-host-not-found failure",
              "native messaging host" in (conn_val.get("error") or "").lower(), json.dumps(conn_val))
        panel0.close()

        # Real config for every test below this point — auto-connect never runs again once a
        # token is already stored and calls keep succeeding (see requestWithAutoConnect).
        helper.evaluate(
            "(cfg) => chrome.storage.local.set(cfg)",
            {"serviceUrl": SERVICE_URL, "token": "test-token"},
        )

        # Record every chrome.storage.session write, keyed by tabId, straight from
        # chrome.storage.onChanged — the SAME mechanism the real panel uses to render live
        # progress. This is more reliable than polling on a Python timer (which can straddle
        # gaps a fast, all-synchronous-widget fill can slip through entirely) and it is a more
        # direct proof that "content.js reports progress ... the panel renders it live" than a
        # sampled poll would be.
        helper.evaluate("""() => {
            window.__stateLog = {};
            chrome.storage.onChanged.addListener((changes, area) => {
                if (area !== 'session') return;
                for (const key of Object.keys(changes)) {
                    const m = /^fillState_(\\d+)$/.exec(key);
                    if (!m) continue;
                    const tabId = Number(m[1]);
                    (window.__stateLog[tabId] = window.__stateLog[tabId] || []).push(changes[key].newValue);
                }
            });
        }""")

        # =====================================================================
        # TAB 1 — basic fill through the REAL panel button, proving per-tab state
        #         and live progress land in chrome.storage.session.
        # =====================================================================
        tab1 = ctx.new_page()
        tab1.goto(PAGE_BASE + "#t=1")
        tab1_id = find_tab_id(helper, "#t=1")
        check("found tab 1's chrome tab id", tab1_id is not None)

        panel1 = ctx.new_page()
        panel1.goto(f"{panel_url}?tabId={tab1_id}")
        panel1.wait_for_function("() => window.__applyPilotPanelReady === true", timeout=5000)

        panel1.click("#scanBtn")
        state1 = wait_for_done(helper, tab1_id, timeout_s=60)

        check("tab 1's fill reached the local (stub) service", len(resolve_calls) >= 1)
        check("tab 1's fill finished and wrote a per-tab state", state1 is not None, str(state1)[:200])
        if state1:
            check("tab 1 state status is 'done'", state1.get("status") == "done", state1.get("status"))
            check("tab 1 state has counts.filled > 0", (state1.get("counts") or {}).get("filled", 0) > 0,
                  json.dumps(state1.get("counts")))
            check("tab 1 state recorded the page URL it ran against",
                  isinstance(state1.get("url"), str) and "#t=1" in state1["url"], state1.get("url"))
            check("tab 1 state marks undo as available", state1.get("undoAvailable") is True)

        log1 = helper.evaluate("(tabId) => (window.__stateLog && window.__stateLog[tabId]) || []", tab1_id)
        running_ticks = [s for s in log1 if s and s.get("status") == "running" and s.get("progress")]
        distinct_ticks = sorted(set((s["progress"].get("current"), s["progress"].get("total")) for s in running_ticks))
        check("chrome.storage.onChanged fired multiple times while tab 1's fill was running (live progress)",
              len(running_ticks) >= 2, f"{len(log1)} total state writes, {len(running_ticks)} while running")
        check("tab 1's progress ticks actually advanced (current increased across writes)",
              len(distinct_ticks) >= 2, str(distinct_ticks[:8]))

        # The real panel button actually rendered something from that state (not just storage).
        summary_text_1 = panel1.eval_on_selector("#fillSummary", "el => el.textContent")
        check("panel 1 rendered a non-empty fill summary after clicking Fill",
              bool(summary_text_1 and summary_text_1.strip()), repr(summary_text_1))

        # Report page: gated on the SAME chrome.permissions check as Fill (see gatePermissions()
        # in sidepanel.js) — clicked here through the real button, on the already-open tab 1
        # panel, whose origin is already granted from the Fill click above.
        try:
            with panel1.expect_download(timeout=5000) as download_info:
                panel1.click("#reportBtn")
            download = download_info.value
            check("clicking Report page triggers a real download of the page structure",
                  download.suggested_filename.startswith("applypilot-page-"), download.suggested_filename)
        except Exception as e:
            check("clicking Report page triggers a real download of the page structure", False, str(e))
        report_status = panel1.eval_on_selector("#statusBox", "el => el.textContent")
        check("panel shows the 'saved the page structure' confirmation after Report",
              "saved the page structure" in (report_status or "").lower(), repr(report_status))

        # =====================================================================
        # TAB 2 — a second tab must get its OWN independent state; filling it must not
        #         disturb tab 1's already-stored result.
        # =====================================================================
        tab2 = ctx.new_page()
        tab2.goto(PAGE_BASE + "#t=2")
        tab2_id = find_tab_id(helper, "#t=2")
        check("found tab 2's chrome tab id, distinct from tab 1's", tab2_id is not None and tab2_id != tab1_id)

        panel2 = ctx.new_page()
        panel2.goto(f"{panel_url}?tabId={tab2_id}")
        panel2.wait_for_function("() => window.__applyPilotPanelReady === true", timeout=5000)
        panel2.click("#scanBtn")
        state2 = wait_for_done(helper, tab2_id, timeout_s=60)
        check("tab 2 independently reached 'done'", bool(state2 and state2.get("status") == "done"),
              str(state2)[:200])

        state1_again = get_state(helper, tab1_id)
        check("tab 1's stored state is untouched by tab 2's fill",
              state1_again == state1, "tab1 state changed after tab2 ran!" if state1_again != state1 else "")

        # =====================================================================
        # TAB 3 — a field whose fill NEVER resolves must be reported as timed out,
        #         without stalling the rest of the fill.
        # =====================================================================
        tab3 = ctx.new_page()
        tab3.goto(PAGE_BASE + "#t=3")
        tab3_id = find_tab_id(helper, "#t=3")
        check("found tab 3's chrome tab id", tab3_id is not None)
        inject_extension_files(helper, tab3_id)
        patch_hang_field(helper, tab3_id, "full_name")

        t0 = time.time()
        start_resp = start_fill_via_message(helper, tab3_id, field_timeout_ms=500, budget_ms=15000)
        check("tab 3's START_FILL was acknowledged immediately (does not await the fill)",
              bool(start_resp and start_resp.get("ok")), json.dumps(start_resp))
        state3 = wait_for_done(helper, tab3_id, timeout_s=15)
        elapsed3 = time.time() - t0

        check("a field that never resolves did not hang the fill (finished well under the 15s poll cap)",
              state3 is not None and elapsed3 < 8, f"elapsed={elapsed3:.2f}s state={str(state3)[:150]}")
        if state3:
            timed_out = [f for f in (state3.get("failed") or []) if "timed out" in (f.get("reason") or "").lower()]
            check("the hung field is reported as failed with a 'timed out' reason",
                  len(timed_out) >= 1, json.dumps(state3.get("failed")))
            check("other fields still got filled despite the one stuck field",
                  (state3.get("counts") or {}).get("filled", 0) > 0, json.dumps(state3.get("counts")))
            check("tab 3's fill still reached a terminal status", state3.get("status") in ("done", "error"),
                  state3.get("status"))

        # =====================================================================
        # TAB 4 — Cancel must stop the fill between fields; already-filled fields stay filled.
        # =====================================================================
        tab4 = ctx.new_page()
        tab4.goto(PAGE_BASE + "#t=4")
        tab4_id = find_tab_id(helper, "#t=4")
        check("found tab 4's chrome tab id", tab4_id is not None)
        inject_extension_files(helper, tab4_id)
        patch_delay_every_field(helper, tab4_id, 150)  # ~150ms per field -> a wide, reliable cancel window

        # How many fields WOULD be attempted, so "cancelled substantially early" is a real
        # assertion and not just "finished at all".
        scanned = helper.evaluate(
            "(tabId) => chrome.scripting.executeScript({ target: { tabId }, func: () => ApplyPilotScanner.scanAll(document).fields.length })",
            tab4_id,
        )
        total_fields_4 = (scanned or [{}])[0].get("result") if scanned else None

        t1 = time.time()
        start_fill_via_message(helper, tab4_id, field_timeout_ms=5000, budget_ms=30000)
        time.sleep(0.35)  # let a couple of (150ms-delayed) fields land before cancelling
        # Via background.js's CANCEL_FILL_TAB (what the real Cancel button now sends), not a
        # direct chrome.tabs.sendMessage to content.js — proves the fan-out path itself works,
        # not just the single-frame CANCEL_FILL handler underneath it.
        cancel_resp = helper.evaluate(
            "(tabId) => chrome.runtime.sendMessage({ type: 'CANCEL_FILL_TAB', tabId })", tab4_id)
        check("CANCEL_FILL was acknowledged", bool(cancel_resp and cancel_resp.get("ok")), json.dumps(cancel_resp))
        state4 = wait_for_done(helper, tab4_id, timeout_s=15)
        elapsed4 = time.time() - t1

        uninterrupted_estimate = (total_fields_4 or 20) * 0.15
        check("cancelling finished well before the fill would have on its own",
              state4 is not None and elapsed4 < max(1.0, uninterrupted_estimate * 0.75),
              f"elapsed={elapsed4:.2f}s uninterrupted_estimate={uninterrupted_estimate:.2f}s total_fields={total_fields_4}")
        if state4:
            check("cancelled fill reports status 'cancelled'", state4.get("status") == "cancelled", state4.get("status"))
            filled_n = (state4.get("counts") or {}).get("filled", 0)
            check("some fields were already filled before cancelling, and stayed filled",
                  filled_n > 0, json.dumps(state4.get("counts")))
            if total_fields_4:
                check("cancelling stopped before every field was attempted",
                      filled_n < total_fields_4, f"filled={filled_n} total={total_fields_4}")
            cancelled_reasons = [f for f in (state4.get("failed") or []) if "cancel" in (f.get("reason") or "").lower()]
            check("remaining fields are reported as not attempted — cancelled",
                  len(cancelled_reasons) >= 1, json.dumps(state4.get("failed"))[:300])

        # =====================================================================
        # TAB 5 — FILL EVERY FRAME: a page on ONE 127.0.0.1 port embedding a form page served
        #         from a DIFFERENT 127.0.0.1 port (a different origin — see the InnerFormHandler
        #         comment above), through the REAL panel button end to end: permission gate,
        #         allFrames injection, one merged /resolve call, per-frame apply. Proves both
        #         the outer page's own field and the cross-origin iframe's fields are scanned,
        #         filled and reported, with zero submissions in EITHER frame.
        # =====================================================================
        resolve_calls_before_tab5 = len(resolve_calls)
        tab5 = ctx.new_page()
        tab5.goto(WRAPPER_URL + "#t=5")
        tab5_id = find_tab_id(helper, "#t=5")
        check("found tab 5's chrome tab id", tab5_id is not None)

        inner_frame = next((f for f in tab5.frames if f.url.startswith(INNER_URL)), None)
        check("the cross-origin iframe actually loaded as a separate frame",
              inner_frame is not None, str([f.url for f in tab5.frames]))

        panel5 = ctx.new_page()
        panel5.goto(f"{panel_url}?tabId={tab5_id}")
        panel5.wait_for_function("() => window.__applyPilotPanelReady === true", timeout=5000)

        panel5.click("#scanBtn")
        state5 = wait_for_done(helper, tab5_id, timeout_s=30)
        check("tab 5 (cross-origin iframe page) fill reached a terminal status",
              state5 is not None and state5.get("status") == "done", str(state5)[:200])

        resolve_delta = len(resolve_calls) - resolve_calls_before_tab5
        check("exactly ONE /resolve call served the whole page (outer field + iframe merged)",
              resolve_delta == 1, f"resolve calls delta = {resolve_delta}")
        if resolve_delta >= 1:
            _, names_seen = resolve_calls[-1]
            check("the single /resolve call's fields include the OUTER (top) frame's own field",
                  "outer_only_field" in names_seen, str(names_seen)[:200])
            check("the single /resolve call's fields include the CROSS-ORIGIN IFRAME's fields",
                  "full_name" in names_seen and "email" in names_seen, str(names_seen)[:200])

        outer_val = tab5.eval_on_selector("#outer_only_field", "el => el.value")
        check("the outer (top) page's own field was filled", bool(outer_val), repr(outer_val))
        if inner_frame:
            inner_val = inner_frame.eval_on_selector("#full_name", "el => el.value")
            check("the cross-origin iframe's field was filled", bool(inner_val), repr(inner_val))

        if state5:
            check("tab 5's combined counts include fields filled in BOTH frames",
                  (state5.get("counts") or {}).get("filled", 0) >= 2, json.dumps(state5.get("counts")))
            check("tab 5 marks undo as available (fields were filled in at least one frame)",
                  state5.get("undoAvailable") is True)

        summary_text_5 = panel5.eval_on_selector("#fillSummary", "el => el.textContent")
        check("panel 5 rendered a non-empty combined fill summary across both frames",
              bool(summary_text_5 and summary_text_5.strip()), repr(summary_text_5))

        # Zero submissions in EITHER frame — the whole point of this test.
        outer_submitted = tab5.evaluate("() => !!window.__FORM_SUBMITTED__")
        check("tab 5: the OUTER (top) frame was never submitted", outer_submitted is False)
        if inner_frame:
            inner_counters = inner_frame.evaluate(
                "() => ({ form: !!window.__FORM_SUBMITTED__, wd: window.__WD_SUBMIT_COUNT__ || 0 })")
            check("tab 5: the cross-origin IFRAME's native form was never submitted",
                  inner_counters["form"] is False, json.dumps(inner_counters))
            check("tab 5: the cross-origin IFRAME's Workday submit control was never triggered",
                  inner_counters["wd"] == 0, json.dumps(inner_counters))
        check("tab 5: the outer page never navigated away", tab5.url.startswith(WRAPPER_URL), tab5.url)
        if inner_frame:
            check("tab 5: the iframe never navigated away", inner_frame.url.startswith(INNER_URL), inner_frame.url)

        # Undo must reach both frames too and report a combined, VERIFIED restore count.
        undo_resp5 = helper.evaluate(
            "(tabId) => chrome.runtime.sendMessage({ type: 'UNDO_TAB', tabId })", tab5_id)
        check("tab 5: UNDO_TAB restored fields across both frames",
              bool(undo_resp5 and undo_resp5.get("restored", 0) >= 2), json.dumps(undo_resp5))
        outer_after_undo = tab5.eval_on_selector("#outer_only_field", "el => el.value")
        check("tab 5: undo cleared the outer field back to empty", outer_after_undo == "", repr(outer_after_undo))

        # =====================================================================
        # TAB 6 — NEVER OVERWRITE THE USER (but DO correct the résumé's own guess), and DIDN'T
        #         STICK verification. Two independent behaviours proven on one page to keep the
        #         browser count down:
        #           (a) a value already on the page BEFORE the fill started (typed by the
        #               operator, or a Workday-style account prefill) is left alone and reported
        #               "kept your value";
        #           (b) a value that appears ONLY because of OUR OWN résumé attach (an ATS's own,
        #               possibly-wrong, résumé parse) is NOT protected — the real profile value
        #               still overwrites it;
        #           (c) a field that silently reverts shortly after being filled (a controlled-
        #               input re-render) is caught by the post-fill verify step and reported
        #               "didn't stick", not counted as Filled.
        # =====================================================================
        tab6 = ctx.new_page()
        tab6.goto(PAGE_BASE + "#t=6")
        tab6_id = find_tab_id(helper, "#t=6")
        check("found tab 6's chrome tab id", tab6_id is not None)
        inject_extension_files(helper, tab6_id)

        tab6.fill("[name='phone']", "555-000-1111")  # predates the fill entirely — must be protected
        patch_resume_with_ats_prefill(helper, tab6_id, "full_name", "ATS Wrong Name")
        patch_revert_after_fill(helper, tab6_id, "linkedin", 100)

        start_fill_via_message(helper, tab6_id)
        state6 = wait_for_done(helper, tab6_id, timeout_s=60)
        check("tab 6's fill reached a terminal status", state6 is not None and state6.get("status") == "done",
              str(state6)[:200])

        phone_val = tab6.eval_on_selector("[name='phone']", "el => el.value")
        check("a value already on the page before the fill started is KEPT, not overwritten",
              phone_val == "555-000-1111", repr(phone_val))
        if state6:
            kept = [f for f in (state6.get("needsYou") or []) if "kept your value" in (f.get("reason") or "")]
            check("the kept-value field is reported as 'kept your value', not silently dropped",
                  len(kept) >= 1, json.dumps(state6.get("needsYou"))[:300])

        full_name_val = tab6.eval_on_selector("#full_name", "el => el.value")
        check("a value that only appeared because of OUR OWN résumé attach is NOT protected "
              "(the real profile value still overwrote the ATS's own guess)",
              full_name_val != "ATS Wrong Name" and bool(full_name_val), repr(full_name_val))
        check("tab 6's patched résumé attach actually ran (attached: true)",
              bool((state6 or {}).get("resume", {}).get("attached")), json.dumps((state6 or {}).get("resume")))

        linkedin_val = tab6.eval_on_selector("[name='linkedin']", "el => el.value")
        check("a field that silently reverted after being filled reads back EMPTY (it really did revert)",
              linkedin_val == "", repr(linkedin_val))
        if state6:
            didnt_stick = [f for f in (state6.get("failed") or []) if "didn't stick" in (f.get("reason") or "").lower()]
            check("the reverted field is reported as \"didn't stick\", not counted as Filled",
                  len(didnt_stick) >= 1, json.dumps(state6.get("failed"))[:300])
            filled_names = json.dumps(state6.get("filled"))
            check("the reverted field never appears in the Filled list",
                  "linkedin" not in filled_names.lower() or not didnt_stick, filled_names[:300])

        # =====================================================================
        # TAB 7 — HONEST "COULDN'T READ": a frame webNavigation finds but this run never
        #         actually got into (simulated here by injecting ONLY the top frame, standing in
        #         for "no host permission for this one" / "injection raced a navigation") must be
        #         counted toward couldNotRead/skippedFrames — and must simply be left alone, not
        #         guessed at — while the frame(s) that DID get in still fill normally.
        # =====================================================================
        tab7 = ctx.new_page()
        tab7.goto(WRAPPER_URL + "#t=7")
        tab7_id = find_tab_id(helper, "#t=7")
        check("found tab 7's chrome tab id", tab7_id is not None)
        # Deliberately top-frame-only injection (frameIds: [0]), NOT allFrames — the iframe is
        # left with no content.js at all, so background.js's PREPARE_AND_SCAN to it must fail.
        helper.evaluate(
            "(tabId) => chrome.scripting.executeScript({ target: { tabId, frameIds: [0] }, files: ['scanner.js', 'capture.js', 'content.js'] })",
            tab7_id,
        )
        start_fill_via_message(helper, tab7_id)
        state7 = wait_for_done(helper, tab7_id, timeout_s=60)
        check("tab 7's fill reached a terminal status", state7 is not None and state7.get("status") == "done",
              str(state7)[:200])
        if state7:
            check("tab 7: the frame we could not inject counts toward couldNotRead",
                  (state7.get("couldNotRead") or 0) >= 1, json.dumps(state7))
            check("tab 7: the frame we could not inject counts toward skippedFrames",
                  (state7.get("skippedFrames") or 0) >= 1, json.dumps(state7))
            check("tab 7: the top frame (which WAS injected) still filled normally",
                  (state7.get("counts") or {}).get("filled", 0) >= 1, json.dumps(state7.get("counts")))
        outer7_val = tab7.eval_on_selector("#outer_only_field", "el => el.value")
        check("tab 7: the injected top frame's own field was filled", bool(outer7_val), repr(outer7_val))
        inner7_frame = next((f for f in tab7.frames if f.url.startswith(INNER_URL)), None)
        if inner7_frame:
            inner7_val = inner7_frame.eval_on_selector("#full_name", "el => el.value")
            check("tab 7: the un-injected iframe's field was correctly left untouched, not guessed at",
                  inner7_val in ("", None), repr(inner7_val))

        # =====================================================================
        # TAB 8 — DRAFT COVER LETTER (item 2), success path through the REAL panel button: page
        #         text extraction (excludes form fields, includes real visible copy), the
        #         DRAFT box, Copy/Download availability, the Insert button appearing only because
        #         this fixture has a cover-letter-labelled textarea, insertion through the normal
        #         guarded fill path (highlighted 'draft', reported in state, undoable).
        # =====================================================================
        tab8 = ctx.new_page()
        tab8.goto(f"{SERVICE_URL}/cover-letter-page.html#t=8")
        tab8_id = find_tab_id(helper, "#t=8")
        check("found tab 8's chrome tab id", tab8_id is not None)

        panel8b = ctx.new_page()
        panel8b.goto(f"{panel_url}?tabId={tab8_id}")
        panel8b.wait_for_function("() => window.__applyPilotPanelReady === true", timeout=5000)

        panel8b.click("#coverLetterBtn")
        panel8b.wait_for_function("() => !document.getElementById('coverLetterBox').hidden", timeout=10000)
        draft_text = panel8b.eval_on_selector("#coverLetterText", "el => el.value")
        check("the draft box shows the service's returned draft text",
              "excited to apply" in (draft_text or ""), repr(draft_text))
        job_line = panel8b.eval_on_selector("#coverLetterJob", "el => el.textContent")
        check("the draft box names the job/company the service identified",
              "Test Engineer" in (job_line or "") and "Great Company" in (job_line or ""), repr(job_line))
        check("a non-empty warnings list from the service is shown, not hidden",
              panel8b.eval_on_selector("#coverLetterWarnings", "el => el.hidden") is False)
        check("the Insert button appears because this fixture has a cover-letter-labelled textarea",
              panel8b.eval_on_selector("#coverLetterInsertBtn", "el => el.hidden") is False)

        check("exactly one /cover-letter call was made, naming this tab's URL",
              len(cover_letter_calls) == 1 and "cover-letter-page.html" in (cover_letter_calls[0].get("urls") or [""])[0],
              json.dumps(cover_letter_calls[-1]) if cover_letter_calls else "none")
        sent_text = cover_letter_calls[0].get("page_text", "") if cover_letter_calls else ""
        check("the extracted page text includes the page's own visible copy",
              "fantastic engineer" in sent_text, sent_text[:200])
        check("the extracted page text excludes form-field content (e.g. the textarea's own name/id)",
              "cover_letter" not in sent_text, sent_text[:200])

        panel8b.click("#coverLetterInsertBtn")
        panel8b.wait_for_function(
            "() => (document.getElementById('coverLetterStatus').textContent || "
            "'').toLowerCase().includes('inserted')", timeout=5000)
        inserted_value = tab8.eval_on_selector("#cl", "el => el.value")
        check("Insert wrote the draft text into the page's own cover-letter textarea",
              inserted_value == draft_text and bool(inserted_value), repr(inserted_value)[:200])
        highlight_kind = tab8.eval_on_selector("#cl", "el => el.getAttribute('data-applypilot-highlighted')")
        check("the inserted field is highlighted as a DRAFT (blue), never a plain fact-fill (green)",
              highlight_kind == "draft", repr(highlight_kind))

        state8 = get_state(helper, tab8_id)
        check("inserting the cover letter is reported through the normal per-tab state "
              "(counts.drafts, undoAvailable) — same shape a fill's own drafts use",
              bool(state8) and (state8.get("counts") or {}).get("drafts", 0) >= 1 and state8.get("undoAvailable") is True,
              json.dumps(state8))

        undo8 = helper.evaluate("(tabId) => chrome.runtime.sendMessage({ type: 'UNDO_TAB', tabId })", tab8_id)
        check("UNDO_TAB restores an inserted cover-letter draft exactly like any other filled field",
              bool(undo8 and undo8.get("restored", 0) >= 1), json.dumps(undo8))
        after_undo8 = tab8.eval_on_selector("#cl", "el => el.value")
        check("after Undo, the cover-letter textarea is back to empty", after_undo8 == "", repr(after_undo8))
        panel8b.close()

        # =====================================================================
        # TAB 9 / 10 — 403 and 422 from /cover-letter are shown to the operator VERBATIM (the
        #              service's own `.detail` text), never a generic failure message, and no
        #              draft box is shown.
        # =====================================================================
        for marker, code, detail in (
            ("cl403", 403, "cloud model not allowed on this computer yet"),
            ("cl422", 422, "couldn't find this job's description to write a letter against"),
        ):
            tab_err = ctx.new_page()
            tab_err.goto(f"{SERVICE_URL}/cover-letter-page.html#t={marker}")
            tab_err_id = find_tab_id(helper, f"#t={marker}")
            check(f"found the {code} cover-letter test tab's chrome tab id", tab_err_id is not None)
            panel_err = ctx.new_page()
            panel_err.goto(f"{panel_url}?tabId={tab_err_id}")
            panel_err.wait_for_function("() => window.__applyPilotPanelReady === true", timeout=5000)
            panel_err.click("#coverLetterBtn")
            # Waits for the DETAIL text specifically (not just "any non-empty status"), since the
            # transient "Drafting…" status is ALSO non-empty and would otherwise race this check.
            try:
                panel_err.wait_for_function(
                    "(needle) => (document.getElementById('coverLetterStatus').textContent || "
                    "'').includes(needle)",
                    arg=detail, timeout=10000)
            except Exception:
                pass
            status_text = panel_err.eval_on_selector("#coverLetterStatus", "el => el.textContent")
            check(f"a {code} from /cover-letter is shown to the operator VERBATIM (the service's own detail)",
                  detail in (status_text or ""), repr(status_text))
            check(f"no draft box is shown after a {code} error",
                  panel_err.eval_on_selector("#coverLetterBox", "el => el.hidden") is True)
            panel_err.close()
            tab_err.close()

        # =====================================================================
        # TAB 11 — REMEMBER MY ANSWERS (item 3): the box only appears once a fill left something
        #          for the operator; clicking it before typing anything reports nothing to
        #          remember (and makes no service call); after typing real answers, exactly one
        #          /answers/learn call carries the CURRENT values keyed by each field's own
        #          label, a password field is NEVER included even though it was also left as
        #          "needs you", and the panel shows both a saved and a skipped result (with its
        #          reason) distinctly.
        # =====================================================================
        tab11 = ctx.new_page()
        tab11.goto(f"{SERVICE_URL}/remember-answers-page.html#t=11")
        tab11_id = find_tab_id(helper, "#t=11")
        check("found tab 11's chrome tab id", tab11_id is not None)

        panel11 = ctx.new_page()
        panel11.goto(f"{panel_url}?tabId={tab11_id}")
        panel11.wait_for_function("() => window.__applyPilotPanelReady === true", timeout=5000)
        panel11.click("#scanBtn")
        state11 = wait_for_done(helper, tab11_id, timeout_s=60)
        check("tab 11's fill reached a terminal status",
              state11 is not None and state11.get("status") == "done", str(state11)[:200])
        check("the Remember my answers box is shown because this fill left fields for the operator",
              panel11.eval_on_selector("#rememberBox", "el => el.hidden") is False)

        panel11.click("#rememberBtn")
        panel11.wait_for_function(
            "() => !document.getElementById('rememberStatus').hidden && "
            "document.getElementById('rememberStatus').textContent.length > 0", timeout=5000)
        empty_status = panel11.eval_on_selector("#rememberStatus", "el => el.textContent")
        check("clicking Remember before typing any answers reports nothing to remember yet",
              "nothing to remember" in (empty_status or "").lower(), repr(empty_status))
        check("no /answers/learn call was made when nothing had been typed",
              len(answers_learn_calls) == 0, json.dumps(answers_learn_calls))

        # Simulate the operator answering the fields a fill left blank, AND typing into the
        # password field too (which must never be sent — see content.js's readFieldsForAnswers()).
        tab11.fill("#notice_period", "Two weeks")
        tab11.fill("#salary_expect", "120000")
        tab11.fill("#fake_password", "hunter2")

        panel11.click("#rememberBtn")
        panel11.wait_for_function(
            "() => (document.getElementById('rememberStatus').textContent || "
            "'').toLowerCase().includes('saved')", timeout=5000)
        remember_status = panel11.eval_on_selector("#rememberStatus", "el => el.textContent")
        check("the panel reports how many answers were saved and how many were skipped",
              "Saved 1" in (remember_status or "") and "skipped 1" in (remember_status or ""),
              repr(remember_status))

        check("exactly one /answers/learn call was made", len(answers_learn_calls) == 1,
              json.dumps(answers_learn_calls))
        qas = {i["question"]: i["answer"] for i in answers_learn_calls[0]} if answers_learn_calls else {}
        check("the notice-period answer sent matches what was typed after the fill",
              qas.get("What is your notice period?") == "Two weeks", json.dumps(qas))
        check("the salary answer sent matches what was typed after the fill",
              qas.get("Desired salary") == "120000", json.dumps(qas))
        check("the password field's value was NEVER sent to /answers/learn, even though it was "
              "also left as \"needs you\" and had text typed into it",
              "hunter2" not in json.dumps(qas) and len(qas) == 2, json.dumps(qas))

        details_text = panel11.eval_on_selector("#rememberDetails", "el => el.textContent")
        check("the details list shows the saved question",
              "What is your notice period?" in (details_text or ""), repr(details_text))
        check("the details list shows the skipped question together with its reason",
              "Desired salary" in (details_text or "") and "profile" in (details_text or ""),
              repr(details_text))

        # =====================================================================
        # TAB 11 continued — REVIEW ROWS (item 5): every row shows a label and a status badge;
        #          a REQUIRED-but-unfilled field (Desired salary, marked `required` in this
        #          fixture) sorts before a non-required one in the same "Need you" section even
        #          though it was scanned later in the DOM; clicking a row scrolls/flashes the
        #          real field on the real page.
        # =====================================================================
        rows_info = panel11.evaluate("""
            () => Array.from(document.querySelectorAll('#results .field-row')).map(el => ({
                label: el.querySelector('.label') ? el.querySelector('.label').textContent : '',
                status: el.querySelector('.status-badge') ? el.querySelector('.status-badge').textContent : '',
                fieldId: el.dataset.fieldId || null,
            }))
        """)
        needs_you_rows = [r for r in rows_info if r["status"].lower() in ("left for you", "kept your value")]
        check("every 'needs you' row shows a label and a status badge",
              len(needs_you_rows) >= 2 and all(r["label"] and r["status"] for r in needs_you_rows),
              json.dumps(needs_you_rows))
        labels_in_order = [r["label"] for r in needs_you_rows]
        salary_idx = next((i for i, l in enumerate(labels_in_order) if "salary" in l.lower()), None)
        notice_idx = next((i for i, l in enumerate(labels_in_order) if "notice period" in l.lower()), None)
        check("the REQUIRED-but-unfilled field (Desired salary) sorts before the non-required "
              "one (notice period) in the same section, even though it was scanned later in the DOM",
              salary_idx is not None and notice_idx is not None and salary_idx < notice_idx,
              json.dumps(labels_in_order))

        salary_row_id = next((r["fieldId"] for r in needs_you_rows if "salary" in r["label"].lower()), None)
        check("the salary row carries a clickable field id", bool(salary_row_id), json.dumps(needs_you_rows))
        if salary_row_id:
            panel11.click(f'[data-field-id="{salary_row_id}"]')
            try:
                tab11.wait_for_function(
                    "() => document.getElementById('salary_expect').getAttribute('data-applypilot-flash') === 'true'",
                    timeout=3000)
                flashed = True
            except Exception:
                flashed = False
            check("clicking a row flashes the real field on the real page", flashed)

        # =====================================================================
        # TAB 12 — APPLICATION LOG (item 4): a completed fill automatically POSTs /log (never a
        #          click) with the page URL, the page's own document title, and counts translated
        #          into the service's own key names (needs_you/unreadable, not needsYou/
        #          couldNotRead); the panel shows "Logged" and a "Mark as applied" button that
        #          POSTs /log/{id}/status and updates the panel to reflect it.
        # =====================================================================
        log_calls_before = len(log_calls)
        tab12 = ctx.new_page()
        tab12.goto(PAGE_BASE + "#t=12")
        tab12_id = find_tab_id(helper, "#t=12")
        check("found tab 12's chrome tab id", tab12_id is not None)

        panel12 = ctx.new_page()
        panel12.goto(f"{panel_url}?tabId={tab12_id}")
        panel12.wait_for_function("() => window.__applyPilotPanelReady === true", timeout=5000)
        panel12.click("#scanBtn")
        state12 = wait_for_done(helper, tab12_id, timeout_s=60)
        check("tab 12's fill reached a terminal status",
              state12 is not None and state12.get("status") == "done", str(state12)[:200])

        panel12.wait_for_function("() => !document.getElementById('logLine').hidden", timeout=5000)
        check("the panel shows 'Logged' once the fill completes, with no click needed",
              "logged" in (panel12.eval_on_selector("#logStatusText", "el => el.textContent") or "").lower())
        check("the Mark as applied button is shown (not yet applied)",
              panel12.eval_on_selector("#markAppliedBtn", "el => el.hidden") is False)

        new_log_calls = log_calls[log_calls_before:]
        check("logging a completed fill made exactly one automatic /log call (no click involved)",
              len(new_log_calls) == 1, json.dumps(new_log_calls)[:300])
        if new_log_calls:
            logged = new_log_calls[0]
            check("the /log call named this tab's own URL", logged.get("url", "").endswith("#t=12"), logged.get("url"))
            check("the /log call sent the page's own document.title",
                  logged.get("title") == "Mock Job Application (ApplyPilot Copilot test page)", logged.get("title"))
            c = logged.get("counts") or {}
            check("the /log call's counts use the service's own key names (needs_you, unreadable) "
                  "translated from the panel's own state, and match it",
                  c.get("filled") == (state12.get("counts") or {}).get("filled")
                  and c.get("needs_you") == (state12.get("counts") or {}).get("needsYou")
                  and c.get("unreadable") == (state12.get("couldNotRead") or 0),
                  json.dumps({"sent": c, "state_counts": state12.get("counts"), "state_couldNotRead": state12.get("couldNotRead")}))

        panel12.click("#markAppliedBtn")
        panel12.wait_for_function(
            "() => (document.getElementById('logStatusText').textContent || "
            "'').toLowerCase().includes('applied')", timeout=5000)
        check("exactly one /log/{id}/status call was made, with status 'applied'",
              len(log_status_calls) == 1 and log_status_calls[0][1] == "applied", json.dumps(log_status_calls))
        check("clicking Mark as applied hides the button once it succeeds",
              panel12.eval_on_selector("#markAppliedBtn", "el => el.hidden") is True)

        # =====================================================================
        # TAB 12 continued — EXPORT FILL REPORT (item 6): downloads a JSON file with, per field,
        #          frame/label/tag-or-widget/status/source/reason, page host/path and counts --
        #          and NEVER a field's value, anywhere in the file.
        # =====================================================================
        check("the Export fill report button is enabled once there's a completed fill to export",
              panel12.eval_on_selector("#exportReportBtn", "el => el.disabled") is False)
        try:
            with panel12.expect_download(timeout=5000) as export_download_info:
                panel12.click("#exportReportBtn")
            export_download = export_download_info.value
            check("clicking Export fill report triggers a real download",
                  export_download.suggested_filename.startswith("applypilot-fill-report-"),
                  export_download.suggested_filename)
            export_path = export_download.path()
            report = json.loads(pathlib.Path(export_path).read_text(encoding="utf-8"))
        except Exception as e:
            check("clicking Export fill report triggers a real download", False, str(e))
            report = None
        if report is not None:
            check("the report names this page's host and path",
                  report.get("page", {}).get("path", "").endswith("test-page.html"), json.dumps(report.get("page")))
            check("the report's counts match the fill's own counts",
                  report.get("counts", {}).get("filled") == (state12.get("counts") or {}).get("filled"),
                  json.dumps(report.get("counts")))
            fields = report.get("fields") or []
            check("the report has one row per field across every list (filled+drafts+needsYou+failed)",
                  len(fields) == sum((state12.get("counts") or {}).get(k, 0) for k in ("filled", "drafts", "needsYou", "failed")),
                  f"report has {len(fields)} rows, state counts: {json.dumps(state12.get('counts'))}")
            check("every row has a label, a status, and a frame (frameId + url)",
                  all(f.get("label") and f.get("status") and f.get("frame", {}).get("url") for f in fields),
                  json.dumps(fields[:3]))
            check("at least one row's frame url matches this tab's own page",
                  any("test-page.html" in (f.get("frame") or {}).get("url", "") for f in fields), json.dumps(fields[:3]))
            raw_report_text = json.dumps(report)
            check("NO field value anywhere in the exported report (the whole point of this export)",
                  '"value"' not in raw_report_text and '"values"' not in raw_report_text, raw_report_text[:300])
            check("the actual filled VALUES ('Test Value ...') never appear anywhere in the report text",
                  "Test Value" not in raw_report_text, raw_report_text[:300])

        # =====================================================================
        # TAB 13 — KEYBOARD SHORTCUT (item 7): "fill-page"'s handler (background.js's
        #          handleFillPageCommand()) fills the ACTIVE tab directly -- no panel click
        #          involved -- when every frame it needs is already permitted (a plain
        #          single-frame 127.0.0.1 page always is, via the manifest's own static
        #          host_permissions). Invoked directly against the real service worker
        #          (Playwright has no API to simulate an OS-level keyboard shortcut for an
        #          extension command) -- the exact same function chrome.commands.onCommand calls.
        # =====================================================================
        tab13 = ctx.new_page()
        tab13.goto(PAGE_BASE + "#t=13")
        tab13_id = find_tab_id(helper, "#t=13")
        check("found tab 13's chrome tab id", tab13_id is not None)

        sw.evaluate(
            "(args) => handleFillPageCommand({ id: args.tabId, url: args.url })",
            {"tabId": tab13_id, "url": PAGE_BASE + "#t=13"},
        )
        state13 = wait_for_done(helper, tab13_id, timeout_s=60)
        check("the fill-page command filled the active tab directly, with no panel click involved",
              state13 is not None and state13.get("status") == "done", str(state13)[:200])
        if state13:
            check("the fill-page command's fill actually filled real fields",
                  (state13.get("counts") or {}).get("filled", 0) > 0, json.dumps(state13.get("counts")))

        # The downstream half of the "missing permission" path: a genuinely cross-origin,
        # ungranted frame can't be produced in THIS harness -- every fixture is 127.0.0.1, always
        # covered by the manifest's own static host_permissions (see this file's existing notes on
        # why the real chrome.permissions.request() prompt itself is equally out of reach here).
        # This calls openPanelWithPermissionNotice() directly with a fabricated missing-frame list
        # to prove what it does once handleFillPageCommand() decides permission is needed: record
        # the right host, open the panel, and never silently fill anything.
        tab14 = ctx.new_page()
        tab14.goto(PAGE_BASE + "#t=14")
        tab14_id = find_tab_id(helper, "#t=14")
        check("found tab 14's chrome tab id", tab14_id is not None)
        sw.evaluate(
            "(args) => openPanelWithPermissionNotice(args.tabId, "
            "[{ frameId: 99, url: 'https://embedded.example.com/app' }])",
            {"tabId": tab14_id},
        )
        state14 = get_state(helper, tab14_id)
        check("openPanelWithPermissionNotice records the missing host, never a fill",
              bool(state14) and (state14.get("permissionNeeded") or {}).get("hosts") == ["embedded.example.com"]
              and (state14.get("counts") or {}).get("filled", 0) == 0,
              json.dumps(state14))

        panel14 = ctx.new_page()
        panel14.goto(f"{panel_url}?tabId={tab14_id}")
        panel14.wait_for_function("() => window.__applyPilotPanelReady === true", timeout=5000)
        panel14.wait_for_function(
            "() => (document.getElementById('statusBox').textContent || '').includes('embedded.example.com')",
            timeout=5000)
        status14 = panel14.eval_on_selector("#statusBox", "el => el.textContent")
        check("the panel shows the exact permission-needed line naming the missing host",
              "needs permission" in (status14 or "").lower() and "embedded.example.com" in (status14 or ""),
              repr(status14))
        check("the panel does NOT show a fill summary since nothing was actually filled",
              panel14.eval_on_selector("#fillSummary", "el => el.hidden") is True)
        panel14.close()

        # =====================================================================
        # TAB 15 — MULTI-STEP CONTINUATION (item 8), opt-in and OFF by default: turning the
        #          panel's toggle ON (a real click, gated the same way Fill/Report are) arms
        #          content.js's watcher; a step transition this test drives directly (standing in
        #          for the operator clicking Workday's own "Next" — this extension never does)
        #          triggers a brand-new fill automatically, with NO click and NO RUN_FILL message
        #          sent by this test; the step counter advances; turning the toggle back off stops
        #          any further automatic fills.
        # =====================================================================
        tab15 = ctx.new_page()
        tab15.goto(f"{SERVICE_URL}/multi-step-page.html#t=15")
        tab15_id = find_tab_id(helper, "#t=15")
        check("found tab 15's chrome tab id", tab15_id is not None)

        panel15 = ctx.new_page()
        panel15.goto(f"{panel_url}?tabId={tab15_id}")
        panel15.wait_for_function("() => window.__applyPilotPanelReady === true", timeout=5000)

        check("the continuation toggle is OFF by default", panel15.eval_on_selector("#continuationToggle", "el => el.checked") is False)
        panel15.check("#continuationToggle")
        panel15.wait_for_function("() => document.getElementById('continuationToggle').checked === true", timeout=5000)

        panel15.click("#scanBtn")
        # Enabling continuation moments ago already wrote an 'idle' state for this tab (see
        # background.js's SET_CONTINUATION) -- wait for THIS click's own fill to actually start
        # (status really becomes 'running') before handing off to wait_for_done(), so it can't
        # mistake that stale 'idle' write for this fill having already finished.
        deadline_running = time.time() + 5
        while time.time() < deadline_running and (get_state(helper, tab15_id) or {}).get("status") != "running":
            time.sleep(0.05)
        state15a = wait_for_done(helper, tab15_id, timeout_s=60)
        check("step 1's fill (a normal, explicitly-clicked fill) completed",
              state15a is not None and state15a.get("status") == "done", str(state15a)[:200])
        step1_val = tab15.eval_on_selector("#s1_name", "el => el.value")
        check("step 1's own field was filled", bool(step1_val), repr(step1_val))
        panel15.wait_for_function(
            "() => (document.getElementById('continuationStepLine').textContent || '').includes('Step 1')",
            timeout=5000)

        resolve_calls_before_step2 = len(resolve_calls)
        tab15.evaluate("() => window.__goToStep2()")  # standing in for clicking Workday's own "Next"

        deadline = time.time() + 15
        while time.time() < deadline and len(resolve_calls) <= resolve_calls_before_step2:
            time.sleep(0.1)
        check("the step-2 transition triggered a brand-new /resolve call AUTOMATICALLY, with no "
              "click and no RUN_FILL message sent by this test",
              len(resolve_calls) > resolve_calls_before_step2,
              f"before={resolve_calls_before_step2} after={len(resolve_calls)}")

        state15b = None
        deadline2 = time.time() + 15
        while time.time() < deadline2:
            s = get_state(helper, tab15_id)
            if s and s.get("status") == "done" and (s.get("continuation") or {}).get("steps", 0) >= 2:
                state15b = s
                break
            time.sleep(0.1)
        check("the automatic step-2 fill completed and the step counter advanced to 2",
              state15b is not None, json.dumps(get_state(helper, tab15_id))[:300])
        if state15b:
            check("step 2's own field was filled automatically (never step 1's, which is gone)",
                  (state15b.get("counts") or {}).get("filled", 0) > 0, json.dumps(state15b.get("counts")))
            step2_val = tab15.eval_on_selector("#s2_email", "el => el.value")
            check("step 2's real field on the real page actually got filled",
                  bool(step2_val), repr(step2_val))
        panel15.wait_for_function(
            "() => (document.getElementById('continuationStepLine').textContent || '').includes('Step 2')",
            timeout=5000)
        check("same guards applied to the automatic step-2 fill: no submission, no navigation",
              tab15.evaluate("() => !window.__FORM_SUBMITTED__") is True)

        # Turning it off must stop any further automatic fills.
        panel15.uncheck("#continuationToggle")
        panel15.wait_for_function("() => document.getElementById('continuationToggle').checked === false", timeout=5000)
        resolve_calls_before_step3 = len(resolve_calls)
        tab15.evaluate("() => window.__goToStep3()")
        time.sleep(2.5)  # generous settle window (debounce + poll + DOM-quiet) — nothing should happen
        check("turning the toggle off stops future automatic fills (no new /resolve call for step 3)",
              len(resolve_calls) == resolve_calls_before_step3,
              f"before={resolve_calls_before_step3} after={len(resolve_calls)}")
        step3_val = tab15.eval_on_selector("#s3_phone", "el => el.value")
        check("step 3's field was correctly left untouched once continuation was turned off",
              step3_val in ("", None), repr(step3_val))

        # =====================================================================
        # tabs.onRemoved cleanup
        # =====================================================================
        tab1.close()
        time.sleep(0.3)
        cleaned = get_state(helper, tab1_id)
        check("closing a tab removes its chrome.storage.session entry", cleaned is None, str(cleaned))

        # =====================================================================
        # the one rule that matters: NOTHING above ever submitted the mock form.
        # =====================================================================
        for name, pg in (("tab2", tab2), ("tab3", tab3), ("tab4", tab4), ("tab6", tab6), ("tab7", tab7), ("tab8", tab8), ("tab11", tab11), ("tab12", tab12), ("tab13", tab13), ("tab14", tab14), ("tab15", tab15)):
            counters = submission_counters(pg)
            check(f"{name}: no native form submission", counters["form"] is False, json.dumps(counters))
            check(f"{name}: no Workday submit click registered", counters["wd"] == 0, json.dumps(counters))
            check(f"{name}: page never navigated away", counters["url"].startswith(SERVICE_URL), counters["url"])

    finally:
        stub_server.shutdown()
        inner_server.shutdown()
        ctx.close()

print(f"\n{len(failures)} failure(s): {failures}" if failures else "\nALL PANEL CHECKS PASSED")
sys.exit(1 if failures else 0)
