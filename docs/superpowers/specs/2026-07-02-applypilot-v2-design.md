# ApplyPilot v2 — One Spine, Two Engines

**Status:** Approved design (user-reviewed section by section, 2026-07-01/02)
**Supersedes:** the 2026-05-15 Reliability-v2 spec and the 2026-05-22 autonomous-loop spec as the forward architecture. Those documents remain the record of v1 learnings.
**Design provenance:** two multi-agent design tournaments (3 apply-engine architectures × 3 judges × adversarial red team; 3 funnel designs × combined stress test), grounded in all 398 rows of live telemetry and a full read of the v1 codebase (~24.6k LOC).

---

## 1. Why v2 (the evidence)

Live telemetry (334 live attempts) indicts the current architecture, not its tuning:

| Fact | Number |
|---|---|
| Live verified pass rate | 38% (128/334) |
| Machine time yielding no application | 59% (~14.3h of 24.2h, excl. one 11.8h hung worker) |
| Effective wall-clock per success | ~11.5 min (median successful apply: 240s) |
| Zero-LLM Greenhouse adapter path | 131s median, $0, 65% pass — best numbers in the system |
| LLM-agent path | 240–315s, 37 turns median, 25–46% pass |
| Failures classified removable (class A) | 69% |
| Tier-1 "skill replay" successes in 334 rows | **0** (recorded-from-agent-transcript skills are not replay-grade) |
| Queue starvation | autonomous loop stalled at iteration 4: zero fresh score≥8 jobs in 24h |
| Wasted applies from broken filters | 14/100 location-ineligible; 55 non-US jobs manually parked |
| Safety incidents | Reddit double-submit (retry-on-success bug); Twilio dry-run submitted live (prompt-level guard failed) |

Conclusions the design is built on:

1. **The AI-drives-the-browser architecture is the bottleneck.** The deterministic path is 2× faster, free, and more reliable; the agent grinding against known blockers until the clock dies is the dominant failure mass.
2. **The funnel is as broken as the apply engine.** The best apply engine cannot be validated, let alone deliver value, on a starved or polluted queue.
3. **Prompt-level guarantees are not controls.** Both real-world incidents trace to trusting the model layer with safety.

## 2. Locked product decisions

- **Delivery model:** self-hosted, local-first. User's machine, user's browser, user's data.
- **Pluggable AI operator:** user brings any provider API key (Anthropic / OpenAI / Google / local). One Operator interface; engine code never knows the provider. A quarantined Claude-CLI shim preserves the no-API-key subscription path.
- **Sequencing:** engine quality before packaging. Target user is ultimately non-technical job seekers; v1 build is for the engine.
- **Scope:** the whole pipeline — discovery and matching are in scope as first-class systems, not just apply.
- **Language:** Python. The leverage is the 24.6k LOC of battle-tested widget/verification knowledge being promoted, not a runtime swap; hot paths are I/O-bound.

## 3. Targets and honest framing

User-selected bar: **90%+ verified pass / ~1 min per apply**. Red-team-corrected framing (accepted):

- **90% is a per-ATS ratchet, not a launch property.** Greenhouse reaches it first (~7 weeks plausible); Ashby/Lever follow a measured shadow-mode burn-in with an explicit go/no-go. The fastest live apply ever recorded is 61s, so:
- **Speed targets: 32–45s median warm Greenhouse; <90s multi-step (Ashby/Workday-class).** Per-phase p50/p90 measured in shadow mode before any public claim. The ≤60s median is a Greenhouse-warm target with a cloud-tier oracle; local-model oracles are a supported degraded mode with their own SLO.
- **Denominators are pinned in §12 now**, with gate-audit sampling so the pass rate cannot be manufactured by over-blocking.

## 4. Architecture overview

Two engines on one spine, with the AI operator plugged in at exactly three bounded seams.

```
                        ┌─────────────── ONE SPINE ────────────────┐
                        │ identity_id · gate engine · queue policy │
                        │ spend ledger · event log · SQLite        │
                        └──────┬───────────────────────┬───────────┘
   FUNNEL ENGINE               │                       │            APPLY ENGINE
┌──────────────────────────────▼──┐               ┌────▼──────────────────────────────┐
│ Board Atlas (freshness rings)   │               │ Session layer (persistent Chrome, │
│ → incremental poll              │    apply      │  watchdog, recycling)             │
│ → gate engine at ingest         │    queue      │ → Pre-flight probe (~2s)          │
│ → compile-then-score            │──────────────►│ → Front-end parses form → IR      │
│ → trust layer (earned autopilot)│               │ → Resolver ($0) → Oracle (bounded)│
└─────────────────────────────────┘               │ → Executor → Verify (network-1st) │
                                                  └───────────────┬───────────────────┘
        AI OPERATOR (BYO key)                                     ▼
┌──────────────────────────────────┐              ┌───────────────────────────────────┐
│ 1. score sub-judgments (JSON)    │              │ SAFETY KERNEL (below everything)  │
│ 2. answer novel questions (JSON) │              │ submit broker + CDP net-block ·   │
│ 3. label unparsed controls (JSON)│              │ two-phase dedupe ledger · canary  │
│    — never drives the browser    │              │ fields · budget caps · kill switch│
└──────────────────────────────────┘              └───────────────────────────────────┘
```

