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
import json
import pathlib
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EXT = pathlib.Path(r"E:\auto-apply-pipeline\extension")
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
check("manifest keeps the original 127.0.0.1 host permissions",
      "http://127.0.0.1/*" in host_perms and "http://127.0.0.1:*/*" in host_perms, str(host_perms))
check("manifest adds broad http(s) host permissions so the panel can follow any active tab",
      "https://*/*" in host_perms and "http://*/*" in host_perms, str(host_perms))
check("minimum_chrome_version bumped to 116 (chrome.sidePanel.setPanelBehavior needs it)",
      manifest.get("minimum_chrome_version") == "116", manifest.get("minimum_chrome_version"))


# ---------------------------------------------------------------------------
# 1. a tiny stub of the local ApplyPilot service — stdlib only, ephemeral port.
# ---------------------------------------------------------------------------
resolve_calls = []  # (url, [field names requested]) — lets a check below prove RESOLVE ran


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
        # Keep the stub's own logic trivial: leave Workday's own popup/prompt widgets and
        # anything unfillable to the "needs you" pile. The timeout/cancel tabs below control
        # timing by patching applyFill() directly, not by relying on any particular widget's
        # real timing, so nothing here needs to touch wd-dropdown/wd-prompt at all.
        if widget in ("wd-prompt", "wd-dropdown") or ftype in ("hidden", "file", "submit", "button", "image", "reset"):
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
        else:
            self._json(404, {"detail": "not found"})


stub_server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
stub_port = stub_server.server_address[1]
stub_thread = threading.Thread(target=stub_server.serve_forever, daemon=True)
stub_thread.start()
SERVICE_URL = f"http://127.0.0.1:{stub_port}"
PAGE_BASE = f"{SERVICE_URL}/test-page.html"


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


def start_fill_via_message(helper_page, tab_id, field_timeout_ms=None, budget_ms=None):
    return helper_page.evaluate(
        "(args) => chrome.tabs.sendMessage(args.tabId, { type: 'START_FILL', fieldTimeoutMs: args.fieldTimeoutMs, budgetMs: args.budgetMs })",
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
        state1 = wait_for_done(helper, tab1_id, timeout_s=20)

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
        state2 = wait_for_done(helper, tab2_id, timeout_s=20)
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
        cancel_resp = helper.evaluate(
            "(tabId) => chrome.tabs.sendMessage(tabId, { type: 'CANCEL_FILL' })", tab4_id)
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
        # tabs.onRemoved cleanup
        # =====================================================================
        tab1.close()
        time.sleep(0.3)
        cleaned = get_state(helper, tab1_id)
        check("closing a tab removes its chrome.storage.session entry", cleaned is None, str(cleaned))

        # =====================================================================
        # the one rule that matters: NOTHING above ever submitted the mock form.
        # =====================================================================
        for name, pg in (("tab2", tab2), ("tab3", tab3), ("tab4", tab4)):
            counters = submission_counters(pg)
            check(f"{name}: no native form submission", counters["form"] is False, json.dumps(counters))
            check(f"{name}: no Workday submit click registered", counters["wd"] == 0, json.dumps(counters))
            check(f"{name}: page never navigated away", counters["url"].startswith(SERVICE_URL), counters["url"])

    finally:
        stub_server.shutdown()
        ctx.close()

print(f"\n{len(failures)} failure(s): {failures}" if failures else "\nALL PANEL CHECKS PASSED")
sys.exit(1 if failures else 0)
