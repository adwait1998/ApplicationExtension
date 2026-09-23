"""Does the extension actually LOAD in a real Chrome, and does its scanner run
against a real DOM with a real layout engine?

No agent could check this — jsdom has no layout engine and chrome.* APIs do not
exist outside an extension context. This launches real Chromium with the
unpacked extension, opens the mock ATS page, and exercises the scanner in the
page's own realm.
"""
import json
import pathlib
import sys
import tempfile

EXT = pathlib.Path(r"E:\auto-apply-pipeline\extension")
PAGE = (EXT / "test-page.html").as_uri()

from playwright.sync_api import sync_playwright  # noqa: E402 — after EXT/PAGE consts

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(name)


user_dir = tempfile.mkdtemp(prefix="apc-chrome-")
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
        page = ctx.new_page()

        # 1. did Chrome accept the manifest and register the MV3 service worker?
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
        ext_id = sw.url.split("/")[2] if sw else None

        # 2. real page, real layout engine, real scanner
        page.goto(PAGE)
        page.add_script_tag(path=str(EXT / "scanner.js"))
        result = page.evaluate("() => ApplyPilotScanner.scanAll(document)")
        fields = result["fields"]
        by_name = {f["name"]: f for f in fields if f.get("name")}
        print(f"\nscanned {len(fields)} fields in real Chrome")
        for f in fields:
            print(f"   {f['id']:4} {f['tag']:8} {str(f['name'])[:22]:24} {f['label'][:44]!r}")
        print()

        check("scanner runs in real Chrome and finds fields", len(fields) >= 8, f"{len(fields)} found")
        check("label[for] resolved", by_name.get("full_name", {}).get("label") == "Full Name")
        check("aria-label resolved", by_name.get("phone", {}).get("label") == "Phone Number")
        check("hidden input excluded (real layout engine)", "hidden_token" not in by_name)
        check("display:none excluded (real layout engine)", "display_none_field" not in by_name)
        check("submit button excluded", not any(f["type"] == "submit" for f in fields))
        check("select options captured",
              any(f["tag"] == "select" and len(f["options"]) >= 3 for f in fields))
        check("radio group collapsed to one descriptor",
              len([f for f in fields if f["type"] == "radio"]) == 1)

        # 2b. Repeating-section detection, against a real layout engine. This is
        #     what lets the structured tier put "Work Experience 2" into the
        #     SECOND position rather than overwriting the first.
        def block(idx):
            return {f["label"]: f for f in fields if f.get("section_index") == idx}

        b1, b2, b3 = block(1), block(2), block(3)
        check("Work Experience 1 fields carry section_index 1", len(b1) >= 6, str(len(b1)))
        check("Work Experience 2 fields carry section_index 2", len(b2) >= 6, str(len(b2)))
        check("index derived from field NAMES when the heading has no digit",
              len(b3) >= 2, str(len(b3)))
        check("section heading text captured",
              (b1.get("Job Title") or {}).get("section", "").lower().startswith("work experience"),
              (b1.get("Job Title") or {}).get("section", ""))
        # The false positive that would matter: a stray number near a field
        # becoming a section index and misplacing a whole block.
        alt = next((f for f in fields if f.get("name") == "altPhone"), None)
        check("a nearby digit ('Phone Numbers (2 max)') does NOT become a section index",
              alt is not None and alt.get("section_index") is None,
              str(alt.get("section_index") if alt else "field missing"))
        no_idx = [f for f in fields
                  if f.get("section_index") is None and f.get("name") in
                  ("full_name", "email", "phone")]
        check("ordinary top-level fields carry no section index", len(no_idx) == 3, str(len(no_idx)))

        # 3. Every selector must resolve. A radio GROUP is deliberately one
        #    descriptor covering several elements that share a name, so it is
        #    expected to match >1; everything else must be unique or the fill
        #    step could write into the wrong input.
        uniq = page.evaluate(
            """(sels) => sels.map(s => { try { return document.querySelectorAll(s).length; }
                                          catch (e) { return -1; } })""",
            [f["selector"] for f in fields])
        bad = [(f["selector"], n) for f, n in zip(fields, uniq)
               if (n < 1) or (n != 1 and f["type"] != "radio")]
        check("every non-radio selector resolves to exactly one element", not bad, str(bad[:3]))
        radios = [(f, n) for f, n in zip(fields, uniq) if f["type"] == "radio"]
        check("radio-group selector covers the whole group",
              all(n >= 2 for _f, n in radios) if radios else False,
              str([(f["selector"], n) for f, n in radios]))

        # 4. the React-controlled input: native setter must survive the revert loop
        react = by_name.get("react_input")
        if react:
            page.evaluate(
                """(sel) => { const el = document.querySelector(sel);
                              ApplyPilotScanner.setNativeValue(el, '165000'); }""",
                react["selector"])
            page.wait_for_timeout(400)      # let the page's revert-on-render loop run
            got = page.eval_on_selector(react["selector"], "el => el.value")
            check("React-controlled input keeps the filled value", got == "165000", repr(got))

            # negative control: a naive assignment should NOT survive
            page.evaluate(
                """(sel) => { document.querySelector(sel).value = 'NAIVE'; }""",
                react["selector"])
            page.wait_for_timeout(400)
            naive = page.eval_on_selector(react["selector"], "el => el.value")
            check("naive .value= is reverted by the page (proves the native setter matters)",
                  naive != "NAIVE", repr(naive))
        else:
            check("react-controlled input present in mock page", False)

        # 5. the click guard, in a real browser
        guard = page.evaluate("""() => {
            const mk = (t, ty) => { const e = document.createElement(t); if (ty) e.type = ty; return e; };
            return { radio: ApplyPilotScanner.isClickSafe(mk('input','radio')),
                     checkbox: ApplyPilotScanner.isClickSafe(mk('input','checkbox')),
                     submit: ApplyPilotScanner.isClickSafe(mk('input','submit')),
                     button: ApplyPilotScanner.isClickSafe(mk('button')) };
        }""")
        check("click guard: radio allowed, submit/button refused",
              guard["radio"] and guard["checkbox"] and not guard["submit"] and not guard["button"],
              json.dumps(guard))

        # 6. nothing navigated
        check("page never navigated away", page.url.startswith("file:"), page.url)
    finally:
        ctx.close()

print(f"\n{len(failures)} failure(s): {failures}" if failures else "\nALL CHROME CHECKS PASSED")
sys.exit(1 if failures else 0)
