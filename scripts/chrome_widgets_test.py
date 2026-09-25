"""Real-Chromium test for the choice-widget driver: react-select / generic ARIA comboboxes,
Ashby Yes/No button groups, checkbox groups, and Lever's location type-ahead.

Why a separate script from chrome_load_test.py (which already covers general scanning,
Workday, and the "Add Another"/résumé/click-guard machinery in a real browser): this file
exercises ONLY the NEW widget kinds added alongside it (see extension/scanner.js's "Choice
widgets" section and extension/test-page.html's "ats-widgets-form" / "lever_label_fixtures" /
"ashby-no-form-container" sections). jsdom already covers the logic exhaustively
(extension/selftest.js); what only a real browser can confirm is that react-select's
mousedown-commits-not-click behavior, real CSS layout/visibility, and real setTimeout-driven
debounce timing all still behave the way the mocks (and the ground truth they were built from)
say they do.

Loads THIS WORKTREE's own extension/ directory (resolved relative to this file), never a
hardcoded path to a different checkout -- otherwise a worktree's own scanner.js changes would
never actually be exercised by this script.
"""
import pathlib
import sys
import tempfile

EXT = pathlib.Path(__file__).resolve().parent.parent / "extension"
PAGE = (EXT / "test-page.html").as_uri()

from playwright.sync_api import sync_playwright  # noqa: E402 -- after EXT/PAGE consts

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(name)


