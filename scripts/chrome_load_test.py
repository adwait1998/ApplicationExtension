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

# Resolved relative to this script's own location (not hardcoded to the main checkout) so this
# loads whichever extension/ this script itself lives next to -- the main checkout when run
# there, or a git worktree's own copy when run from inside one, which is where this most often
# actually runs from (each worktree needs to prove ITS OWN scanner.js/test-page.html changes,
# never a stale copy elsewhere on disk).
EXT = pathlib.Path(__file__).resolve().parent.parent / "extension"
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
        #    A field found inside an open shadow root (see scanAll's shadow-DOM traversal) is
        #    checked SEPARATELY below: its own selector was only ever built to be unique WITHIN
        #    that shadow root's own tree, and a plain document.querySelectorAll() can never
        #    resolve it at all (that is the whole point of a shadow boundary) -- that is not a
        #    uniqueness bug, just a different, correct scope to check it in.
        top_fields = [f for f in fields if not f.get("shadow")]
        uniq = page.evaluate(
            """(sels) => sels.map(s => { try { return document.querySelectorAll(s).length; }
                                          catch (e) { return -1; } })""",
            [f["selector"] for f in top_fields])
        bad = [(f["selector"], n) for f, n in zip(top_fields, uniq)
               if (n < 1) or (n != 1 and f["type"] != "radio")]
        check("every non-radio, non-shadow selector resolves to exactly one element", not bad, str(bad[:3]))

        # A shadow field's selector was only ever built to be unique WITHIN its OWN shadow
        # root's tree (see buildSelector's nth-child fallback) -- checked per-root, not summed
        # across every open shadow root: two DIFFERENT shadow roots can each independently
        # contain, say, a lone top-level <input> with no other differentiating context, which
        # would legitimately both fall back to the exact same bare "input" selector -- unique
        # within either one, just not a globally-unique string across unrelated trees (and
        # nothing ever needs it to be: the registry holds the real element reference directly,
        # never re-resolving a shadow field's selector against the wrong root).
        shadow_fields = [f for f in fields if f.get("shadow")]
        check("scanAll found at least one field inside an open shadow root", len(shadow_fields) >= 1,
              str(len(shadow_fields)))
        shadow_uniq = page.evaluate(
            """(sels) => sels.map(s => {
                const roots = (window.ApplyPilotScanner && ApplyPilotScanner.findOpenShadowRoots)
                    ? ApplyPilotScanner.findOpenShadowRoots(document) : [];
                return roots.map(r => { try { return r.querySelectorAll(s).length; } catch (e) { return -1; } });
            })""",
            [f["selector"] for f in shadow_fields])
        bad_shadow = [(f["selector"], counts) for f, counts in zip(shadow_fields, shadow_uniq)
                      if not any(c == 1 for c in counts) and f["type"] != "radio"]
        check("every shadow-DOM field's selector resolves to exactly one element WITHIN some (its own) open shadow root",
              not bad_shadow, str(bad_shadow[:3]))
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

        # 5e. WORKDAY WIDGETS. Built twice before from guessed markup; each time a
        #     mock built from the same guesses passed while the real form failed.
        #     This mock uses selectors taken from working autofillers' source, and
        #     its spinners REVERT the previously-shipped technique — so the
        #     negative controls below prove the mock can tell right from wrong.
        page.evaluate("() => { window.__WD_SUBMIT_COUNT__ = 0; window.__FORM_SUBMITTED__ = false; }")

        wd_dates = page.evaluate("""async () => {
            const S = ApplyPilotScanner, $ = id => document.getElementById(id);
            // Negative control: the OLD technique (value + input/change) must be REVERTED.
            S.setNativeValue($('wd_start_month'), '6');
            const oldTechnique = $('wd_start_month').value;
            const w = S.findWorkdayDateWrappers(document);
            const my = w.find(x => x.shape === 'my' && x.monthEl && x.monthEl.id === 'wd_start_month');
            const y = w.find(x => x.shape === 'y' && x.yearEl && x.yearEl.id === 'wd_edu_from_year');
            const mdy = w.find(x => x.shape === 'mdy');
            // setWorkdaySpinnerValue/setWorkdayDateValue are now asynchronous (a real yield is
            // the fix for the "hangs on dates" report -- see the project brief), so these must
            // be awaited like the dropdown/prompt widgets below.
            const okMy = await S.setWorkdayDateValue(Object.assign({kind: 'wd-date-my'}, my), '06/2023');
            const okY = await S.setWorkdayDateValue(Object.assign({kind: 'wd-date-y'}, y), '08/2021');
            const okMdy = await S.setWorkdayDateValue(Object.assign({kind: 'wd-date-mdy'}, mdy), '07/04/2026');
            return { oldTechnique, found: w.length, okMy, okY, okMdy,
                     month: $('wd_start_month').value, year: $('wd_start_year').value,
                     eduYear: $('wd_edu_from_year').value,
                     monthDisplay: $('wd_start_month-display').textContent,
                     yearDisplay: $('wd_start_year-display').textContent,
                     selfid: S.getWorkdayDateValue(Object.assign({kind: 'wd-date-mdy'}, mdy)) };
        }""")
        check("NEGATIVE CONTROL: the old value+input/change technique is reverted by the mock",
              wd_dates["oldTechnique"] in ("", None), json.dumps(wd_dates))
        check("Workday MM/YYYY spinner commits 06/2023 via the async set-then-ArrowUp technique",
              wd_dates["okMy"] is True and str(int(wd_dates["month"] or 0)) == "6"
              and wd_dates["year"] == "2023", json.dumps(wd_dates))
        check("the \"-display\" divs show the committed values, never left showing the placeholder",
              wd_dates["monthDisplay"] == "06" and wd_dates["yearDisplay"] == "2023", json.dumps(wd_dates))
        check("Workday year-only spinner commits 2021 (variant needing TWO ArrowUps)",
              wd_dates["okY"] is True and wd_dates["eduYear"] == "2021", json.dumps(wd_dates))
        check("Workday Month+Day+Year (Self-Identify shape) commits all three parts, Day is never dropped",
              wd_dates["okMdy"] is True and wd_dates["selfid"] == "07/04/2026", json.dumps(wd_dates))

        # A dateInputWrapper whose spinbutton input is genuinely invisible (visibility:hidden)
        # behind its own visible "-display" div must still fill -- gating on the input's own
        # visibility (the old bug) refuses this; gating on the wrapper's visibility does not.
        wd_hidden = page.evaluate("""async () => {
            const S = ApplyPilotScanner, $ = id => document.getElementById(id);
            const hiddenVisible = S.isVisible($('wd_hidden_month'));
            const w = S.findWorkdayDateWrappers(document).find(x => x.monthEl && x.monthEl.id === 'wd_hidden_month');
            const ok = await S.setWorkdayDateValue(Object.assign({kind: 'wd-date-my'}, w), '04/2018');
            return { hiddenVisible, ok, value: S.getWorkdayDateValue(Object.assign({kind: 'wd-date-my'}, w)) };
        }""")
        check("the hidden spinbutton input is genuinely invisible in a REAL layout engine",
              wd_hidden["hiddenVisible"] is False, json.dumps(wd_hidden))
        check("a fill still succeeds when the input is invisible but its wrapper is visible (real browser)",
              wd_hidden["ok"] is True and wd_hidden["value"] == "04/2018", json.dumps(wd_hidden))

        def dropdown(button_id, value):
            return page.evaluate("""async ([bid, v]) => {
                const S = ApplyPilotScanner, b = document.getElementById(bid);
                const before = b.textContent.trim();
                const r = await S.fillWorkdayDropdown({ button: b }, v);
                return { r, before, after: b.textContent.trim() };
            }""", [button_id, value])

        deg = dropdown("wd_degree_button", "MS")
        check("Degree 'MS' -> 'Masters Degree or Equivalent' (portalled listbox via aria-controls)",
              deg["r"]["ok"] is True and "master" in deg["after"].lower(), json.dumps(deg))
        ctry = dropdown("wd_country_button", "United States")
        check("Country 'United States' -> 'United States of America'",
              ctry["r"]["ok"] is True and ctry["after"] == "United States of America", json.dumps(ctry))
        bogus = dropdown("wd_country_button", "Atlantis")
        check("dropdown with NO confident match selects NOTHING (never the first option)",
              bogus["r"]["ok"] is False and bogus["after"] == ctry["after"], json.dumps(bogus))

        def prompt(input_id, value):
            return page.evaluate("""async ([iid, v]) => {
                const S = ApplyPilotScanner;
                const e = S.findWorkdayPrompts(document).find(p => p.input && p.input.id === iid);
                if (!e) return { error: 'prompt not detected' };
                const r = await S.fillWorkdayPromptValue(e, v);
                const sel = Array.from(e.formField.querySelectorAll(
                    '[data-automation-id="selectedItemList"] li')).map(li => li.textContent.trim());
                return { r, selected: sel };
            }""", [input_id, value])

        fos_bad = prompt("wd_fos_input", "Underwater Basket Weaving")
        check("prompt with no match does NOT grab the first suggestion ('Computer Science')",
              "Computer Science" not in (fos_bad.get("selected") or []), json.dumps(fos_bad))
        fos = prompt("wd_fos_input", "Computer Science")
        check("Field of Study prompt selects the exact match",
              "Computer Science" in (fos.get("selected") or []), json.dumps(fos))
        skills = prompt("wd_skills_input", ["SQL", "Python", "Spark"])
        sel = " | ".join(skills.get("selected") or [])
        check("Skills: 'SQL' matched by acronym to 'Structured Query Language (SQL)'",
              "(SQL)" in sel, sel)
        check("Skills: 'Python' added by exact match", "Python" in sel, sel)
        check("Skills: unmatched 'Spark' skipped, NOT substituted with another skill",
              "Project Management" not in sel, sel)

        trap = page.evaluate("() => ({ wd: window.__WD_SUBMIT_COUNT__ || 0, "
                             "submitted: !!window.__FORM_SUBMITTED__, url: location.href })")
        check("NONE of the Workday interactions submitted anything or navigated",
              trap["wd"] == 0 and trap["submitted"] is False and trap["url"].startswith("file:"),
              json.dumps(trap))
        guard = page.evaluate("""() => {
            const S = ApplyPilotScanner, b = document.getElementById('wd_submit_btn');
            return { opener: S.isWorkdayDropdownOpenerSafe(b), option: S.isWorkdayOptionSafe(b, document.body),
                     hardDeny: S.hasWorkdayHardDenyAutomationId(b) };
        }""")
        check("Workday's real submit button is refused by every new guard",
              guard["hardDeny"] is True and guard["opener"] is False and guard["option"] is False,
              json.dumps(guard))

        # 5f. Self-Identify disability CheckboxGroup (CC-305): a single-choice question backed
        #     by independent checkboxes, "requires exactly one selection". Also proves
        #     agreementCheckbox (terms/consent) is never offered as fillable and is refused by
        #     its own guard even if targeted directly -- a real layout engine, not jsdom.
        cg = page.evaluate("""() => {
            const S = ApplyPilotScanner, $ = id => document.getElementById(id);
            const scanned = S.scanFields(document);
            // Disambiguated by widget "wd-checkbox-group", not just type "checkbox-group" --
            // the choice-widget driver (Greenhouse/Ashby/Lever) legitimately emits the SAME
            // type string for its own, unrelated checkbox groups; only `widget` tells this one
            // apart as Workday's.
            const cgField = scanned.fields.find(f => f.widget === 'wd-checkbox-group');
            const cgEntry = cgField && scanned.registry[cgField.id];
            const agreementScanned = scanned.fields.some(f => scanned.registry[f.id] && scanned.registry[f.id].el === $('wd_agreement_checkbox'));
            const okYes = cgEntry ? S.applyFill(cgEntry, 'Yes, I have a disability, or have had one in the past') : null;
            const yesChecked = $('wd_disability_yes').checked, noChecked = $('wd_disability_no').checked;
            const okDecline = cgEntry ? S.applyFill(cgEntry, 'Decline to self-identify') : null;
            return {
                found: !!cgField, options: cgField && cgField.options, label: cgField && cgField.label,
                agreementScanned, okYes, yesChecked, noChecked,
                okDecline, declineChecked: $('wd_disability_decline').checked,
                yesStillCheckedAfterSwitch: $('wd_disability_yes').checked,
                agreementGuard: S.isWorkdayCheckboxGroupOptionSafe($('wd_agreement_checkbox'), $('wd_disability_group')),
                agreementStillUnchecked: $('wd_agreement_checkbox').checked === false
            };
        }""")
        check("the disabilityStatus CheckboxGroup is scanned as one type=checkbox-group field with the 3 CC-305 options",
              cg["found"] is True and len(cg["options"] or []) == 3, json.dumps(cg))
        check("agreementCheckbox (terms/consent) is never scanned as a fillable field at all, in a real browser",
              cg["agreementScanned"] is False, json.dumps(cg))
        check("checkbox-group: exact CC-305 text checks exactly that box",
              cg["okYes"] is True and cg["yesChecked"] is True and cg["noChecked"] is False, json.dumps(cg))
        check("checkbox-group: the decline answer-family matches, and switching answers unchecks the previous one",
              cg["okDecline"] is True and cg["declineChecked"] is True and cg["yesStillCheckedAfterSwitch"] is False,
              json.dumps(cg))
        check("agreementCheckbox is refused by the checkbox-group guard even targeted directly, and was never ticked",
              cg["agreementGuard"] is False and cg["agreementStillUnchecked"] is True, json.dumps(cg))

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

        # attachResumeFile is async: it waits for Workday's own upload card
        # (file-upload-item-name) because Workday empties input.files after
        # consuming the file — reading input.files alone reported a real,
        # successful upload as a failure on the operator's form.
        attach = page.evaluate("""async () => {
            const file = new File([new Uint8Array([37, 80, 68, 70, 45, 49, 46, 52])],
                                  'resume.pdf', { type: 'application/pdf' });
            const res = await ApplyPilotScanner.attachResumeFile(document, file);
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

        # Workday upload path, exercised DIRECTLY and unconditionally. (An
        # earlier version of this check was wrapped in `if shown:` and silently
        # never ran, because the generic attach picked a different dropzone on
        # this page. A check that can skip itself proves nothing.)
        wd_up = page.evaluate("""async () => {
            const S = ApplyPilotScanner, input = document.getElementById('wd_resume_input');
            const before = S.findWorkdayUploadedFilename(document);
            const dt = new DataTransfer();
            dt.items.add(new File([new Uint8Array([37, 80, 68, 70])], 'resume.pdf',
                                  { type: 'application/pdf' }));
            input.files = dt.files;
            input.dispatchEvent(new Event('change', { bubbles: true }));
            const confirmed = await S.waitForWorkdayUploadSuccess(document, 'resume.pdf', 5000);
            const inputFilesAfter = input.files ? input.files.length : -1;
            const shown = S.findWorkdayUploadedFilename(document);
            const again = await S.attachResumeFile(document,
                new File([new Uint8Array([37, 80, 68, 70])], 'resume.pdf', { type: 'application/pdf' }));
            const cards = document.querySelectorAll('[data-automation-id="file-upload-item"]').length;
            return { before, confirmed, inputFilesAfter, shown, again, cards };
        }""")
        check("the Workday mock EMPTIES input.files after consuming the file (as real Workday does)",
              wd_up["inputFilesAfter"] == 0, json.dumps(wd_up))
        check("upload is CONFIRMED from Workday's own file card, not input.files",
              bool(wd_up["confirmed"]) and "resume" in (wd_up["shown"] or ""), json.dumps(wd_up))
        check("a SECOND attach does NOT upload a duplicate résumé",
              wd_up["again"].get("attached") is not True
              and "already" in json.dumps(wd_up["again"]).lower(), json.dumps(wd_up["again"]))
        check("still exactly one uploaded-file card", wd_up["cards"] == 1, json.dumps(wd_up))

        # NEGATIVE CONTROL (project brief §5, "two upload channels"): this dropzone answers
        # BOTH a native 'drop' (see test-page.html) and 'change', each independently adding a
        # card -- exactly the shape that produced a duplicate attachment before the fix. Cleans
        # up the cards the tests above already created so this exercises one fresh attach() end
        # to end, through the real code path (unlike the "again" duplicate-prevention check
        # above, which short-circuits before ever reaching the drop-fallback logic at all).
        wd_double = page.evaluate("""async () => {
            const S = ApplyPilotScanner;
            document.getElementById('wd_resume_uploaded_area').innerHTML = '';
            document.getElementById('wd_resume_input').value = '';
            // Isolate the Workday dropzone as the ONLY résumé-eligible candidate on this shared
            // page: findResumeFileTarget() would otherwise rank gh_resume_input above it (the
            // earlier "5b" check already used gh_resume_input and left a file sitting in it),
            // which would silently test the wrong input entirely -- a plain file input has no
            // Workday mock behaviour at all, so that would report "success" without ever
            // exercising attachResumeFile()'s drop-fallback logic this control exists to prove.
            const others = ['cover_letter_upload', 'attach_upload', 'gh_resume_input'].map(id => document.getElementById(id));
            const removed = others.map(el => ({ el, parent: el.parentElement, next: el.nextSibling }));
            removed.forEach(({ el, parent }) => parent.removeChild(el));
            try {
                const file = new File([new Uint8Array([37, 80, 68, 70])], 'no-duplicate.pdf', { type: 'application/pdf' });
                const target = S.findResumeFileTarget(document);
                const res = await S.attachResumeFile(document, file);
                const cards = document.querySelectorAll('[data-automation-id="file-upload-item"]').length;
                return { targetId: target && target.el.id, res, cards };
            } finally {
                removed.forEach(({ el, parent, next }) => parent.insertBefore(el, next));
            }
        }""")
        check("this negative control genuinely targets the Workday dropzone (not some other file input)",
              wd_double.get("targetId") == "wd_resume_input", json.dumps(wd_double))
        check("attachResumeFile() on a widget answering BOTH change and drop still reports success",
              (wd_double.get("res") or {}).get("attached") is True, json.dumps(wd_double))
        check("NEGATIVE CONTROL: exactly ONE uploaded-file card results, never two, from a single attachResumeFile() call",
              wd_double["cards"] == 1, json.dumps(wd_double))

        # The cover-letter input must still be empty — attaching to the wrong
        # field would send the résumé as a cover letter.
        cover = page.evaluate(
            """() => { const el = document.getElementById('cover_letter_upload');
                       return el ? (el.files ? el.files.length : -1) : -2; }""")
        check("the cover-letter input received nothing", cover == 0, str(cover))

        # And the guard must hold: no click path was introduced by attachment.
        check("attachment introduced no submit click (page still on the form)",
              page.url.startswith("file:"), page.url)

        # 6. Shadow DOM (real Chrome, real layout engine): scanAll/scanFields and capture.js
        #    must traverse OPEN shadow roots (recursively), with fills/guards working on
        #    elements inside them; a CLOSED shadow root must stay completely unreachable; and a
        #    submit button living inside the SAME open shadow root as the field being filled
        #    must never be clicked by any scan/fill path -- though a DIRECT click bypassing
        #    every guard must still genuinely submit (a real trap, not a vacuous one). See
        #    test-page.html's own "Shadow DOM" fixture block for the exact markup.
        page.add_script_tag(path=str(EXT / "capture.js"))
        shadow = page.evaluate("""async () => {
            const before = ApplyPilotScanner.scanAll(document);
            const shadowField = before.fields.find(f => f.name === 'shadow_name');
            const nestedField = before.fields.find(f => f.name === 'shadow_nested');
            const hasClosed = before.fields.some(f => f.name === 'shadow_closed_unreachable');
            const hasHidden = before.fields.some(f => f.name === 'shadow_hidden');

            let fillOk = false, readback = '';
            if (shadowField) {
                const entry = before.registry[shadowField.id];
                fillOk = await ApplyPilotScanner.applyFill(entry, 'Ada Lovelace');
                const input = document.getElementById('shadow_dom_host').shadowRoot.getElementById('shadow_name_input');
                readback = input.value;
            }
            const submitCountAfterFill = window.__SHADOW_FORM_SUBMIT_COUNT__ || 0;

            // Genuine trap, not vacuous: bypassing every guard with a direct click DOES submit.
            document.getElementById('shadow_dom_host').shadowRoot.getElementById('shadow_submit_btn').click();
            const submitCountAfterDirectClick = window.__SHADOW_FORM_SUBMIT_COUNT__ || 0;

            const hiddenInput = document.getElementById('shadow_dom_hidden_host').shadowRoot.getElementById('shadow_hidden_input');
            const hiddenVisible = ApplyPilotScanner.isVisible(hiddenInput);

            const structure = ApplyPilotCapture.captureStructure(document);
            const shadowNodeIds = structure.nodes.filter(n => n.shadow).map(n => n.id);

            return {
                shadowFieldFound: !!shadowField, shadowFieldTagged: shadowField && shadowField.shadow === true,
                nestedFieldFound: !!nestedField, hasClosed, hasHidden, hiddenVisible,
                fillOk, readback, submitCountAfterFill, submitCountAfterDirectClick,
                shadowNodeIds,
            };
        }""")
        check("scanAll() finds the field inside the open shadow root, tagged shadow:true",
              shadow["shadowFieldFound"] and shadow["shadowFieldTagged"], json.dumps(shadow))
        check("scanAll() also finds the field inside the NESTED open shadow root (recursion)",
              shadow["nestedFieldFound"], json.dumps(shadow))
        check("scanAll() reports zero fields for the CLOSED shadow root (unreachable, by design)",
              not shadow["hasClosed"], json.dumps(shadow))
        check("isVisible() crosses the shadow boundary via .host: aria-hidden LIGHT-DOM ancestor hides a field inside an open shadow root (real layout engine)",
              shadow["hiddenVisible"] is False, json.dumps(shadow))
        check("...and that hidden shadow field never turns up in scanAll() at all",
              not shadow["hasHidden"], json.dumps(shadow))
        check("applyFill() fills a text input living inside an open shadow root, and it reads back (real Chrome)",
              shadow["fillOk"] is True and shadow["readback"] == "Ada Lovelace", json.dumps(shadow))
        check("none of the scanning/filling above ever clicked the submit button living inside the SAME shadow root",
              shadow["submitCountAfterFill"] == 0, json.dumps(shadow))
        check("bypassing every guard and clicking the shadow-DOM submit button directly DOES fire its form's submit handler (real trap, not vacuous)",
              shadow["submitCountAfterDirectClick"] == 1, json.dumps(shadow))
        check("captureStructure() (real Chrome) includes nodes from inside open shadow roots, tagged shadow:true",
              "shadow_name_input" in shadow["shadowNodeIds"] and "shadow_submit_btn" in shadow["shadowNodeIds"],
              json.dumps(shadow["shadowNodeIds"]))
        check("captureStructure() also reaches the NESTED open shadow root",
              "shadow_nested_input" in shadow["shadowNodeIds"], json.dumps(shadow["shadowNodeIds"]))
        check("captureStructure() never captures anything from the CLOSED shadow root (unreachable)",
              "shadow_closed_input" not in shadow["shadowNodeIds"], json.dumps(shadow["shadowNodeIds"]))

        # 7. nothing navigated
        check("page never navigated away", page.url.startswith("file:"), page.url)
    finally:
        ctx.close()

print(f"\n{len(failures)} failure(s): {failures}" if failures else "\nALL CHROME CHECKS PASSED")
sys.exit(1 if failures else 0)
