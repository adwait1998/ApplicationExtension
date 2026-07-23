# ApplyPilot Project State — 2026-05-08

Self-contained context for continuing this work in a fresh Claude Code session. Read this file first.

## What this is

Auto-apply pipeline for **Nida Shah** (Product/UX Designer, 5 yrs, needs visa sponsorship, San Jose CA). Forked from `Pickle-Pixel/ApplyPilot` (AGPL-3.0). Discovers jobs across boards, scores via local LLM, and auto-submits applications on company ATSes.

User of this machine = adwai (different person than Nida — running this on her behalf).

## Current state (as of 2026-05-08, ~13:00 PT)

**Pipeline stages working end-to-end:**
- Discover: JobSpy + Workday + ATS boards (Greenhouse/Lever/Ashby JSON APIs)
- Score: Gemma 3 4B via Ollama (no rate limits, free)
- Apply: Sonnet 4.6 via Claude Code CLI driving Playwright MCP

**LLM setup:**
- Local: Ollama serving `gemma3:4b` for scoring, `phi4-mini` available
- `LLM_URL=http://localhost:11434/v1` in `.env` (takes priority over Gemini)
- Gemini key still set as fallback (free tier, exhausted daily quota during initial scoring)

**Tier 3 unlocked:** Claude Code CLI is the npm-global install at `C:\Users\adwai\AppData\Roaming\npm\claude.cmd` (NOT the Microsoft Store sandbox version which is inaccessible from regular Python processes). The .cmd shim is what `CLAUDE_BIN` points to and what `applypilot doctor` resolves. Currently version 2.1.136. The earlier `...\Roaming\Claude\claude-code\<version>\claude.exe` path documented in older notes is gone.

## Critical environment variables

```powershell
$env:APPLYPILOT_DIR     # = "E:\applypilot-data" (REQUIRED — keeps data off C drive)
$env:CLAUDE_BIN         # = full path to claude.exe (override for find_claude_binary)
$env:OLLAMA_MODELS      # = "E:\ollama-models" (keeps models off C drive)
$env:LLM_URL            # = "http://localhost:11434/v1"
$env:LLM_MODEL          # = "gemma3:4b"
```

All persisted via `[Environment]::SetEnvironmentVariable(..., "User")`.

## Important paths

