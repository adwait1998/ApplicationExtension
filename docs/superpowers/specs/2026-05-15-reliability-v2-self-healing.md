# Design: Reliability v2 — Self-Healing Deterministic ATS Adapters

**Status:** approved direction (scientist analysis + 2026 literature), pending implementation
**Owner:** Adwait
**Date:** 2026-05-15
**Decision drivers:** eliminate the *removable* error class, drive cost→~0 on repeats, confine the LLM to genuine novelty. Sonnet exhausts usage limits fast; iter-13 live batches ran 60-80% with the dominant losses being self-inflicted (skill-flow crashes, timeout-retry waste, react-select desync) rather than genuine novelty.

## Problem (decomposition)

Every apply re-derives a ~stable form from scratch with a stochastic agent against an adversarial environment. Error sources split:

- **(A) Removable**: re-solving stable structure stochastically. The MAJORITY of observed failures + ALL the cost/rate-limit pain. A Greenhouse form is ~identical across all 19 Greenhouse companies; solving it with an LLM every time is the defect.
- **(B) Irreducible**: environment defenses (CAPTCHA, email-verification, anti-bot — Linear/Dropbox/Ashby) + genuine novelty (custom free-text questions). Must be *contained*, not "fixed".

Prior iterations (1-13) firefought (A) instance-by-instance. This spec deletes the class.

## Evidence (2026 field research)

- Production pattern is hybrid: deterministic Playwright for the predictable 80%, AI only for the novel 20%.
- Stagehand's caching → cost approaches ~zero after first run on repeated same-site workflows (empirical proof the record-once/replay thesis works *when replay is robust*).
- 2026 arXiv "Zero-Cost Self-Healing via DOM Accessibility Tree": a 10-tier priority-ranked locator hierarchy (`get_by_role` → `data-testid` → ARIA → CSS fragment → visible text) + multi-signal element fingerprint + scored re-discovery in <1s, **no LLM**. This is the principled fix for the Skill-Playbook drift problem (MCP records the agent's prose, not stable selectors).

## Architecture

```
apply(url)
  ▼ detect ATS (greenhouse|lever|ashby|workday)
  ▼ SELF-HEALING DETERMINISTIC ADAPTER  (one per ATS, NOT per company)
      fill standard fields via semantic locators (role/ARIA/label hierarchy)
      on selector miss → fingerprint + scored re-discovery (<1s, no LLM)
  ▼ for each remaining custom free-text question:
      SEMANTIC ANSWER-CACHE  (embed Q → NN vs profile Q&A bank)
        hit  → deterministic fill
        miss → ONE cheap LLM call → store answer in bank
  ▼ ENVIRONMENT GATE: CAPTCHA / email-verif / anti-bot detected?
      → route to human-in-the-loop queue (don't burn budget at a wall)
  ▼ submit + existing verifier
```

LLM usage collapses from "drives the whole form, every time" to "answers a genuinely-new free-text question, once, ever".

## Phases (each its own ralph iteration, each ships executable tests)

| Phase | Scope | Done-when (NO live cost unless noted) |
|---|---|---|
| A (H5) | **Telemetry first.** Persist per-apply structured data to review.jsonl: input/output/cache_read/cache_create tokens, cost_usd, turns, failure_class, tier_used, heal_events. Add `applypilot report` summarizer (A vs B cost split, cache hit-rate, $/apply, pass-rate by ATS). | A synthetic/replayed apply produces a telemetry row with all fields; `report` prints the (A)-vs-(B) breakdown. Unit-tested. |
| B (H2) | **Self-healing locator core.** New module `src/applypilot/apply/healing.py`: 10-tier priority locator hierarchy + element fingerprint + scored re-discovery. Pure Playwright, zero LLM. | Unit tests vs a synthetic DOM that mutates ids/classes between calls — locator re-discovers the element in <1s, ≥95% of mutation cases. |
| C (H1) | **Per-ATS Greenhouse adapter.** Generalize prefill into `adapters/greenhouse.py` using healing locators; cover the FULL standard Greenhouse form deterministically (all text, EEO, screening Yes/No, resume, submit). | Synthetic Greenhouse form test: 100% standard fields filled + submit, no LLM. Then ONE user-authorized live Greenhouse apply reaches `applied`. |
| D (H3) | **Semantic answer-cache.** `src/applypilot/apply/answer_cache.py`: embed question (local embedding), NN vs a profile-derived Q&A bank, LLM only on miss (via existing llm.py local-capable client), persist answer. | Unit test: repeated/again-seen questions served from cache (0 LLM); novel question → 1 LLM call → subsequently cached. |
| E (H4) | **Automatability gate + human queue.** Early-detect CAPTCHA/email-verif/anti-bot; mark `needs_human`, write to a review queue file/table instead of retrying; per-company automatability metric in `report`. | Unit test: a CAPTCHA-signal page routes to needs_human without an LLM apply attempt; report shows per-company automatability. |
| F | **Live validation batch (user-authorized).** Measure vs iter-13 baseline: (A)-class errors → ~0, $/apply down materially, pass-rate up. | A ≥10-job live batch (explicit user go) shows (A) failures eliminated and cost/apply materially below the Sonnet iter-13 number. |

Phases A-E are **zero live cost** (synthetic tests + replayed transcripts). Only Phase C's single validation and Phase F require explicit user authorization for real submissions.

## Working rules (ralph)

- Spec is source of truth. Re-read every iteration. Unanswered question → write the answer back into the spec, then proceed.
- Surgical edits; don't refactor unrelated code. Keep the existing LLM apply path working as the fallback throughout (best-effort skill/adapter, fall back to LLM — never hard-fail a job).
- **Every phase ships with executable tests that cost $0** (synthetic DOM / replayed stream-json). Full `pytest` must stay green (currently 149).
- **NEVER fire a live `applypilot apply` autonomously.** Live submissions = real applications under Nida's name + cost + rate limits. Phase C's single validation and Phase F's batch each require an explicit, fresh user "go" — the loop must STOP and ask, not assume prior authorization carries over.
- Commit after each phase (one-shot identity override; no push). Append iteration notes to `docs/ralph-iterations.md` under "## Iteration N — Reliability v2 Phase X".
- Telemetry (Phase A) lands first so every later phase is measured, not anecdotal.

## Acceptance criteria

1. Phase A telemetry visible: `report` shows $/apply, cache hit-rate, (A)-vs-(B) failure split.
2. Self-healing locator re-discovers mutated elements <1s, ≥95% synthetic cases, no LLM.
3. Greenhouse adapter fills 100% of standard fields on the synthetic form with zero LLM; one live Greenhouse job reaches `applied`.
4. Answer-cache: second occurrence of a question → 0 LLM calls.
5. Automatability gate routes CAPTCHA/email-verif to human queue without an LLM apply attempt.
6. Live validation (Phase F, user-authorized): zero (A)-class failures; $/apply materially below the iter-13 Sonnet baseline; pass-rate ≥ iter-13.

When Phase F validates criteria 1-6 end-to-end, emit `RALPH-DONE: RELIABILITY V2 SHIPPED`.

## Out of scope (v2)

- Replacing Claude with a local *agentic* browser driver (small models fail the noisy multi-step residual; laptop can't host a big one). A local model is in scope ONLY as the narrow free-text answerer in Phase D, via the existing llm.py local endpoint.
- CAPTCHA solving (route to human, don't solve).
- Non-Greenhouse adapters beyond a Lever/Ashby stub (Greenhouse is the dominant ATS and the proof case; others follow the same pattern post-v2).
