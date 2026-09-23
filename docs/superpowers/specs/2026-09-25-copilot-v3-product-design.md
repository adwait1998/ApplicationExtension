# Copilot v3 — resume-first onboarding, résumé attachment, and a product-grade UI

**Status:** design, 2026-09-25
**Builds on:** v1 (`2026-09-23`, the ladder + never-submit) and v2 (`2026-09-24`, structured /
answer-bank / draft tiers + profile editor)

## The problem with v2

v2 shipped a profile editor with repeatable work-history and education rows. It works, and the
operator's verdict was correct: **"too manual."**

Asking someone to retype their entire career into a form is the worst possible first-run
experience, and it is exactly the information already sitting in their résumé. A tool whose
whole purpose is to save time cannot open with twenty minutes of data entry.

Two things follow, and they are the whole of v3:

1. **The résumé is the input.** Upload it once; the profile fills itself; the operator
   reviews and corrects rather than authors.
2. **The résumé is also an output.** Nearly every application requires attaching the file.
   v1 declared this out of scope ("OS-level picker"), which was wrong — a content script can
   populate a file input via `DataTransfer`. It is the single biggest manual step left.

Plus: make it look and feel like a product someone would choose to use.

## A. Résumé → profile

```
options page: [ Upload résumé ]  (.pdf / .docx / .txt)
      |  multipart POST /profile/import-resume
      v
  text extraction        pypdf (present) / python-docx (optional) / plain text
      |
  deterministic pass     email, phone, LinkedIn/GitHub/portfolio URLs, name
      |                  — high-confidence regex, no model needed
  LLM pass               work_history[], education[], skills, titles, dates
      |                  — one call, one time, not per-field
      v
  DRAFT profile returned -> editor populates -> operator reviews -> Save
```

### Rules

- **Never auto-save.** Parsing a résumé is fuzzy. The result lands in the editor as a draft
  the operator confirms. A silently-saved wrong employer would propagate into real
  applications for months.
- **Canary fields are never inferred.** A résumé does not reliably state work authorisation,
  sponsorship need, or salary expectation. Those stay explicitly operator-set — guessing them
  is precisely the harm this project refuses everywhere else.
- **Show provenance.** Fields the parser filled are visually marked as "from your résumé" so
  the operator knows what to check.
- **Store the file.** The upload is saved as `resume.pdf` / `resume.docx` in the profile
  directory, which is what makes section B possible, and is also where the pipeline already
  expects a résumé to live.
- **Degrade honestly.** No LLM configured ⇒ the deterministic pass still runs and fills
  contact details; the editor says the rest needs filling in by hand.

## B. Attaching the résumé to the form

Currently a file input is skipped with "attach your résumé yourself". Instead:

1. The background worker fetches the résumé bytes from `GET /resume` (token-gated, local).
2. The content script builds a `File` and assigns it via `DataTransfer`:

```js
const dt = new DataTransfer();
dt.items.add(new File([bytes], 'resume.pdf', { type: 'application/pdf' }));
input.files = dt.files;
input.dispatchEvent(new Event('change', { bubbles: true }));
```

3. For drag-and-drop zones (Greenhouse renders "or drag and drop here"), dispatch a synthetic
   `drop` event carrying the same `DataTransfer`.

### Honesty about limits

Some sites verify `event.isTrusted` or use bespoke upload widgets, and this will not work
there. So the attachment is **best-effort with visible, checkable feedback**: after assigning,
read back `input.files[0].name` and report success only when the file is genuinely attached.
A silent failure here is worse than a skip, because the operator would submit an application
with no résumé — which is exactly how the autonomous pipeline lost a real submission in July.

Never attach to a field that is not a résumé target (a cover-letter dropzone, a portfolio
upload). Reuse v1's existing cover-letter/résumé disambiguation.

## C. Product-grade UI

### Options page: onboarding, not a blank form

First run should be one obvious action, not six fieldsets:

```
  ┌──────────────────────────────────────────────┐
  │  Get started                                 │
  │  Upload your résumé and we'll fill this in.  │
  │            [ Choose file ]                   │
  │  Nothing leaves your machine.                │
  └──────────────────────────────────────────────┘
        ...then the editor, pre-filled, for review
```

- A **profile completeness meter** ("8 of 12 essentials — missing: work authorisation").
  Completeness is what determines how much of a form can be filled, so it is the single most
  useful number to show.
- Work-authorisation stays an explicit Yes/No the operator sets, visually flagged as required
  and never pre-guessed.
- Collapsed sections by default once populated; expand to edit.

### Popup: one primary action

- A single prominent **Fill this page**, with the result summary directly beneath.
- Counts that mean something: *"Filled 12 · 2 drafts to review · 3 need you"*.
- Grouped results — facts, drafts, needs-you — not one flat list.
- Résumé attachment surfaced as its own line with a clear attached/failed state.
- Keep the standing reminder that the operator reviews and submits.

## Out of scope (unchanged)

- Submitting or navigating. Still absolute.
- Inferring canary answers by any means.
- Sending anything off the machine.