| Path | What |
|---|---|
| `E:\auto-apply-pipeline\` | Forked ApplyPilot source (editable pip install) |
| `E:\applypilot-data\` | All runtime data: profile, resume, DB, logs, tailored outputs |
| `E:\applypilot-data\applypilot.db` | SQLite — all discovered/scored/applied jobs |
| `E:\applypilot-data\profile.json` | Nida's personal data, work auth, EEO answers |
| `E:\applypilot-data\resume.txt` | Resume as plain text (LaTeX-stripped from `E:\Nida_Application\resume.tex`) |
| `E:\applypilot-data\resume.pdf` | The PDF that gets uploaded (32KB) |
| `E:\applypilot-data\.env` | LLM config + CapSolver key (blank) |
| `E:\applypilot-data\logs\review.jsonl` | One JSONL row per apply attempt (supervisor surface) |
| `E:\applypilot-data\chrome-workers\worker-0\` | Persistent Chrome profile (cookies survive across runs) |

## Code changes made (vs upstream ApplyPilot)

### Discovery
- **NEW** `src/applypilot/discovery/ats_boards.py` — hits Greenhouse/Lever/Ashby JSON APIs directly. ~50x faster than HTML scraping. 601 jobs in 17s.
- **NEW** `src/applypilot/config/ats_companies.yaml` — list of design-friendly companies per ATS.
- `discovery/jobspy.py` — parallelized the (query × location) loop with ThreadPoolExecutor (4 workers). ~4x faster.
- `pipeline.py` — added `--source` CLI flag to run subset of discovery sub-stages. Disabled smart-extract by default (replaced by ats_boards).
- `cli.py` — added `--source` Typer option.

### Scoring
- `scoring/scorer.py` — rewrote `SCORE_PROMPT` with explicit field-match rule + few-shot examples. Bumped `max_tokens` 512→4096 (Qwen verbose). Tolerant parser handles markdown bold, slash format, `<think>` blocks. Added title-based pre-filter (`_FIELD_KEYWORDS`) that auto-scores 1 for obvious mismatches without LLM call.
- `llm.py` — fixed `/no_think` injection bug (was checking only first message; now finds first user message).
- `discovery/ats_boards.py` writes `description=full_description` and `application_url=url` directly so scoring works without enrichment.

### Auto-apply (Tier 3)
- **NEW** `src/applypilot/apply/prefill.py` — CDP-connect helper that pre-fills 4 standard Greenhouse fields + resume upload. Read-back-after-fill verifies React-controlled values stick. Doesn't kill Chrome (`Browser.close()` on CDP-connected only releases socket).
- `apply/launcher.py` — added master-resume fallback when `tailored_resume_path IS NULL`. Hoisted Claude binary detection (`find_claude_binary` with multi-location glob fallback). `run_job` returns 3-tuple `(status, duration_ms, prefill_status)`. `write_review_log` writes 4 prefill fields.
- `apply/prompt.py` — `build_prompt` accepts `prefill_status`, injects HARD RULES section telling Sonnet not to navigate (the navigate would reload page and wipe pre-fill).
- `cli.py` apply gate — relaxed from "tailored resume required" to "scored job available".
- `apply/launcher.py` SQL — relaxed `tailored_resume_path IS NOT NULL` filter; added inner-loop manual-ATS skip so launcher doesn't bail when first job is LinkedIn.
- `config/sites.yaml` — added LinkedIn Easy Apply / Indeed Quick Apply / Glassdoor / ZipRecruiter URL patterns to `manual_ats` (skip aggregator quick-apply flows).

### Auto-apply v2 — Form Compiler (Phase 3, added 2026-07)

**Status: Phases 0–3 complete + verified @ commit `5f79378`** (730 passed, 1 intentional
skip). A second, deterministic apply engine for Greenhouse only, living entirely in
`src/applypilot/apply/v2/`. Behind the `APPLYPILOT_V2_ENGINE` env flag (fresh-read every
call, `1`/`true`/`yes`/`on`; default OFF = byte-for-byte legacy passthrough). Fails OPEN
to the legacy LLM-agent path pre-submit, fails CLOSED (`needs_review:v2_crashed_post_submit`,
never a silent re-apply) post-submit. Reuses the SAME safety kernel (submit broker /
submission ledger / browser_stream) the legacy path uses — v2 constructs none of it.

Package map (`src/applypilot/apply/v2/`):
- `__init__.py` — `V2_ENGINE_ENV` ("APPLYPILOT_V2_ENGINE") / `V2_TIER_LABEL` ("v2_greenhouse") constants.
- `ir.py` — `FormSchema`/`Step`/`Field` IR; churn-resistant `field_fp`/`template_fp`/`questions_fp` fingerprints; `LAZY` options sentinel; canary-key taxonomy (work_auth/sponsorship/citizenship/salary/address/dob).
- `operator.py` — `Operator` protocol + `LLMOperator`/`ClaudeCLIOperator`; JSON-in/JSON-out field resolution, enumerated answers by index only (never free-typed), one retry, park-don't-guess on failure.
- `mapping_cache.py` — `mapping_cache` + `submit_endpoints` SQLite tables: demote-never-archive locator bindings (2 verified failures demotes, row is never deleted) + submit-endpoint harvest.
- `frontend_greenhouse.py` — `BrowserObservation` → `FormSchema` parser: semantic-key synonym table, widget-kind classifier, options=LAZY (dropdowns are never opened at parse).
- `resolver.py` — `FormSchema` + profile + caches → `FillPlan`: canary-first ladder (exact profile path → resume-path → non-canary profile path → EEO-decline default → hard-refusal park → mapping-cache → answer-bank read-only → Oracle).
- `drivers.py` — `WidgetDriver` registry (`text`/`textarea`/`file`/`react_select`/`native_select`/`typeahead_location`/`phone_intl`); every commit is read-back verified; promotes `prefill.py`'s react-select/location/phone helpers rather than re-authoring them.
- `executor.py` — walks a `FillPlan` step-by-step: resume/file fields first, then a MutationObserver quiet-window settle gate before the rest (zero fixed sleeps of its own); required-completeness interlock gates `ready_to_submit`.
- `verify.py` — Tier-1 passive submit-POST network evidence (auto-harvests `submit_endpoints` on a confirmed success) + Tier-2 reuses the legacy DOM verdict core; ambiguity → `needs_review`.
- `orchestrator.py` — `run_form_compiler`: Parse → Resolve → Oracle → Fill → Submit → Verify conductor; owns the fail-open/fail-closed boundary described above.
- `flight_recorder.py` — `FlightRecorder` per-attempt bundle writer (IR + per-field provenance + network log + captured DOM + phase timings) feeding `applypilot fixtures promote`; **not yet called from `orchestrator.py`** — no live attempt writes a bundle today.

The dispatch seam (`_dispatch_apply_v2_aware`, `_v2_enabled`, `_make_v2_production_fn`)
lives in `launcher.py`, not the `v2/` package — it threads the worker's existing
broker/ledger/browser_stream into the orchestrator and reconciles the ledger INTENT on
every v2 terminal status. New CLI: `applypilot report --v2-cutover` (3-leg cutover gate:
pass-rate + p50 speed + manual safety audit) and `applypilot fixtures promote <run>`
(flight-recorder bundle → `tests/fixtures/v2/<company>.html` + `.expected.json`). See
`docs/OPERATOR_CHEATSHEET.md` §11 for the operator workflow and known limitations
(resume-binding gap, missing radio_group/checkbox/date drivers, unwired flight recorder,
enumerated-custom-question parking, react-select sleep tax).

### Config / utilities
- `config.py` — `find_claude_binary()` checks PATH then globs common Windows install roots. Returns whatever exists.

## Live perf measurements

| Run | Site | Time | Actions | Cost (theoretical) |
|---|---|---|---|---|
| Baseline (no pre-fill) | Chime — Principal PD | 281s | 54 | $1.30 |
| First Figma w/ pre-fill (BUG: Sonnet re-filled) | Figma — PD Design/Dev/AI | 506s | 84 | $2.10 |
| **After fix** | Figma — PD Growth & Monetization | **208s** | **34** | **$0.88** |

Net improvement: ~3 min/job → ~3.5 min/job typical Greenhouse, with the pre-fill working it's ~2-3.5 min depending on form length.

User is on Claude Max 5x — costs are theoretical; actual billing is the flat $100/mo.

## Job queue state

952 jobs total, 350 scored. Distribution:
- Score 9: 11 (mostly Greenhouse — Figma, Linear, Replit, Chime)
- Score 8: 123 (Greenhouse-heavy + Workday + LinkedIn)
- Score 7: 137
- All LinkedIn URLs auto-skip (manual_ats), Workday tenants need per-tenant account creation

3 Greenhouse score-9 jobs were dry-run-tested then reset to NULL apply_status — ready for live submission as of last session.

Motorola Solutions Workday job (ECH Application Specialist) is marked applied from a successful dry-run; the title isn't actually a design role, so it's deliberately left "applied" to filter out.

## Known issues / future work

- **Lever / Ashby pre-fill not implemented** — selectors not reliably documented. Would need to inspect production pages.
- **Workday tenants need pre-registered accounts** with the email verification flow done manually. The shared Workday password lives in `profile.json` (`personal.password`, updated 2026-05-09); works for Motorola, but each new tenant requires creating + verifying an account first. The profile email (`personal.email` in `profile.json`) is used across all job sites including Workday.
- **MiKTeX not installed** — original plan called for LaTeX vision-feedback resume tailoring. Skipped. Resume is fixed `.pdf` (32KB).
- **Resume tailoring per-job is disabled** — launcher now uses master `resume.pdf` as fallback. The scoring/tailor module exists but Gemma 3 4B fabricates content (validator catches; status `failed_validation`). Tailoring deferred until a stronger model is wired in.
- **Smart-extract disabled** — replaced by ats_boards. Set `APPLYPILOT_SMART_EXTRACT=1` to re-enable.
- **Cover letter generation** — also subject to fabrication issues. Currently no cover letter is uploaded; Sonnet handles "tell us about yourself" inline if asked.
- **`/superpowers` plugin scope** — installed but scoped to `E:` drive only. Slash commands like `/requesting-code-review` only register if Claude Code is launched from `E:\`. To make global, change scope in Customize panel.
- **Style nit (low priority)** — `prompt.py` STEP-BY-STEP still says "1. browser_navigate" while pre-fill HARD RULES section says don't navigate. Sonnet is reconciling correctly; could be tightened by conditionally rewriting step 1.
- **v2 Form Compiler (Phase 3, `APPLYPILOT_V2_ENGINE`, Greenhouse only) known limitations** — see "Auto-apply v2" above for the package map:
  - Resume/file field has no resolver binding at commit `5f79378`, so every live Greenhouse form parks as `needs_review:v2_incomplete_required` before Fill/Submit (safe, but shadow mode currently measures parks, not applies). Fixed in follow-up commit `9a632ae` on `main` ("resume binding — thread prologue-resolved resume_path into resolver") — confirm it has landed on the branch you enable the flag on.
  - No driver registered for `radio_group` / `checkbox` / `date` widgets at `5f79378` (`drivers._REGISTRY` covered only `text`/`textarea`/`file`/`react_select`/`native_select`/`typeahead_location`/`phone_intl`); a required field of one of those kinds fails to commit and parks the form. Implemented in follow-up commit `85d00f4` ("v2: radio_group/checkbox/date drivers — close Task 6 registry gap") on branch `p3-drivers-gap` — not yet merged into this Phase 3 baseline.
  - `flight_recorder.FlightRecorder` is built and tested but not yet called from `orchestrator.run_form_compiler` — no live v2 attempt writes a bundle to `$APPLYPILOT_DIR\flight\` yet, so `applypilot fixtures promote` has nothing real to promote from until that wiring lands.
  - Enumerated (dropdown/react-select) custom questions always park — options are LAZY at parse so the Oracle can never index them; free-text custom questions work today. Enumerating them is Phase 4.
  - The promoted react-select drivers still carry fixed `time.sleep()` calls from `prefill.py` (~0.25s clean commit, up to ~1.4s across 5 calls on the keyboard-fallback/desync path) — not covered by the executor's own zero-fixed-sleep invariant; watch this against the 45s p50 cutover budget.

## Quick command reference

```powershell
# Check status
applypilot doctor                        # Tier check, dependency check
applypilot status                        # DB job counts