Three governing ideas:

1. **Determinism is the product; the AI is a consultant.** Every task the AI used to do is restated as a question with a typed, schema-validated answer. Code composes the answers. This is simultaneously the speed story (no multi-turn sessions), the reliability story (no stochastic actor in the control loop), and the pluggability story (any model that returns valid JSON qualifies).
2. **One spine.** One `identity_id` per underlying job from discovery through receipt. One gate engine issuing eligibility verdicts with reason codes, consumed read-only by every other layer. One queue-policy function deciding "may this apply happen" (gates ∧ score ∧ mode ∧ budget ∧ not-paused), default-deny on any read failure. One spend ledger with one enforcement point.
3. **Failures are named, counted, replayable.** Every attempt produces a flight-recorder bundle; any failure promotes to a CI fixture with one command; CI fails on unnamed terminal states. 90% is a ratchet because every regression becomes a permanent test.

## 5. The spine

### 5.1 Job identity

- `identity_id` = canonical per-ATS key (`greenhouse:token:12345`) extracted from the URL, plus normalized (company, title, location) exact-match folding for v1. SimHash cross-source folding is deferred until aggregator mining returns as a source.
- `url` remains the raw-row primary key; alias rows are never queue-visible.
- **All money/reputation-bearing records key off `identity_id`:** decisions, receipts, submission ledger, already-applied block, company cooldown.
- Repost semantics: same identity + new job id on the same board inherits history; if the user already applied to the identity, re-apply is hard-blocked. Concurrent distinct job ids on one board are never folded (3 identical open seats = 3 legitimate applies).
- Company cooldown: default 2 applies/company/30 days, enforced at gate time.

### 5.2 Gate engine (single authority for eligibility)

One module, run at ingest on every new job, before scoring. Verdict schema: `PASS / REJECT(code, evidence_snippet) / UNKNOWN(flag)` per rule; results persisted with `gate_version`. **UNKNOWN never auto-passes.** Jobs are always stored (audit + retroactive re-gating); only `eligible AND automatable` rows become queue-visible.

Rules (deterministic first, LLM micro-check only for UNKNOWNs, batched, cached by description hash):

1. **Location/geo:** structured parser + bundled gazetteer (countries, US states with word-boundary abbreviation matching, top metros), remote-marker and remote-scope detection, plus description-level carve-out regexes ("not available to CA residents", "must overlap PST 4 hours", ITAR/US-person). Substring matching is banned by construction (a lint test asserts every gate pattern is word-boundary anchored). Output is structured geo vs the profile's geo policy.
2. **Seniority/track:** level lexicon (intern → chief) with IC-vs-management disambiguation ("Lead" is IC; "Manager, Design Systems" is not), matched against the profile's accepted bands. Generalizes v1's three rounds of hand-tuned exclude_titles into compiled per-profile data.
3. **Sponsorship (for sponsorship-needing profiles):** hard-marker regex battery ("without sponsorship", "US citizenship required", clearance) → hard-ineligible with the quoted sentence. Absence of markers ≠ safe: `sponsorship_unknown` routes to the review queue, never auto-apply. A company-level prior from public H-1B LCA disclosure data (offline, quarterly-refreshed) informs ranking and the review card.
4. **Automatability from URL alone:** manual-ATS patterns; **Workday tenant without a registered account → `account_required` at $0** (vs 216–432s live discovery in v1); supported-adapter allowlist.
5. **Employment type / freshness / already-applied / cooldown.**

`gate --rerun` re-evaluates all rows with stale `gate_version`, including previously parked ones — a bad rule is repairable retroactively instead of silently costing weeks of queue. Every rejection carries a machine-readable reason code; the gate-rejection breakdown is a first-class UI view.

### 5.3 Queue policy (single authority for "may this apply happen")

One function: `eligible AND automatable AND fresh AND score ≥ threshold AND (category graduated OR row individually approved) AND budget_ok AND NOT paused`. Default-deny on any policy-table read failure. Nothing else in the codebase may decide to submit.

