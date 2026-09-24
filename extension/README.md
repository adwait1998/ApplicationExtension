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

There are exactly two places `scanner.js` ever calls `.click()`, each gated by its own
independently-auditable guard, re-checked at the point of click rather than trusted from
scanning time:

- `isClickSafe()` / `safeClick()` — radio and checkbox inputs only, needed to fill them.
- `isAddAnotherButtonSafe()` / `safeClickAddButton()` — a Workday-style "Add Another" /
  "+ Add Position" button that expands a repeating section, so profile entries beyond the
  first have somewhere to go. Refuses anything whose effective type is `submit`/`image` with
  a form owner (the classic "a `<button>` with no `type=""` attribute submits its form"
  trap), and refuses `input[type=submit|image]` outright. Only ever runs as part of an
  explicit "Scan & fill this page" click, and only while a capturing, document-level `submit`
  listener (`installSubmitShield()`) is installed to stop and report anything that slips
  through anyway — belt and braces on top of the guard, not instead of it.

## Loading it unpacked

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and select this `extension/` folder.
4. Pin the extension (puzzle-piece icon in the toolbar → pin ApplyPilot Copilot) so it's easy
   to reach.

## Connecting it to the local service

The extension talks to a companion service that runs on your machine (built separately, under
`src/applypilot/`, started with something like `applypilot serve-extension`). On first run
that service prints a URL and a token.

1. Right-click the extension icon → **Options** (or click **Settings** inside the popup).
2. Paste the **Service URL** (defaults to `http://127.0.0.1:8787` — it must always be
   `http://127.0.0.1`, the options page refuses anything else) and the **token**.
3. Click **Save**, then **Test connection**. You should see which decision tiers the service
   reports (`canary`, `deterministic`, and — if the optional Laya model loaded — `laya`).

If the service isn't running, or the token is wrong, the popup and options page show a plain
error message instead of failing silently (see `background.js` — 401 → "token is wrong",
fetch failure → "is the service running?", any other non-2xx → the status code and body).

## Using it on a job application page

1. Open the application page and make sure it has loaded (React/Angular ATS pages often
   render the form async — wait for the fields to actually appear).
2. Click the extension icon. It injects into **only this tab** (see "Permissions" below) and
   reports how many fillable fields it can see — this is a cheap heuristic
   (`content.js#detect`: 3+ fields, or 1+ field inside a `<form>`), not a real scan, and it
   never fills anything by itself.
3. Click **Scan & fill this page**. This is the only user action that writes to the DOM. It:
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
   - fills every field the service marked `auto_fill: true`, with a green outline and a
     tooltip explaining what was filled and why,
   - leaves every other detected field alone, with an amber dashed outline and a tooltip
     explaining why it was skipped (e.g. a canary question like sponsorship or salary that
     has no safe automatic answer).
4. Read the popup's Filled / Skipped list. **Review the page itself before submitting** —
   the popup is a summary, not a substitute for looking at the actual form.
5. If something's wrong, click **Undo fill** to restore every field's prior value.

## Permissions, and why they're this narrow

- `activeTab` + `scripting`: lets the popup inject `scanner.js`/`content.js` into the tab you
  clicked the icon on, and only that tab, only after you clicked. There is **no**
  `content_scripts` entry in `manifest.json` — the extension does not run on every page you
  visit, and does not request `<all_urls>`.
- `storage`: holds the service URL and token in `chrome.storage.local` (never synced).
- `host_permissions`: `http://127.0.0.1/*` and `http://127.0.0.1:*/*` — the only host the
  background worker is allowed to `fetch()`. It refuses to save a service URL that isn't
  `127.0.0.1` (see `options.js`).

## Architecture / file map

