# Design: ApplyPilot Skill Playbook (v1)

**Status:** approved architecture, pending implementation plan
**Owner:** Adwait
**Date:** 2026-05-14
**Decision drivers:** wall-clock per job (A) + reliability (B). User authorized "complete overhaul" if needed.

## Problem

The current apply pipeline ships every form interaction through an LLM round-trip:

```
Python launcher → spawn Claude Code CLI → Playwright MCP → Chrome over CDP
                                  ↑↓ ~30-100 turns/form
                          (each: snapshot → reason → tool call → snapshot)
```

Pain observed in the latest live batches:
- Wall-clock 115s (good case) to 480s (timeout)
- Last 22-attempt batch: 27% pass rate
- Failure modes traced to LLM reasoning on React-select dropdowns, stale snapshots, dual JSON/RESULT emission confusion, runaway CAPTCHA polling
- Subprocess churn: each apply spawns a fresh Claude Code CLI (~3-5s startup) + Chrome cleanup/relaunch race conditions visible as `Task exception was never retrieved` errors

The architecture is over-investing the LLM in the deterministic 80% of every form (name, email, phone, EEO, common screening Q's), and under-defending against its known weak spots (React-select dropdowns, ambiguous instructions).

## Goal

Cut the LLM out of the 80% that's repeatable across runs of the same form. Confine it to the ≤20% that's genuinely novel (free-text custom questions, surprise fields). Measurably:

- **Wall-clock target:** ≤30s for any form we've seen before, comparable to today (~115s) on first apply per company.
- **Reliability target:** ≥80% live apply pass rate (vs current 27-60%).
- **Reliability invariant:** zero LLM-confusion failures on standard Greenhouse/Lever/Ashby fields (we promise these deterministically).

## Architecture: Three Tiers

```
apply_job(url)
  │
  ▼
detect (company, ATS, form_layout_hash)
  │
  ▼
skill = load_skill(company)        # E:\applypilot-data\skills\<company>.yaml
  │
  ├── HIT  ─────────────────────► Tier 1: DETERMINISTIC REPLAY (no LLM)
  │                                Python + Playwright sync API fills the
  │                                form using recorded selectors and values
  │                                pulled from profile.json. Submits.
  │                                Verifier checks success page. ~10-30s.
  │
  │                                If any field on the page is NOT in the
  │                                recipe → fall through to Tier 2.
  │
  │   ┌─────────────────────────► Tier 2: LLM PATCH (scoped, short-lived)
  │   │                            Spawn Claude Code with Playwright CLI
  │   │                            (NOT MCP) and a tightly-scoped prompt:
  │   │                            "These N fields are unresolved. Fill
  │   │                            them, do not submit, then exit."
  │   │                            Returns to Tier 1 to do the submit.
  │   │
  └── MISS ─────────────────────► Tier 3: RECORD MODE (LLM-driven, full)
                                   Spawn Claude Code with Playwright CLI.
                                   Instrument every browser_* call.
                                   On success, write skill YAML so the
                                   NEXT apply to this company is Tier 1.
```

### Why this beats off-the-shelf alternatives

| Option | Why not | Where it shows up here |
|---|---|---|
| Stagehand alone | React-select known buggy (exactly Greenhouse's UI); cache lives on Browserbase servers (vendor lock-in); not Python-first | We borrow the action-caching pattern but implement it locally and use raw Playwright for React-select (already proven in `prefill.py`) |
| Browser Use alone | Still 1 LLM call per action; no real cache; 3/5 form reliability rating; same dropdown issues | Not used |
| Playwright CLI alone | Doesn't address the "LLM in the loop every turn" problem; just makes each turn ~4× cheaper | Adopted for Tier 2 + Tier 3 (replaces MCP) but not the whole story |
| Just expand `prefill.py` | Hand-coding every new company quirk doesn't scale | Tier 1 generalizes the pattern via YAML — same engine, data-driven |

### Language choices (per user authorization, no Python-only constraint)

| Layer | Language | Rationale |
|---|---|---|
| Tier 1 replay engine | Python | Extends existing `prefill.py`; Playwright sync API is mature in Python |
| Skill file format | YAML | Language-agnostic, human-readable, easy to hand-edit when a recording goes wrong |
| Tier 2 / Tier 3 LLM driver | Claude Code CLI subprocess (Node) + **Playwright MCP** for v1 (Playwright CLI is the future migration) | Keep MCP for v1 to avoid conflating Tier 1's correctness with a CLI migration. Migrate to Playwright CLI in a follow-up once Tier 1 is shipped. |
| Gmail MCP | Node (existing) | Stays as-is; verification-code retrieval already works |
| Launcher / orchestration | Python | Existing |

## Components

### 1. Skill file format

`E:\applypilot-data\skills\<company>.yaml`

```yaml
# Auto-generated by Tier 3 record mode. Hand-editable.
version: 1
company: figma
ats: greenhouse
recorded_at: "2026-05-14T20:30:00Z"
recorded_from_url: "https://boards.greenhouse.io/figma/jobs/5973463004"

# Used at replay time to detect form drift. Hash of (sorted required-field
# selectors observed on the page when recording). If the page's current
# hash doesn't match, this skill is stale → fall through to record mode.
form_layout_hash: "sha256:abc123..."

# Optional: a list of canonical-selectors that MUST be present for this
# skill to apply. Cheaper check than the full hash.
required_selectors:
  - "input#first_name"
  - "input#email"
  - "input[type='file']#resume"

# Ordered list of actions. The replay engine runs them sequentially.
actions:
  - kind: fill
    selector: "input#first_name"
    value_source: profile.personal.first_name   # JSONPath into profile.json
  - kind: fill
    selector: "input#last_name"
    value_source: profile.personal.last_name
  - kind: fill
    selector: "input#email"
    value_source: profile.personal.email
  - kind: select_react
    # For react-select: click toggle, type query, press enter.
    toggle_selector: "div[class*='select__control']:has-text('Phone')"
    type_value: "+1"
    expected_option_text: "+1 (United States)"
  - kind: upload
    selector: "input[type='file']#resume"
    value_source: file:E:\applypilot-data\resume.pdf
  - kind: fill_textarea
    selector: "textarea[id*='why_join']"
    value_source: profile.responses.why_figma   # specific to company
  # ... etc

# Fields the recorder couldn't resolve to a profile value. These will fall
# through to Tier 2 LLM patch on each replay.
unresolved_fields:
  - selector: "textarea#question_8501"
    label: "What's a product you've shipped that you're proud of?"
    type: long_text

# Verification expectations — used by the verifier to confirm submission.
success_signals:
  url_pattern: "/jobs/.*/apply"
  page_text_contains_any:
    - "thank you for your interest"
    - "we'll be in touch"
```

### 2. Tier 1 — replay engine (Python)

New file: `src/applypilot/apply/replay.py`

Public entry point:
```python
def replay_skill(
    skill: dict,
    page,  # playwright.sync_api.Page already loaded at the apply URL
    profile: dict,
    *,
    dry_run: bool,
) -> ReplayResult:
    """Execute the skill's actions sequentially. Returns ReplayResult with:
       - status: 'submitted' | 'needs_patch' | 'drift_detected' | 'failed'
       - unresolved: list of fields needing Tier 2 LLM patch
       - duration_ms
       - error (if failed)
    """
```

Action kinds supported in v1:
- `fill` — text input via Playwright `locator.fill()`
- `fill_textarea` — same but for textarea
- `select_native` — `<select>` element via `selectOption()`
- `select_react` — React-select pattern (toggle → search → enter)
- `select_combobox` — Greenhouse-specific combobox (already exists in `prefill.py`)
- `upload` — file input via `set_input_files()`
- `click` — generic click
- `check` / `uncheck` — checkboxes
- `wait_for` — wait for selector visible (used after submit before verifier)

Drift detection: before running actions, compare `current_form_layout_hash()` to `skill.form_layout_hash`. On mismatch → return `status='drift_detected'`, launcher routes to Tier 3.

### 3. Tier 2 — LLM patch (Claude Code via Playwright CLI)

Spawned only when Tier 1 returns `needs_patch` with a list of unresolved fields.

New prompt: `src/applypilot/apply/prompt_patch.py`

Tightly scoped:
```
You have N unresolved fields on a partially-completed form.
The page is already loaded at <url>. Other fields are filled.

For each unresolved field, fill it using Playwright CLI:
  - {selector: "textarea#question_8501", label: "..."} → answer text
  - {selector: "...", label: "..."} → ...

DO NOT click Submit. DO NOT touch any other field. When all N are filled,
exit with RESULT:PATCHED.
```

For v1, Tier 2 also uses Playwright MCP (not CLI) — same reasoning as Tier 3. Migration to Playwright CLI is a separate v1.1 task.

Tier 2 returns control to Tier 1 to handle submit + verification.

### 4. Tier 3 — record mode (Claude Code + stream-json recorder)

Same Claude Code + Playwright invocation as Tier 2 but with the full apply prompt. The "recorder" is **not a separate process** — it's a new function `record_actions_from_stream(stream_json_lines)` in the existing stream-json parser at `launcher.py:945-1027`. That parser already iterates over Claude Code's stdout and dispatches on `tool_use` blocks. We add a sidecar dict that captures, for each successful `mcp__playwright__browser_*` tool_use:
- `name` (e.g., `browser_click`, `browser_fill_form`)
- `input` (selector, value, ref)
- a "value source" inferred from the input value (does it match `profile.personal.email`? then store as `value_source: profile.personal.email`. Else: store the literal value and tag it as `unresolved_fields` for Tier 2.)

On `RESULT:APPLIED` (and verified success), the launcher writes the captured trace to `skills/<company>.yaml`. On failure, the partial recording is discarded.

This means Tier 3 requires no new IPC or process supervision — it's a side effect of the existing stdout-stream parsing.

**Playwright transport for v1:** keep the existing **Playwright MCP** (via Claude Code's `--mcp-config`) — not Playwright CLI — for Tier 2 and Tier 3. CLI is the future migration once Tier 1 replay is shipped and stable; mixing both at once would conflate the cause of any regression. Tier 1 uses Python Playwright sync API directly (no MCP, no CLI subprocess — it's all in-process).

### 5. Verifier (unchanged)

The existing `_verify_submission_success` in launcher.py stays. Skill-specific overrides come from the YAML's `success_signals` block.

### 6. Database changes

Add to `jobs` table:
- `skill_used` (TEXT, NULL = Tier 3 record mode, otherwise the skill filename)
- `replay_duration_ms` (INTEGER, NULL = no Tier 1 ran)
- `patch_duration_ms` (INTEGER, NULL = no Tier 2 patch)

(Backward-compatible additive migration.)

## Data flow

```
1. acquire_job() returns (url, company, ats)
2. launcher: skill = load_skill(company)
3. if skill is None:
       Tier 3 RECORD: spawn Claude Code, instrument, save skill on success
   else:
       Tier 1 REPLAY:
         playwright.goto(url)
         result = replay_skill(skill, page, profile)
         if result.status == 'drift_detected':
             archive stale skill, route to Tier 3
         elif result.status == 'needs_patch':
             Tier 2 PATCH: spawn Claude Code scoped to result.unresolved fields
             then: playwright.click(skill.submit_selector)
             verify
         elif result.status == 'submitted':
             verify
         elif result.status == 'failed':
             mark needs_review with error
4. write review.jsonl row including which tier ran
```

## Error handling

| Failure | Tier 1 response | Escalation |
|---|---|---|
| Selector not found | Try fallback selectors in YAML; else mark field unresolved | If submit-button missing, return `failed:drift_detected` |
| Wrong value type (e.g., select expects "Yes"/"No" but value is bool) | Coerce; on failure mark unresolved | Tier 2 patch |
| Resume upload mid-form re-renders form | Wait for stable DOM, retry once | Tier 2 if still failing |
| reCAPTCHA visible | Skip in Tier 1; verifier handles | (Unchanged) |
| Email verification step on submit | Skip in Tier 1; Tier 2 picks up using Gmail MCP (existing) | (Unchanged) |

## Validation criteria

Implementation is done when:

1. A Tier 3 record run on a fresh Figma Greenhouse job produces a `skills/figma.yaml` file and the apply reaches `applied` status.
2. A subsequent Tier 1 replay on a different Figma Greenhouse job hits the same form layout, runs in ≤30s, hits `applied` or `needs_review:unverified_submission`.
3. If the recorded form has a custom free-text question, Tier 2 patch runs successfully, fills only the unresolved field, returns control, and Tier 1 submits + verifies.
4. Drift simulation (manually edit `form_layout_hash` to a wrong value in a skill) routes to Tier 3 cleanly without crashing.
5. End-to-end: a 9-job batch across 5+ distinct companies reaches ≥80% `applied`.

## Out of scope (v1)

- Stagehand-style auto-healing: if a skill drifts, we re-record from scratch; we don't try to patch the recipe mid-flight.
- Per-applicant skill variants: skills are tied to (company, form layout), not (company, applicant).
- Multi-user / multi-tenant skill sharing.
- Cover letter tailoring (still a separate concern outside the form-fill loop).
- A UI for hand-editing skills (use a text editor for v1; build UI later if needed).
- Speculative parallel pre-fetch of skills before discovering eligible jobs.

## Risks / open questions

1. **Tier 3 first-apply still slow** — same as today. Mitigation: this is a one-time per-company cost, then every subsequent apply is fast.
2. **Greenhouse form drift detection accuracy** — `form_layout_hash` over required selectors may be too strict (false positives on cosmetic page changes) or too lax (misses meaningful changes). Need empirical tuning. Start with required-selectors hash, layer in DOM-fingerprint if needed.
3. **Profile data → form value mapping** — JSONPath into `profile.json` works for most fields, but EEO and screening questions sometimes need a `responses` sub-tree per company. Need a profile schema extension. Manageable.
4. **What if the recorder captures a bad sequence** (e.g., LLM clicked the wrong dropdown option then corrected)? The recording needs deduplication / "final state wins" logic. Defer to implementation, but flag it.
5. **Playwright CLI maturity** — released Feb 2026, only ~3 months old. We should treat it as "promising but maybe rough", and keep Playwright MCP as a Tier 2/Tier 3 fallback that we can flag-flip.

## Timeline / sizing

| Phase | Scope | Estimated effort |
|---|---|---|
| 0 | Design (this doc) + plan | done |
| 1 | Skill file format + Tier 1 replay engine (no LLM) | 2-3 days |
| 2 | Tier 3 recorder (capture trace from Claude Code stdout) | 2 days |
| 3 | Tier 2 patch flow + new prompt | 1 day |
| 4 | Launcher integration + DB schema | 1 day |
| 5 | Drift detection + invalidation | 1 day |
| 6 | First end-to-end Figma record → replay → patch loop | 1-2 days |
| 7 | Migrate the remaining live Greenhouse companies via record runs | 1-2 days |

Total: ~2 weeks of focused work. Each phase ships independently — Phase 1 + 4 alone already give us deterministic Greenhouse-field replay, even before record mode lands.

## Migration / rollout

- v1 ships behind a feature flag (`APPLYPILOT_USE_SKILLS=1`). When off, the launcher uses the existing prompt-driven flow.
- First production use: record Figma + Stripe (already mostly working today) → validate Tier 1 replay → expand.
- Keep `prefill.py` as Tier 1's bootstrap library; rename or fold its standard-Greenhouse logic into the skill schema.
- The current `prompt.py` becomes the Tier 3 record-mode prompt. The Tier 2 prompt is new (~50 lines).

## Open questions for the user before writing the implementation plan

1. **Skill granularity:** per-company is the v1 default. If a company has multiple form variants, we'll either record multiple skills with `form_layout_hash` discriminators, or split into (company, role-family). Which feels right?
2. **Tier 2 frequency:** if a recording covers 80% of fields, ~80% of replays will need Tier 2. That's still better than today (Tier 2 is short, scoped, and uses cheap CLI), but it means Tier 2 has to be rock-solid. Should we tune the recording to be exhaustive (record + ask the LLM to also try common future variants) at the cost of slower first-apply, or keep it lean and accept Tier 2 will often run?
3. **Tier 2 model:** should it use the same model as Tier 3 (Haiku for cost) or step up to Sonnet (better at one-off free-text)?