### 5.4 Spend ledger

Every LLM call and every apply writes `(ts, stage, identity_id?, provider, model, tokens, cost_usd)`. One enforcement point in the engine loop: over-cap → visible `paused:budget` state with a resume path. Any automatic funnel-narrowing under budget pressure is surfaced in the brief, never silent.

### 5.5 Storage

SQLite remains, extended additively via the existing `ensure_columns` pattern: new tables `boards`, `source_runs`, `job_identities`, `decisions`, `trust_state`, `spend_ledger`, `receipts`, `submission_ledger`, `filter_rules`, `engine_control`, `mapping_cache`, `job_embeddings`; new jobs columns for gate verdicts, category, scorecard, embeddings ref. Config resolution moves from import-time module constants to an injected context object (removes the 12 import-time path constants and enables future multi-profile support without a rewrite).

## 6. The apply engine — "Form Compiler"

Pipeline per job: **Navigate → Probe → Parse → Resolve → Fill → Advance (loop per step) → Submit → Verify → Record**, on one persistent browser session and one shared data structure (the FormSchema IR).

### 6.1 Session layer

- One Chrome per worker, launched once per batch, one long-lived Playwright-over-CDP connection. Kills v1's per-job relaunch (3s sleep + port sweep) and ~8 per-job reconnect cycles.
- **Watchdog:** per-phase deadlines (probe ≤5s, parse ≤5s, fill ≤30s/step, hard per-job cap), Chrome recycling on N-jobs/RSS/zombie-tab thresholds and on CDP disconnect after one failed reconnect. Structural fix for the 11.8h hung worker.
- **Worker profiles are built clean** — never cloned from the user's real Chrome (v1's clone caused the stale-identity-cookie bug). Deliberate cookies only; per-tenant Workday sessions persist in the worker profile.
- Windows reality: a multi-day soak test on the actual Windows box is a Phase-3 exit criterion; batch mode documents power settings and pins Chrome update timing.
- Speculative preload: while job N fills, job N+1's URL loads in a spare page of the same browser.

### 6.2 Pre-flight probe (~2s, on DOMContentLoaded)

Classifies before any fill work: `{ats_kind, login_wall, captcha_present, sso_gate, job_expired, form_frame_path}`. Converts v1's 216–432s discoveries into <5s named terminal states. Additionally, PreGate runs at **enqueue** time (during discovery/scoring) so most blocked jobs never reach the browser.

### 6.3 Front-ends → FormSchema IR

One pure DOM→IR parser per ATS dialect: Greenhouse, Ashby, Lever; Workday behind account gating; Generic fallback built on the existing `_OBSERVE_JS` scanner (which already extracts label/role/type/required/value/frame for every control including shadow DOM).

```
FormSchema { ats, company, url, steps: [Step], template_fp, questions_fp }
Step  { index, fields: [Field], advance_control, terminal: bool }
Field { field_id, frame_path, label_text, question_text,
        semantic_key | None,          # canonical taxonomy: first_name, email, phone,
                                      # location, resume, work_auth, sponsorship,
                                      # eeo.*, salary, years_exp, custom.*
        widget: { kind, framework_hints },   # text | textarea | native_select |
                                             # react_select | radio_group | file |
                                             # date | phone_intl | typeahead_location |
                                             # oj_* | segmented_button ...
        options: [str] | lazy,        # enumerated LAZILY at fill time, never at parse
        required, char_limit, depends_on, locator_spec }
```