| File | Role |
|---|---|
| `manifest.json` | MV3 manifest — permissions, icons, popup, options page, service worker. |
| `scanner.js` | Pure-DOM field scanning + filling logic. No `chrome.*` calls. Shared verbatim between the real content script and the offline Node self-test (see below). Exposes `ApplyPilotScanner` on the global object. |
| `content.js` | The real content script. Loaded after `scanner.js` in the same isolated world. Listens for `DETECT` / `SCAN` / `APPLY_FILLS` / `UNDO` messages, owns the live element registry and the "prior value" snapshots used by Undo, and does all DOM highlighting. |
| `background.js` | MV3 service worker. The only file that holds the token and calls `fetch()`. Talks to the local service's `/resolve` and `/health`. |
| `popup.html` / `popup.js` / `popup.css` | The review panel: injects the content script, drives the scan → resolve → fill flow, renders Filled/Skipped/Failed, and the Undo button. |
| `options.html` / `options.js` | Where you paste the service URL and token. |
| `icons/` | `icon16.png` / `icon48.png` / `icon128.png` — generated with a tiny Node script using only the core `zlib` module (flat-color square + circle badge). No external assets. |
| `test-page.html` | An offline mock application form exercising every label-resolution pattern, a select, a radio group with `<fieldset>/<legend>`, a textarea, deliberately-excluded fields (hidden, `aria-hidden`, `display:none`), an auto-generated-looking id, and a simulated React-controlled input. |
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
  guess, the extension reports how many frames it skipped (popup status line, and
  `detect().skippedFrames`) so you know to fill those fields by hand. This matters in
  practice: some ATS integrations (e.g. a Greenhouse or Lever form embedded via `<iframe>` on
  a company's own careers page, hosted from `boards.greenhouse.io`/`jobs.lever.co`) are
  cross-origin from the parent page and will be skipped for this reason.

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
**Undo fill** replays those snapshots through the same `applyFill()` path used to fill them
(so it survives React's controlled-input re-renders the same way filling does) and removes
every highlight it added.

## Known limitations / where a real ATS form could defeat the scanner

- **Cross-origin iframes** are skipped entirely (see above) — this is the most likely
  real-world gap, since several ATS platforms are commonly embedded this way.
- **Multi-step / dynamically-inserted forms**: if a form renders new fields after you click
  "Next" without a full page navigation (common in Workday and some Greenhouse flows), you
  need to re-open the popup and click "Scan & fill" again for the newly-visible step — the
  extension only scans what's in the DOM at the moment you click, on purpose (no
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

I do not have a way to drive a real Chrome instance or a live ATS page from here. What I could
and did verify:

- `node --check` on every `.js` file.
- `manifest.json` parses as JSON and has the required MV3 keys (`manifest_version: 3`,
  `background.service_worker`, `action.default_popup`, `options_page`, narrow
  `permissions`/`host_permissions`).
- The three generated PNG icons have valid PNG signatures and the declared dimensions
  (16/48/128).
- **`selftest.js`**, run under Node with `jsdom` (installed only in a scratch/dev directory,
  never as part of this extension — see the comment at the top of that file for the exact
  command), loads `test-page.html`, evaluates the real `scanner.js` inside that page's own
  jsdom window (so it runs exactly the way the real content script would — same globals, same
  prototype chain), and as of this writing asserts 126 checks pass, covering (among others):
  every label-resolution pattern, the select, the radio group (collapsed to one descriptor with
  2 options, legend-derived label), the textarea, the three deliberately-excluded fields
  (hidden input, `aria-hidden` wrapper, `display:none` wrapper), the auto-generated-id
  heuristic, every emitted selector being non-empty, select2/Chosen custom-widget pairing,
  résumé-vs-cover-letter file target selection, the `isAddAnotherButtonSafe()` guard (allows
  `type=button` buttons and `role=button` divs/anchors; refuses a no-type in-form trap button,
  `input[type=submit|image]`, and submit/save/continue-labelled buttons), the "Add Another"
  click driving a real DOM-block increase for the right section only, the submit shield
  blocking and reporting a bypassed click, and the split month/year date pair (both an
  input+input Workday-style pair and a select+input pair) being scanned as one field and
  filled back into both underlying controls. What jsdom cannot verify at all — no real
  rendering/layout, no `DataTransfer`/`DragEvent`, no real network — is called out inline in
  that file's comments and confirmed separately via `scripts/chrome_load_test.py` instead.
- A separate ad hoc script (not committed — it lived in the scratch directory) exercised the
  **filling** path against the same jsdom page: confirmed `applyFill()` on the simulated
  React-controlled input survives the page's own revert-on-re-render loop (while a raw
  `el.value = x` on that same kind of field would not have), confirmed `select` fills match by
  visible option text, confirmed radio fills check the right option by label text, and
  confirmed the undo round-trip (`getCurrentValue()` → fill → `applyFill()` back to the
  snapshot) restores the original value exactly.

### Manual verification in a real browser

Not yet done — no live Chrome session available here. To finish verifying by hand:

1. Load the extension unpacked (see above), open `test-page.html` directly (`file://.../
   extension/test-page.html`), and confirm the popup detects the expected field count.
2. Without a running local service, click **Scan & fill this page** and confirm the popup
   shows a clear "service unreachable" message rather than failing silently or throwing.
3. Start a stub or real `/resolve` service, click **Scan & fill this page**, and visually
   confirm: green outlines on filled fields with correct tooltips, amber on skipped fields,
   the `react_input` field's value actually sticks (open DevTools and watch it survive the
   page's `setInterval`), the select's chosen option is visibly selected, the radio group
   shows the correct option checked, and **Undo fill** restores everything.
4. Try it against a real Greenhouse/Lever/Ashby/Workday application page to see how the label
   heuristics and selector generation hold up outside the mock page — this is exactly the
   class of thing the mock page can't fully substitute for.
