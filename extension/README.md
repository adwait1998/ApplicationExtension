# ApplyPilot Copilot (Chrome extension)

A Manifest V3 Chrome extension that reads a job application form on the page you're looking
at, asks a **local** ApplyPilot service (`127.0.0.1` only) which of your profile values
belongs in each field, fills them in, and highlights exactly what it did. Your profile and the
page you're on never leave your machine — every request goes to that local service and nowhere
else. The one exception: for **Draft cover letter** and **Tailor my résumé**, that local service
separately fetches the job POSTING ITSELF from the ATS's own **public** job API (Greenhouse,
Lever, Ashby, Workday — a plain GET of the same public listing anyone visiting the page can
already see, no login involved) so it can write against the real job description — see
`job_context.py`. That outbound request carries no profile data, no résumé, and nothing you've
typed; it's just the posting's own public URL. The AI-generated features (**Draft cover letter**,
**Tailor my résumé**) use a model running on this computer by default; they only ever reach a
model that isn't on this computer if you've explicitly turned that on in Settings ("Allow AI
features to use a model that isn't on this computer" — off by default). Nothing is submitted for
you.

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
of an explicit "Fill this page" click, and only while a capturing, document-level `submit`
listener (`installSubmitShield()`) is installed — one per **frame** being filled (see "Fill
every frame" below), each covering that frame's ENTIRE participation in the fill — repeating-
section expansion, attaching the résumé, waiting for the page to settle, scanning, the merged
`/resolve` round-trip, and every field, in that order, with no gap between phases — to stop and
report anything that slips through anyway — belt and braces on top of every guard above, not
instead of them. A fill runs to completion in `content.js` regardless of whether the side panel
is open, so each frame's shield's lifetime is tied to that frame's own participation in the
fill, never to the panel's UI.

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
`src/applypilot/`, started with something like `applypilot serve-extension`).

### Auto-connect (the normal path)

Run `applypilot extension install-host` once, from a terminal, and you never need a terminal or
a pasted token again. That command pins this extension's id (giving `manifest.json` a `key`, so
the id no longer depends on which folder you loaded it from) and registers a Chrome
native-messaging host (`com.applypilot.copilot`) that only THIS extension id may talk to.

From then on, `background.js` calls that host itself, right before the first service call it
ever needs to make (and again, once, if a later call ever comes back unauthorized or
unreachable — e.g. the service was restarted and rotated its token): the host starts
`applypilot serve-extension` if nothing is already answering on its port, waits for it to come
up, and hands back its port and token, which `background.js` stores exactly where the manual
flow below already looks (`chrome.storage.local.serviceUrl`/`token`). Nothing is pasted, nothing
is typed — the first time you click **Fill this page** after installing the host, it just works.
Every attempt (success or failure) is recorded to `chrome.storage.local.serviceConnection` —
`{mode: "native"|"manual", ok, error, checkedAt}` — for the Settings page to show.

### Manual fallback

If you haven't run `install-host` (or you're on a browser build where native messaging isn't
available), the extension falls back to exactly the flow that existed before auto-connect:

1. Right-click the extension icon → **Options** (or click **Settings** inside the side panel).
2. Paste the **Service URL** (defaults to `http://127.0.0.1:8787` — it must always be
   `http://127.0.0.1`, the options page refuses anything else) and the **token** `applypilot
   serve-extension` printed on its own first run.
3. Click **Save**, then **Test connection**. You should see which decision tiers the service
   reports (`canary`, `deterministic`, and — if the optional Laya model loaded — `laya`).

