# ApplyPilot Copilot (Chrome extension)

A Manifest V3 Chrome extension that reads a job application form on the page you're looking
at, asks a **local** ApplyPilot service (`127.0.0.1` only) which of your profile values
belongs in each field, fills them in, and highlights exactly what it did. Nothing leaves your
machine. Nothing is submitted for you.

## The one rule that matters

**This extension never clicks submit and never navigates.** It fills form fields and draws a
highlight around each one. You review the highlighted page and click Submit yourself. There
is no code path anywhere in this extension that programmatically submits a form, clicks a
submit-typed button, or calls any navigation API. If you ever see that behavior, it's a bug.

There are a handful of places `scanner.js` ever dispatches a click-shaped interaction, each
gated by its own independently-auditable guard, re-checked at the point of interaction rather
than trusted from scanning time:

- `isClickSafe()` / `safeClick()` — radio and checkbox inputs only, needed to fill them.
- `isAddAnotherButtonSafe()` / `safeClickAddButton()` — a Workday-style "Add Another" /
  "+ Add Position" button that expands a repeating section, so profile entries beyond the
  first have somewhere to go. Refuses anything whose effective type is `submit`/`image` with
  a form owner (the classic "a `<button>` with no `type=""` attribute submits its form"
  trap), and refuses `input[type=submit|image]` outright.
- `isWorkdayDropdownOpenerSafe()` — a Workday dropdown's `button[aria-haspopup="listbox"]`
  (Degree, Country, State, ...), opened with a real pointer/mouse event sequence
  (`dispatchPointerClickSequence()`), never a bare `.click()`.
- `isWorkdayOptionSafe()` — an option inside the popup that opener controls (or the results
  panel a prompt/multi-select opened for itself), never an arbitrary element elsewhere on the
  page, even one that is shaped exactly like an option.
- `isWorkdaySpinnerInputSafe()` / `isWorkdayMaskedDateInputSafe()` — the two Workday date-entry
  shapes (a Month/Year spinbutton pair, or a single masked MM/YYYY field), driven by a real
  `ArrowUp` keydown or a genuine per-character keydown/keypress/input/keyup sequence — never
  plain `.value =` + `input`/`change`, which Workday's own controlled re-render ignores anyway.

Every one of the guards above ALSO carries a hard, unconditional refusal for any element whose
own `data-automation-id` contains `bottom-navigation`, `submit`, `next`, or `save` — Workday's
real "Submit"/"Next"/"Save and Continue" controls — regardless of how legitimate its shape
otherwise looks. None of this runs on page load: every interaction above happens ONLY as part
of an explicit "Fill this page" click, and only while a single capturing, document-level
`submit` listener (`installSubmitShield()`) is installed for the ENTIRE fill — repeating-section
expansion, scanning, the `/resolve` round-trip, every field, and the résumé attach, in that
order, with no gap between phases — to stop and report anything that slips through anyway —
belt and braces on top of every guard above, not instead of them. A fill runs to completion in
`content.js` regardless of whether the side panel is open, so this shield's lifetime is tied to
the fill itself, never to the panel's UI.

## Loading it unpacked

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and select this `extension/` folder.
4. Pin the extension (puzzle-piece icon in the toolbar → pin ApplyPilot Copilot) so it's easy
   to reach. Clicking the icon now opens the **side panel** (docked to the side of the window)
   instead of a popup — see "Using it on a job application page" below for why.
5. Requires Chrome 116 or newer (`chrome.sidePanel.setPanelBehavior`, added in 116).

## Connecting it to the local service

The extension talks to a companion service that runs on your machine (built separately, under
`src/applypilot/`, started with something like `applypilot serve-extension`). On first run
that service prints a URL and a token.

1. Right-click the extension icon → **Options** (or click **Settings** inside the side panel).
2. Paste the **Service URL** (defaults to `http://127.0.0.1:8787` — it must always be
   `http://127.0.0.1`, the options page refuses anything else) and the **token**.
3. Click **Save**, then **Test connection**. You should see which decision tiers the service
   reports (`canary`, `deterministic`, and — if the optional Laya model loaded — `laya`).

