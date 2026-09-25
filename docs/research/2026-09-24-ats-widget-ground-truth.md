# ATS widget ground truth: Workday dates, skills, dropdowns, VD/Self-ID, upload; Greenhouse, Ashby, Lever

Date: 2026-09-24. Scope: the Workday widget paths in `extension/scanner.js` (working tree of 2026-09-24; line numbers are `~L`, the file is being edited concurrently), plus gaps on Greenhouse/Ashby/Lever.

**Why.** Workday support was built twice from guessed markup, and mocks built from the same guesses passed while the real form failed. The user reports that the fill "gets hung on the dates part and skills part" on their tenant, while Jobright fills those fields fine. This brief replaces guesses with what working code does against real sites.

**Legend**
- Every claim cites repo@sha + path + lines. All 13 repos were downloaded as tarballs of the default branch on 2026-09-24, and the shas are the branch HEADs reported by the GitHub API that day.
- **INFERRED**: my inference, not something read in code.
- **UNVERIFIED**: one source claims it and nothing corroborates it.
- *content-script-grade*: the source sends synthetic (`isTrusted=false`) events from JS, like we do. Playwright/Selenium/Puppeteer sources send trusted CDP input. Their **selectors and DOM facts transfer** to us. Their **event recipes do not**.

---

## 0. Sources examined

