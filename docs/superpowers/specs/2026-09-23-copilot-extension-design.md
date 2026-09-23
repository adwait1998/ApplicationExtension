# ApplyPilot Copilot — Chrome Extension Design

**Status:** design, 2026-09-23
**Goal:** the operator opens a job application in their own browser and the extension fills it
from their ApplyPilot profile — like Simplify or Jobright, but running entirely locally.

## Why this is different from the pipeline

The existing pipeline drives a headless browser and submits autonomously. That is the
high-risk path, and it is why the safety kernel exists.

This extension is the *human-in-the-loop* path: the operator has already opened the page and
is watching. That single fact removes most of the risk — so the extension's defining rule is:

> **The extension NEVER clicks submit, and never navigates.** It fills fields and highlights
> what it did. The human reviews and submits.

Everything else follows from that. There is no ledger, no submit broker, no CDP containment,
because nothing is ever submitted programmatically.

## Architecture

```
Chrome extension (MV3, unpacked/private)
  content script   scan DOM -> FieldDescriptor[]; apply FillPlan; highlight
  popup            review panel: what was filled, what was skipped and why
  background       holds the service token; talks to the local service
        |  HTTP, 127.0.0.1 only
        v
Local service  (FastAPI)   `applypilot serve-extension`
  GET  /health            liveness + which decision tiers are available
  POST /resolve           FieldDescriptor[] -> FillPlan
        |
        v
  Resolution ladder (see below) -> reuses applypilot.apply.canary and
  applypilot.config.load_profile() — the SAME profile.json and the SAME
  canary rules the pipeline uses.
        |
        v
  Laya Router (OPTIONAL, lazy) — non-autoregressive typed-decision engine
```

Laya is not a text generator. It answers `choice` questions with a calibrated confidence in
one forward pass, which is exactly the shape of "which profile field does this input want?".
It is used **only** for semantic field classification, never to invent an answer.

### Measured reality of Laya on this machine (probe, 2026-09-23)

`pip install laya` works and is legitimate (PyPI matches the repo; publisher Convai
Innovations). Resolved `laya==0.3.6`, `torch==2.14.0+cpu`, `transformers==5.17.0`.
**This laptop has no GPU**, so everything below is CPU numbers.

Three findings shape the design, and none of them was obvious from the README:

1. **Laya does not batch across fields.** Its single-pass batching covers *many questions
   about one state*, not *one question about many states*. A form has N fields = N separate
   states, so it costs N sequential calls. Measured: ~370ms warm per call, so a 20-field
   form would block ~7 seconds.
   **Consequence:** Laya runs ONLY on fields tiers 0–2 could not resolve. On a typical ATS
   form the deterministic tier handles most standard inputs via `autocomplete`, leaving a
   handful of ambiguous ones — a few hundred ms to ~2s, not 7. The extension also fills the
   deterministic results immediately and applies Laya's asynchronously, so the UI never
   blocks on it.

2. **Confidence is only calibrated up to ~10 options.** The library itself warns that
   choice questions with 11+ options ship out-of-range temperatures and that confidence
   should then be treated as uncalibrated. Our profile has ~35 leaf keys, which would blow
   straight past that — and the confidence gate is the entire safety mechanism for this tier.
   **Consequence:** candidates are pre-ranked by the deterministic matcher and the top
   **≤9 plus `none`** are offered to Laya. One call per field, always inside the calibrated
   range.

3. **`Router(preload=True)` silently downloads all three checkpoints (2.26 GB)** when a
   US-English job-application use case needs only the English one (807 MB), and peaks at
   4.4 GB RSS with all three resident.
   **Consequence:** load the English checkpoint explicitly. Never call bare `preload=True`.

Quality spot-check: 5/6 realistic field labels classified correctly. The one miss
("First Name" → `none`) carried the lowest confidence of the set (0.461 vs 0.83–1.00 for
the correct ones), so the gate would have caught it. Threshold is set at **0.75**.

