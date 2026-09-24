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

        # 5a. Custom dropdowns (select2 / Chosen). On a real Viasat form the
        #     State/Province and Country fields were never even highlighted —
        #     the libraries hide the native <select> and the visibility filter
        #     dropped it. jsdom has no layout or widget behaviour, so whether
        #     this works at all is only knowable here.
        by_name_all = {f["name"]: f for f in fields if f.get("name")}
        for nm in ("state_s2", "state_chosen"):
            check(f"hidden native select behind a custom widget is scanned ({nm})",
                  nm in by_name_all, str(sorted(by_name_all)[:6]))
        check("a hidden select with NO paired widget is still ignored",
              "decoy_hidden_select" not in by_name_all)

        # The profile stores the full name ("Arizona"). The case that matters is
        # that value landing on a CODE-only option list ("AZ") and vice versa —
        # passing "AZ" to an "AZ" option would only prove an exact match.
        for nm, value in (("state_s2", "Arizona"), ("state_chosen", "Arizona")):
            f = by_name_all.get(nm)
            if not f:
                continue
            got = page.evaluate(
                """([sel, v]) => {
                    const el = document.querySelector(sel);
                    const ok = ApplyPilotScanner.setSelectValue(el, v);
                    const opt = el.options[el.selectedIndex];
                    return { ok, text: opt ? opt.text : null, value: el.value };
                }""", [f["selector"], value])
            check(f"custom-widget select filled with {value!r} and read back ({nm})",
                  got["ok"] is True and got["text"] and
                  ("arizona" in got["text"].lower() or got["value"].upper().endswith("AZ")),
                  json.dumps(got))

        # 5c. "Add Another" expansion. This is the one place the extension clicks
        #     a BUTTON rather than a radio/checkbox, so it is the one place a bug
        #     could submit a real application. The test page plants a trap: a
        #     <button> with NO type attribute, text "Add Another", inside a <form>
        #     — which HTML treats as a SUBMIT button.
        g = page.evaluate("""() => {
            const S = ApplyPilotScanner, $ = id => document.getElementById(id);
            return {
              trap: S.isAddAnotherButtonSafe($('trap_add_btn')),
              work: S.isAddAnotherButtonSafe($('myexp_add_btn')),
              edu:  S.isAddAnotherButtonSafe($('myedu_add_btn')),
              trapType: $('trap_add_btn').type, trapHasForm: !!$('trap_add_btn').form,
            };
        }""")
        check("the trap really is a submit button (no type, inside a form)",
              g["trapType"] == "submit" and g["trapHasForm"], json.dumps(g))
        check("guard REFUSES the no-type in-form 'Add Another' (would submit)", g["trap"] is False)
        check("guard accepts a type=button 'Add Another'", g["work"] is True)
        check("guard accepts a role=button 'Add Education' div", g["edu"] is True)

        # Clicking the trap through the guarded path must do nothing at all.
        r = page.evaluate("""() => {
            window.__FORM_SUBMITTED__ = false;
            const clicked = ApplyPilotScanner.safeClickAddButton(document.getElementById('trap_add_btn'));
            return { clicked, submitted: !!window.__FORM_SUBMITTED__ };
        }""")
        check("safeClickAddButton on the trap refuses and submits NOTHING",
              r["clicked"] is False and r["submitted"] is False, json.dumps(r))

        # The shield as a backstop: simulate a guard bug by clicking the trap
        # DIRECTLY, bypassing the guard entirely. The shield must still stop it.
        shield = page.evaluate("""() => {
            const S = ApplyPilotScanner, trap = document.getElementById('trap_add_btn');
            window.__FORM_SUBMITTED__ = false;
            let fired = 0;
            const off = S.installSubmitShield(document, () => { fired++; });
            trap.click();                       // guard deliberately bypassed
            const shielded = !!window.__FORM_SUBMITTED__;
            off();
            // Negative control: with the shield removed the trap REALLY submits,
            // proving the check above is not passing vacuously.
            window.__FORM_SUBMITTED__ = false;
            trap.click();
            const unshielded = !!window.__FORM_SUBMITTED__;
            window.__FORM_SUBMITTED__ = false;
            return { shielded, unshielded, fired };
        }""")
        check("shield blocks a submit even when the guard is BYPASSED",
              shield["shielded"] is False and shield["fired"] >= 1, json.dumps(shield))
        check("negative control: without the shield the trap genuinely submits",
              shield["unshielded"] is True, json.dumps(shield))

        pick = page.evaluate("""() => {
            const S = ApplyPilotScanner;
            const w = S.findAddButtonForKind(document, 'work_history');
            const e = S.findAddButtonForKind(document, 'education');
            return { work: w && w.id, edu: e && e.id };
        }""")
        check("work-history expansion picks ITS OWN button (not the trap, not Education's)",
              pick["work"] == "myexp_add_btn", json.dumps(pick))
        check("education expansion picks the Education button",
              pick["edu"] == "myedu_add_btn", json.dumps(pick))

        grew = page.evaluate("""() => {
            const before = !!document.getElementById('myexp2_title');
            const ok = ApplyPilotScanner.safeClickAddButton(document.getElementById('myexp_add_btn'));
            return { before, ok, after: !!document.getElementById('myexp2_title'),
                     url: location.href, submitted: !!window.__FORM_SUBMITTED__ };
        }""")
        check("clicking the real 'Add Another' creates block 2 in a real browser",
              grew["before"] is False and grew["ok"] is True and grew["after"] is True, json.dumps(grew))
        check("expansion submitted nothing and did not navigate",
              grew["submitted"] is False and grew["url"].startswith("file:"), json.dumps(grew))

        # 5d. Workday-style split month/year date ("From" rendered as two spinbuttons).
        dates = page.evaluate("""() => {
            const S = ApplyPilotScanner;
            const pairs = S.findDatePartPairs(document);
            if (!pairs.length) return { found: 0 };
            const p = pairs[0];
            const ok = S.setDatePartsValue(p, '03/2022');
            return { found: pairs.length, label: p.label, ok,
                     month: p.monthEl.value, year: p.yearEl.value,
                     readBack: S.getDatePartsValue(p) };
        }""")
        check("split month/year date detected as ONE field", dates.get("found", 0) >= 1, json.dumps(dates))
        check("split date filled and read back as 03/2022",
              dates.get("ok") is True and dates.get("readBack") == "03/2022"
              and dates.get("year") == "2022", json.dumps(dates))

        # 5b. Résumé attachment. jsdom has no DataTransfer/DragEvent at all, so
        #     this mechanism is entirely unverified until it runs here. The
        #     failure mode that matters is a SILENT one: reporting success
        #     while no file is attached would mean submitting an application
        #     without a résumé.
        target = page.evaluate("""() => {
            const t = ApplyPilotScanner.findResumeFileTarget(document);
            return t ? { id: t.el.id, name: t.el.name, dropzone: !!t.isDropzone } : null;
        }""")
        check("a résumé target is found among the file inputs", target is not None, str(target))
        check("the COVER-LETTER input is never chosen as the résumé target",
              (target or {}).get("id") != "cover_letter_upload", str(target))

        attach = page.evaluate("""() => {
            const file = new File([new Uint8Array([37, 80, 68, 70, 45, 49, 46, 52])],
                                  'resume.pdf', { type: 'application/pdf' });
            const res = ApplyPilotScanner.attachResumeFile(document, file);
            const t = ApplyPilotScanner.findResumeFileTarget(document);
            return { res, readBack: t && t.el.files && t.el.files[0]
                        ? { name: t.el.files[0].name, size: t.el.files[0].size,
                            type: t.el.files[0].type }
                        : null };
        }""")
        check("attachResumeFile reports success in real Chrome",
              (attach.get("res") or {}).get("attached") is True, json.dumps(attach.get("res"))[:120])
        check("the file is GENUINELY on the input afterwards",
              (attach.get("readBack") or {}).get("name") == "resume.pdf",
              json.dumps(attach.get("readBack")))
        check("the attached file has real bytes (not a zero-length placeholder)",
              (attach.get("readBack") or {}).get("size", 0) > 0,
              str((attach.get("readBack") or {}).get("size")))

        # The cover-letter input must still be empty — attaching to the wrong
        # field would send the résumé as a cover letter.
        cover = page.evaluate(
            """() => { const el = document.getElementById('cover_letter_upload');
                       return el ? (el.files ? el.files.length : -1) : -2; }""")
        check("the cover-letter input received nothing", cover == 0, str(cover))

        # And the guard must hold: no click path was introduced by attachment.
        check("attachment introduced no submit click (page still on the form)",
              page.url.startswith("file:"), page.url)

        # 6. nothing navigated
        check("page never navigated away", page.url.startswith("file:"), page.url)
    finally:
        ctx.close()

print(f"\n{len(failures)} failure(s): {failures}" if failures else "\nALL CHROME CHECKS PASSED")
sys.exit(1 if failures else 0)