The panel's footer line says so directly when the native host genuinely isn't installed yet
("Specified native messaging host not found" is Chrome's own wording for that) and nothing is
configured: "Tip: run `applypilot extension install-host` once and the service will start by
itself." If the service isn't running, or the token is wrong, the panel and options page show a
plain error message instead of failing silently (see `background.js` — 401 → "token is wrong",
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
   the scanner into the tab, and the only one that writes to the DOM.
   - **The first time on a new site**, Chrome asks you to allow ApplyPilot on that site (and any
     cross-origin iframe embedded in it) — see "Permissions" below. Decline it and nothing runs;
     allow it and you won't be asked again for that site.
   - It asks the local service how many `work_history`/`education` entries your profile has,
     and — only if that succeeds — clicks each repeating section's "Add Another" button
     (guarded, see above) just enough times to make room for them before scanning, capped at
     10 clicks total per fill and stopping immediately the moment a click fails to add a new
     block (never retried blindly). If the service doesn't support this yet (404) or isn't
     reachable, this step is skipped entirely and the page is scanned exactly as before.
   - It attaches your résumé (if it can find where to put one) **before** scanning anything
     else, then waits briefly for the page to settle — see "Résumé first" below for why.
   - It scans the page — and every frame you allowed, including a cross-origin iframe embedding
     a Greenhouse/Lever-style form on a company's own careers page (see "Fill every frame"
     below) — for fields, including a Workday-style date rendered as separate month and year
     inputs/selects behind one visual "From"/"To" label, which is scanned as a single merged
     field and split back into both underlying controls on fill.
   - It sends only the field metadata (labels, names, autocomplete, options — never page
     content, never your profile) to the local service, in **one** request for the whole page
     regardless of how many frames it came from.
   - It fills every field the service marked `auto_fill: true` **one at a time**, with a green
     outline and a tooltip explaining what was filled and why, showing live progress in the
     panel ("Filling 12/40 — Skills (adding 3/15)") as it goes — never a field that already
     holds a value you (or the page) put there before this fill started, see "Never overwrite
     the user" below.
   - It leaves every other detected field alone, with an amber dashed outline and a tooltip
     explaining why it was skipped (e.g. a canary question like sponsorship or salary that
     has no safe automatic answer, or a value it deliberately kept — see below).
4. **This keeps running even if you close the panel, switch tabs, or the panel's window loses
   focus.** Every frame's fill lives in that frame's own content script, not in the panel —
   closing the panel only stops you from watching it. Switch back (or reopen the panel) any time
   to see where it's at, or the finished result.
5. **Cancel** stops the fill between fields (and between items within a multi-value field like
   Skills), in every frame at once — whatever was already filled stays filled and is reported;
   everything after that point is reported as "not attempted — cancelled" so you know exactly
   what still needs doing.
6. No single field can hang the fill: each one gets a ~12s timeout (reported as "timed out" and
   skipped, not retried) and the whole fill gives up on any remaining fields after a ~120s
   budget ("not attempted — time budget") rather than spinning forever on one stuck widget.
7. Read the panel's Filled / Drafted / Need you / Could not fill lists. **Review the page itself
   before submitting** — the panel is a summary, not a substitute for looking at the actual
   form, and it does not overstate what actually happened: a field is only ever counted as
   Filled after a brief re-check confirms the value actually stuck (some React-style forms
   silently revert a field a moment after it's written — that's reported as "didn't stick", not
   Filled), and the summary line says plainly how many visible fields — plus any frame that
   couldn't be read at all — the scanner never got to (e.g. "Filled 18 · 3 need you · 2 couldn't
   read"). Switching tabs and back shows that tab's own last result; if the tab has since
   navigated to a different page, the panel says so ("from a previous page") rather than
   presenting old results as current.
8. If something's wrong, click **Undo** to restore every field's prior value in every frame, and
   the panel tells you how many were actually confirmed restored (read back after undoing), not
   just how many restores were attempted. **Report page** (unchanged, top frame only) saves the
   form's structure — never values — for diagnosing a bad fill.

## Draft cover letter

The panel's **Draft cover letter** button asks the local service to write one, from your profile
and résumé, against the job behind the page you're looking at:

1. `content.js` (top frame only, same as **Report page**) reads the page's own main visible text
   — capped at ~15,000 characters, walking live text nodes and skipping anything inside a form
   field (`input`/`select`/`textarea`/`button`/`option`) or anything the scanner would already
   treat as invisible — and separately scans for a `textarea` or plain text `input` whose resolved
   label mentions "cover letter".
2. `background.js` calls `POST /cover-letter` with the tab's URL, every frame's URL (an embedded
   Greenhouse/Lever/Ashby/Workday iframe's own URL is what lets the service recognize the posting
   through its public API — see `job_context.py`), and that page text as a last resort.
3. The result is shown in a clearly-marked **DRAFT** box (blue, "review before using" — the same
   color this panel uses for every piece of generated text, never green) with the job the service
   identified, any validator warnings, and **Copy** / **Download .txt** buttons. **Insert into the
   cover-letter box** only appears when step 1 actually found a field for it.
4. Inserting goes through the exact same guarded `applyFill()` / highlight path a normal fill
   uses — so it survives a controlled-input re-render the same way, is highlighted **draft**
   (blue), is recorded in this frame's own `priorValues`, and **Undo** restores it exactly like
   any other field.
5. A 403 ("no language model available on this computer, or the operator hasn't allowed the
   configured cloud one yet") or 422 ("couldn't find this job's description to write a letter
   against", or the draft was refused because it claimed experience the profile doesn't have — see
   `grounding.py`) is shown **verbatim** — the service's own `detail` text, not a generic failure.

## Tailor my résumé

The panel's **Tailor my résumé for this job** button (next to Draft cover letter) asks the local
service to tailor your résumé against the job behind the page you're looking at, the same
page-text/frame-URL sourcing **Draft cover letter** already uses:

1. `background.js` calls `POST /resume/tailor` with the tab's URL, every frame's URL, and the
   page's own visible text as a last resort, and gets back `{id, url, title, company, status
   ("approved" | "approved_with_judge_warning"), judge: {verdict, issues}, warnings, created, pdf,
   text}`.
2. The result is shown in a clearly-marked **TAILORED RÉSUMÉ — review before using** box (the
   same blue "generated content, review before using" color the cover-letter draft uses, never
   green — this is model-tailored text, not a plain fact pulled from your profile) with the job
   identified, the judge's own issues when the status carries a warning, the validator's
   warnings, a collapsible preview of the tailored text, **Download PDF**, and **Use for this
   application**.
3. A 403 (no model on this computer, and no cloud model allowed), 422 (no job description found,
   no résumé on file, or the tailored version failed the fabrication checks), or 503 (no model
   available at all) is shown **verbatim** — the service's own `detail` text, exactly like the
   cover letter's own 403/422 handling above.
4. **Download PDF** fetches `GET /resume/tailored/{id}` through `background.js` (the panel never
   holds the token) and saves it under the filename the service's own `Content-Disposition`
   header names (`"<Full Name> - Resume.pdf"`).
5. **Use for this application** remembers the choice **per tab** (it survives this tab's next
   fill, the same way the multi-step continuation toggle's own state does) and, right away, tries
   attaching the tailored PDF immediately if this page already has a résumé upload with nothing
   in it yet — through the exact same guarded `attachResumeFile()` path (and the same Workday
   upload-confirmation wait) the base résumé already uses. If nothing on the page can take a
   résumé yet, nothing happens immediately and the **next** "Fill this page" attaches the
   tailored PDF instead of the base résumé when it gets to its own résumé-first step. Either way,
   the résumé line says which file actually landed — `"Attached <name> (tailored)"` or `"...
   (base)"` — never leaving that ambiguous.