That miss is also reassuring about the tier ordering: "First Name" is precisely the kind of
field the deterministic tier nails via `autocomplete="given-name"`, so Laya is never asked.
Laya's weakness sits where the deterministic tier is strongest.

## Resolution ladder

Applied per field, first match wins:

| Tier | Rule | Auto-fills? |
|---|---|---|
| 0 | **Secret guard.** `personal.password` and anything on the secret denylist is never emitted, under any circumstance. | never |
| 1 | **Canary.** `canary.is_canary(label)` → `canary.resolve_canary(label, profile)`. Work auth, sponsorship, citizenship, salary, EEO, address, DOB, clearance, export control. | only on an exact profile hit; otherwise REFUSED |
| 2 | **Deterministic.** `autocomplete` attribute first (it is a browser standard and is the highest-precision signal available), then name/id/label patterns → profile path. | yes |
| 3 | **Laya.** `choice` over the profile-key catalogue, gated on confidence ≥ threshold. | yes, above threshold |
| 4 | **Unresolved.** | no — left for the human |

**Canaries are never answered by Laya.** This mirrors the pipeline's invariant 7: an
unresolvable canary stays unresolved rather than being guessed. Getting "do you require
sponsorship?" wrong is a real-world harm, not a UX annoyance.

EEO fields follow the pipeline's existing policy: answer from `eeo_voluntary.*` if present,
otherwise decline to self-identify — never guess.

## FieldDescriptor / FillPlan

```jsonc
// request: POST /resolve
{ "url": "https://boards.greenhouse.io/acme/jobs/123",
  "fields": [
    { "id": "f0",                      // opaque, assigned by the content script
      "selector": "#first_name",       // resolved back to the element by the content script
      "tag": "input", "type": "text",
      "name": "first_name", "autocomplete": "given-name",
      "label": "First Name", "placeholder": "", "required": true,
      "options": [] }                  // for select/radio: the visible option texts
  ] }

// response
{ "fills": [
    { "id": "f0", "value": "Nida", "source": "deterministic",
      "profile_key": "personal.full_name", "confidence": 1.0,
      "auto_fill": true, "reason": "autocomplete=given-name" }
  ],
  "skipped": [
    { "id": "f7", "source": "canary", "auto_fill": false,
      "reason": "canary:salary not present in profile — answer this yourself" }
  ],
  "tiers_available": ["canary", "deterministic", "laya"] }
```

The service returns values **only for fields the page actually contains**. The whole profile
is never sent to the page.

## Security

- The service binds `127.0.0.1` only and refuses any other host.
- A token is generated on first run and printed by the CLI; the operator pastes it into the
  extension's options page once. Every request must carry it.
- CORS is restricted to the extension origin.
- The secret denylist is enforced server-side, so a compromised or buggy content script
  still cannot extract a password.
- The extension requests the narrowest permissions that work: `activeTab`, `storage`, and
  host permission for the local service. No broad `<all_urls>` content-script injection at
  install time — the user invokes it per page.

## Degradation

Laya is **optional**. If it is absent, fails to import, or the probe finds it unusable, the
service reports `tiers_available` without `laya` and the ladder runs tiers 0–2 only. The
extension is fully functional on the deterministic tier for standard fields (name, email,
phone, address, LinkedIn, résumé upload); Laya only widens coverage on the ambiguous tail.

This is deliberate: the feature ships regardless of whether a days-old dependency works out.

## Out of scope (v1)

- Submitting, or clicking anything that navigates.
- Generating free-text answers ("why do you want to work here?"). Laya cannot generate, and
  a fabricated answer under the operator's name is exactly the failure this project avoids.
- Résumé file upload automation (the file picker is OS-level; v1 surfaces the résumé path
  for the human to attach).
- Writing back to the pipeline's database. The extension is read-only with respect to
  ApplyPilot state.