If the service isn't running, or the token is wrong, the panel and options page show a plain
error message instead of failing silently (see `background.js` — 401 → "token is wrong",
fetch failure → "is the service running?", any other non-2xx → the status code and body).

## Using it on a job application page

The review UI is a **side panel**, not a popup. This matters: Chrome destroys a popup's entire
JS context the instant it loses focus, which used to kill a fill mid-way the moment a slow
Workday page (or just clicking into DevTools) stole focus. The side panel's document stays alive
across tab switches and focus changes, and a fill now runs independently of it anyway — see
below.

1. Open the application page and make sure it has loaded (React/Angular ATS pages often
   render the form async — wait for the fields to actually appear).
2. Click the extension icon to open the panel (it opens once and stays docked — you don't need
   to reopen it per tab or per page). **Nothing is injected into any page yet.** The panel
   follows whichever tab is active in its window (`chrome.tabs.onActivated`/`onUpdated`), purely
   to decide what to show you; it does not run anything on a tab just because you switched to
   it.
3. Click **Fill this page**. This is the only user action (besides **Report page**) that injects
   the scanner into the tab, and the only one that writes to the DOM. It:
   - asks the local service how many `work_history`/`education` entries your profile has,
     and — only if that succeeds — clicks each repeating section's "Add Another" button
     (guarded, see above) just enough times to make room for them before scanning, capped at
     10 clicks total per fill and stopping immediately the moment a click fails to add a new
     block (never retried blindly). If the service doesn't support this yet (404) or isn't
     reachable, this step is skipped entirely and the page is scanned exactly as before.
   - scans the page for fields — including a Workday-style date rendered as separate month
     and year inputs/selects behind one visual "From"/"To" label, which is scanned as a
     single merged field and split back into both underlying controls on fill,
   - sends only the field metadata (labels, names, autocomplete, options — never page
     content, never your profile) to the local service,
   - fills every field the service marked `auto_fill: true` **one at a time**, with a green
     outline and a tooltip explaining what was filled and why, showing live progress in the
     panel ("Filling 12/40 — Skills (adding 3/15)") as it goes,
   - leaves every other detected field alone, with an amber dashed outline and a tooltip
     explaining why it was skipped (e.g. a canary question like sponsorship or salary that
     has no safe automatic answer).
4. **This keeps running even if you close the panel, switch tabs, or the panel's window loses
   focus.** The fill lives in the page's own content script, not in the panel — closing the
   panel only stops you from watching it. Switch back (or reopen the panel) any time to see
   where it's at, or the finished result.
5. **Cancel** stops the fill between fields (and between items within a multi-value field like
   Skills) — whatever was already filled stays filled and is reported; everything after that
   point is reported as "not attempted — cancelled" so you know exactly what still needs doing.
6. No single field can hang the fill: each one gets a ~12s timeout (reported as "timed out" and
   skipped, not retried) and the whole fill gives up on any remaining fields after a ~120s
   budget ("not attempted — time budget") rather than spinning forever on one stuck widget.
7. Read the panel's Filled / Drafted / Need you / Could not fill lists. **Review the page itself
   before submitting** — the panel is a summary, not a substitute for looking at the actual
   form. Switching tabs and back shows that tab's own last result; if the tab has since
   navigated to a different page, the panel says so ("from a previous page") rather than
   presenting old results as current.
8. If something's wrong, click **Undo** to restore every field's prior value. **Report page**
   (unchanged) saves the form's structure — never values — for diagnosing a bad fill.

## Permissions, and why they're this narrow

- `activeTab` + `scripting`: lets the panel inject `scanner.js`/`capture.js`/`content.js` into a
  tab, and only after you click **Fill this page** or **Report page** in it — never on load,
  never just because you switched to that tab. There is **no** `content_scripts` entry in
  `manifest.json` — the extension does not run on every page you visit.
- `sidePanel`: lets `background.js` open the side panel when you click the toolbar icon
  (`chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true })`) instead of the old
  popup.