6. If a résumé is **already** attached on the page (base or otherwise) when either of the above
   runs, nothing is removed or replaced automatically: the panel says "A résumé is already
   attached — remove it on the page, then click Attach tailored résumé," and offers that button,
   which retries the exact same guarded attach and refuses again for as long as something is
   still sitting there.

## Remember my answers

After a fill, if it left anything for you to answer yourself, the panel shows a **Remember my
answers** button. Clicking it (never automatic):

1. `content.js` reads the CURRENT value of exactly the fields that fill's "needs you" list named
   (by field id) — non-empty only, and never a password or file field, regardless of what's typed
   into one.
2. `background.js` sends each one to `POST /answers/learn` with that field's own resolved label as
   the question, then the panel shows what was **saved** and what was **skipped** (with the
   reason the service gave — a canary question, a screening attestation, something
   company-specific, ...; see `answer_memory.py`).
3. Saved answers join the active profile's own answer bank, so the answer-bank tier fills them
   automatically next time the same (or a very similarly worded) question comes up.

If nothing you were left with has an answer typed in yet, clicking the button says so and makes no
service call at all.

## Application log

Every completed fill is logged automatically — no click required. `background.js` POSTs
`/log` with the page URL, the page's own `document.title`, a best-effort company guess (a
recognized ATS host — Greenhouse/Lever/Ashby/Workday — is left for the service's own smarter,
posting-aware lookup; anything else gets a plain hostname-derived guess), and the fill's counts
translated into the service's own key names (`needs_you`, `unreadable`). The panel then shows
**Logged** and a **Mark as applied** button; clicking it posts `/log/{id}/status` and the line
updates to say so. A second fill of the same page within the hour updates that one entry rather
than creating a duplicate (the service's own behavior — see `app_log.record()`) — exactly what a
multi-step form's later steps, or just clicking Fill again, produce.

## Review rows

Every row in Filled / Drafted / Need you / Could not fill shows the field's own label, a status
badge (**Verified**, **Draft**, **Left for you**, **Kept your value**, **Failed**, or **Didn't
stick**), and — when the fill had one — its source (`profile`, `answer_bank`, `draft`, ...).
Clicking a row asks `background.js` to route to the exact frame that field lives in (every row's
id is qualified with its own frame — see `applyFrameReport()`), which scrolls it into view and
flashes its highlight right on the page. Within "Need you" and "Could not fill", a **required**
field that's still unfilled sorts to the top of its section, so the field most likely to block
your application is never buried below a dozen optional ones. The summary line's "N couldn't
read" count (see "Report honestly" below) is unchanged by any of this.

## Export fill report

**Export fill report** downloads a JSON file built entirely from what's already in
`chrome.storage.session` — no page access, no permission prompt. Per field: which frame it's in
(a frame id, plus that frame's own **host/path**), its label, its tag/widget, its status, its
source, and a **category** for why it landed where it did — plus the page's own host/path and the
fill's counts. **Never a value, and never the free-text reason, anywhere in the file.** An
earlier build redacted only double-quoted substrings out of the reason text, which missed a value
in single/curly quotes or with no quotes at all (an unquoted skills term, a canary question's own
wording); the export is now built from an **allow-list** instead — `category` is one of a small,
fixed, closed vocabulary (`content.js`'s `categoryForSource()`/every push site in `applyFills()`)
content.js sets at the exact point it already knows why an entry was produced, never derived by
parsing anyone's prose — so nothing the service or the page ever says can leak into this file,
regardless of how either one happens to phrase a reason on screen. The page's and every frame's
URL are stripped to host/path the same way — an ATS's own query string can carry a one-time or
identifying token (Greenhouse's `validityToken=…`, iCIMS's `hashed=…` id, ...) that has no
business leaving this machine in a file meant to be forwarded to someone else. This file is meant
to be sent to someone else to diagnose a bad fill.

## Keyboard shortcuts

Two commands, both rebindable at `chrome://extensions/shortcuts`:

- **Alt+Shift+F** opens the panel (`_execute_action`, a name Chrome itself dispatches by
  simulating the toolbar icon's own click — no code here, it just reuses `openPanelOnActionClick`).
- **Alt+Shift+G** fills the active tab directly, without opening the panel or clicking Fill first.
  The shortcut's own gesture grants `activeTab` for the tab's top frame, which is enough on its
  own for a page with no cross-origin embedded form. If the page DOES have one (an embedded
  Greenhouse/Lever-style iframe) and its origin isn't already granted, this never fills the top
  frame alone and silently skips the rest — it opens the panel and shows the same "ApplyPilot
  needs permission..." line the panel's own Fill/Report buttons show, so you grant it the normal
  way (click **Fill this page**).

## Multi-step continuation (opt-in)

A per-tab toggle in the panel, **off by default**: "Keep filling as I go through steps." Turning
it on (gated the same way Fill/Report are — nothing runs until you do) arms `content.js`'s own
watcher in the top frame for a Workday-style multi-step form. It watches for two independent,
concrete signals that a new step just rendered — never a guess, never a timer alone:

1. `location.href` changed since the last step (a step that changes the URL, or a hash-only route
   change).
2. The form root re-rendered: most of the elements the last fill actually touched are no longer
   connected to the document at all.

Either one, once the page settles, triggers a completely normal fill for the new step — same
`PREPARE_AND_SCAN`/`APPLY_FILLS` pipeline, same submit shield, same post-fill verification, same
"never overwrite the user" rule. **This never clicks Next or Submit itself** — it only reacts once
you (or the page) have already moved to the next step. The panel shows the step count and the
toggle stays visible so turning it off is always one click away. It's automatically treated as off
the moment the tab navigates to a different host than the one it was turned on for.

## Fill every frame

About a quarter of real job postings are a company's own careers page embedding the actual
application form (Greenhouse, Lever, ...) in a **cross-origin** `<iframe>` — e.g. `sofi.com`
embedding `job-boards.greenhouse.io/embed/job_app`. A normal content script cannot read into a
cross-origin frame at all, so until this build, only the top frame was ever scanned — the entire
embedded form was invisible.

Filling now works across every frame you've granted:

- When the panel renders a tab, it asks `chrome.webNavigation.getAllFrames` for every frame the
  tab currently has and checks which of their origins are already permitted.
- Clicking **Fill this page**, once permission is confirmed (see below), injects
  `scanner.js`/`capture.js`/`content.js` into **every** one of those frames
  (`chrome.scripting.executeScript` with `allFrames: true` — Chrome silently skips any frame this
  extension isn't allowed into, it never errors the whole call).
- `background.js` — the only context that can message a *specific* frame of a tab — then asks
  every frame to expand/attach-résumé/scan **itself**, merges every frame's fields into **one**
  list (each field's id tagged with which frame it came from) and sends that single merged list
  to the local service in **one** `/resolve` call. It splits the answer back into each frame's
  own slice and hands each frame only its own fields to apply.
- From there each frame fills its own fields completely independently — its own progress, its
  own response to Cancel, its own per-field timeout, and its own copy of the submit shield
  covering it for as long as it's being filled — and reports back the same way a single-frame
  fill always has. `background.js` merges every frame's report into the one result the panel
  shows, so a two-frame page still looks like one fill: one progress bar, one set of lists, one
  Undo.
- A frame that is *same-origin* with its own parent is left to `scanner.js`'s existing one-level
  same-origin recursion (see "Field scanning notes" below) rather than also being scanned
  independently — the two mechanisms are made to partition the frame tree between them, not
  overlap, so a same-origin child is never scanned or filled twice.
- Any frame this run could not get into at all (no permission, or it disappeared between being
  found and being asked to scan) is never guessed at — it's counted honestly in the summary's
  "couldn't read" figure and the "N embedded frame(s) could not be scanned" note (see "Report
  honestly" below), and everything else on the page still fills normally.

## Résumé first

Attaching a résumé happens **before** anything else is scanned or filled, not after. Several
ATSs (Workday, Lever, Ashby) parse an uploaded résumé and use it to prefill or overwrite fields
like name, email, phone or work history — sometimes with the wrong value. Scanning and filling
first, the way earlier builds did, meant those parsers would silently clobber the values this
extension had just filled in. Now: attach the résumé (or confirm one is already attached) →
wait for the page to settle (a Workday-style upload has its own confirmation marker this extension
waits for directly; everything else gets a short, generic "nothing has moved in ~1.2s" wait,
capped at 3s) → **then** scan → resolve → apply.

## Never overwrite the user

Before writing **any** field — a plain text/select/textarea field, a radio group, a react-select
or plain ARIA combobox, an Ashby-style Yes/No button group, a checkbox group, a Workday dropdown/
date/prompt, or the Lever location field, every widget kind this extension knows how to fill — if
it already holds a non-empty, non-placeholder value that differs from what's about to be written,
and that value was genuinely there before this fill touched anything, the fill leaves it alone and
reports "kept your value" instead of clobbering it — a Workday page can prefill several fields
from your account, and you may have typed or picked something into the form yourself before
clicking Fill.

This used to be scoped to only plain fields and radio groups, on the theory that "already has a
value" was unambiguous only for those two. In practice that let a react-select "How did you
hear?" combobox already committed to "Referral" get silently changed to "LinkedIn", and an Ashby
Yes/No button group already set to "No" get flipped to "Yes" — both reported "Verified" as if
nothing had been kept. Every kind now gets the same protection.

Because résumé attachment now happens *before* scanning (see above), "already had a value" is
deliberately **not** enough on its own — that would just as happily protect an ATS's own
résumé-parse guess as it would your own input, presenting the ATS's guess as if it were yours.
Two independent signals decide whether a value actually predates this fill: a snapshot of every
field's value taken before expansion/résumé/scanning even started, and a real (browser-trusted)
input/change/**click** event on that field at any point during the run (`click` is what makes this
work for button-group and a Workday dropdown's own opener button — both are driven purely by
clicks and never fire input/change at all) — this extension's own programmatic writes are never
"trusted" this way, so it can never mistake its own fill for yours. A value that only appears
*because of* this run's own résumé attach is treated as the résumé parser's guess, not yours, and
the real profile value still overwrites it.

A Workday dropdown's un-opened button often shows non-empty PLACEHOLDER text ("Select One" —
Workday's own real wording) that isn't a real answer, and a Workday date's masked/empty state can
likewise read back as its own mask skeleton ("MM/YYYY") rather than a clean empty string — both
are treated as "no value" rather than "kept your value", never mistaken for one. The Skills/
Field-of-Study prompt is additive — a matched fill never removes an existing pill — so protecting
it only ever means "don't add on top of a real prior answer that differs," never "erase what's
there."

## Replace kept values with my profile

Workday in particular prefills several fields from your **account** on earlier steps of a
multi-step flow — sometimes with a stale or simply wrong value for *this* application — and
"never overwrite the user" (above) will faithfully keep it, exactly as it would keep something you
typed yourself. When a fill kept one or more values, the panel shows each one as its own row under
**Need you**, with a checkbox, the **page's current value** and **your profile's value** shown
side by side, and a **Replace kept values with my profile** button. Tick the ones that are
actually wrong, click the button, and only those fields are re-filled — through the exact same
guarded fill path every other field uses (per-field timeout, highlight, post-fill verification,
and **Undo** restores the page's own prior value exactly like any other fill). Fields you don't
tick are left exactly as they were. The page/profile values shown here are **panel-only** — they
are never written into the fill-report export (see "Export fill report" below).

## Permissions, and why they're this narrow

- `activeTab` + `scripting`: lets the panel inject `scanner.js`/`capture.js`/`content.js` into a
  tab, and only after you click **Fill this page** or **Report page** in it — never on load,
  never just because you switched to that tab. There is **no** `content_scripts` entry in
  `manifest.json` — the extension does not run on every page you visit.
- `sidePanel`: lets `background.js` open the side panel when you click the toolbar icon
  (`chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true })`) instead of the old
  popup.
- `webNavigation`: read-only — lets the panel ask `chrome.webNavigation.getAllFrames` what
  frames a tab currently has, so it knows which origins a fill will need (see below) and so
  `background.js` can find and message each one by its own frame id (see "Fill every frame").
  It never lets this extension see browsing history beyond the current tab's own frame tree.
- `host_permissions` is **only** `http://127.0.0.1/*` and `http://127.0.0.1:*/*` — the local
  service, and nothing else. That is the *only* host this extension can ever act on without
  asking you first. There is no static grant for `https://*/*`, `http://*/*`, or `<all_urls>` —
  an earlier build in this repo's history did request those broadly (a permission "granted at
  install and never revisited") and that has been deliberately narrowed to the least-privilege
  model below, matching how a well-behaved MV3 extension is supposed to ask for site access.
- `optional_host_permissions` declares `https://*/*` and `http://*/*` as permissions this
  extension *may ask for*, but does not hold, until you say yes:
  - When the panel renders a tab, it works out every origin a fill would touch — the tab's own,
    plus every frame's (via `webNavigation.getAllFrames`) — and checks which are already granted
    (`chrome.permissions.contains`).
  - Clicking **Fill this page** (or **Report page**) calls `chrome.permissions.request()` for
    whatever's missing, **synchronously, as the very first thing the click handler does** — Chrome
    only honors a permission prompt as part of the click that triggered it if nothing has
    `await`ed anything first, so the origin list and which parts of it are missing are worked out
    ahead of time (at render, not at click) specifically so the click handler never has to.
  - You're asked **once per site** (Chrome remembers the grant — see `chrome://extensions` → this
    extension → "Site access" — and it's revocable there any time); if you decline, the panel
    shows one line — "ApplyPilot needs permission to fill forms on \<hosts\> — nothing runs until
    you allow it." — and **nothing is injected, nothing runs**.
  - A 127.0.0.1 test/dev page never triggers this prompt at all — it's already covered by the
    static `host_permissions` above.
  - None of this widens what the extension does *unprompted*: injection is still 100% gated on a
    Fill/Report click, exactly as before. A granted origin only ever changes what the extension is
    *allowed* to be asked to do next time you click Fill on that site — never what it does on its
    own.
- `storage`: holds the service URL and token in `chrome.storage.local` (never synced), and is
  also what backs `chrome.storage.session` — the per-tab fill-result store described below.
- `nativeMessaging`: lets `background.js` talk to the `com.applypilot.copilot` native host (see
  "Auto-connect" above) to start the local service and fetch its port/token without a terminal.
  The host only ever accepts a connection from the extension id `manifest.json`'s `key` pins —
  see `allowed_origins` in the host manifest `install-host` writes — so this permission cannot be
  used to reach any OTHER native host on the machine.

## Architecture / file map

| File | Role |
|---|---|
| `manifest.json` | MV3 manifest — permissions, icons, side panel, options page, service worker. |
| `scanner.js` | Pure-DOM field scanning + filling logic. No `chrome.*` calls. Shared verbatim between the real content script and the offline Node self-test (see below). Exposes `ApplyPilotScanner` on the global object. |
| `content.js` | The real content script — injected once per **frame** (see "Fill every frame"), loaded after `scanner.js` in the same isolated world. Listens for `PREPARE_AND_SCAN` / `APPLY_FILLS` / `ABORT_FILL` / `CANCEL_FILL` / `DETECT` / `UNDO` / `CAPTURE` messages, all of them sent by `background.js` (never directly by the panel). Runs this frame's own share of the fill pipeline (expand → attach résumé → wait for the page to settle → scan, then later, once `background.js` hands back this frame's own fills, apply → verify they stuck) so it survives the side panel closing; reports live progress and the final result to `background.js` via `chrome.runtime.sendMessage`, never by returning a value the panel has to stay open to receive. Owns this frame's own live element registry and the "prior value" snapshots used by Undo, and does all of this frame's own DOM highlighting. |
| `capture.js` | "Report page" structure capture — page furniture (tags, roles, labels, option lists) only, never field values. See "Field scanning notes" / privacy note in its own header. |
| `background.js` | MV3 service worker. The only file that holds the token and calls `fetch()` (talks to the local service's `/resolve`, `/health`, `/profile/counts`, `/resume`), and the only file that writes `chrome.storage.session`. Sets `chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true })` so the toolbar icon opens the panel. Also the cross-frame **coordinator**: `RUN_FILL` asks every frame of a tab (via `chrome.webNavigation.getAllFrames`) to `PREPARE_AND_SCAN` itself, merges every frame's fields into one list for a single `/resolve` call, and hands each frame back its own slice via `APPLY_FILLS` (see "Fill every frame"); `CANCEL_FILL_TAB` / `UNDO_TAB` fan the same two actions out to every frame of a tab and (for Undo) sum how many fields were actually restored. Merges every frame's own `FILL_STATE_UPDATE` into ONE combined result per tab, persisted into `chrome.storage.session` keyed `fillState_<tabId>` (per-tab writes are queued so two updates for the same tab can never race each other), and cleans that entry up on `chrome.tabs.onRemoved`. |
| `sidepanel.html` / `sidepanel.js` / `sidepanel.css` | The review UI (replaces the old popup — see "Using it on a job application page"). A pure renderer: reads `chrome.storage.session` (plus `chrome.storage.onChanged` for live updates) for whichever tab is active in its window, and only ever tells `background.js` what to do (`RUN_FILL`/`CANCEL_FILL_TAB`/`UNDO_TAB`) or talks to the top frame directly for `CAPTURE` — it never writes fill-result state itself. Also owns the optional-permission flow (see "Permissions"): precomputes each tab's needed origins and which are already granted, and calls `chrome.permissions.request()` synchronously from the Fill/Report click handlers. Supports an optional `?tabId=` query parameter that pins it to a specific tab instead of following the active tab; this exists **only** for `scripts/chrome_panel_test.py` and must never change behavior when absent. |
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
- Same-origin iframes are scanned too, from INSIDE `scanAll()` itself (it walks
  `document.querySelectorAll('iframe')` and recurses one level into any `contentDocument` it can
  reach without throwing) — `scanAll()` on its own still cannot see into a **cross-origin**
  iframe at all; that's a normal content script limitation, not something this file can work
  around from inside one frame's own JS realm.
  - `content.js`/`background.js` close that gap from the OUTSIDE instead (see "Fill every
    frame" above): every frame of the tab — same-origin or cross-origin, at any nesting depth —
    that `chrome.webNavigation` can see and this extension has host permission for gets its own
    independent content-script injection and its own scan, so a Greenhouse/Lever-style form
    embedded cross-origin on a company's own careers page (e.g. `sofi.com` embedding
    `job-boards.greenhouse.io/embed/job_app`) is filled like any other frame. The two mechanisms
    are kept from double-handling the same frame (see `excludeFramesCoveredByParentRecursion()`
    in `background.js`): a same-origin child already covered by its parent's one-level recursion
    above is not ALSO scanned independently.
  - A frame this extension genuinely could not get into at all (no host permission for its
    origin, or it disappeared between being found and being asked to scan) is never guessed at —
    it's counted honestly, both in the fill summary's "couldn't read" figure and in the
    "N embedded frame(s) could not be scanned" note (see "Report honestly" below).

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

## Report honestly

The fill summary is written to never overstate what actually happened:

- After the whole apply loop for a frame finishes, it waits briefly (~500ms) and then reads
  every field it just filled back with `getCurrentValue()`, comparing it to what it set. A field
  that no longer matches — some controlled-input re-render reasserting old state, or a combobox
  clearing its own search text on blur — is moved out of "Filled" and reported as **"didn't
  stick"** instead. The summary's Filled count only ever includes verified fills.
- **A question an earlier answer only just revealed is not missed.** Some forms render part of
  themselves only in response to an earlier answer — e.g. a Lever-style US EEO survey (16 radios)
  that appears only once "What is your location?" is set to United States. After the verify step
  above, each frame re-scans **itself** (comparing by the actual DOM element, never scanner.js's
  own positional id, which can shift once new fields are spliced in) for up to 2 more rounds,
  resolving and filling only what's genuinely new through the exact same guards, shield,
  verification, per-field timeout and Cancel as the first round — nothing about a rescanned field
  is treated any differently. The "couldn't read" figure below is computed from the page as it
  stands after every rescan round, not frozen at the snapshot taken before the first field was
  ever touched.
- The summary line includes how many visible, interactive-control-shaped elements on the page
  (`input`/`select`/`textarea`/`role=combobox`/`role=radiogroup`, minus the ones the scanner
  itself excludes on purpose) never made it into the scanner's own registry at all, **plus** any
  frame this run could not get into to find out — e.g. "Filled 18 · 3 need you · 2 couldn't
  read". Neither of these is guessed at or rounded away.
- A separate note spells out how many embedded frames could not be scanned at all (see "Fill
  every frame" above) so it's clear that gap is about a whole frame, not a single field.

## Undo

Before writing any field, `content.js` snapshots its current value (`getCurrentValue()`) —
text, select value, checkbox boolean, or checked radio's value/none — keyed by field id, in
every frame that fill touched. **Undo** fans out to every frame and replays each one's own
snapshots through the same `applyFill()` path used to fill them (so it survives React's
controlled-input re-renders the same way filling does), removing every highlight it added. It
then reads each restored field back and reports how many were actually **confirmed** restored
— not how many restores were merely attempted, which can silently overstate what Undo did if a
widget rejects a programmatic write the same way a fill sometimes can — and, just as honestly,
how many restores it attempted but could **not** confirm, so a widget that quietly rejected the
restore is surfaced ("3 could not be confirmed restored — check them by hand") instead of just
disappearing from the count.

## Known limitations / where a real ATS form could defeat the scanner

- **A frame's origin the operator declines (or is never asked about) is skipped entirely** —
  cross-origin iframes are no longer a blanket gap (see "Fill every frame" above), but a
  specific frame is still skipped if its origin wasn't granted: the operator said no to the
  permission prompt, or a frame appeared after the panel's own `webNavigation.getAllFrames`
  snapshot was taken (e.g. one inserted by the page's own JS a moment after load) and so was
  never in the list offered for permission or injection in the first place. Both cases are
  counted honestly (see "Report honestly"), never guessed at.
- **Same-origin vs. cross-origin frame classification is best-effort.** A same-origin child is
  left to its parent's one-level recursive scan rather than also being scanned independently
  (see `excludeFramesCoveredByParentRecursion()` in `background.js`) — this is correct for the
  common shapes (a form with no iframes, or one cross-origin ATS iframe) but has not been proven
  against a real multi-level nested-iframe page outside the test suite's own fixtures.
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
  `chrome_load_test.py`, `--load-extension` and all), as of this writing 255 checks across 19 tabs
  plus the manifest/panel-load checks, exercises everything this README's "Using it on a job
  application page" section above describes that `chrome_load_test.py` cannot:
  - the manifest has no `default_popup`, does declare `side_panel`, holds no static broad
    `https://*/*`/`http://*/*`/`<all_urls>` host permission, and declares both as
    `optional_host_permissions` instead (see "Permissions");
  - clicking **Fill this page** in a real panel drives a real fill against a tiny stub of the
    local service (no live `applypilot serve-extension` needed — the stub binds an OS-assigned
    free port so it can never collide with a real one) and the result lands in
    `chrome.storage.session` keyed per tab; a second tab gets independent state and filling it
    never disturbs the first tab's stored result; `chrome.storage.onChanged` fires multiple
    times with advancing progress while a fill is running;
  - **Cancel** stops a fill partway through (some fields filled and kept, the rest reported as
    "not attempted — cancelled"), well before the fill would have finished on its own, via the
    same `CANCEL_FILL_TAB` fan-out the real button now uses;
  - a field patched (via `chrome.scripting.executeScript`, from outside the extension — no
    source file is modified) to never resolve is reported as "timed out" without stalling the
    other 30+ fields on the same page;
  - **fill every frame**: a page on one 127.0.0.1 port embedding a form page served from a
    DIFFERENT 127.0.0.1 port (a genuinely different origin) has both the outer page's own field
    and the cross-origin iframe's fields scanned, filled and reported through exactly **one**
    `/resolve` call, with zero submissions in either frame, and `UNDO_TAB` restoring fields in
    both; a frame deliberately left uninjected (standing in for "no permission"/"injection
    raced a navigation") is counted honestly in `couldNotRead`/`skippedFrames` while the frame
    that WAS injected still fills normally;
  - **never overwrite the user, but do correct the résumé's own guess**: a value already on the
    page before the fill started is kept and reported "kept your value", while a value a
    (simulated) résumé attach introduces is still overwritten by the real profile value;
  - **never overwrite the user, every widget kind (reviewer round 4)**: a react-select combobox
    already committed to one option, and an Ashby-style Yes/No button group already set to "No",
    both end the fill unchanged and reported "kept your value" — proving the exact two cases the
    reviewer reproduced against the pre-round-4 code; a value that only appears in a combobox
    *after* the (simulated) résumé attach is still replaced by the real profile value, same
    distinction as the plain-field case above; **Replace kept values with my profile** changes
    exactly the rows ticked (and only those) and they end up verified, everything left unticked
    stays exactly as it was;
  - **the fill-report export's `frame` field carries no query string**: a frame URL with a
    Greenhouse-style `validityToken=…`/iCIMS-style `hashed=…` query string still exports as bare
    host/path, same as the page field;
  - **didn't stick**: a field made to silently revert shortly after being filled is caught by
    the post-fill verify step and reported "didn't stick", never counted as Filled;
  - closing a tab clears its `chrome.storage.session` entry; and none of the above ever submits
    any mock form, in any frame. Run it the same way as `chrome_load_test.py` (below).
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
2. **The real permission prompt** — this is the one thing about this build that genuinely cannot
   be exercised by automation: Playwright's `chrome.permissions.request()` on a real, ungranted
   site would need to interact with a native Chrome permission bar/dialog no test in this repo
   drives, so every 127.0.0.1 page these tests use is covered by the static `host_permissions`
   and never actually shows the prompt (see `chrome_panel_test.py`'s own comments). On a real,
   never-visited https site: click **Fill this page**, confirm Chrome's own permission prompt
   appears naming that site, decline it once and confirm the panel shows the plain "ApplyPilot
   needs permission to fill forms on \<host\> — nothing runs until you allow it" line with
   nothing injected (check the page's own DevTools console/Elements panel for the absence of any
   `data-applypilot-*` attribute), then click Fill again, allow it, and confirm the fill runs; a
   third click on the SAME site should not prompt again. If Chrome's actual behavior here ever
   turns out to differ from `chrome.permissions.request()`'s documented contract (e.g. a partial
   grant when multiple origins are requested at once), `gatePermissions()` in `sidepanel.js`
   treats anything short of a fully-`true` resolution as a full decline — nothing is injected —
   rather than guessing which origins actually came through.
3. Without a running local service, click **Fill this page** and confirm the panel shows a clear
   "service unreachable" message rather than failing silently or throwing.
4. Start the real `applypilot serve-extension`, click **Fill this page** on a real page, and
   visually confirm: green outlines on filled fields with correct tooltips, amber on skipped
   fields, live progress text while it runs, and **Undo** restores everything.
5. Try it against a real Greenhouse/Lever/Ashby/Workday application page to see how the label
   heuristics and selector generation hold up outside the mock page — this is exactly the
   class of thing the mock page can't fully substitute for.
6. Try it against a real company careers page that embeds its ATS form in a cross-origin
   `<iframe>` (the scenario "Fill every frame" above exists for) and confirm the embedded form's
   fields get scanned, filled and highlighted exactly like a top-level form would.
7. **The keyboard shortcuts** — press Alt+Shift+F on any tab and confirm the panel opens exactly
   as it does from the toolbar icon; press Alt+Shift+G on a real job application page (no panel
   open at all) and confirm it fills the page directly. On a real page embedding a cross-origin
   ATS iframe you haven't already granted, confirm Alt+Shift+G does NOT partially fill the top
   frame — it opens the panel showing the "needs permission" line instead, and a real, native
   `chrome.commands` keypress is not something Playwright can simulate at all, so
   `chrome_panel_test.py` instead calls `handleFillPageCommand()`/`openPanelWithPermissionNotice()`
   directly against the real service worker — see that file's own comments on both this and item
   7's other genuinely-manual-only gap, `chrome.sidePanel.open()`'s user-gesture timing once the
   permission check's own `await` has already happened (this build's own code degrades safely
   either way: the permission note is written to storage regardless of whether the panel visibly
   auto-opens, so it's there the next time the panel *is* opened, manually or otherwise).