user_dir = tempfile.mkdtemp(prefix="apc-chrome-widgets-")
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
        page.goto(PAGE)
        page.add_script_tag(path=str(EXT / "scanner.js"))

        result = page.evaluate("() => ApplyPilotScanner.scanAll(document)")
        fields = result["fields"]
        by_label = {f["label"]: f for f in fields}
        print(f"\nscanned {len(fields)} fields in real Chrome\n")

        check("scanner runs in real Chrome and finds fields", len(fields) >= 20, f"{len(fields)} found")

        # -----------------------------------------------------------------------------------
        # 1. Combobox scanning shape
        # -----------------------------------------------------------------------------------
        combo_fields = [f for f in fields if f.get("widget") == "combobox"]
        check("combobox widgets scanned: Country/School/Languages/Role/Preferred office/Current location",
              len(combo_fields) >= 6, str(sorted(f["label"] for f in combo_fields)))
        check("Country combobox scanned with its real label (trailing '*' preserved, matching Greenhouse's own markup)",
              by_label.get("Country*", {}).get("tag") == "input" and by_label["Country*"]["widget"] == "combobox")

        # -----------------------------------------------------------------------------------
        # 2. Combobox fill: static list, real mousedown-then-click commit, real CSS visibility
        # -----------------------------------------------------------------------------------
        def fill_combobox(label, value):
            return page.evaluate(
                """async ([label, value]) => {
                    const S = ApplyPilotScanner;
                    const scanned = S.scanFields(document);
                    const field = scanned.fields.find(f => f.label === label);
                    if (!field) return { error: 'field not found: ' + label };
                    const entry = scanned.registry[field.id];
                    const ok = await S.applyFill(entry, value);
                    return { ok, committed: S.getComboboxCommittedValue(entry), inputValue: entry.input.value };
                }""",
                [label, value],
            )

        r = fill_combobox("Country*", "United States of America")
        check("Country combobox (real Chrome): mouseup opens the menu, mousedown+click commits the option",
              r.get("ok") is True and r.get("committed") == "United States of America", str(r))
        check("Country combobox (real Chrome): the search input is EMPTY after a genuine commit",
              r.get("inputValue") == "", str(r))

        # NEGATIVE CONTROL: typed-but-not-selected text must never read back as committed.
        typed_only = page.evaluate(
            """() => {
                const S = ApplyPilotScanner;
                const scanned = S.scanFields(document);
                const field = scanned.fields.find(f => f.label === 'Role*');
                const entry = scanned.registry[field.id];
                S.setNativeValue(entry.input, 'Software Engineer');
                return S.getComboboxCommittedValue(entry);
            }"""
        )
        check("NEGATIVE CONTROL (real Chrome): typed-but-not-selected combobox text is never a committed value",
              typed_only == "", repr(typed_only))

        # NEGATIVE CONTROL: two rendered options both contain the value -- must stay unfilled.
        r = fill_combobox("Role*", "Engineer")
        check("NEGATIVE CONTROL (real Chrome): ambiguous combobox match ('Engineer' -> 2 options) stays unfilled",
              r.get("ok") is False and r.get("committed") == "", str(r))

        # Async/filtered catalog -- real setTimeout-driven debounce.
        r = fill_combobox("School*", "University of Washington")
        check("School combobox (real Chrome, async catalog): types to filter, waits for the real debounce, commits the exact match",
              r.get("ok") is True and r.get("committed") == "University of Washington", str(r))
        r_bad = fill_combobox("School*", "Underwater Basket Weaving University")
        check("NEGATIVE CONTROL (real Chrome): async catalog with no match never blind-picks the first alphabetical row",
              r_bad.get("ok") is False, str(r_bad))

        # Multi-select chips.
        r = fill_combobox("Languages spoken*", ["English", "French"])
        check("multi-select combobox (real Chrome): a list value adds each option as its own chip",
              r.get("ok") is True and r.get("committed") == "English, French", str(r))

        # Generic ARIA combobox, no react-select classes.
        r = fill_combobox("Preferred office", "Austin")
        check("generic ARIA combobox (real Chrome, opened via ArrowDown): filled and verified via aria-activedescendant",
              r.get("ok") is True and r.get("committed") == "Austin", str(r))

        # -----------------------------------------------------------------------------------
        # 3. Ashby button groups: scoped clicks, decoy submit never clicked, real no-form shape
        # -----------------------------------------------------------------------------------
        bg_fields = [f for f in fields if f.get("widget") == "button-group"]
        check("button groups scanned: work-authorization, sponsorship, decoy-adjacent, and the no-form real shape",
              len(bg_fields) == 4, str([f["label"] for f in bg_fields]))

        def fill_button_group(label, value):
            return page.evaluate(
                """async ([label, value]) => {
                    const S = ApplyPilotScanner;
                    const scanned = S.scanFields(document);
                    const field = scanned.fields.find(f => f.label === label);
                    if (!field) return { error: 'field not found: ' + label };
                    const entry = scanned.registry[field.id];
                    const ok = await S.applyFill(entry, value);
                    return { ok };
                }""",
                [label, value],
            )

        page.evaluate("() => { window.__ATS_WIDGETS_SUBMIT_COUNT__ = 0; window.__ASHBY_NOFORM_SUBMIT_COUNT__ = 0; window.__FORM_SUBMITTED__ = false; }")

        r = fill_button_group("Are you permanently authorized to work in the United States without visa sponsorship?", "No")
        check("Ashby work-authorization button group (real Chrome): 'No' clicked and verified",
              r.get("ok") is True, str(r))
        r = fill_button_group("Will you now or in the future require sponsorship to work in the US?", "Yes")
        check("Ashby sponsorship button group (real Chrome): 'Yes' clicked and verified, a SEPARATE question",
              r.get("ok") is True, str(r))
        r = fill_button_group("Are you comfortable relocating?", "Yes")
        check("button group beside a decoy submit button (real Chrome) still fills correctly",
              r.get("ok") is True, str(r))
        check("the decoy submit button next to it was NEVER clicked (real Chrome, zero submissions)",
              page.evaluate("() => window.__ATS_WIDGETS_SUBMIT_COUNT__ || 0") == 0)

        # Real Ashby shape: no <form> at all, every button (including the decoy) type-less.
        no_form_shape = page.evaluate(
            """() => {
                const yes = document.getElementById('ashby_noform_yes');
                const decoy = document.getElementById('ashby_noform_submit');
                return { yesType: yes.type, yesForm: yes.form, decoyType: decoy.type, decoyForm: decoy.form };
            }"""
        )
        check("fixture sanity (real Chrome): the no-form option button is genuinely type-less (type='submit') with no form owner",
              no_form_shape["yesType"] == "submit" and no_form_shape["yesForm"] is None, str(no_form_shape))
        r = fill_button_group("Are you legally authorized to work in this country?", "No")
        check("real Ashby shape (real Chrome, type-less + no <form>): 'No' clicked and verified via isChoiceButtonSafe's relaxed path",
              r.get("ok") is True, str(r))
        check("the type-less DECOY beside it (also no form) was never clicked (real Chrome)",
              page.evaluate("() => window.__ASHBY_NOFORM_SUBMIT_COUNT__ || 0") == 0)

        # Proof the decoys are REAL traps in a real browser: bypass every guard, click directly.
        trap = page.evaluate(
            """() => {
                document.getElementById('ashby_decoy_submit').click();
                document.getElementById('ashby_noform_submit').click();
                return { ats: window.__ATS_WIDGETS_SUBMIT_COUNT__ || 0, noform: window.__ASHBY_NOFORM_SUBMIT_COUNT__ || 0 };
            }"""
        )
        check("bypassing every guard and clicking the decoys directly DOES fire their click/submit handlers (real Chrome, genuine traps)",
              trap["ats"] >= 1 and trap["noform"] >= 1, str(trap))

        # -----------------------------------------------------------------------------------
        # 4. Checkbox groups: scanning shape + fill, across all three real ATS shapes
        # -----------------------------------------------------------------------------------
        cg_fields = [f for f in fields if f.get("type") == "checkbox-group"]
        check("checkbox groups scanned: Greenhouse (shared name[]), Ashby (fieldset), Lever (cards[..][fieldN])",
              len(cg_fields) == 3, str([(f["name"], f["label"]) for f in cg_fields]))
        gh_group = next((f for f in cg_fields if f["name"] == "question_lang[]"), None)
        check("Greenhouse checkbox group labelled from its <fieldset><legend>, required marker stripped (real Chrome)",
              gh_group is not None and gh_group["label"] == "What language(s) are you fluent in?", str(gh_group))
        lever_group = next((f for f in cg_fields if f["name"].startswith("cards[92a51f92")), None)
        check("Lever checkbox group gets its real question text from .application-question .application-label .text",
              lever_group is not None and lever_group["label"] == "Have you previously worked at this company as an intern?",
              str(lever_group))

        cg_fill = page.evaluate(
            """async () => {
                const S = ApplyPilotScanner;
                const scanned = S.scanFields(document);
                const field = scanned.fields.find(f => f.type === 'checkbox-group' && f.name === 'question_lang[]');
                const entry = scanned.registry[field.id];
                const ok = await Promise.resolve(S.applyFill(entry, ['Spanish', 'French']));
                return {
                    ok,
                    en: document.getElementById('gh_lang_en').checked,
                    es: document.getElementById('gh_lang_es').checked,
                    fr: document.getElementById('gh_lang_fr').checked,
                };
            }"""
        )
        check("checkbox-group fill (real Chrome): a LIST value ticks each matching option, leaves the rest untouched",
              cg_fill.get("ok") is True and cg_fill.get("es") is True and cg_fill.get("fr") is True and cg_fill.get("en") is False,
              str(cg_fill))

        # -----------------------------------------------------------------------------------
        # 5. Lever location type-ahead: real mousedown commit, real debounce timing
        # -----------------------------------------------------------------------------------
        def fill_lever(value):
            return page.evaluate(
                """async (value) => {
                    const S = ApplyPilotScanner;
                    const scanned = S.scanFields(document);
                    const field = scanned.fields.find(f => f.label === 'Current location');
                    if (!field) return { error: 'Current location field not found' };
                    const entry = scanned.registry[field.id];
                    const ok = await S.applyFill(entry, value);
                    return { ok, hidden: document.getElementById('lever_selected_location').value };
                }""",
                value,
            )

        check("Lever location field scanned with a CLEAN label (real Chrome; no dropdown/status text pollution)",
              by_label.get("Current location") is not None, str(sorted(by_label)[:5]))

        r = fill_lever("Seattle")
        check("NEGATIVE CONTROL (real Chrome): Lever location city alone matches two states -- ambiguous, never guesses",
              r.get("ok") is False and r.get("hidden") == "", str(r))
        r = fill_lever("Seattle, Washington")
        check("Lever location (real Chrome): city + full state name resolves to the one matching suggestion, committed via mousedown",
              r.get("ok") is True and r.get("hidden") == "Seattle, Washington", str(r))
        page.evaluate("() => { document.getElementById('lever_selected_location').value = ''; }")
        r = fill_lever("Austin, TX")
        check("Lever location (real Chrome): city + 2-letter state code also resolves via the US state table",
              r.get("ok") is True and r.get("hidden") == "Austin, Texas", str(r))

        # -----------------------------------------------------------------------------------
        # 6. Ambiguity + read-back fixes (pure-logic checks, still worth confirming they made
        #    it into the real Chrome-loaded copy of scanner.js, not just the jsdom one)
        # -----------------------------------------------------------------------------------
        mco = page.evaluate(
            """() => {
                const S = ApplyPilotScanner;
                return {
                  engineer: S.matchChoiceOption('Engineer', ['Software Engineer', 'Site Engineer']),
                  yesAmbiguous: S.matchChoiceOption('Yes', ['Yes, I am a U.S. citizen or permanent resident', 'Yes, I am authorized but will require sponsorship', 'No']),
                  declineAssertion: S.matchChoiceOption('Decline to self-identify', ['Yes, I self-identify as LGBTQ+']),
                };
            }"""
        )
        check("containment tier ambiguity (real Chrome): 'Engineer' matching 2 options never guesses the first",
              mco["engineer"] == -1, str(mco))
        check("yes/no family ambiguity (real Chrome): 'Yes' matching 2 different 'Yes, ...' statements never guesses",
              mco["yesAmbiguous"] == -1, str(mco))
        check("decline family (real Chrome): an option that ALSO asserts an identity claim is rejected",
              mco["declineAssertion"] == -1, str(mco))

        readback = page.evaluate(
            """() => {
                const S = ApplyPilotScanner;
                const numEl = document.getElementById('revert_on_input_number');
                const badNum = S.applyFill({ kind: 'element', el: numEl }, '120000 USD');
                const numValueAfterBad = numEl.value; // captured BEFORE the next fill overwrites it
                const goodNum = S.applyFill({ kind: 'element', el: numEl }, '120000');
                const numValueAfterGood = numEl.value;
                const revertEl = document.getElementById('revert_on_input_field');
                const entry = { kind: 'element', el: revertEl };
                const badText = S.applyFill(entry, 'this will be reverted');
                return { badNum, numValueAfterBad, goodNum, numValueAfterGood, badText, textValue: revertEl.value, reason: entry._lastReason };
            }"""
        )
        check("read-back verification (real Chrome): '120000 USD' into input[type=number] is honestly reported as failure",
              readback["badNum"] is False and readback["numValueAfterBad"] == "", str(readback))
        check("read-back verification (real Chrome): a genuinely valid number still reports success",
              readback["goodNum"] is True and readback["numValueAfterGood"] == "120000", str(readback))
        check("read-back verification (real Chrome): a page that reverts the value on 'input' reports failure, not a blind true",
              readback["badText"] is False and readback["textValue"] == "", str(readback))

        # -----------------------------------------------------------------------------------
        # 7. Nothing above ever navigated away (the deliberate bypass-and-click proofs above
        #    DO set window.__FORM_SUBMITTED__ / the per-decoy counters on purpose -- that is
        #    what proves the decoys are real traps, not vacuous ones -- so the guarded fills'
        #    own zero-submission checks earlier are what matters, not a blanket "never" here).
        # -----------------------------------------------------------------------------------
        check("page never navigated away", page.url.startswith("file:"), page.url)
    finally:
        ctx.close()

print(f"\n{len(failures)} failure(s): {failures}" if failures else "\nALL CHROME WIDGET CHECKS PASSED")
sys.exit(1 if failures else 0)