- `host_permissions` — `http://127.0.0.1/*` and `http://127.0.0.1:*/*` (the only hosts the
  background worker is allowed to `fetch()` — it refuses to save a service URL that isn't
  `127.0.0.1`, see `options.js`), plus **`https://*/*` and `http://*/*`**. That second pair
  looks broad, so it's worth explaining: because the panel is one persistent surface per
  *window* rather than a popup you reopen per *tab*, it has to be able to draw the right UI
  (and, once you click Fill, inject) for **whichever tab you switch to after opening it** — the
  `activeTab` permission only ever covers the tab that was active at the moment of a click, and
  cannot reach a tab you switch to afterwards. Declaring these two patterns is what makes that
  possible. It does **not** widen when anything actually runs: injection is still 100%
  on-demand, gated on a Fill/Report click, exactly as before — a broader `host_permissions` only
  ever changes what the extension is *allowed* to be asked to do, never what it does
  unprompted. `<all_urls>` itself is never requested.
- `storage`: holds the service URL and token in `chrome.storage.local` (never synced), and is
  also what backs `chrome.storage.session` — the per-tab fill-result store described below.

## Architecture / file map

| File | Role |
|---|---|
| `manifest.json` | MV3 manifest — permissions, icons, side panel, options page, service worker. |
| `scanner.js` | Pure-DOM field scanning + filling logic. No `chrome.*` calls. Shared verbatim between the real content script and the offline Node self-test (see below). Exposes `ApplyPilotScanner` on the global object. |
| `content.js` | The real content script. Loaded after `scanner.js` in the same isolated world. Listens for `START_FILL` / `CANCEL_FILL` / `DETECT` / `UNDO` / `CAPTURE` messages. Runs the ENTIRE fill pipeline itself (expand → scan → resolve → apply, one field at a time with a per-field timeout and an overall time budget → résumé attach) so it survives the side panel closing; reports live progress and the final result to `background.js` via `chrome.runtime.sendMessage`, never by returning a value the panel has to stay open to receive. Owns the live element registry and the "prior value" snapshots used by Undo, and does all DOM highlighting. |
| `capture.js` | "Report page" structure capture — page furniture (tags, roles, labels, option lists) only, never field values. See "Field scanning notes" / privacy note in its own header. |
| `background.js` | MV3 service worker. The only file that holds the token and calls `fetch()` (talks to the local service's `/resolve`, `/health`, `/profile/counts`, `/resume`), and the only file that writes `chrome.storage.session`. Sets `chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true })` so the toolbar icon opens the panel. Persists every `FILL_STATE_UPDATE` from `content.js` into `chrome.storage.session` keyed `fillState_<tabId>` (per-tab writes are queued so two updates from the same tab can never race each other), and cleans that entry up on `chrome.tabs.onRemoved`. |
| `sidepanel.html` / `sidepanel.js` / `sidepanel.css` | The review UI (replaces the old popup — see "Using it on a job application page"). A pure renderer: reads `chrome.storage.session` (plus `chrome.storage.onChanged` for live updates) for whichever tab is active in its window, and only ever tells `content.js` what to do (`START_FILL`/`CANCEL_FILL`/`UNDO`/`CAPTURE`) — it never writes fill-result state itself. Supports an optional `?tabId=` query parameter that pins it to a specific tab instead of following the active tab; this exists **only** for `scripts/chrome_panel_test.py` and must never change behavior when absent. |
| `options.html` / `options.js` | Where you paste the service URL and token. |
| `icons/` | `icon16.png` / `icon48.png` / `icon128.png` — generated with a tiny Node script using only the core `zlib` module (flat-color square + circle badge). No external assets. |
| `test-page.html` | An offline mock application form exercising every label-resolution pattern, a select, a radio group with `<fieldset>/<legend>`, a textarea, deliberately-excluded fields (hidden, `aria-hidden`, `display:none`), an auto-generated-looking id, a simulated React-controlled input, and — in its own `#wd-form` — real-Workday-shaped fixtures for every widget in "Workday widget notes" below, plus a decoy `bottom-navigation-submit-button`. |
| `selftest.js` | Runs `scanner.js`'s scanning logic against `test-page.html` under Node (via `jsdom`) and asserts the extracted FieldDescriptors are correct. See below for how to run it. |

## Field scanning notes

- Collects `input` (excluding `type=hidden|submit|button|image|reset`), `select`, `textarea`.
- Skips `disabled` fields, anything with zero-size `getBoundingClientRect()`, anything under
  `display:none`/`visibility:hidden`/`[aria-hidden="true"]` **at any ancestor level** (CSS
  `display` doesn't cascade to computed style the way you'd expect, so this walks the whole
  ancestor chain rather than trusting one `getComputedStyle()` call).
- Label resolution order: `label[for=id]` → wrapping `<label>` → `aria-label` →
  `aria-labelledby` → `placeholder` → nearest preceding text node (walks up to 4 ancestor
  levels, checking previous siblings at each level).
- Selectors: prefers `#id` when the id looks hand-authored (rejects React `useId()` patterns
  like `:r3:`, `radix-`/`mui-`/`ember-`/`headlessui-`/`css-` prefixes, UUIDs, long hex
  strings, and pure-numeric ids), then `tag[name=...]`, then a guaranteed-unique
  `nth-child` path from the document root. Every candidate is verified with
  `querySelectorAll(sel).length === 1` before being accepted (see `buildSelector` /
  `isUnique` in `scanner.js`); radio groups are the one deliberate exception — their selector
  intentionally matches every radio in the group.
- Radio groups are collapsed into **one** `FieldDescriptor` per `name`, with `options` set to
  each radio's resolved label text, matching the `/resolve` contract. The group's own label
  comes from an ancestor `<fieldset><legend>`, then `[role=radiogroup]`/`[role=group]`
  `aria-label(ledby)`, then the same "preceding text" fallback used for everything else.
- Same-origin iframes are scanned too (`scanAll()` walks `document.querySelectorAll('iframe')`
  and recurses into any `contentDocument` it can reach without throwing). **Cross-origin
  iframes are skipped** — a normal content script cannot read into them, and rather than
  guess, the extension reports how many frames it skipped (in the fill summary line, from
  `state.skippedFrames`, once a fill runs — see `content.js#detect`/`scanAll`) so you know to
  fill those fields by hand. This matters in
  practice: some ATS integrations (e.g. a Greenhouse or Lever form embedded via `<iframe>` on
  a company's own careers page, hosted from `boards.greenhouse.io`/`jobs.lever.co`) are
  cross-origin from the parent page and will be skipped for this reason.

## Workday widget notes

Workday does not render a date/dropdown/multi-select as a plain `input`/`select` — its own
markup was reverse-engineered from two published, real-Workday-tested autofillers
(`berellevy/job_app_filler`, `ankitsharma38/Workday-Autofill-Assistant`), not guessed, after
two earlier attempts guessed wrong and both passed a self-authored mock while failing on the
operator's real form. Each becomes exactly one `FieldDescriptor` with a `widget` property:

- **`wd-date-my` / `wd-date-y`** — `div[data-automation-id^="formField-"] >
  div[data-automation-id="dateInputWrapper"]`, holding either `input[aria-label="Month"]` +
  `input[aria-label="Year"]` (`wd-date-my`), just `input[aria-label="Year"]` (`wd-date-y`,
  e.g. education), or exactly one plain `<input>` with neither aria-label (also `wd-date-my`,
  ankitsharma38's masked-field fallback). The first two are numeric spinbuttons: plain
  `.value =` + `input`/`change` is silently reverted, so `setWorkdaySpinnerValue()` instead
  sets the value ONE BELOW the target with no event, then dispatches a real `ArrowUp` keydown
  (retrying once if a variant needs it twice) — the technique lives only in `scanner.js`, nowhere
  else. The masked fallback is typed character by character
  (`keydown`/`keypress`/`input`/`keyup` per character) via `typeMaskedTextField()`. `wd-date-y`
  accepts either a bare year or a full `MM/YYYY` value and uses only the year part.
- **`wd-dropdown`** — `button[aria-haspopup="listbox"]` inside a `formField-*` container
  (Degree, Country, State, ...). Its popup is portalled to `<body>`, found via the button's
  `aria-controls` id (falling back to any newly-visible `[role="listbox"]`). Opened and
  selected with a real `pointerdown`/`mousedown`/`pointerup`/`mouseup`/`click` sequence, never
  a bare `.click()`. Matches an option by exact text, then a small explicit degree-family
  (bachelor/master/doctorate/associate/high-school) or country-alias table, then a one-way
  "option contains target" check — **never** the first option when nothing matches
  confidently; it presses Escape and reports instead.
- **`wd-prompt`** — `div[data-automation-id^="formField-"] >
  div[data-automation-id="multiSelectContainer"]`, with an inner `<input>` (Field of Study,
  School, Certification, Skills, ...). Types the term, presses Enter, polls up to ~3s for
  `[data-automation-id*="promptOption"]` / `[data-automation-id*="checkboxItem"]` /
  `[role="option"]` results **scoped to that field's own container/formField only** — never a
  whole-document search, which could otherwise bind to a different field's results left open
  elsewhere on the page — then matches by exact text, then an acronym form (`"(SQL)"`), then a
  word-boundary "starts with", then substring. No confident match clears the typed text and
  reports, same "never guess" rule as the dropdown. When the service sends a **list** (Skills),
  each term is added in turn; one with no confident match is skipped, not fatal to the rest.
  A checkbox-shaped result row (`checkboxItem`) is clicked via the checkbox itself
  (`isClickSafe`-gated); a plain result row gets the same pointer/mouse sequence as a dropdown
  option. Skills' exact `formField-*` id is UNVERIFIED against a real tenant — detected
  structurally (by the `multiSelectContainer`), not by an id whitelist, so this doesn't matter
  in practice.
- **Every new interaction path above carries its own guard**
  (`isWorkdayDropdownOpenerSafe`/`isWorkdayOptionSafe`/`isWorkdaySpinnerInputSafe`/
  `isWorkdayMaskedDateInputSafe`/`isWorkdayPromptInputSafe`), each with a hard, unconditional
  refusal for any element whose own `data-automation-id` contains `bottom-navigation`,
  `submit`, `next`, or `save` — see "The one rule that matters" above.
- **Résumé upload**: the file attach itself already worked on Workday; only the *verification*
  was wrong, because Workday consumes the `File` and empties `input.files` right after
  accepting it. `attachResumeFile()` now checks for `[data-automation-id="file-upload-item-name"]`
  containing the filename (inside `[data-automation-id="file-upload-item"]`) or
  `[data-automation-id="file-upload-successful"]`, waiting up to ~5s via a `MutationObserver`,
  and also dispatches a `drop` at `[data-automation-id="file-upload-drop-zone"]` if the plain
  input path doesn't produce a file item. Before touching anything, it checks whether a
  `file-upload-item-name` already shows a file — if so, it reports "a résumé is already
  attached" and does **not** attach again (uploading twice is worse than not uploading).

## Filling notes (the part that's easy to get subtly wrong)

Real ATS forms (Greenhouse, Lever, Ashby, Workday) are React-based. Setting `el.value = x`
directly does not notify React's controlled-input state, so the value can silently revert the
moment the component re-renders — including right before submit. `scanner.js`'s
`setNativeValue()` instead calls the **native** property setter from the element's own
prototype chain (resolved via the element's own `ownerDocument.defaultView`, so this is safe
even for elements in a different same-origin iframe realm) and then dispatches real `input`
and `change` events, which is what React's synthetic event system listens for. `select`
elements are matched by visible option text (case-insensitive, exact then substring) or by
option `value`, then `selectedIndex` is set the same native-setter way. Radio buttons are
filled with a real `.click()` (the most faithful simulation of an actual user click — it
handles mutual exclusivity and fires `click`/`input`/`change` the way the browser does
natively), matched against each radio's resolved label text.

## Undo

Before writing any field, `content.js` snapshots its current value (`getCurrentValue()`) —
text, select value, checkbox boolean, or checked radio's value/none — keyed by field id.
**Undo** replays those snapshots through the same `applyFill()` path used to fill them
(so it survives React's controlled-input re-renders the same way filling does) and removes
every highlight it added.

## Known limitations / where a real ATS form could defeat the scanner

- **Cross-origin iframes** are skipped entirely (see above) — this is the most likely
  real-world gap, since several ATS platforms are commonly embedded this way.
- **Multi-step / dynamically-inserted forms**: if a form renders new fields after you click
  "Next" without a full page navigation (common in Workday and some Greenhouse flows), you
  need to click "Fill this page" again (the panel is already open — no need to reopen it) for the
  newly-visible step — the extension only scans what's in the DOM at the moment you click, on
  purpose (no
  MutationObserver auto-fill, per the "nothing runs automatically" rule). It DOES now handle
  the narrower "Add Another" case within a single step (Workday's "My Experience" repeating
  work-history/education blocks) — see "The one rule that matters" above — but only for the
  section kinds it recognizes (work history, education) and only up to 10 add-clicks per fill.
- **"Add Another" expansion can't always find the right button.** `findAddButtonForKind()`
  requires the button to sit after the section's last field with no other section's field in
  between; a page whose markup doesn't fit that shape (or genuinely has no such button) is
  left exactly as before — expansion is skipped for that kind, never guessed at.
- **Custom-styled radio/checkbox widgets** that hide the native input completely (e.g.
  `width:0; height:0` rather than the more common 1px/clip-based screen-reader-only pattern)
  will be treated as invisible and skipped, even though a sighted user can interact with the
  custom control. The visibility check is deliberately conservative here — it errs toward
  skipping and telling the user, not toward guessing.
- **Shadow DOM**: `querySelectorAll` does not pierce open or closed shadow roots. A field
  rendered inside a web component's shadow tree will not be found. (None of the label
  patterns in the spec mentioned shadow DOM, and none of the major ATS platforms this was
  built against use it for their core form fields as of this writing, but it's worth knowing
  about if a form scans as having fewer fields than it visibly has.)
- **Ambiguous label text for radio options** — if two options in the same group resolve to
  identical or near-identical label text, `setRadioValue()`'s matching (exact → value →
  substring) picks the first match, which may not be the one the service intended.

## Verifying this without a live browser

Live ATS pages (real Greenhouse/Workday/etc. applications behind a login, mid-application state)
are still out of reach from here — see "Manual verification in a real browser" below for those.
Everything else has moved to real, automated verification over time
(`scripts/chrome_load_test.py`, `scripts/chrome_panel_test.py` — real Chromium via Playwright,
`--load-extension` and all); what follows is what's checked, split between what a live browser
can prove and what a plain offline check already covers:

- `node --check` on every `.js` file.
- `manifest.json` parses as JSON and has the required MV3 keys (`manifest_version: 3`,
  `background.service_worker`, `side_panel.default_path`, `options_page`, narrow
  `permissions`/`host_permissions`, no `action.default_popup` — see `chrome_panel_test.py`).
- The three generated PNG icons have valid PNG signatures and the declared dimensions
  (16/48/128).
- **`selftest.js`**, run under Node with `jsdom` (installed only in a scratch/dev directory,
  never as part of this extension — see the comment at the top of that file for the exact
  command), loads `test-page.html`, evaluates the real `scanner.js` inside that page's own
  jsdom window (so it runs exactly the way the real content script would — same globals, same
  prototype chain), and as of this writing asserts 206 checks pass, covering (among others):
  every label-resolution pattern, the select, the radio group (collapsed to one descriptor with
  2 options, legend-derived label), the textarea, the three deliberately-excluded fields
  (hidden input, `aria-hidden` wrapper, `display:none` wrapper), the auto-generated-id
  heuristic, every emitted selector being non-empty, select2/Chosen custom-widget pairing,
  résumé-vs-cover-letter file target selection, the `isAddAnotherButtonSafe()` guard (allows
  `type=button` buttons and `role=button` divs/anchors; refuses a no-type in-form trap button,
  `input[type=submit|image]`, and submit/save/continue-labelled buttons), the "Add Another"
  click driving a real DOM-block increase for the right section only, the submit shield
  blocking and reporting a bypassed click, the split month/year date pair (both an
  input+input Workday-style pair and a select+input pair) being scanned as one field and
  filled back into both underlying controls, and every Workday widget in "Workday widget
  notes" above: the spinner-date ArrowUp technique (proven adversarially — the mock genuinely
  reverts plain `.value=`+`input`/`change` and only commits on a real keydown), its
  retry-twice path, the masked single-input fallback, the Degree/Country dropdown (portalled
  listbox resolution, degree-family and country-alias matching, Escape-and-report on no
  match), the Field of Study / Skills prompt (exact/acronym/startsWith/substring matching, the
  Skills list's skip-and-continue, no-blind-first-result), duplicate-résumé prevention, the
  upload-confirmation `MutationObserver` wait, and every new guard's hard deny-by-automation-id
  rule. What jsdom cannot verify at all — no real rendering/layout, no `DataTransfer`/
  `DragEvent` (so the résumé attach's actual file assignment, Workday's own File-consuming
  behavior, and the drop-zone dispatch are untestable end-to-end here), no `chrome.*` APIs (so
  none of `content.js`'s message handling, its whole-fill submit-shield install/removal, or
  the side panel can be exercised by this harness at all — only `scanner.js`'s exported
  functions are), and no real network — is called out inline in that file's comments and
  confirmed separately via `scripts/chrome_load_test.py` instead.
- **`scripts/chrome_panel_test.py`** (same real-Chromium-via-Playwright approach as
  `chrome_load_test.py`, `--load-extension` and all) exercises everything this README's
  "Using it on a job application page" section above describes that `chrome_load_test.py`
  cannot: it asserts the manifest has no `default_popup` and does declare `side_panel`; that the
  panel page itself loads; that clicking **Fill this page** in a real panel drives a real fill
  against a tiny stub of the local service (no live `applypilot serve-extension` needed — the
  stub binds an OS-assigned free port so it can never collide with a real one) and that the
  result lands in `chrome.storage.session` keyed per tab; that a second tab gets independent
  state (and that filling it never disturbs the first tab's stored result); that
  `chrome.storage.onChanged` actually fires multiple times with advancing progress while a fill
  is running; that **Cancel** stops a fill partway through (some fields filled and kept, the
  rest reported as "not attempted — cancelled"), well before the fill would have finished on its
  own; that a field patched (via `chrome.scripting.executeScript`, from outside the extension —
  no source file is modified) to never resolve is reported as "timed out" without stalling the
  other 30+ fields on the same page; that closing a tab clears its `chrome.storage.session`
  entry; and that none of the above ever submits the mock form. Run it the same way as
  `chrome_load_test.py` (below).
- A separate ad hoc script (not committed — it lived in the scratch directory) exercised the
  **filling** path against the same jsdom page: confirmed `applyFill()` on the simulated
  React-controlled input survives the page's own revert-on-re-render loop (while a raw
  `el.value = x` on that same kind of field would not have), confirmed `select` fills match by
  visible option text, confirmed radio fills check the right option by label text, and
  confirmed the undo round-trip (`getCurrentValue()` → fill → `applyFill()` back to the
  snapshot) restores the original value exactly.

### Manual verification in a real browser

Most of this is now covered automatically by `scripts/chrome_load_test.py` and
`scripts/chrome_panel_test.py` (real Chromium via Playwright — see above): the scanner against a
real layout engine, every Workday widget, the submit shield, and now the side panel itself
(manifest wiring, per-tab state, live progress, Cancel, the per-field timeout, zero
submissions). What's left is genuinely manual — things automation on a mock page can't tell you:

1. Load the extension unpacked (see above), open a real job application page, click the toolbar
   icon, and confirm the side panel opens docked to the window (not a popup) and stays open when
   you click into DevTools or switch tabs and back.
2. Without a running local service, click **Fill this page** and confirm the panel shows a clear
   "service unreachable" message rather than failing silently or throwing.
3. Start the real `applypilot serve-extension`, click **Fill this page** on a real page, and
   visually confirm: green outlines on filled fields with correct tooltips, amber on skipped
   fields, live progress text while it runs, and **Undo** restores everything.
4. Try it against a real Greenhouse/Lever/Ashby/Workday application page to see how the label
   heuristics and selector generation hold up outside the mock page — this is exactly the
   class of thing the mock page can't fully substitute for.