| # | Repo @ HEAD (commit date) | Kind | Events | Weight |
|---|---|---|---|---|
| S1 | [berellevy/job_app_filler](https://github.com/berellevy/job_app_filler) @6d6062cb98bb (2024-12-11; last push 2025-02-11; 28★; published as the "JobAppFiller" Chrome extension) | MV3 extension | synthetic + React props | **High** (Workday, Greenhouse-new) |
| S2 | [tarunravisankar/tso_autofiller](https://github.com/tarunravisankar/tso_autofiller) @fe7b3deac7be (2026-09-14) | MV3 extension; its Workday date fixture contains markup "copied out of a live tenant" (NVIDIA) | synthetic | **High** (date markup and verification) |
| S3 | [theRoadLessOrdinary/add-skills](https://github.com/theRoadLessOrdinary/add-skills) @f1cb0bf2e5f8 (2026-09-15) | DevTools console script for the Workday Skills picker | synthetic | **High** (Skills popup anatomy) |
| S4 | [ajithchandraapplywizz/Workday_Auto](https://github.com/ajithchandraapplywizz/Workday_Auto) @037bc92f88d0 (2026-09-24) | Playwright engine that drives the full live wizard | trusted | High for selectors and flows; not for event recipes |
| S5 | [RohitVarmaSixtyFive/Workday-Automation-Bot](https://github.com/RohitVarmaSixtyFive/Workday-Automation-Bot) @273d66aef9b4 (2025-08-20) | Playwright (Python) | trusted | Medium |
| S6 | [ubangura/Workday-Application-Automator](https://github.com/ubangura/Workday-Application-Automator) @bc4f8372d5cc (2026-06-19; 78★; archived) | Puppeteer | trusted | Medium |
| S7 | [andrewmillercode/Autofill-Jobs](https://github.com/andrewmillercode/Autofill-Jobs) @a82a3165856f (2025-06-27; 36★) | MV3 extension | synthetic | Medium |
| S8 | [ankitsharma38/Workday-Autofill-Assistant](https://github.com/ankitsharma38/Workday-Autofill-Assistant) @b07ee63a3882 (2026-09-18) | MV3 extension + LLM mapper | synthetic | Medium for skills, **Low for dates** |
| S9 | [raghuboosetty/workday](https://github.com/raghuboosetty/workday) @fef73df1c920 (2024-04-18; 30★) | Selenium | trusted | Medium (calendar-popup path) |
| S10 | [amgenene/workday_auto](https://github.com/amgenene/workday_auto) @d7c81625f2d3 (2025-03-20) | Selenium | trusted | Low–Medium |
| S11 | [Jamalfox85/application-autofiller](https://github.com/Jamalfox85/application-autofiller) @4776bbada143 (2026-09-22) | MV3 extension; Greenhouse month fix verified on live Cloudflare/Dropbox/Coinbase boards ([PR #22](https://github.com/Jamalfox85/application-autofiller/pull/22), merged 2026-09-22) | synthetic | **High** for Greenhouse-new; Medium for Lever and Ashby (its own smoke doc marks several flows "not yet proven on a live form") |
| S12 | [Kaitzz/Auto-Apply-Helper](https://github.com/Kaitzz/Auto-Apply-Helper) @b41321835e9e (2026-02-07) | MV3 extension | synthetic | Medium (Greenhouse-new) |
| S13 | [Nevin-Chen/autofilltool](https://github.com/Nevin-Chen/autofilltool) @1e238aeac8f8 (2026-09-24) | MV3 extension | synthetic | **Low**: its Workday fixture is hand-written |

**Not found in any of the 13 repos:**
- `activeListContainer` (0 hits).
- A single masked `MM/YYYY` Workday date input. Only S8's generic, LLM-mapped filler types "MM/YYYY" into whatever element the mapper picked.

### Headline findings
1. **Skills (the most likely hang).** Workday renders prompt and multiselect results in a **portalled popup**, not inside the field. S1 says so in a code comment, and S3, S4, S5, S7 and S8 all query document-wide. `fillWorkdayPromptTerm` searches only inside the field, so every term waits 3 s and then fails. It also clears the input and reports failure when Workday had already auto-committed the term.
2. **Dates: there is one DOM, not two generations.** `dateSectionMonth-input` *is* the `input[role=spinbutton][aria-label=Month]` (verbatim NVIDIA markup, S2). The scanner's claim that `dateSection*-input` "appears in NEITHER working source" is wrong. Date failures come from **technique and verification**, not selectors:
   - read-back is synchronous and reads our own write;
   - there is no yield between month and year;
   - the keydown has no `keyCode`;
   - the fill is gated on the input's visibility, and sources treat that input as possibly hidden;
   - Month/Day/Year wrappers are treated as Month/Year.
3. **The truth for a Workday date part is the `-display` div / `aria-valuetext`, not `input.value`** (S2).
4. **Greenhouse new boards:** `input.select__input[role=combobox]` is filled as a plain text box today. Nothing is committed, but the scanner reports success.
5. **Résumé:** `attachResumeFile` can upload twice on Workday (change event, then a synthetic drop). Its 5 s wait is shorter than any source's (10–21 s).

---

## 1. Workday dates

### 1.1 Ground truth

**DOM: a single family.** The markup below is verbatim from a live NVIDIA tenant ([S2 `tests/nvidia-date.html` L34-35](https://github.com/tarunravisankar/tso_autofiller/blob/fe7b3deac7be/tests/nvidia-date.html#L34-L35), commented "Verbatim from the reported page"; `class` attributes are elided here):

```html
<div data-automation-id="formField-firstYearAttended" data-fkit-id="education-6--firstYearAttended">
 <fieldset><legend><label id="label16"><span>From<abbr aria-hidden="true">*</abbr></span></label></legend>
  <div><div>
   <div aria-hidden="true" id="helpText-education-6--firstYearAttended">current value is YYYY</div>
   <div id="education-6--firstYearAttended" aria-labelledby="hiddenDateValueId-education-6--firstYearAttended"
        role="group" data-automation-id="dateInputWrapper">
    <div tabindex="-1">
     <div id="education-6--firstYearAttended-dateSectionYear" tabindex="-1">
      <div aria-hidden="true" id="education-6--firstYearAttended-dateSectionYear-display"
           data-automation-id="dateSectionYear-display">YYYY</div>
      <input role="spinbutton" aria-describedby="helpText-education-6--firstYearAttended" aria-label="Year"
             aria-valuemax="9999" aria-valuemin="1" aria-valuetext="YYYY"
             id="education-6--firstYearAttended-dateSectionYear-input"
             data-automation-id="dateSectionYear-input" value="">
     </div></div></div>
   <div hidden id="helpText2-education-6--firstYearAttended">use right and left arrows to navigate spin buttons</div>
  </div></div></fieldset></div>
```

The Month+Year work-experience example in the same file ([L10-11](https://github.com/tarunravisankar/tso_autofiller/blob/fe7b3deac7be/tests/nvidia-date.html#L10-L11)) is described as the "Exact shape of the reported field, with a month section added". It follows the same pattern:
- a `…-dateSectionMonth` section with a `-display` div ("MM") and `input[role=spinbutton][aria-label=Month][aria-valuemin=1][aria-valuemax=12][data-automation-id=dateSectionMonth-input]`;
- a `<div aria-hidden="true">/</div>` separator;
- then the Year section.

**Every other source sees the same elements through different attributes:**
- **S4** lists `dateSectionMonth-input`, `dateSectionMonth-display` and `dateInputWrapper` side by side ([`lib/workdayDateFill.mjs` L11-36](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdayDateFill.mjs#L11-L36)). It describes "a pair of spin inputs (dateSectionMonth / dateSectionYear)" ([L1-7](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdayDateFill.mjs#L1-L7)).
- **S6** uses `div[data-automation-id="formField-startDate"] input[data-automation-id="dateSectionMonth-input"]` ([`apply.js` L276-310](https://github.com/ubangura/Workday-Application-Automator/blob/bc4f8372d5cc/apply.js#L276-L310)).
- **S7** matches ids containing `startDate-dateSectionMonth` / `endDate-dateSectionYear` ([`workday.js` L249-268](https://github.com/andrewmillercode/Autofill-Jobs/blob/a82a3165856f/src/public/contentScripts/workday.js#L249-L268)).
- **S10** targets `div[data-automation-id='dateSectionMonth-display']` ([`processing/form_processor.py` L127-158](https://github.com/amgenene/workday_auto/blob/d7c81625f2d3/processing/form_processor.py#L127-L158)).
- **S1** keys on `input[@aria-label='Month'|'Year'|'Day']` under `formField-*`, with `dateInputWrapper` as the wrapper ([`workday/xpaths.ts` L23-35, L64-70](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/xpaths.ts#L23-L70); [`Dates/MonthYear.ts` L23-45](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/Dates/MonthYear.ts#L23-L45)).

**Answer to "which generation does a page use":** no source shows two generations. What does vary:

| Shape | Where seen | Source |
|---|---|---|
| Month + Year | work From/To (`formField-startDate`, `formField-endDate`) | S1 MONTH_YEAR explicitly excludes Day ([xpaths.ts L64-70](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/xpaths.ts#L64-L70)) |
| Year only | education From / "To (Actual or Expected)" (`formField-firstYearAttended`, `formField-lastYearAttended`) | S2 [L35-39](https://github.com/tarunravisankar/tso_autofiller/blob/fe7b3deac7be/tests/nvidia-date.html#L35-L39); S1 YEAR [L23-28](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/xpaths.ts#L23-L28) |
| Month + Day + Year | Self-Identify "Date"; some "Availability/Start date" questions | S1 MONTH_DAY_YEAR ([xpaths.ts L29-35](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/xpaths.ts#L29-L35), [Dates/MonthDayYear.ts L45-72](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/Dates/MonthDayYear.ts#L45-L72)); S4 fills 3 spinbuttons on Self-Identify ([`workdayQuestionFill.mjs` L2394-2440](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdayQuestionFill.mjs#L2394-L2440)) |
| Each section a `div[role=spinbutton]` with no value property | "newer tenants" | **UNVERIFIED**: S2 comment ([`content/date-handler.js` L3-5](https://github.com/tarunravisankar/tso_autofiller/blob/fe7b3deac7be/content/date-handler.js#L3-L5)); its own verbatim capture uses `<input>` |
| Spinbuttons rendered lazily until the wrapper is clicked | some tenants | **INFERRED** from S4: if nothing is found it clicks `dateInputWrapper`/`dateSectionMonth` and re-scans ([L589-597](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdayDateFill.mjs#L589-L597)); comment "Activate the widget (lazy spins / display divs)" ([L619-623](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdayDateFill.mjs#L619-L623)) |

**How working code sets one date part**

| Source | Recipe | Events |
|---|---|---|
| S1 (current) | 1. `el.value = (target-1)`. 2. `dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowUp',code:'ArrowUp',keyCode:38,which:38,bubbles:true,cancelable:true}))`. 3. `el.click()`. Each part is `await`ed in a serial queue (month, then year). | synthetic. [Dates/utils.ts L42-52](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/Dates/utils.ts#L42-L52), [shared/utils/events.ts L1-13, L69](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/shared/utils/events.ts#L1-L13), [MonthYear.ts L57-66](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/Dates/MonthYear.ts#L57-L66), [fieldFillerQueue.ts L20-52](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/shared/utils/fieldFillerQueue.ts#L20-L52) |
| S1 (previous, commented out) | Called React `onKeyDown({nativeEvent:{key:'Up'}, currentTarget:{value: target-1}})`, slept 100 ms, looped up to twice, then clicked. Comment: *"the input elements need to be clicked after calling the onkeydown. Also, sometimes, we have to send the onKeyDown event more than once"*. This shows the handler increments from `currentTarget.value`. | [Dates/utils.ts L54-82](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/Dates/utils.ts#L54-L82) |
| S2 | Three attempts. Each is verified for ≤900 ms against the rendered display, and all parts are cleared between attempts: (1) assign each part with the native setter + `InputEvent('input',{inputType:'insertText'})`, blurring once at the end; (2) type every digit into the first part and let the widget advance; (3) type each part separately. A keystroke is keydown/keypress (key, `code:'Digit#'`, keyCode/charCode/which) + beforeinput/input + keyup, 20 ms apart. | synthetic. [date-handler.js L67-96](https://github.com/tarunravisankar/tso_autofiller/blob/fe7b3deac7be/content/date-handler.js#L67-L96), [utils/normalization.js L89-104](https://github.com/tarunravisankar/tso_autofiller/blob/fe7b3deac7be/utils/normalization.js#L89-L104) |
| S4 | `click({force:true})` → 70 ms → Ctrl+A, Delete, Backspace → press digits 35 ms apart → verify → retry with `pressSequentially` → re-write month if it drifted → Enter on year. Header: *"Do not Tab between segments and do not click the calendar icon."* | trusted. [L530-571](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdayDateFill.mjs#L530-L571), [L619-671](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdayDateFill.mjs#L619-L671) |
| S6 | Focus `dateSection*-input`, `keyboard.type` the digits with a 100 ms delay; month, then year. | trusted. [apply.js L276-310](https://github.com/ubangura/Workday-Application-Automator/blob/bc4f8372d5cc/apply.js#L276-L310) |
| S10 | `send_keys` to the `dateSection*-display` div. | trusted. [form_processor.py L127-158](https://github.com/amgenene/workday_auto/blob/d7c81625f2d3/processing/form_processor.py#L127-L158) |
| S9 | **Calendar popup, clicks only.** Click `div[role=button][data-automation-id=dateIcon]` in the formField. While `span[data-automation-id=monthPickerSpinnerLabel]` ≠ year, click `button[data-automation-id=monthPickerLeftSpinner]`. Then click `//label[text()='<Mon>']`. | trusted. [workday.py L223-237](https://github.com/raghuboosetty/workday/blob/fef73df1c920/workday.py#L223-L237) |

**How working code verifies**
- S2 treats the rendered display text or `aria-valuetext` as *"the honest answer to 'did the page accept this value'; an input's value property only reports what we wrote into it"* ([L26-32](https://github.com/tarunravisankar/tso_autofiller/blob/fe7b3deac7be/content/date-handler.js#L26-L32)). It treats "MM"/"DD"/"YYYY" as the empty state ([L15](https://github.com/tarunravisankar/tso_autofiller/blob/fe7b3deac7be/content/date-handler.js#L15)) and accepts "08" shown with `aria-valuenow="8"` ([L41-52](https://github.com/tarunravisankar/tso_autofiller/blob/fe7b3deac7be/content/date-handler.js#L41-L52)).
- S2 also reads the field's error text after filling and **clears the date** if the form rejected it ([L163-169](https://github.com/tarunravisankar/tso_autofiller/blob/fe7b3deac7be/content/date-handler.js#L163-L169)).
- S4 reads `value || aria-valuenow || aria-valuetext || textContent` and rejects placeholders ([L484-515](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdayDateFill.mjs#L484-L515)).

**Pitfalls the sources document**
- **Editing the year can clear the month.** S4 re-checks and re-writes the month after the year ([L645-653](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdayDateFill.mjs#L645-L653)).
- **Digit typing produced wrong dates on a live tenant.** S2 names two causes: a widget "that advances focus between its own sections while discarding assigned values", and one "whose keys land in the wrong section entirely" ([TEST-RESULTS.md L113-119](https://github.com/tarunravisankar/tso_autofiller/blob/fe7b3deac7be/TEST-RESULTS.md#L113-L119)). ArrowUp-from-(N-1), S1's recipe, does not depend on which section has focus.
- **The keydown sometimes has to be sent more than once** (S1 [utils.ts L60-62](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/Dates/utils.ts#L60-L62)).
- **The input may be hidden behind the display div.** S2: *"A section may still be backed by an input that is visually hidden; prefer it"* ([L6-14](https://github.com/tarunravisankar/tso_autofiller/blob/fe7b3deac7be/content/date-handler.js#L6-L14)). It deliberately keeps INPUT parts even when not visible ([L114-118](https://github.com/tarunravisankar/tso_autofiller/blob/fe7b3deac7be/content/date-handler.js#L114-L118)). S4 always uses `click({force:true})`, which bypasses visibility checks.
- **Validation text** "Must end after start date" is observed. Workday's resume parsing can prefill "To" before "From" (S4 [`workdayExperience.mjs` L1931-1950](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdayExperience.mjs#L1931-L1950)).

**"I currently work here" and the To date**
- The checkbox's id / automation id contains `currentlyWorkHere` (S7 [workday.js L239-243](https://github.com/andrewmillercode/Autofill-Jobs/blob/a82a3165856f/src/public/contentScripts/workday.js#L239-L243)). S4 locates it with `input[type="checkbox"][data-automation-id*="currentlyWork" i]`, `#currentlyWorkHere`, or the label "I currently work here" ([L1687-1776](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdayExperience.mjs#L1687-L1776)).
- When the role is current, S4 **skips To entirely** ([L1899-1907](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdayExperience.mjs#L1899-L1907)).
- **INFERRED:** Workday removes the To field when the box is ticked. So the checkbox must be set before the dates, and the page re-scanned afterwards.

### 1.2 Differences from `scanner.js`
1. **`findWorkdayDateWrappers` (~L835)** keys only on `input[aria-label="Month"|"Year"]`.
   - The comment block above it (~L824-833) says `dateSectionMonth-input` "appears in NEITHER working source it was rebuilt from". S2, S4, S6, S7 and S10 all contradict that.
   - aria-labels are presumably localized on non-English tenants (**INFERRED**). The locale-independent hook is `data-automation-id`.
2. **Same function:** a Month+Day+Year wrapper becomes shape `'my'` and Day is ignored. The Self-Identify "Date" and any MDY question would be written as month+year only, which is an invalid date. S1 handles MDY as a separate shape.
3. **Same function:** a wrapper whose spinbuttons are not rendered yet (lazy) or are `div`s is **dropped silently**. The `'masked'` fallback needs exactly one `<input>`.
4. **`isWorkdaySpinnerInputSafe` (~L859)** requires `isVisible(el)` on the input itself. If real CSS makes that input 0×0 behind `-display` (**INFERRED** from S2/S4's defensive code; needs a live snapshot), every date is refused instantly.
5. **`setWorkdaySpinnerValue` (~L891):**
   - (a) The keydown from `dispatchKeyboardEvent` (~L554) carries only `key`/`code`. S1 also sets `keyCode:38`, `which:38`.
   - (b) It reads `el.value` **synchronously** right after dispatching. If Workday commits asynchronously (**INFERRED**: a React 18 root flushes discrete-event updates in a microtask), the read sees our own `N-1` and reports a false failure. The "retry once" is also synchronous, so it cannot help.
   - (c) It verifies `el.value`, which is what we wrote, rather than Workday's model (`-display` / `aria-valuetext`).
6. **`setWorkdayDateValue` (~L937)** sets month and year back to back with no yield. S1 awaits each part. It never re-checks the month after the year (S4 does) and never checks the field's error text (S2 does).
7. **`getWorkdayDateValue` (~L964)** reads `input.value`, so undo and verification can report our uncommitted write as the field's value.
8. **`typeMaskedTextField` (~L911) and the `'masked'` shape** rest only on S8. No verified capture shows that shape.
9. **The mock** (`extension/test-page.html` ~L405-445) models dates as plain `<input aria-label>`s that commit synchronously on ArrowUp, with no `-display` div and no `data-automation-id`. It cannot catch items 3, 4, 5b, 5c or 6.

### 1.3 Recommended implementation

**Detection**
```js
wrapper  = formField.querySelector('[data-automation-id="dateInputWrapper"]')
part(P)  = wrapper.querySelector('[data-automation-id="dateSection'+P+'-input"]')        // P = Month|Day|Year
        || wrapper.querySelector('input[aria-label="'+P+'"]')                            // English fallback
        || wrapper.querySelectorAll('[role="spinbutton"]')[orderIndex(P)]                // div spinbuttons
display(P) = doc.getElementById(input.id.replace(/-input$/, '-display'))
          || input.parentElement.querySelector('[data-automation-id$="-display"]')
shape = Day ? 'mdy' : Month ? 'my' : 'y'
```
- Gate on the **wrapper's** visibility plus the input being connected and not disabled. Do not gate on the input's own visibility.
- If the wrapper holds no parts, click the wrapper once (a narrowly scoped click on a `div`, not a button), wait 150 ms and re-scan (S4).

**Fill one part.** This is S1's recipe with S2's verification; both are content-script-grade.
1. If `read(P) === target`, skip it.
2. Call `input.focus({preventScroll:true})`. This mirrors S4 and S6; whether it is needed is **INFERRED**.
3. Set `input.value = String(target - 1)`. Use plain assignment like S1 and fire **no** input event.
4. Dispatch keydown `{key:'ArrowUp', code:'ArrowUp', keyCode:38, which:38, bubbles:true, cancelable:true}`, then keyup, then `input.click()`.
5. **`await sleep(60)`**, a macrotask, so the page can commit. Then `read(P)`. If it still shows `target-1`, repeat step 4 once and wait 60 ms again.

**Fill the whole field**
1. Fill the parts in order (month, day, year), awaiting each.
2. `await sleep(150)`, then **re-read every part**, because a year edit can clear the month. Redo any part that drifted, one pass at most.
3. Dispatch `focusout`/blur on the last part.
4. Read the formField's error text (`[data-automation-id="errorMessage"]` or `[data-automation-id*="error"]`). If it mentions a date, report failure.

**`read(P)`** returns the first non-placeholder of: the `-display` text (placeholder `/^[MDY]+$/i`), then `aria-valuetext`, then `aria-valuenow`. Compare numerically.

**Budget:** at most 1.5 s per date field.

**Fallback for MM/YYYY only:** the S9 calendar path. Click `dateIcon`, step `monthPickerLeftSpinner` until `monthPickerSpinnerLabel` shows the year, then click the month label. This uses clicks only, so it would need its own narrowly scoped click guard.

**Ordering:** set "I currently work here" first, wait 300 ms, re-scan. If the box is ticked, report To as "not applicable".

**Negative controls the mock must include** (each should make today's code fail):
- **Asynchronous commit** (`queueMicrotask`/`setTimeout(0)`), so a synchronous read-back is wrong.
- **One model per wrapper.** A year update computed from stale state resets the month unless the filler yields between parts.
- **Truth lives only in `-display` + `aria-valuetext`.** `input.value` keeps whatever the script wrote even when the model rejected it.
- **A 0×0 / `opacity:0` input** under the display div.
- **An MDY wrapper**, and a wrapper labelled `aria-label="Monat"` with correct `data-automation-id`s.
- **A handler that increments from `currentTarget.value`** (S1 L71-78) and ignores `input` events.

---

## 2. Workday prompt / multiselect: Skills, Field of Study, School, "How did you hear"

### 2.1 Ground truth

**Field containers seen**

| Container | Source |
|---|---|
| `formField-skills` | S7 [workday.js L166-168](https://github.com/andrewmillercode/Autofill-Jobs/blob/a82a3165856f/src/public/contentScripts/workday.js#L166-L168) |
| `formField-skillsPrompt` | S6 [apply.js L371](https://github.com/ubangura/Workday-Application-Automator/blob/bc4f8372d5cc/apply.js#L371) |
| `formField-field-of-study`, `formField-schoolItem`, `formField-source`, `formField-sourcePrompt`, `formField-certification`, `formField-country-phone-code` | S1 [xpaths.ts L47-58](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/xpaths.ts#L47-L58) |
| `#source--source`, `[data-automation-id="source--source"]` | S4 [workdaySource.mjs L74-80](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdaySource.mjs#L74-L80) |

**The input**
- It sits inside `div[data-automation-id="multiSelectContainer"]` (S1 [DropdownSearchable.ts L78-83](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/DropdownSearchable.ts#L78-L83); S5 [final.py L2079-2100](https://github.com/RohitVarmaSixtyFive/Workday-Automation-Bot/blob/273d66aef9b4/final.py#L2079-L2100)).
- S7 reaches it through `[data-automation-id="multiselectInputContainer"]`, where the input's automation id is `monikerSearchBox` ([L166-178](https://github.com/andrewmillercode/Autofill-Jobs/blob/a82a3165856f/src/public/contentScripts/workday.js#L166-L178)).
- S3 finds it as `#skills--skills` or `input[data-automation-id="searchBox"]` ([add-skills.js L75-80](https://github.com/theRoadLessOrdinary/add-skills/blob/f1cb0bf2e5f8/add-skills.js#L75-L80)).
- S4 finds it by the label "Type to Add Skills" / "Enter a skill below" ([workdaySkills.mjs L100-111](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdaySkills.mjs#L100-L111)).

**Where results render: a portalled popup. Every source agrees.**
- **S1:** *"This element's id is used to identify the dropdown element, since the dropdown is a popup and not a direct child of this field."* Its XPath is `.//body/div[@data-automation-widget='wd-popup'][//div[@data-associated-widget='<multiSelectContainer id>']]` ([DropdownSearchable.ts L72-102](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/DropdownSearchable.ts#L72-L102)). The link is `multiSelectContainer.id` ⇄ `[data-associated-widget]` inside the popup, and click-scoping uses `closest("[data-associated-widget='<id>']")` ([L104-110](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/DropdownSearchable.ts#L104-L110)).
- **S3:** document-level `[data-automation-id="menuItem"]` ([L82-99](https://github.com/theRoadLessOrdinary/add-skills/blob/f1cb0bf2e5f8/add-skills.js#L82-L99)).
- **S5:** document-level `div[data-automation-id="promptLeafNode"]` after Enter plus 2 s ([final.py L2128-2149](https://github.com/RohitVarmaSixtyFive/Workday-Automation-Bot/blob/273d66aef9b4/final.py#L2128-L2149)).
- **S8:** any leaf whose text starts "Search Results", walking up at most 6 levels, with a document-wide `[data-automation-id*='checkboxItem']` fallback ([filler.js L622-667](https://github.com/ankitsharma38/Workday-Autofill-Assistant/blob/b07ee63a3882/extension/content/filler.js#L622-L667)).
- **S4:** page-wide `[role="option"]` / `[data-automation-id="promptOption"]` ([workdaySkills.mjs L113-136](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdaySkills.mjs#L113-L136)). Its comment: *"Options render in a page-level popup (promptOption), not inside the field"* ([interaction/workdayCustomDropdown.mjs L1-8](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/interaction/workdayCustomDropdown.mjs#L1-L8)).
- **S7:** document-level `.ReactVirtualized__Grid__innerScrollContainer` rows, each with an `aria-label` ([L190-213](https://github.com/andrewmillercode/Autofill-Jobs/blob/a82a3165856f/src/public/contentScripts/workday.js#L190-L213)).
- `activeListContainer`: no source uses it (**UNVERIFIED**).

**Row anatomy of the Skills results** (S3 [L82-106, L164-185](https://github.com/theRoadLessOrdinary/add-skills/blob/f1cb0bf2e5f8/add-skills.js#L82-L185)):
```
[data-automation-id="menuItem"][data-automation-selected="true|false"]      row; "true" marks the highlighted row
  [data-automation-id="promptLeafNode"][data-automation-checked="Checked"]   checked state
    input[data-automation-id="checkboxPanel"]                                 the checkbox
    [data-automation-id="promptOption"][data-automation-label="<text>"]       label text
```
- A "Search Results (N)" header appears above the rows (S8 L622-645).
- The empty state is a leaf reading "No Items." or "No matches found" (S8 [L330-338](https://github.com/ankitsharma38/Workday-Autofill-Assistant/blob/b07ee63a3882/extension/content/filler.js#L330-L338), [L750-756](https://github.com/ankitsharma38/Workday-Autofill-Assistant/blob/b07ee63a3882/extension/content/filler.js#L750-L756)).

**The list is virtualized.** *"Only rows near the current scroll position actually exist in the DOM, so a match that hasn't scrolled into view yet can't be queried for directly."* S3 walks the highlight with ArrowDown instead ([README L52-66](https://github.com/theRoadLessOrdinary/add-skills/blob/f1cb0bf2e5f8/README.md#L52-L66)). S7 hits the ReactVirtualized grid, and S4 scrolls and ArrowDowns to collect options ([workdaySource.mjs L401-431](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdaySource.mjs#L401-L431)).

**What Enter and Tab do**
- **Single-select prompts can auto-commit.** S1 on Field of Study, School and Source: *"typing a query into the inputElement and hitting tab or enter. The dropdown doesn't need to be opened. on tab down, a the value of e.target is used to search for and select the correct answer, if available."* It waits 500 ms for `selectedItemList li` **before** touching the popup ([L135-175](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/DropdownSearchable.ts#L135-L175)).
- **Skills (multiselect) opens a results list** that must be checked (S3 L133-185; S5 L2128-2189; S8 L715-819).
- S6 presses Enter twice: the second Enter takes the highlighted row. It waits 1–5 s ([apply.js L331-336, L367-377](https://github.com/ubangura/Workday-Application-Automator/blob/bc4f8372d5cc/apply.js#L331-L377)).
- S3: the row marked `data-automation-selected="true"` is *"what actually gets checked when Enter is pressed"* (L86-89).

**Timing used by working code**

| Source | Timing |
|---|---|
| S3 | 500 ms after typing before Enter; up to 10 s for any row; 2 s to settle; 1 s per ArrowDown; 2 s after selecting ([L19-26](https://github.com/theRoadLessOrdinary/add-skills/blob/f1cb0bf2e5f8/add-skills.js#L19-L26), L135-153) |
| S5 | 2 s after Enter |
| S8 | polls 15 × 200 ms |
| S4 | 450 ms, then an Enter fallback |
| S1 | 500 ms before, 1 s for the popup |

**How the pick is made**
- **S3:** full pointer sequence at the element's centre (`clientX/Y`, `buttons:1`) on `checkboxPanel`, falling back to `promptLeafNode`. Success is `data-automation-checked="Checked"`. Its README says *"React ignores a bare el.click()"* ([L58-68](https://github.com/theRoadLessOrdinary/add-skills/blob/f1cb0bf2e5f8/add-skills.js#L58-L68)).
- **S8:** plain `checkbox.click()`, then waits for the pill count to grow ([L807-826](https://github.com/ankitsharma38/Workday-Autofill-Assistant/blob/b07ee63a3882/extension/content/filler.js#L807-L826)). S3 and S8 disagree on whether a plain click works, so verification is mandatory.
- **S1** clicks the **first** `promptOption` blindly on single-select prompts ([L177-189](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/DropdownSearchable.ts#L177-L189)). Our rules forbid copying that.

**How a pick is confirmed**
- `ul[data-automation-id='selectedItemList'] li`, **scoped to the formField** (S1 [L51-63](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/DropdownSearchable.ts#L51-L63)).
- Chips `[data-automation-id="selectedItem"]` / `"promptSelectedItem"` (S4 [selectVerified.mjs L198](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/interaction/selectVerified.mjs#L198)), with a `[data-automation-id="delete-item"]` remove button (S4 [engine.mjs L634](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/engine.mjs#L634)).

**Closing the popup between terms**
- S8 sends Escape keydown/keyup on the input (L767-770, L829-832).
- S3 clears the input (L187-189).
- S1 deletes the popup node, noting "Ugly, but better than nothing" (L112-122).

**Hierarchical prompts ("How did you hear")**
- S4: *"cascading: parent click → submenu → child click"*. It opens the popup, clicks the parent, waits 900 ms, takes the options not in the top level as children, presses `ArrowRight` if there are none, clicks the child, waits 600 ms, and verifies the chip ([L1-6](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdaySource.mjs#L1-L6), [L675-740](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdaySource.mjs#L675-L740)).
- S5 does the same with 1.5 s waits ([L2193-2260](https://github.com/RohitVarmaSixtyFive/Workday-Automation-Bot/blob/273d66aef9b4/final.py#L2193-L2260)).
- S1 handles `formField-source`/`sourcePrompt` with a typed search plus Tab. **INFERRED:** typing the leaf ("LinkedIn") searches across categories, so category navigation can be avoided.

**How many skills sources add:** S4 adds exactly 1 required skill ([workdaySkills.mjs L12](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdaySkills.mjs#L12)). S8 stops at 10 pills (L724).

### 2.2 Differences from `scanner.js`
1. **`fillWorkdayPromptTerm` (~L1414) looks only inside the field.** It waits for results in `entry.container`/`entry.formField` only (~L1437-1446).
   - Its comment (~L1426-1436) says "nothing in the ground truth says a prompt's results are portalled away from their own field". S1 L72-77 says exactly that, and every other source queries document-wide.
   - On a real tenant, each term times out after 3 s as "no results". N skills cost about 3N seconds. **This is the most likely cause of the skills hang.**
2. **Same function: no pill check before waiting.** It never looks at `selectedItemList` first, so Workday's own auto-commit (S1) goes unnoticed. After the timeout it calls `clearWorkdayPromptInput` and reports failure even though the pill exists.
3. **`WD_PROMPT_RESULT_SELECTOR` (~L1346)** lacks `menuItem` and `promptLeafNode`. It matches both an outer `role=option` row and the inner `promptOption`, which duplicates candidates. The label should come from `data-automation-label`.
4. **Input events are thin.** There is no "No Items." fast-fail and no debounce before Enter. The Enter keydown lacks `keyCode`/`which` 13 and the keypress/keyup that S3 and S8 send.
5. **Virtualization plus loose matching can pick the wrong skill.** `matchWorkdayPromptOption`'s word-boundary startsWith (~L1377-1400) lets "Java" match a rendered "JavaScript" while the exact "Java" row is not rendered yet (**INFERRED** from S3's documented virtualization). For skills, only exact or "(ACRONYM)" matches are safe.
6. **Verification falls back to the whole document.** Both the check and the read-back use `doc.querySelector('ul[data-automation-id="selectedItemList"]')` (~L1473-1474, ~L1511-1512), so another field's pills can satisfy the check.
7. **The popup is never closed between terms.** A stale popup may intercept the next term (**INFERRED**).
8. **`safeClick(checkbox)` is a bare `.click()`.** S3 says React ignores that on this widget; S8 says it works. The code needs verification through `data-automation-checked` and a fallback.
9. **Detection and safety are narrow.** `findWorkdayPrompts` (~L1348) looks only for `multiSelectContainer`. `isWorkdayPromptInputSafe` requires `isVisible(input)`, which walks `aria-hidden` ancestors. If Workday aria-hides the page while a popup is open (**INFERRED**, unverified), every later term is refused.
10. **The mock is wrong.** In `test-page.html` (~L464-485 plus its script) results render **inside** the formField, which is exactly the guess the ground truth contradicts.

### 2.3 Recommended implementation

**Selectors**
```js
field   = the formField-* (skills | skillsPrompt | field-of-study | schoolItem | source | sourcePrompt | certification)
input   = field.querySelector('[data-automation-id="multiSelectContainer"] input, [data-automation-id="multiselectInputContainer"] input,'
        + ' input[data-automation-id="searchBox"], input[data-automation-id="monikerSearchBox"]')
pills() = field.querySelectorAll('ul[data-automation-id="selectedItemList"] li, [data-automation-id="selectedItem"], [data-automation-id="promptSelectedItem"]')  // field-scoped only
popup() = (id = input.closest('[data-automation-id="multiSelectContainer"]')?.id)
          ? doc.querySelector('[data-associated-widget="'+CSS.escape(id)+'"]')?.closest('[data-automation-widget="wd-popup"]')
          : the ONE wd-popup / [role=listbox] that appeared AFTER our Enter (snapshot before typing; never a pre-existing one)
rows()  = popup.querySelectorAll('[data-automation-id="menuItem"]')
label(r)= r.querySelector('[data-automation-id="promptOption"]')?.getAttribute('data-automation-label') || cleanText(r.textContent)
```

**Per term**
1. If a pill already equals the term (normalized), report success.
2. Snapshot the existing popups. Focus the input, set the value with the native setter, and dispatch `InputEvent('input',{inputType:'insertText',data})`. **Wait 500 ms.**
3. Dispatch keydown, keypress and keyup with `{key:'Enter', code:'Enter', keyCode:13, which:13}`.
4. For up to **8 s**, using a MutationObserver on `body`, take whichever happens first:
   - (a) a **new pill** matching the term: success, because Workday auto-committed;
   - (b) popup rows appear;
   - (c) the popup shows "No Items." / "No matches found": **fail fast**.
5. On (b), look for an exact normalized match or the "(ACRONYM)" form. **For skills, never accept startsWith or substring matches.** If no rendered row matches and the list scrolls, press ArrowDown on the input up to 40 times, 150–300 ms apart. Read the label of `menuItem[data-automation-selected="true"]` each time and stop on an exact match (S3).
6. Select the row:
   - skills: `checkboxPanel.click()`, then wait up to 1.5 s for `promptLeafNode[data-automation-checked="Checked"]` or the pill count to grow. If neither happens, send a pointer sequence with centre `clientX/Y` to `promptLeafNode`;
   - single-select: pointer sequence on the `promptOption`, then wait for the pill.
7. Close: Escape on the input, clear it with the native setter, and wait up to 1 s for the popup to disappear.

**Caps:** at most 10–15 skills or 60 s in total. Report the rest as "not attempted".

**Hierarchical source:** first type the leaf and press Enter (S1's path). Only if a category list appears, click the expected parent, wait up to 1.5 s, click the exact child, and verify the chip in the field.

**Negative controls the mock must include**
- **Results only in the portal:** `body > div[data-automation-widget="wd-popup"] > div[data-associated-widget="<container id>"]`, with nothing inside the formField.
- **A decoy** second wd-popup from another field, opened earlier, listing the same term.
- **Auto-commit:** an exact term commits on Enter with **no** popup.
- **An empty result:** "No Items.".
- **Virtualization:** 8 rendered rows; "JavaScript" is rendered, and "Java" is only reachable with ArrowDown.
- **Latency:** results arrive 1.5–3 s after Enter.
- **Picky checkbox:** it ignores `.click()` but responds to a pointer sequence (and a second variant that does the reverse), with truth in `data-automation-checked`.
- **Cross-field pills:** another field's `selectedItemList` already contains the term.

---

## 3. Workday dropdowns (`button[aria-haspopup="listbox"]`)

### 3.1 Ground truth
- **Opener.** A `button[aria-haspopup="listbox"]` inside `formField-*` (S1 [xpaths.ts L42-46](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/xpaths.ts#L42-L46), [Dropdown.ts L36-38](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/Dropdown.ts#L36-L38)). Named examples: `degree`, `gender`, `nationality`, `addressSection_countryRegion`, `phone-device-type` (S6 [L194-215, L342](https://github.com/ubangura/Workday-Application-Automator/blob/bc4f8372d5cc/apply.js#L194-L215); S9 [L105-116, L189-201](https://github.com/raghuboosetty/workday/blob/fef73df1c920/workday.py#L105-L201)). The button's aria-label is question + value + "required", for example "Are you legally authorized … select one required" (S9 [L172-183](https://github.com/raghuboosetty/workday/blob/fef73df1c920/workday.py#L172-L183)).
- **Opening.**
  - S1 uses a plain `button.click()` when the list is closed; it treats the **presence** of `aria-expanded` as "open" ([Dropdown.ts L50-72](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/Dropdown.ts#L50-L72)).
  - S8 sends a pointer/mouse sequence plus `click`, then waits 450 ms ([filler.js L408-415](https://github.com/ankitsharma38/Workday-Autofill-Assistant/blob/b07ee63a3882/extension/content/filler.js#L408-L415)).
  - S6 clicks, types the option text and presses Enter, i.e. type-ahead ([L339-347](https://github.com/ubangura/Workday-Application-Automator/blob/bc4f8372d5cc/apply.js#L339-L347)).
- **Where the popup lives: portalled.**
  - `//body//ul[@id='<aria-controls>']` (S1 [Dropdown.ts L74-83](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/Dropdown.ts#L74-L83)).
  - A wrapper `div[visibility="opened"]` containing `li[role="option"]` (S5 [L1639-1660](https://github.com/RohitVarmaSixtyFive/Workday-Automation-Bot/blob/273d66aef9b4/final.py#L1639-L1660), [L2276-2295](https://github.com/RohitVarmaSixtyFive/Workday-Automation-Bot/blob/273d66aef9b4/final.py#L2276-L2295)).
  - `ul[role="listbox"][tabindex="-1"]` (S7 [L370](https://github.com/andrewmillercode/Autofill-Jobs/blob/a82a3165856f/src/public/contentScripts/workday.js#L370)).
  - `ul[role='listbox'] li[role='option'] div` (S10 [workday.py L1602-1613](https://github.com/amgenene/workday_auto/blob/d7c81625f2d3/workday.py#L1602-L1613)).
- **Options.** An `li[role=option]` whose child `div` holds the text (S1 [L102-105](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/Dropdown.ts#L102-L105); S9 clicks `//div[text()='Yes']`).
- **Selecting.** S1 calls the li's React `onClick` ([L115-143](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/Dropdown.ts#L115-L143)). S8 sends a pointer sequence plus `click`, then waits 350 ms (L449-457).
- **Verifying and closing.** S1 checks that the button's `innerText` contains the answer and closes by clicking the button twice ([L46-48, L63-68](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/Dropdown.ts#L46-L68)).
- **Long lists are scrolled or virtualized.** S4 scrolls and ArrowDowns to collect everything (workdaySource.mjs L401-431).
- **Voluntary Disclosures variant.** On some live tenants, VD dropdowns are `selectOne`/`selectWidget` + `promptIcon` rather than a `<button>`, with options in a page-level popup (S4 [workdayCustomDropdown.mjs L1-31](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/interaction/workdayCustomDropdown.mjs#L1-L31); fixture [`tests/fixtures/voluntary-eeo-live.html`](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/tests/fixtures/voluntary-eeo-live.html), labelled "live combined blob" and partly modelled).

### 3.2 Differences from `scanner.js`
`fillWorkdayDropdown` (~L1298) mostly matches the ground truth: portal lookup via `aria-controls`, pointer-sequence open and select, and button-text verification. The gaps:
- **(a) Rendered rows only.** It never scrolls or ArrowDowns, so Country, State or Source fail with "no confident match" when the target is off-screen.
- **(b) The wrong listbox is possible.** The fallback in `resolveWorkdayListbox` (~L1026) returns the **first** visible `[role=listbox]`, which may be a stale one. It should prefer one that appeared after the click.
- **(c) Some dropdowns are invisible to the scan.** Only `button[aria-haspopup="listbox"]` is registered (~L1590), so `selectWidget`-style VD dropdowns are missed entirely.
- **(d) Possible double toggle.** The pointer sequence might open and then close a button that toggles on both mousedown and click (**INFERRED**). Check `aria-expanded` after opening and fall back to a plain `.click()`.

### 3.3 Recommended implementation
1. **Open** with `button.click()` (S1). Wait up to 2 s for `aria-expanded="true"` and a visible `getElementById(aria-controls)`. If it did not open, try the pointer sequence once.
2. **Read the options** from `li[role="option"]`, using the normalized text of the child div.
3. **If nothing matches and the list scrolls**, scroll by `clientHeight` (up to 15 times) or press ArrowDown, re-collecting each time. Stop after two rounds with no new labels.
4. **Select** with a pointer sequence (or `.click()`) on the `li`. Within 1 s, the listbox must be gone and the button text must contain the label.
5. **On failure**, press Escape or click the button (S1 closes it with two clicks).

**Negative controls:**
- a 60-item virtualized country list with the target at position 45;
- a stale second listbox left open;
- one option that commits only on `click` and another that commits only on `mousedown`;
- a `selectWidget` dropdown with no `<button>`.

---

## 4. Voluntary Disclosures and Self-Identify (CC-305)

### 4.1 Ground truth

**Page ids:** `voluntaryDisclosuresPage`, `selfIdentificationPage` (S6 [L68, L75](https://github.com/ubangura/Workday-Application-Automator/blob/bc4f8372d5cc/apply.js#L68-L75)).

**VD fields**
- `button[data-automation-id="gender"]`, `button[data-automation-id="nationality"]` (S9 [L189-201](https://github.com/raghuboosetty/workday/blob/fef73df1c920/workday.py#L189-L201)).
- S7 lists ethnicity, race, gender, veteran and disability ([utils.js L98-104](https://github.com/andrewmillercode/Autofill-Jobs/blob/a82a3165856f/src/public/contentScripts/utils.js#L98-L104)).
- Question wordings seen: "Please select your gender", "Are you Hispanic/Latino?", "Please select the ethnicity which most accurately describes how you identify yourself", "Please select the veteran status which most accurately describes how you identify yourself" (S4 [label regexes L574-587](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdayQuestionFill.mjs#L574-L587); fixture richText).

**Option texts seen**

| Question | Options | Source |
|---|---|---|
| Veteran | "I identify as one or more of the classifications of a protected veteran" · "I identify as a veteran, just not a protected veteran" · "I am not a veteran" · "I am not a protected veteran" · "I choose not to disclose" | S4 [voluntary-eeo-live.html L78-88](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/tests/fixtures/voluntary-eeo-live.html#L78-L88) |
| Veteran, variants | "No, I am not a veteran" · "I AM NOT A VETERAN" · "I am not a protected veteran." | S4 [L625-633](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdayQuestionFill.mjs#L625-L633) |
| Race / ethnicity | "Asian (United States of America)" · "White (United States of America)" · "Asian, not Hispanic or Latino (United States of America)" · "Asian (Not Hispanic or Latino)" | S4 fixture L79; L635-641 |
| Hispanic / Latino | "Yes" · "No" · "Not Hispanic or Latino" · "Hispanic or Latino" | S4 fixture L80; L643-645 |
| Gender | "Male" · "Female" (plus tenant-specific options) | S4 L646-648 |

- "Not a veteran" and "not a protected veteran" are **different** answers ([workdayCustomDropdown.mjs L46-60](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/interaction/workdayCustomDropdown.mjs#L46-L60)).
- S4 recognizes these decline phrasings: `choose not to disclose|do not (wish|want) to|decline to|prefer not` (same file, L55).

**Required markers are often missing.** *"Workday Voluntary Disclosures fields frequently omit standard `*` or `aria-required` tags"*, yet Workday blocks the next step while any is still "Select One" (S4 [HANDOVER.md L74-77](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/HANDOVER.md#L74-L77)).

**Terms-and-conditions consent**
- Selector: `input[type='checkbox'][data-automation-id='agreementCheckbox']` (S9 [L204-209](https://github.com/raghuboosetty/workday/blob/fef73df1c920/workday.py#L204-L209)).
- Label texts: "I have read and consent to the terms and conditions", "Privacy Statement", "I confirm that I understand and agree" (S4 [L316-377](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdayQuestionFill.mjs#L316-L377)).

**Self-Identify (CC-305)**
- Field order: Language (a dropdown on some tenants, "English"), **Name** (text), **Date** (Month/Day/Year spinbuttons), then "Please check one of the boxes below:" (S4 [L2388-2463, L2546-2620, L2658+](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdayQuestionFill.mjs#L2388-L2463)).
- S7's field map for this page is name, month-input, day-input, year-input ([utils.js L105-110](https://github.com/andrewmillercode/Autofill-Jobs/blob/a82a3165856f/src/public/contentScripts/utils.js#L105-L110)).

**The disability answer is a checkbox group on tenants seen by S5**
- `fieldset[data-automation-id="disabilityStatus-CheckboxGroup"]` of `input[type=checkbox]` that *"requires exactly one selection"*; each option's label is at `nextElementSibling.nextElementSibling` ([final.py L1880-1925](https://github.com/RohitVarmaSixtyFive/Workday-Automation-Bot/blob/273d66aef9b4/final.py#L1880-L1925)).
- S4 handles it as a radio **or** a checkbox (L2546-2620).
- S1 treats multi-checkbox fields as single-choice: `div[role='cell']` + `label` + `input[type=checkbox][aria-checked]`. It unchecks the current choice, then clicks the answer ([CheckboxesSingle.ts L45-99](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/CheckboxesSingle.ts#L45-L99)).
- Checkbox state updates asynchronously: *"the checked value of the input element takes some time to change after it's clicked"* (S1 [BooleanCheckbox.ts L57-73](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/BooleanCheckbox.ts#L57-L73)).

**CC-305 option texts**
- Current form: "Yes, I have a disability, or have had one in the past" · "No, I do not have a disability and have not had one in the past" · "I do not want to answer" (S4 [L2466](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/workdayQuestionFill.mjs#L2466); S11 [eeoHandlers.ts L97-105](https://github.com/Jamalfox85/application-autofiller/blob/4776bbada143/src/utils/siteRules/eeoHandlers.ts#L97-L105); OFCCP form text as hosted at [SmartRecruiters](https://www.smartrecruiters.com/oneclick-ui/resources/html/ofccpDisability?lang=en) and in [BU's 2023 PDF](https://www.bu.edu/eoo/files/2023/05/503Self-IDForm-04262023.pdf)).
- Pre-2023 wording ("…have a history/record of having a disability", "I don't wish to answer") may remain on stale tenants: **INFERRED** from memory of the prior revision.

### 4.2 Differences from `scanner.js`
- **The disability group is not modelled as one question.** The generic path treats each checkbox as an independent boolean (`setCheckboxValue`), so "exactly one" (unchecking the others) and the label→option mapping are missing (**INFERRED** from the generic path; check against the in-flight EEO work, `matchAnswerFamily` in the working tree).
- **The Self-Identify Date is MDY,** so it is broken by §1.2 item 2.

### 4.3 Recommended implementation
- Treat `fieldset[data-automation-id$="-CheckboxGroup"]`, or a formField with more than one checkbox, as a **single-choice** group. The option label comes from `label[for]` or the `div[role=cell]` text. Click the target, wait up to 1 s until it reads as checked, then uncheck any other checked box.
- Self-Identify Date: fill it as MDY via §1.3, using today's local date. Name: the full legal name.
- EEO dropdowns go through §3 plus the family matching.
- **Never auto-tick `agreementCheckbox` / terms consent.** It is a legal attestation; highlight it for the user instead. This is a policy recommendation.

**Negative controls:**
- disability checkboxes where checking one does **not** uncheck the others;
- a VD dropdown with no `*`;
- a veteran list containing both "I am not a veteran" and "I am not a protected veteran".

---

## 5. Workday résumé upload

### 5.1 Ground truth
- **Field and input.** A formField containing `div[data-automation-id="file-upload-drop-zone"]` (S1 [xpaths.ts L13-17](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/xpaths.ts#L13-L17)). The input is `input[type=file][data-automation-id="file-upload-input-ref"]` (S10 [workday.py L674](https://github.com/amgenene/workday_auto/blob/d7c81625f2d3/workday.py#L674); S6 [L399-407](https://github.com/ubangura/Workday-Application-Automator/blob/bc4f8372d5cc/apply.js#L399-L407); S7 utils.js L95). There is a "Select files" button with `data-automation-id="select-files"` (S8 [scanner.js L107](https://github.com/ankitsharma38/Workday-Autofill-Assistant/blob/b07ee63a3882/extension/content/scanner.js#L107)).
- **After upload.** A `div[data-automation-id="file-upload-item"]` appears, containing `div[data-automation-id="file-upload-item-name"]` and `button[data-automation-id="delete-file"]`. On success a node with `data-automation-id="file-upload-successful"` is **added** (S1 [FileMulti.ts L35-83](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/FileMulti.ts#L35-L83)). S4 also accepts a "Successfully Uploaded" banner ([engine.mjs L504-540](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/engine.mjs#L504-L540)).
- **How files get in.**
  - S1 clicks every `delete-file`, sleeps 50 ms, then calls the drop zone's React `onDrop({dataTransfer:{files:[file]}})` ([L102-122](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/workday/FileMulti.ts#L102-L122)).
  - S7 sets `input.files` and fires `change` ([workday.js L136-155](https://github.com/andrewmillercode/Autofill-Jobs/blob/a82a3165856f/src/public/contentScripts/workday.js#L136-L155)).
  - S4, S6 and S9 use trusted `setInputFiles` / `uploadFile` / `send_keys` on the input.
- **Timing.** S4 polls 30 × 700 ms, about 21 s ([L509](https://github.com/ajithchandraapplywizz/Workday_Auto/blob/037bc92f88d0/workday-auto-apply/auto-apply/lib/engine.mjs#L509)). S9 sleeps 10 s ([L159-161](https://github.com/raghuboosetty/workday/blob/fef73df1c920/workday.py#L159-L161)).
- **Duplicates.** This is a multi-file widget (S1 calls it `FileMulti`). S1 and S9 ([L152-157](https://github.com/raghuboosetty/workday/blob/fef73df1c920/workday.py#L152-L157)) delete existing files before uploading. **INFERRED:** a second upload adds a second item rather than replacing the first.

### 5.2 Differences from `scanner.js`
- **Two upload channels.** `attachResumeFile` (~L2481) fires `input`/`change` (~L2515). If `input.files` is then empty, it **also** dispatches a synthetic `drop` on the drop zone (~L2546-2556). The scanner's own comment says Workday always consumes the File and clears the input. If the change was accepted, the drop is a second upload and creates a **duplicate attachment**. S1 shows the drop handler needs only `e.dataTransfer.files`, so the synthetic drop would likely be honoured (**INFERRED**).
- **Too short a wait.** `waitForWorkdayUploadSuccess` (~L2443) waits 5 s, shorter than any source.
- **Narrow "already attached" check.** It only looks at `file-upload-item-name`. S4 also accepts `file-upload-item` and `delete-file`.

### 5.3 Recommended implementation
1. Use **one channel:** set `file-upload-input-ref.files` and fire `change`.
2. Wait up to 3 s for a `file-upload-item` or an upload-progress node.
3. **Only if nothing appeared at all**, try the drop zone's `drop` once.
4. Then wait up to **20 s** for `file-upload-successful`, or a `file-upload-item-name` containing the filename.

Keep the current behavior of reporting "already attached" when an item exists before we start, and never delete the user's file.

**Negative controls:**
- a widget that uploads on **both** change and drop and clears `input.files` synchronously (expect exactly one item);
- a success marker that appears only after 8 s.

---

## 6. Greenhouse (new and classic), Ashby, Lever

### 6.1 Greenhouse new boards (job-boards.greenhouse.io, React)

**Markup**
- The page container is `div.application--container` (S1 [greenhouseReact/index.ts L14-17](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/greenhouseReact/index.ts#L14-L17)).
- A select field is `div.select` (S1 [greenhouseReact/xpaths.ts L16-41](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/greenhouseReact/xpaths.ts#L16-L41)). The label sits in `.select__container`, with a `.select-shell` sibling (S12 [autofill.js L1512-1522](https://github.com/Kaitzz/Auto-Apply-Helper/blob/b41321835e9e/extension/content/autofill.js#L1512-L1522)).
- Inside it: `.select__control` → `.select__value-container` → one of `.select__placeholder`, `.select__single-value`, `.select__multi-value(__label)`.
- The input is `input.select__input[role=combobox][aria-autocomplete=list][aria-expanded]`, with the field name as its `id`. There is also `button[aria-label="Toggle flyout"]`.
- The menu `.select__menu` renders **inside the field**, not portalled. S1 waits for it under the field element ([greenhouseReact/DropdownSearchable.ts L231-253](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/greenhouseReact/DropdownSearchable.ts#L231-L253)). S12: *"it should be INSIDE our selectShell"* (L1568).
- The listbox is `#react-select-<inputId>-listbox[role=listbox]`, and options are `[role=option]#react-select-<inputId>-option-<n>` / `.select__option` (S11 [inputHandlers.ts L313-325](https://github.com/Jamalfox85/application-autofiller/blob/4776bbada143/src/utils/inputHandlers.ts#L313-L325)). The "No options" notice is `.select__menu-notice--no-options` (S12 L1642).

**Field ids**
- `question_<id>` for custom questions.
- `gender`, `hispanic_ethnicity`, `race`, `veteran_status`, and a disability field for EEO (S7 [utils.js L9-38](https://github.com/andrewmillercode/Autofill-Jobs/blob/a82a3165856f/src/public/contentScripts/utils.js#L9-L38); S11 eeoHandlers.ts L28-49).
- `candidate-location` for location; `#country` is the **phone dialing-code** combobox, not country of residence (S11 [greenhouse.ts L126-157](https://github.com/Jamalfox85/application-autofiller/blob/4776bbada143/src/utils/siteRules/greenhouse.ts#L126-L157)).
- Education: `school--N`, `degree--N`, `discipline--N`, `start-month--N`, `end-month--N` are comboboxes; `start-year--N` / `end-year--N` are `type=number` (L156-235).
- Employment: `company-name-N`, `title-N`, `start-date-month-N`, `start-date-year-N`, `end-date-month-N`, `end-date-year-N`, `current-role-N_1` (S11 [docs/SMOKE_DUAL_CONFIRM.md L20](https://github.com/Jamalfox85/application-autofiller/blob/4776bbada143/docs/SMOKE_DUAL_CONFIRM.md#L20), marked not yet proven live).

**Opening the menu**
- S11, verified live in PR #22: *"The flyout button preventDefault's click; the menu toggles on mouseup of the control and on ArrowDown keyup."* So dispatch `mouseup` on the Toggle flyout button, and if that fails, `keyup` ArrowDown on the input ([inputHandlers.ts L193-205](https://github.com/Jamalfox85/application-autofiller/blob/4776bbada143/src/utils/inputHandlers.ts#L193-L205)).
- S1 calls React `onMouseUp` on `.select-shell > div` ([L125-129, L220-225](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/greenhouseReact/DropdownSearchable.ts#L125-L129)).
- S12 sends a pointer sequence with centre coordinates to `.select__control` ([L1539-1558](https://github.com/Kaitzz/Auto-Apply-Helper/blob/b41321835e9e/extension/content/autofill.js#L1539-L1558)).

**Static lists vs async catalogs**
- For static lists, open the menu and read it **before** typing: *"A short query such as 'Yes' filters out sentence options that do not contain that word"* (S11 [L105-130](https://github.com/Jamalfox85/application-autofiller/blob/4776bbada143/src/utils/inputHandlers.ts#L105-L130)).
- For async catalogs, type first: *"School and degree catalogs must receive the query before the menu opens, or the first fetch is the alphabetical A-page (Alverno College)."* And: *"School search is async (Greenhouse debounceTimeout is 300ms) and the menu reads 'No options' until that request returns"* (L107-109, [L137-140](https://github.com/Jamalfox85/application-autofiller/blob/4776bbada143/src/utils/inputHandlers.ts#L137-L140)).

**Selecting and verifying**
- *"react-select commits an option on mousedown (click alone runs after blur and is dropped)"*, so send mousedown, mouseup and click (S11 [L206-212](https://github.com/Jamalfox85/application-autofiller/blob/4776bbada143/src/utils/inputHandlers.ts#L206-L212)). S1's plain `.click()` on `.select__option` works in its flow ([L156-173](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/greenhouseReact/DropdownSearchable.ts#L156-L173)). S7 commits with Enter, which takes the focused first option ([autofill.js L166-174](https://github.com/andrewmillercode/Autofill-Jobs/blob/a82a3165856f/src/public/contentScripts/autofill.js#L166-L174)); that is a first-option risk.
- Verify that `.select__single-value` equals the option **and** the input is empty: *"Search text sitting in a react-select input is not a committed answer"* (S11 [L215-240](https://github.com/Jamalfox85/application-autofiller/blob/4776bbada143/src/utils/inputHandlers.ts#L215-L240)). Multi-selects show `.select__multi-value__label` (S12 L446).
- To clear a value, send `mousedown` to `.select__indicators > div[aria-hidden="false"]` (S1 L193-200).

### 6.2 Greenhouse classic (boards.greenhouse.io legacy form)
- Fields are `div.field` elements (S1 [greenhouse/xpaths.ts L1-53](https://github.com/berellevy/job_app_filler/blob/6d6062cb98bb/src/inject/app/services/formFields/greenhouse/xpaths.ts#L1-L53)) containing one of:
  - select2 over a native `<select>` (`.select2-container`, or `.select2-container-multi` for multi);
  - a searchable select2 with no `<select>`;
  - a plain `<select>`;
  - "MM" / "YYYY" text inputs for dates;
  - drop-zone uploads;
  - an `<auto-complete>` location element.
- select2 opens on `mousedown` on `.select2-container a` (S1 greenhouse/Dropdown.ts L38-50). The résumé input is `input#resume` (S7 L124-133).
- The existing select2/Chosen path in the scanner covers these.
- S11 fills employment on "boards.greenhouse.io and job-boards embeds" with the react-select ids (greenhouse.ts L236). **INFERRED:** many boards.greenhouse.io URLs now serve the new React form.

### 6.3 Ashby (jobs.ashbyhq.com)
- **No `<form>`.** The roots are `.ashby-application-form-container` and `.ashby-survey-form-container` (the EEO survey) (S11 [siteRules/ashby.ts L36](https://github.com/Jamalfox85/application-autofiller/blob/4776bbada143/src/utils/siteRules/ashby.ts#L36)).
- **Fields.** An entry is `[data-field-path]` / `.ashby-application-form-field-entry`, with its title in `.ashby-application-form-question-title` ([L210-240](https://github.com/Jamalfox85/application-autofiller/blob/4776bbada143/src/utils/siteRules/ashby.ts#L210-L240)). S13 also uses `[data-testid="FieldEntry"]` (ashby.ts L19).
- **Field types per Ashby's API:** String, Email, File, Date, Number, Boolean, LongText, ValueSelect, MultiValueSelect, Phone, Score, SocialLink ([developers.ashbyhq.com, "Creating a Custom Careers Page"](https://developers.ashbyhq.com/docs/creating-a-custom-careers-page)).
- **System fields.** `_systemfield_name` is a single full-name box; there are also `_systemfield_email` and `_systemfield_resume`, the résumé drop zone, which is separate from the "Autofill from resume" uploader (S11 L77).
- **Boolean Yes/No is two `<button>`s**, sometimes carrying `data-option`, plus a hidden checkbox mirror (S11 `clickYesNo` [L341-352](https://github.com/Jamalfox85/application-autofiller/blob/4776bbada143/src/utils/siteRules/ashby.ts#L341-L352); S13 `detectButtonGroup` [L282+](https://github.com/Nevin-Chen/autofilltool/blob/1e238aeac8f8/src/adapters/ashby.ts#L282)).
- **Radio and checkbox groups are native inputs** under `.ashby-application-form-input-radio-group-option` / `-checkbox-group-option`, labelled by `label[for=<uuid>]`. Ids can start with a digit, so use attribute selectors, not `#id` (L222, L243-250).
- **Autocomplete** (location, school, and EEO in the survey) is an input with `aria-autocomplete="list"` or inside `.ashby-application-form-input-autocomplete`; its options are `[role=option]` ([L255-260](https://github.com/Jamalfox85/application-autofiller/blob/4776bbada143/src/utils/siteRules/ashby.ts#L255-L260), [L328-339](https://github.com/Jamalfox85/application-autofiller/blob/4776bbada143/src/utils/siteRules/ashby.ts#L328-L339)).
- **Education** blocks are `.ashby-application-form-input-education-entry`, added with `button.ashby-application-form-input-education-add`. Dates are native `<select>`s under `[id$="-startDate"]` / `[id$="-endDate"]`; there is an `-isCurrent` checkbox and `-degree` / `-major` text inputs (L271-300).

### 6.4 Lever (jobs.lever.co)
- **Structure.** The form is `#application-form` / `form.application-form`. Each question is `.application-question` with its label in `.application-label .text`, and the required marker is "✱". Radio and checkbox option labels are `label .application-answer-alternative` (S11 [siteRules/lever.ts L45, L131-156](https://github.com/Jamalfox85/application-autofiller/blob/4776bbada143/src/utils/siteRules/lever.ts#L131-L156)).
- **Selects are native `<select>`s**, including EEO `select[name="eeo[gender]"|"eeo[race]"|"eeo[veteran]"|"eeo[disability]"]` plus `eeo[disabilitySignature]` and `eeo[disabilitySignatureDate]` (S7 [utils.js L39-63](https://github.com/andrewmillercode/Autofill-Jobs/blob/a82a3165856f/src/public/contentScripts/utils.js#L39-L63)). They may be wrapped by jQuery/select2 (S11 L183-208).
- **Location:** *"Lever's location widget searches on keydown (500ms debounce) and clears the input on blur unless a .dropdown-location row was chosen with mousedown."* The hidden `input[name="selectedLocation"]` holds the value (S11 [L211-247](https://github.com/Jamalfox85/application-autofiller/blob/4776bbada143/src/utils/siteRules/lever.ts#L211-L247)).
- **Résumé:** `input#resume-upload-input` (S7 autofill.js L124-127).

### 6.5 Differences from `scanner.js`
- **False "filled".** `isEligible` (~L1522) plus the default branch of `applyFill` (~L2271) treat Greenhouse's `input.select__input[role=combobox]`, Ashby's autocomplete inputs and Lever's location input as plain text boxes, and return `true`. S11 says typed text is not committed, and Lever clears it on blur.
- **No react-select support.** `findPairedWidget` / `looksLikeSelectWidget` (~L99-170) help only when a native `<select>` exists, and react-select renders none.
- **Ashby Yes/No can't be answered.** `isClickSafe` (~L1985) allows clicks only on INPUT radio/checkbox, so Ashby's Yes/No `<button>`s are out of reach.

### 6.6 Recommended implementation

**A new widget kind, `rs-combobox`,** for `input[role=combobox]` inside `.select__control` (or with `aria-autocomplete=list`):
1. **Open** with `mouseup` on the Toggle flyout button, else `keyup` ArrowDown on the input.
2. **Find options** in `#react-select-<id>-listbox`, falling back to `aria-controls`.
3. **Static list:** pick the exact option from the open menu. **Async catalog:** type with the native setter + `InputEvent`, then poll for up to 4 s, ignoring "No options" for the first ~800 ms.
4. **Select** with mousedown → mouseup → click on the option.
5. **Verify** within 1 s that `.select__single-value` equals the label and the input is empty. On failure, clear the input and press Escape.

**Lever location:** type, send a keydown, wait up to 3.5 s for `.dropdown-location`, send `mousedown` to the exact row, then verify that `input[name=selectedLocation]` is non-empty.

**Ashby Yes/No:** a narrowly scoped button-click path. The button must read exactly "Yes" or "No", be `type=button`, and sit inside `.ashby-application-form-field-entry`. Verify through `aria-pressed`, the active class, or the hidden checkbox (**INFERRED**).

**Negative controls:**
- a react-select whose flyout ignores `click`;
- an option that commits only on mousedown;
- "No options" shown for 400 ms before results arrive;
- a static Yes/No question whose correct option is a sentence;
- typed text left in the input without a commit must count as **failure**.

---

## 7. Jobright / Simplify: what's publicly known (short)
- **No credible technical disclosure found.** Simplify's help centre only lists the Workday stages Copilot fills: personal info, work experience, education, résumé upload, and Voluntary Disclosures ([help.simplify.jobs](https://help.simplify.jobs/articles/2415391-using-copilot-to-autofill-applications)).
- A search snippet said Simplify's **Skills autofill is off by default** behind a settings toggle. The help pages I fetched don't say so: **UNVERIFIED**.
- A separate store extension, "[Workday Filler](https://chromewebstore.google.com/detail/workday-filler/dgpeidimblpbhkljdnejabiibmkgccpg)", exists only to automate Workday skills entry and calls Skills "often the most time-consuming part". That confirms Skills is the hard widget, nothing more.
- Jobright's store listing is marketing only. The one useful signal is that Jobright is a content-script extension like ours and fills these fields, which is consistent with S1/S3-style synthetic techniques being sufficient (**INFERRED**).

---

## 8. Prioritized changes most likely to fix "hangs on dates and skills"

1. **`fillWorkdayPromptTerm`: look in the right place.** Find results in the **portalled popup**: the `data-associated-widget` link first, then a popup that is new since our Enter, never a pre-existing one. Check pills first, and fail fast on "No Items." [S1, S3, S5, S8]. *Expected effect:* skills stop costing 3 s each, and auto-committed terms report success instead of being cleared.
2. **`fillWorkdayPromptTerm`: input and matching hygiene.**
   - Wait 500 ms before Enter, and send Enter as keydown/keypress/keyup with `keyCode` 13.
   - For skills, accept exact matches only, and walk the highlighted row with ArrowDown for virtualized lists.
   - Escape and clear the input between terms.
   - Cap the count (~10–15) and the total time (~60 s). [S3, S6, S8]
3. **Remove the document-wide `selectedItemList` fallbacks** in both verification and `getWorkdayPromptCurrentValue`. [S1]
4. **Make `setWorkdaySpinnerValue` / `setWorkdayDateValue` asynchronous.**
   - Add `keyCode`/`which` 38.
   - Wait about 60 ms before reading back.
   - Verify through `-display` / `aria-valuetext`, not `input.value`.
   - Yield between parts, re-check the month after the year, and read the field's error text. [S1, S2, S4]
5. **`findWorkdayDateWrappers` / `isWorkdaySpinnerInputSafe`: find parts reliably.**
   - Detect parts by `data-automation-id="dateSection*-input"`, falling back to aria-label, then `role=spinbutton`.
   - Support Month/Day/Year.
   - Gate on the wrapper's visibility, not the input's.
   - Click the wrapper once when its spinbuttons are lazily rendered. [S2, S1, S4]
6. **Set "I currently work here" before the dates, then re-scan.** If ticked, To is not applicable. [S4, S7]
7. **Rebuild the Workday mock with the negative controls in §1.3 and §2.3 before claiming a fix.** Today's mock encodes the two guesses that the ground truth refutes: results inside the field, and synchronous commit.
8. **Before coding, capture one real snapshot from the user's tenant** with the extension's "Report page" export (commit 76a8392): the Skills popup with results open, and one date wrapper. Diff them against §1.1 and §2.1. Three unknowns remain:
   - whether the spinbutton input is visible in CSS;
   - whether opening the popup sets `aria-hidden` on the rest of the page;
   - whether this tenant has `data-associated-widget`.
9. **Escape hatch, only if 1–5 fail:** `chrome.debugger` with CDP `Input.dispatchKeyEvent` / `Input.insertText` gives trusted keystrokes, which S4 and S6 effectively rely on. The cost is the "debugging this browser" banner and the `debugger` permission (**INFERRED** feasibility).