# Discovery
applypilot run discover                  # Full: JobSpy + Workday + ATS boards
applypilot run discover --source ats_boards   # Just ATS boards (~17s, 600+ jobs)
applypilot run discover --source jobspy       # Just LinkedIn/Indeed
applypilot run discover --source workday      # Just Workday companies

# Scoring (uses Gemma 3 4B locally)
applypilot run score --min-score 7

# Apply (Tier 3 — uses Sonnet via Claude Code)
applypilot apply --dry-run --workers 1 --limit 1   # Test, fills form but doesn't submit
applypilot apply --workers 1 --limit 1             # Real submission, one job
applypilot apply --workers 1 --limit 3             # Three jobs serially
applypilot apply --workers 3 --limit 9             # Three parallel browsers
```

## Useful one-off SQL via Python

Reset jobs for retry:
```powershell
py -3.12 -c "import sqlite3; c=sqlite3.connect(r'E:\applypilot-data\applypilot.db'); c.execute(\"UPDATE jobs SET apply_status=NULL, apply_attempts=0, apply_error=NULL WHERE site LIKE '%greenhouse%' AND fit_score >= 9\"); c.commit(); print('reset')"
```

Show top-scoring jobs:
```powershell
py -3.12 -c "import sqlite3; c=sqlite3.connect(r'E:\applypilot-data\applypilot.db'); [print(f'  [{s}] {si[:25]:25} {t[:55]}') for s, si, t in c.execute('SELECT fit_score, site, title FROM jobs WHERE fit_score >= 8 AND apply_status IS NULL ORDER BY fit_score DESC LIMIT 30').fetchall()]"
```

Tail review log:
```powershell
Get-Content -Wait E:\applypilot-data\logs\review.jsonl
```

## How to get going in a fresh session

1. Open Claude Code from `E:\` (so plugins scoped to E: load).
2. `cat E:\auto-apply-pipeline\CONTEXT.md` (this file).
3. `applypilot doctor` — confirm Tier 3.
4. `applypilot status` — see queue state.
5. Check `E:\applypilot-data\logs\review.jsonl` tail for last applied jobs.
6. Proceed.