- Semantic keys are assigned deterministically from label-synonym tables (promotion of the Greenhouse adapter's `_standard_plan`).
- **Fingerprints are two-level:** `template_fp` (step structure + standard fields — shared across companies on the same ATS template) and `questions_fp` (the custom-question delta). Field-level `field_fp` (label + widget + options-shape) is the primary cache key so per-job custom questions don't cold-start the whole form.
- Iframes are first-class: every field carries a frame path; vanity-domain embeds and nested Workday frames use the same code path.
- Multi-step flows parse lazily (step N+1 is parsed after step N advances); the discovered step graph is cached per fingerprint.
- **Option-probe discipline:** enumerated options are read lazily at fill/oracle-request time (open, read, Escape where unavoidable, with per-ATS suppression rules) — never during parse. Typeahead/async widgets are excluded from the option-index contract and use dedicated type-and-verify drivers.

### 6.4 Resolver (deterministic, zero I/O)

Ladder per field: (a) `semantic_key` → profile path; (b) option matching via real-option matching with polarity guards (never fuzzy-match "Yes" onto "No, I do not…"); (c) answer-bank hit (hardened — see §10.3); (d) **mapping-cache hit** per `field_fp`: stores `{semantic_key | answer-binding, winning locator tier, widget driver}` — **bindings like `profile.phone` or `answer:<question_fp>`, never literal values** (privacy-safe, survives profile edits); (e) unresolved → batched to the oracle.

A policy table encodes safe canonical answers (EEO decline chains, veteran/disability defaults) and hard refusals (never invent salary beyond the profile band; never answer legal attestations the profile doesn't cover — park instead).

**Cache policy is demote-never-archive:** 2 verified failures demote a mapping for re-resolution; versions are kept and diffed. (v1's record→drift→archive cycle killed its skill system; this is the structural fix.)

### 6.5 Oracle call (the only AI in the apply loop)

- All unresolved fields for the current step go out in **one** `FieldResolutionRequest`; the response is JSON-schema-validated with one retry.
- **Enumerated answers must be an index into the options list provided in the request.** Free-typed option values are rejected at the schema layer — the react-select-desync bug class becomes a type error. At commit time the index resolves to text and is committed with read-back against freshly-read options (index is validation, not the commit key).
- Text answers are length-clamped; `cannot_answer` on a required field parks the job (park-don't-guess).
- Accepted answers write back to the answer bank and mapping cache, so per-company oracle usage decays toward zero.
- **Canary fields never reach the oracle** (§10.3).

### 6.6 Executor

- Walks the FillPlan through a WidgetDriver registry (text, react-select keyboard-commit, native select, radio, checkbox, file upload with filename-chip wait, date, phone-intl, location typeahead, Workday oj-* family).
- **Every driver returns a read-back-verified CommitResult; click success is never trusted.** (Generalizes v1's hardest-won lesson.)
- Zero fixed sleeps: waits are mutation-settle (MutationObserver quiet-window), locator-state expectations, and CDP network-idle on step transitions. v1's ~36 fixed sleeps are deleted with the code that contained them.
- Locator resolution seeds from `Field.locator_spec` through the existing 10-tier healing ladder; the winning tier writes back to the mapping cache.
- Multi-step: after a step's plan commits and the required-completeness sweep passes, click `advance_control` (through the submit broker — §10.2), wait on URL-change/mutation-settle/network, re-parse next step, loop.
- Resume upload runs first in fill order (Greenhouse's server-side resume parse re-renders the form; filling before it settles caused v1's stale-snapshot bug).
- Workday: same loop + a session vault holding per-tenant credentials with a deterministic login sub-flow; unregistered tenants never get here (gated at $0).

### 6.7 Verification

- **Tier 1 — network evidence:** the application POST observed via CDP (language-independent; kills the English-phrase-allowlist false-negative class). Endpoint signatures auto-harvest from the HAR of every confirmed success — never hand-maintained config.
- **Tier 2 — DOM signals:** confirmation text/URL-change/submit-gone composite (v1's verifier, retained as fallback).
- A **verify-tier-share histogram** ships in the first cut as a blocking weekly metric: tier-1 share dropping = alert; any confirmed submission tier-1 failed to match auto-files a fixture.
- Ambiguity fails to `needs_review` with the flight-recorder evidence one click away.

### 6.8 Degraded tier and legacy fallback

- **Degraded parse tier:** when a front-end can't parse a form (DOM churn), GenericFrontend + one batched `label_controls` oracle call produces a best-effort IR — explicitly counted, capped per run.
- **Legacy agent tier:** the v1 Claude-CLI agent path survives through burn-in as a quarantined, counted fallback (insurance against correlated ATS-wide DOM churn zeroing out a dialect overnight). Retired per-ATS once v2 ≥ v1 on 100+ live rows.
- Nightly canary-parse of ~20 live forms per ATS alarms on churn before the queue feels it; fixture-promotion turnaround for parse gaps is same-day by policy.

### 6.9 Speed budget (measured, not promised)

Warm Greenhouse target 32–45s: navigate (preloaded) ~1s · probe 2s · parse ~1s · resolve ~0ms · fill 15–25s (jittered pacing spends some headroom looking human) · submit+verify 5–8s. Cold adds one oracle round-trip (2–6s cloud-tier). MHTML capture happens on failure/sampled runs only — never on the critical path. Per-phase p50/p90 land in the flight recorder from day one.

## 7. The funnel engine

### 7.1 Board Atlas (discovery at scale)

- Greenhouse/Lever/Ashby boards are enumerable: one token = one cheap public JSON GET. A `boards` table replaces `ats_companies.yaml` (one-time import).
- **Token sources:** one-time Common Crawl URL-index mining batch (offline, zero ATS load, thousands of candidates; re-run quarterly) → validation at ~4 rps/host; jobspy demoted from apply-source to registry miner (harvests direct-ATS URLs from aggregator listings, weekly, cheap); existing DB/search mining.
- Registry membership is **profile-agnostic** (a board with no design jobs today may post one tomorrow; the next user is a data engineer). What a board posts (departments, cadence, locations) is a scheduling input, not a membership criterion.
- The validated Atlas ships as a **bundled snapshot** with per-track top-N burst lists (new-user first discovery in ~1–2 min) and a cached postings sample (calibration deck renders instantly, offline). Community publishing/contribution infra is deferred (§14).

### 7.2 Freshness scheduler

- Rings: hot (recently posted relevant jobs) polled every 30–60 min jittered; warm every 6–12h; cold via periodic snapshot refreshes, **not** client polling (a thousand clients must not re-poll 40k boards each).
- Each client polls only its profile-relevant rings-0/1 subset (~3–6k boards, ~10–17k req/day, well inside per-host politeness budgets: token buckets, backoff, honest bot UA).
- **Incremental:** job-id-set hash diff per poll; unchanged board = one ~2KB request; only new ids fetch content.
- **No daemon in v1:** an idempotent catch-up tick on app-open/wake (discover→gate→score→queue in <10 min). The honest UX is "since you last looked." Always-on is an advanced option.
- Per-source accounting (`source_runs`): requests, yield, cost joined down-funnel; zero-yield sources demote automatically.
- A vendor-behavior canary (429/auth-wall rate per host) runs from day one — ATS policy change toward these endpoints is the acknowledged existential discovery risk.

### 7.3 Compile-then-score matching

- **MatchProfile compilation (once, at onboarding):** resume → structured facts via the user's model (with source spans, shown as editable chips); a fixed 8-question form with structured pickers for hard gates (location is an enum picker — free-text location entry is banned product-wide); LLM compiles both into MatchProfile + per-track gate config; **the user confirms a rendered summary of what will be auto-rejected.** Compiled artifacts are versioned data, unit-tested against a shipped 300-title fixture per track.
- **Staged scoring:** Stage 0 gates (§5.2, ~60% killed free) → Stage 1 local-embedding prefilter over title + extracted requirements sections (not the boilerplate intro), adaptive top-P keep with a starvation floor; keep-rate derives from the user's budget cap and measured per-job model cost → Stage 2 deep score on the user's key.
- **The model never emits the final 1–10.** It returns rubric-anchored sub-judgments (role_track 0–3, seniority_fit 0–3, skills found/missing, constraint_risk, domain_appeal, red_flags), each with a **verbatim JD quote verified by code as a substring of the JD after NFKC/quote/dash normalization**. Failed evidence → one re-ask → render "quote unverified", never silently discount. Code computes the score with fixed weights and hard caps (missing must-haves cap 6; sponsorship likely-blocked cap 3; location conflict cap 2). Deterministic, explainable, provider-independent arithmetic.
- **ScoreCard** per job: gate trail, embed percentile, sub-scores with quotes, the arithmetic, caps applied. Rendered in the queue UI and `applypilot explain <url>`.
- Anchors: shipped canonical exemplars per track + up to 4 of the user's own confirmed labels retrieved by similarity (personalized few-shot, no fine-tuning). Borderline-band jobs route to the human review queue (not k=3 self-consistency — the review surface exists and is cheaper and more trustworthy).
- Cost commitment: ≈$0.25/1000 discovered jobs fully processed on a cheap tier; ≈$1.90 premium tier. A weekly shadow audit deep-scores 2% of Stage-0/1 rejects → false-reject rate in `report`.
- **Model-switch smoke eval:** ~20-job golden subset for the user's track on any provider/model change; threshold offset applied, never score rewrites.

### 7.4 Feedback loop

- Queue cards: approve/pass + reason chips (one shared enum: wrong_seniority, wrong_role_family, company_no, location, comp_low, weak_match, stale, other).
- A pass triggers rule synthesis → plain-English rendering → **one-tap confirm** → `filter_rules` (single provenance-tracked store, executed by the gate engine at $0 forever). Rules carry hit counters and "recently blocked N — still right?" review nudges.
- Approvals/passes also become scoring anchors. v1's three rounds of hand-edited keyword fixes become single taps.

### 7.5 Queue-depth controller

One controller, fresh-eligible-depth SLI as setpoint, ordered and logged levers: widen freshness window (adaptive to measured market volume — a 5-day-old job in a 2-jobs/week market is fresh) → raise Stage-1 keep-rate → surge discovery (promote warm boards) → **propose** profile relaxations to the user. Per-stage pass-rate counts render in the funnel-health panel so "where did my 400 jobs go" is always answerable. Starvation is an alarm with causes, never a silent stall.

**Eligible-but-non-automatable jobs surface in an "apply yourself" list** with deep links — discovery value is not held hostage by apply automation. Onboarding shows a coverage forecast from the Atlas ("~N matching postings/month for your profile+geo") — the product refuses to overpromise in thin markets.

## 8. Trust layer & product surface

### 8.1 Modes (autonomy is earned per category)

- **REVIEW (default):** everything queues; nothing submits without a tap. The only mode on day 1.
- **COPILOT (per-category):** a category = `role_family × seniority_band`, derived **deterministically from gate output** (the LLM never assigns it). Graduation requires: ≥20 individually-tapped decisions, Wilson lower bound ≥85% agreement (bulk approve-all taps do not count), zero hard-gate leaks in the window, and explicit user acceptance of the graduation prompt.
- Demotion: any "shouldn't have applied" report demotes the category; editing hard-gate profile fields demotes all categories. Hard gates are never delegated in any mode.
- No "auto-apply everything" mode exists. (AUTOPILOT-as-ceiling deferred; COPILOT per category is the v1 ceiling.)

### 8.2 Receipts

Every submission writes a receipt: confirmation screenshot, all answers given, actual cost, verification evidence, mode, category — keyed by `identity_id`, indexed to the flight-recorder artifacts (90-day artifact retention, rows forever). The Copilot consent dialog states verbatim: *"Submitted applications go to real employers under your name and cannot be recalled. That's why ApplyPilot earns this per category first."*

### 8.3 The daily brief

One screen: applied-since-last-looked (receipt cards + "report a problem"); waiting-for-you (queue cards with verified JD quotes, approve/pass); pipeline health (rendered only when actionable, cause→action form); always-visible spend line. Failure states arrive translated (mapping table from failure taxonomy → headline, what happened, who can fix it, action button); raw failure classes never reach the UI. Notification budget: one morning digest, one starvation alert, one budget-80% alert.

### 8.4 Spend controls

§5.4 ledger + `{daily, monthly, per-apply}` caps. Onboarding measures the user's model's actual per-job cost and shows the trade-off ("$1/day ≈ 120 deep evaluations on this model; ≈1,100 on model X"). Recommended default: cheap cloud tier.

### 8.5 Cold start (≤30 min, the trust-deciding session)

Resume upload → extraction to editable chips → 8 fixed questions (structured pickers, hard gates first, "why we ask" microcopy on sponsorship) → **compiled-gates confirmation screen** → calibration deck: 10 cards from the snapshot's cached postings, scored on the recommended tier ("Nothing is being applied to. You're teaching ApplyPilot your taste") → contract screen (tomorrow's plan, REVIEW mode explained, budget slider). Week one samples gate rejections into the brief ("we rejected 210 as wrong-role — spot-check 5") so a bad compile is caught in days.

### 8.6 Kill switch

Red pause on every screen → `engine_control.paused`, checked before each job and at every checkpoint. Mid-apply: abort-if-pre-submit; if submit was issued, verification completes. Effective ≤5s. Undo is honestly tiered in the UI: queued approvals are undoable; submitted applications are explicitly not.

### 8.7 Web UI

The existing FastAPI app extends into the primary surface with local-token auth on all mutating endpoints. The "UI physically cannot live-apply" rule is replaced by the engine-enforced queue policy (§5.3) — required for non-technical users, tested with the same rigor as the verifier. CLI remains for power users.

## 9. The Operator interface

```python
class Operator(Protocol):
    def score(self, job, rubric, anchors) -> SubJudgments        # §7.3
    def resolve_fields(self, request: FieldResolutionRequest) -> FieldAnswers  # §6.5
    def label_controls(self, snapshot) -> ControlLabels          # §6.8 degraded tier
```

- All JSON-in → schema-validated JSON-out; enumerated answers by index; no streaming requirement; no tool-use requirement; no browser access, ever.
- Providers: Anthropic, OpenAI, Google, local/OpenAI-compatible — built over the existing `llm.py` router (which already gains structured-output support). Plus `ClaudeCLIOperator` as one quarantined provider file preserving the subscription no-API-key path and proving transport-agnosticism.
- `doctor` runs a ~20-question capability floor check on any configured model; per-provider answer-acceptance and park rates are published metrics. **Equal interface, honestly-measured per-provider outcomes.**
- Text-LLM uses elsewhere (enrichment extraction, tailoring later) ride the same provider router.

## 10. Safety kernel (below everything, including the parser)

### 10.1 Network-layer submit containment

CDP request interception blocks known per-ATS application-POST endpoints unless a SafetyKernel ticket is armed. **Dry-run means the POST physically cannot leave the browser.** Unknown submit-shaped endpoints fail closed. This sits below the front-end parser — a misparse cannot become a submission.

### 10.2 Submit broker

Every terminal-**candidate** click requires a ticket: any button matching a multilingual submit lexicon, any click on the last parsed step, any click with required-completeness satisfied. Unknown button on an unclassifiable step = park, never click. (v1's `"submit" in text` heuristic is retired.)

### 10.3 Canary fields and answer-bank hardening

- **Canary classes — work authorization, sponsorship, citizenship/legal attestations, compensation, address/DOB/date — resolve exclusively from exact profile paths or typed policy defaults. Never fuzzy-matched, never oracle-answered, never written to or served from the answer bank.**
- The answer cache's 0.30-cosine marker-channel shortcut (a live v1 bug that has fuzzy-served salary, citizenship, and address answers) is removed: fuzzy hits require exact marker-set match AND cosine ≥0.85 AND answer-type match.
- Pre-commit polarity contradiction check of any answer against profile facts.
- **First apply per company lands in the review queue** (acceptance criterion, not a preference) — caps the wrong-answer blast radius.
- Per-field provenance (profile path / bank / oracle / cache) recorded in the flight recorder; a nightly sampled audit surface of oracle answers with one-click correction.

### 10.4 Two-phase submission ledger

INTENT row (keyed by `identity_id`) before the click → CONFIRMED/FAILED after verification. The submit-critical section is watchdog-exempt with a bounded +15s grace. Dangling INTENTs require human reconciliation with flight-recorder evidence one click away; dangling-INTENT rate is a health metric. Retry-on-ambiguity without reconciliation is impossible — the Reddit double-submit class is structurally dead.

### 10.5 Anti-bot posture

Jittered inter-field pacing (spending speed headroom to look human), per-company and global submit rate limits, persistent real cookies in clean worker profiles, weekly per-ATS CAPTCHA/block-rate as a **blocking** release metric with an automatic pacing-slowdown switch, evaluation of Runtime.enable-avoiding CDP clients in Phase 2. CAPTCHAs route to a human-solve queue in the UI — solver services are out.

## 11. Testing, telemetry, CI

- **Flight recorder:** per-attempt bundle — form IR, per-field provenance and commit results, network log, screenshots, MHTML on failure/sample. Storage-capped with retention policy.
- **FixtureFactory:** `applypilot fixtures promote <run>` turns any bundle into a replayable CI regression (real recorded DOM replaces v1's synthetic-only HTML). CI fails on unnamed terminal states.
- **CI on push** (currently manual-only), with the PyPI publish workflow gated on green tests.
- **Nightly canary parse** of ~20 live forms per ATS → parse-rate alert.
- **Gate-audit sampling:** weekly human review of ~20 preflight/gate-blocked jobs; over-blocking ≥5% fails the release gate. 5% of gate-blocked jobs are periodically re-attempted.
- **Blocking weekly metrics from Phase 2 on:** per-ATS pass rate + CAPTCHA/block rate, verify-tier share, cache hit rate, parse-gap rate, escalation/degraded-tier usage, dangling-INTENT rate, false-reject rate, queue-depth SLI.
- **North star (ungameable):** verified submissions per 100 eligible discovered jobs, alongside per-ATS gated pass rate.

## 12. Acceptance gate (pinned)

Denominator: eligible, automatable jobs on supported ATSes (Greenhouse, Ashby, Lever), cold path included, honesty-checked by gate-audit sampling (over-block <5%).

1. Verified pass ≥90% per supported ATS (ratchet: Greenhouse first), on 100+ live rows per ATS.
2. Median wall-clock per verified apply: ≤45s warm Greenhouse; ≤90s multi-step; measured per-phase p50/p90 published.
3. Zero canary-field violations; zero identity-duplicate submissions; zero unauthorized submissions (dry-run/network containment) — all property-tested, plus zero occurrences across the burn-in corpus.
4. Operator swap (Anthropic ↔ OpenAI ↔ Google ↔ local) requires zero engine changes; per-provider park/acceptance rates published; capability floor enforced by `doctor`.
5. Queue: fresh-eligible depth ≥5 (median daily) for a mainstream tech profile over 14 days; zero silent stalls (every starvation state shows ≥1 cause→action within 24h).
6. Waste: 0/100 applies violating any deterministic gate on manual audit (v1 baseline: 14/100); user-reported "shouldn't have applied" <2% over 30 days.
7. Cost: full discovery+scoring day within the user's cap with zero silent quality changes; default-tier ≤$0.50/day at ~1,500 discovered jobs.
8. Cold start ≤30 min to confirmed gates + ≥10 calibration decisions; onboarding LLM spend ≤$0.50.
9. Every failed attempt carries a named failure class, a plain-language UI explanation, and a replayable artifact; ≥85% of new jobs gated with zero LLM calls.

## 13. Build plan

Phases overlap; each has an exit gate. Calendar honesty: judges flagged 1.5–2× on solo-engineer estimates; the sequencing is designed so value lands continuously and Nida's live queue never regresses.

- **Phase 0 — hygiene (days):** fix the 3 red adapter tests; commit the web UI + tests; CI on push; purge PII strays from the repo root (resume backup, stale data files); .gitignore fixes. Baseline must be green before A/B telemetry means anything.
- **Phase 1 — spine + safety under the CURRENT engine (wks 1–3):** `identity_id` + folding; gate engine + `gate --rerun`; safety kernel (network containment, broker, two-phase ledger, canary rule, answer-bank hardening); spend ledger. **Exit:** v1 runs with kernel + gates live; waste classes measurably drop.
- **Phase 2 — Atlas + shadow mode (wks 2–5):** Common Crawl mining → validated Atlas + bundled snapshot; catch-up-tick scheduler + per-source accounting; snapshot compiler in shadow on live traffic (parse coverage, fingerprint hit rates, widget census, per-phase timings on Ashby/Lever). **Exit: go/no-go on Ashby/Lever numbers; cache-hit and parse-gap rates measured, not assumed.**
- **Phase 3 — apply engine v2 on Greenhouse (wks 4–8):** front-end + resolver + oracle + executor + drivers + verify tiers; dry-run vs fixtures; live A/B per form family; Windows soak test. **Exit: Greenhouse cutover at v2 ≥ v1 on 100+ live rows; legacy tier quarantined.**
- **Phase 4 — Ashby/Lever + funnel + trust (wks 7–12):** driver long-tail from the shadow census; compile-then-score + MatchProfile onboarding; brief/receipts/modes/decisions; queue-depth controller. **Exit: acceptance-gate items 5–8.**
- **Phase 5 — the ratchet (ongoing):** named-failure-class grind to 90% per ATS; retire legacy tiers; Workday behind session vault; packaging/installer for non-technical users.

## 14. Explicitly out of scope for v1 (YAGNI)

Community registry publishing/contribution infra (bundled snapshot only); client polling of the full cold-board Atlas; long-running discovery daemon (tick model instead); AUTOPILOT ceiling mode; cross-provider calibration harness beyond the 20-job smoke eval; k=3 self-consistency (review queue instead); embedding preference memory (standing rules first); SimHash cross-source identity folding; grounded tailoring and cover letters (master resume only — highest reputation-risk per unit value; revisit post-gate with the bullet-ID grounding design); withdraw-helper deep links; starvation forecaster (threshold alarm suffices); jobspy/theirstack as apply sources; LinkedIn/Indeed quick-apply; CAPTCHA solver services; multi-tenant server deployment.

## 15. Top risks and their mitigations

| Risk | Mitigation |
|---|---|
| Wrong-answer-at-scale (canary polarity) | §10.3 — hard rule, bank hardening, first-apply-per-company review, provenance audit |
| Submit misclassification / dry-run escape | §10.1–10.2 — network containment below the parser; broker on all terminal candidates |
| Correlated ATS DOM churn zeroing a dialect | nightly canary parse; degraded oracle-labeling tier (counted, capped); legacy agent fallback through burn-in; same-day fixture promotion |
| Anti-bot escalation (existential, external) | §10.5 posture from day one; blocking weekly CAPTCHA metric; pacing kill switch; human-solve queue |
| Fingerprint/cache hit-rate overstated | field-level cache primary; two-level fingerprints; hit-rate measured in shadow before any cutover claim |
| 60s claim vs physics | honest per-ATS targets; option probes off the parse path; MHTML off the critical path; per-phase telemetry first |
| Denominator gaming | pinned gate + weekly gate-audit; ungameable north-star metric |
| Windows persistent-Chrome instability | clean profiles; recycling policy; multi-day soak as exit criterion |
| ATS vendors auth-walling public JSON APIs | politeness budgets; vendor canary; snapshot amortization; risk acknowledged as external |
| Queue starvation in niche/thin markets | coverage forecast at onboarding; adaptive freshness; "apply yourself" list; depth SLI with causes |
| Weak BYO model (quality variance) | capability floor in doctor; park-don't-guess; per-provider published metrics; recommended default tier |
| Solo-engineer schedule slip | phase exit gates, not dates; value lands under v1 from Phase 1; live queue never regresses |
