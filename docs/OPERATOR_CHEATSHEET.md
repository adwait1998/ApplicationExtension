# ApplyPilot — Operator Cheatsheet

Practical commands to drive the pipeline. PowerShell (your default shell).
Everything here is copy-paste ready.

---

## 0. Set up once per terminal

```powershell
cd E:\auto-apply-pipeline
$env:APPLYPILOT_USE_SKILLS = "1"          # enables the skill-playbook dispatcher
$PY = "C:\Users\adwai\AppData\Local\Programs\Python\Python312\python.exe"
```

Run anything below as `& $PY -m applypilot <command>`.

Key paths:
- Code: `E:\auto-apply-pipeline`
- Data root: `E:\applypilot-data`

```
E:\applypilot-data\
  active_profile              which person commands act for by default
  shared\                     atlas.db (boards, source_runs, mapping_cache,
                              submit_endpoints), settings.json, ui_runs\
  profiles\<id>\              profile.json, resume.pdf, searches.yaml,
                              applypilot.db, logs\, ui_settings.json
```

Before migration the data dir is flat (`profile.json` etc. at the root) and
everything still works unchanged — see §0b.

---

## 0b. Profiles (who you are applying for)

ApplyPilot can run independent job searches for several people. Learned ATS
knowledge is shared; everything personal is isolated on disk.

```powershell
& $PY -m applypilot profile list                 # ids + names, * marks active
& $PY -m applypilot profile show                 # the resolved profile
& $PY -m applypilot profile use nida             # change the default
& $PY -m applypilot profile add adwait           # scaffold a new one
& $PY -m applypilot --profile adwait <command>   # act for one command only
```

Which profile a command acts for: `--profile` > `APPLYPILOT_PROFILE` >
`active_profile` > the only profile if there is just one. It is resolved before
the process loads any config, so it applies to the whole command.

**One-time migration** from the old flat layout (moves real data, backs up first):

```powershell
& $PY -m applypilot profile migrate --yes
```

**One batch at a time, globally.** There is one Chrome rig and one kill switch,
so a batch for one profile blocks batches for every other. `profile use` refuses
to switch while a batch is running. The spend cap is global (one wallet);
`max_live_applies_per_day` is per profile.

---

## 0c. Copilot Chrome extension (fill a form you opened yourself)

The pipeline applies autonomously. The Copilot extension is the opposite: **you** open a job
application in your own Chrome, click once, and it fills what it can from your profile and
highlights what it deliberately left alone.

> **It never clicks submit and never navigates.** It fills and highlights; you review and
> submit. It clicks only what filling needs — radios, checkboxes, an "Add Another" button for a
> repeating section, and Workday's own dropdown/date/prompt widgets — each through its own guard
> that re-checks the target at the point of action and refuses anything submit-, next- or
> save-shaped. A capturing submit shield also blocks any form submission for the whole fill.

**One-time setup (no terminal needed afterwards):**

```powershell
& $PY -m applypilot extension install-host     # registers the native host (per user, no admin)
```

Then load the extension: Chrome → `chrome://extensions` → **Developer mode** → **Load unpacked** →
`E:\auto-apply-pipeline\extension` (or click **Reload** if it was loaded before — its ID is now
pinned to `noooclaijfiejnfgabkemnpabcbdnaac`, so a previously pasted token is no longer needed).
From then on the extension starts the local service itself (detached, no window, logging to
`<data dir>\logs\extension-service.log`) and fetches its token through the native host. Settings
→ Connection says "Connected automatically". Without `install-host`, the old way still works:
run `& $PY -m applypilot serve-extension` and paste the printed token into Settings.

**Day to day:** open an application and click the ApplyPilot icon (or **Alt+Shift+F**) — Chrome's
**side panel** opens and stays open as you switch tabs, keeping each tab's last result. Click
**Fill this page** (or **Alt+Shift+G**). The first time on a site Chrome asks for permission for
that site and for any embedded form's site (e.g. a company page embedding Greenhouse) — nothing
runs on a page until you click. Progress shows live; **Cancel** stops between fields.

What the panel shows, per field: **verified** (read back from the page), **draft** (generated
text — review it), **left for you** (with the reason), **kept your value** (it was already there
before the fill), **didn't stick** (the site reverted it), **failed**. Required fields that still
need you are listed first; click a row to jump to the field. **Undo** reverts what this fill
changed and reports how many it could restore.

More in the panel: **Draft cover letter** (a DRAFT from the posting and your résumé; copy,
download, or insert into a cover-letter box), **Remember my answers** (saves what you typed into
the fields it left for you, so next time they fill), **Mark as applied** (the application log in
Settings; CSV export), **Export fill report** (labels/statuses only, never values — send this when
a page fills badly), and an opt-in **Keep filling as I go through steps** for multi-step forms
like Workday (off by default; never clicks Next; stops if the site changes).

**When a page fills badly:** click **Export fill report** (and **Report page** for the form's
structure) and send both — they contain no values.

How each field is decided, first match wins:

| Tier | What it does | Fills? |
|---|---|---|
| secret guard | passwords/SSN/card fields | never |
| attestation | "I certify…", "I authorize … to verify/contact", e-signatures, terms & conditions | never — you tick/sign these |
| canary | work auth, sponsorship, salary, EEO, address, DOB — from exact profile paths only | only on an exact hit, else skipped; EEO with nothing set → "Decline to self-identify" |
| screening | criminal record, background check, drug test, non-compete, travel, how you heard — from your Settings answers only | yes when set and the question is plainly worded; otherwise left for you |
| deterministic | `autocomplete` attribute, then name/id/label patterns (short-answer boxes only, never essay questions) | yes |
| structured | `work_history[]` / `education[]`, indexed by the block heading ("Work Experience 2") | yes |
| laya | local semantic classification, confidence ≥ 0.75 | yes, above threshold |
| answer bank | semantic match against your past answers (`answer_bank.json`); a choice field only takes an answer that IS one of its options | yes |
| draft | LLM-written from your résumé facts; never for a choice field | yes, **badged DRAFT** |
| unresolved | — | no, left for you |

Canary questions are **never** answered by a model. An unanswerable canary is left blank on
purpose — that is the design, not a failure.

**Drafts are the one tier that puts generated text under your name.** They render blue rather
than green and are grouped separately in the panel. A draft that claims experience somewhere
absent from your history is refused outright ("draft refused — it claimed experience at X"),
so a model ignoring its instructions cannot invent an employer for you. Answer-bank hits are
never second-guessed: those are your own past words.

Tier defaults: the **answer bank is ON** by default (toggle it in Options → Smart fill);
**drafts and Laya are OFF**. Environment variables override the Options toggles:

```powershell
$env:APPLYPILOT_LAYA    = "1"   # semantic classification (~2GB RAM, ~28s first load) — off by default
$env:APPLYPILOT_ANSWERS = "0"   # answer bank — ON by default; "0" turns it off
$env:APPLYPILOT_DRAFTS  = "1"   # LLM drafts (needs the answer bank on) — off by default
$env:APPLYPILOT_MAX_DRAFTS = "5"  # per-page cap, default 5
```

`GET /health` reports which tiers are live.

### Setting up your profile — upload a résumé

Open the extension's **Options**. The first thing there is **Get started → Choose file**:
upload a `.pdf`, `.docx` or `.txt` résumé and the profile fills itself.

- Contact details (name, email, phone, LinkedIn, GitHub) come from plain pattern matching —
  no model involved.
- Work history and education are extracted by one local LLM call and marked
  **"AI-parsed, verify"**, a stronger badge than the plain "from résumé" mark, so you know
  which fields deserve a second look.
- **Nothing is saved until you click Save profile.** The banner says so; the draft sits in
  the form for you to correct first.
- **Work authorisation and sponsorship are never filled from a résumé**, even when the
  résumé states them outright. A résumé is not evidence of visa status and those answers
  carry real consequences, so they stay explicitly yours to set.
- The uploaded file is stored and is what gets **attached to application forms** for you.

A **completeness meter** shows "N of 12 essentials · missing: …" with clickable chips that
jump to whatever is unfinished. Completeness is what decides how much of a form can be
filled, so it is the number worth watching.

Below that, everything is editable by hand: personal details, work authorisation (explicit
Yes/No/Not-set), compensation, experience, and repeatable **work history** / **education**
rows with reordering. **Row 1 must be your most recent position** — that is what
"Work Experience 1" on a Workday form maps to. There is also a profile switcher, so several
people can share one installation.

Repeating blocks fill from the matching position, and a block beyond your history is skipped
rather than wrapped ("no 3rd position in your work history"), so your current job can never
land in a previous-employer box.

### Résumé attachment

The extension attaches your stored résumé to the form's file input, including
Greenhouse-style drag-and-drop zones. It picks the résumé target over a cover-letter input,
and **verifies the file is genuinely attached afterwards** rather than assuming — some ATS
sites reject programmatic uploads, and a silent failure would mean submitting with no
résumé. The panel shows an explicit attached/not-attached line either way.

See `extension/README.md` for known limitations (cross-origin iframes, shadow DOM, multi-step
forms). Verification harnesses: `scripts/e2e_extension.py`, `scripts/e2e_profile_editor.py`,
`scripts/e2e_options_resume.py`, `scripts/chrome_load_test.py`.

---

## 1. TL;DR daily flow

```powershell
& $PY -m applypilot doctor                 # 1. is the setup healthy?
& $PY -m applypilot run discover enrich score --quick --target-ready 10  # 2. fast lane for enough fresh jobs to apply
& $PY -m applypilot status                 # 3. what's in the queue?
& $PY -m applypilot apply --limit 10 --model claude-haiku-4-5-20251001 --headless --no-live --job-timeout 720 --max-transient-retries 1   # 4. apply (LIVE — real submissions)
& $PY -m applypilot report                 # 5. how did it go? cost / pass-rate / failure split
```

Defaults already set so you don't pass them: `--min-score 8`, `--max-age-hours 24`, dedupe on, timeouts not retried.

---

## 2. Health / setup

```powershell
& $PY -m applypilot doctor                 # checks Chrome, claude CLI, profile, resume, deps
& $PY -m applypilot --version
```

---

## 3. Inspect the queue (read-only, $0, instant)

```powershell
& $PY -m applypilot status                 # counts by pipeline stage
& $PY -m applypilot report                 # reliability + cost: $/apply, cache hit-rate, A-vs-B failures, pass-rate by ATS/tier
```

**What's eligible to apply right now** (score>=8, fresh<=24h, has app URL, not applied):
```powershell
& $PY -c @"
from applypilot.database import get_connection, init_db
from datetime import datetime, timezone, timedelta
init_db(); c=get_connection()
cut=(datetime.now(timezone.utc)-timedelta(hours=24)).isoformat()
q='''SELECT title,site,fit_score FROM jobs WHERE fit_score>=8 AND applied_at IS NULL
 AND application_url IS NOT NULL AND (apply_status IS NULL OR apply_status=\"failed\")
 AND discovered_at>=? ORDER BY fit_score DESC, site'''
rows=c.execute(q,(cut,)).fetchall()
print(f'APPLY QUEUE: {len(rows)} jobs')
for r in rows[:40]: print(f'  [{r[\"fit_score\"]}] {r[\"title\"][:55]:55s} {r[\"site\"]}')
"@
```

**Queue by company / score band:**
```powershell
& $PY -c @"
from applypilot.database import get_connection, init_db
init_db(); c=get_connection()
for r in c.execute('SELECT fit_score,COUNT(*) n FROM jobs WHERE applied_at IS NULL AND fit_score>=6 GROUP BY fit_score ORDER BY fit_score DESC').fetchall():
    print(f'  score {r[\"fit_score\"]}: {r[\"n\"]}')
"@
```

**What's already been applied to:**
```powershell
& $PY -c @"
from applypilot.database import get_connection, init_db
init_db(); c=get_connection()
for r in c.execute('SELECT site,title,applied_at FROM jobs WHERE applied_at IS NOT NULL ORDER BY applied_at DESC LIMIT 30').fetchall():
    print(f'  {r[\"applied_at\"][:16]}  {r[\"site\"][:20]:20s} {r[\"title\"][:45]}')
"@
```

---

## 4. Get FRESH jobs (discover → enrich → score)

```powershell
# Greenhouse/Lever/Ashby ONLY (cleanest, applyable forms, no LinkedIn/Workday noise):
& $PY -m applypilot run discover enrich score --source ats_boards

# TheirStack API ONLY (requires THEIRSTACK_API_KEY; consumes credits per returned job):
& $PY -m applypilot run discover score --source theirstack

# Expand the direct ATS registry first, then crawl it:
& $PY -m applypilot discover-ats --query "Product Designer,UX Designer,Senior Product Designer" --search-limit 300
& $PY -m applypilot run discover enrich score --source ats_boards

# Fast lane for a small apply batch (ATS boards + small LinkedIn/Google crawl,
# bounded enrichment/scoring, skips slow Workday by default):
& $PY -m applypilot run discover enrich score --quick --target-ready 10

# Backfill LinkedIn listing pages into direct company/ATS apply URLs.
# Dry-run first; --write only stores resolved non-Easy-Apply outbound URLs:
& $PY -m applypilot resolve-linkedin --limit 25 --min-score 8 --dry-run --corpus-report
& $PY -m applypilot resolve-linkedin --limit 25 --min-score 8 --write

# Fast lane with tailored resumes for the exact top apply-queue jobs:
$env:LLM_PROVIDER="claude"; $env:LLM_MODEL="sonnet"  # optional: use Claude Code for score/tailor/cover instead of local LLM
& $PY -m applypilot run discover enrich score tailor pdf --quick --target-ready 10

# Everything (also jobspy LinkedIn/Indeed + Workday):
& $PY -m applypilot run discover enrich score

# Full pipeline incl. resume tailoring + cover letters:
& $PY -m applypilot run
```

- `--source` values: `ats_boards` (Greenhouse/Lever/Ashby), `theirstack` (TheirStack API), `jobspy` (LinkedIn/Indeed), `workday`, `smartextract`.
- Freshness window is `hours_old` in `E:\applypilot-data\searches.yaml` (currently 24).
- Companies discovered = the list in `src\applypilot\config\ats_companies.yaml` (56 design-heavy companies). Add a company by its ATS board token under the right ATS.
- Re-running discover is safe — it dedupes by URL.

---

## 5. APPLY (live — real submissions under Nida's name)

Main command + the flags that matter:

```powershell
& $PY -m applypilot apply --workers 2 --limit 20 --model claude-haiku-4-5-20251001 --headless --no-live --job-timeout 720 --max-transient-retries 1
```

TheirStack-only apply batch:

```powershell
& $PY -m applypilot apply --site-contains TheirStack --workers 2 --limit 10 --model claude-haiku-4-5-20251001 --headless --no-live --job-timeout 720 --max-transient-retries 1
```

| Flag | Meaning / recommended |
|---|---|
| `--limit N` | Max jobs this run. Start small (10) to validate, then scale. |
| `--site-contains TEXT` | Restrict apply queue to sources/companies whose `site` contains text, e.g. `TheirStack`. Ignored for explicit `--url`. |
| `--workers N` | Parallel browsers. 1 = safe/diagnosable; 2 = ~2× faster (per-site lock makes it safe). |
| `--model` | `claude-haiku-4-5-20251001` (~⅓ cost, has the Greenhouse playbook) or `sonnet` (more robust, exhausts limits fast). |
| `--job-timeout 720` | Seconds/job. 720 needed for email-verification forms (480 default times out). |
| `--max-transient-retries 1` | Retries on transient fails. Timeouts are never retried (waste). |
| `--headless` | Hidden Chrome. Drop it to watch the browser. |
| `--no-live` | One-line progress per tool call. Drop it for the Rich dashboard. |
| `--dry-run` | Fill everything, **never click Submit**. Zero real submissions — rehearsal. |
| `--continuous` | Keep polling for new jobs forever (instead of `--limit`). |
| `--url "<job url>"` | Apply to ONE specific job. |
| `--min-score 9` | Override the default 8 (near-perfect only). |
| `--max-age-hours 0` | Disable the 24h freshness gate (drain everything, incl. stale). |
| `--reset-manual` | Reset jobs marked manual ATS so they can re-enter the apply queue. |
| `--resolved-only` | With `--reset-manual`, only reset rows whose `application_url` is already a non-LinkedIn outbound URL. |

**Rehearse with no submissions first:**
```powershell
& $PY -m applypilot apply --limit 5 --model claude-haiku-4-5-20251001 --headless --no-live --dry-run --job-timeout 720
```

**One specific job:**
```powershell
& $PY -m applypilot apply --url "https://boards.greenhouse.io/figma/jobs/123" --limit 1 --model sonnet --headless --no-live --job-timeout 720
```

---

## 6. Watch a run in progress (second terminal)

```powershell
Get-Content E:\applypilot-data\logs\review.jsonl -Tail 5 -Wait     # one line per finished apply
Get-Content E:\applypilot-data\logs\worker-0.log -Tail 20 -Wait    # live agent reasoning + tool calls
```

Stop a run: `Ctrl+C` once = skip current job, twice = stop. Safe — no DB corruption; discovery/applies commit incrementally.

---

## 7. After a run — triage

```powershell
& $PY -m applypilot report                 # cost, pass-rate, (A) removable vs (B) irreducible failures
```

**See the latest attempts + why they failed:**
```powershell
& $PY -c @"
from applypilot.database import get_connection, init_db
from datetime import datetime, timezone, timedelta
init_db(); c=get_connection()
cut=(datetime.now(timezone.utc)-timedelta(hours=6)).isoformat()
for r in c.execute('SELECT site,title,apply_status,last_failure_class,verification_confidence FROM jobs WHERE last_attempted_at>=? ORDER BY last_attempted_at DESC',(cut,)).fetchall():
    print(f'  [{(r[\"apply_status\"] or \"?\"):14s}] {(r[\"last_failure_class\"] or \"\"):30s} conf={r[\"verification_confidence\"] or \"-\"} {r[\"site\"][:18]:18s} {r[\"title\"][:35]}')
"@
```

Full agent transcript for a failed job: newest `E:\applypilot-data\logs\claude_*.txt`.
Verifier evidence for an unverified one: `E:\applypilot-data\logs\verify_*.json`.

---

## 8. Skills (the deterministic replay layer)

```powershell
dir E:\applypilot-data\skills              # one <company>.yaml per recorded company
dir E:\applypilot-data\skills\_archive     # drifted/broken skills auto-moved here
```
A bad/drifted skill auto-archives and falls back to the LLM — you don't manage these by hand. Delete a `<company>.yaml` to force a fresh record next apply.

---

## 9. Common fixes

```powershell
# Re-try previously failed jobs (clears the failed lock):
& $PY -m applypilot apply --reset-failed

# Re-try resolved manual links from one source after running the LinkedIn resolver:
& $PY -m applypilot apply --reset-manual --site-contains TheirStack --resolved-only

# Manually mark a job applied / failed (DB only, no browser):
& $PY -m applypilot apply --url "<url>" --mark-applied
& $PY -m applypilot apply --url "<url>" --mark-failed --fail-reason "manual"

# Generate the prompt for one job WITHOUT applying (debug):
& $PY -m applypilot apply --url "<url>" --gen

# Stale 'in_progress' locks after a hard kill — released automatically on next run,
# or inspect:
& $PY -c @"
from applypilot.database import get_connection, init_db
init_db(); c=get_connection()
for r in c.execute(\"SELECT site,title FROM jobs WHERE apply_status='in_progress'\").fetchall(): print(r['site'], r['title'][:40])
"@
```

---

## 10. Mental model

```
discover → enrich → score → (tailor → cover) → APPLY → verify
   |          |        |                          |        |
ats_boards  full    fit 1-10                  skill replay  success-page
(56 cos)    desc   (prefilter kills          OR LLM agent   check → applied
                    PM/PgM/etc for $0)       (Haiku/Sonnet)  / needs_review
```
- Only **score >= 8** and **discovered <= 24h** jobs are applied to. Dupes (same company+title) collapse to one.
- Apply path: deterministic `prefill` fills ~14 fields + EEO/screening dropdowns (option-aware) → LLM handles custom free-text + verification + submit → verifier confirms.
- `report` tells you the truth; `status` tells you volume.
```
```

---

## 11. v2 engine (Form Compiler) — SHADOW / EXPERIMENTAL, Greenhouse only

Phase 3 shipped a second apply engine (`src/applypilot/apply/v2/`) that replaces the
LLM-agent loop with a deterministic **Parse → Resolve → Fill → Verify** pipeline for
Greenhouse forms only. It is fully behind a flag, defaults OFF, and fails open to the
legacy path before a submit fires. Status: **Phases 0–3 complete + verified @ commit
`5f79378`** (730 passed, 1 intentional skip).

### 11.1 The flag

```powershell
$env:APPLYPILOT_V2_ENGINE = "1"     # also accepts true / yes / on (case-insensitive)
```
- Read fresh on every dispatch (`launcher._v2_enabled`) — no restart needed, flip it and the next job picks it up.
- Only routes when BOTH the flag is on AND the job's ATS passes the per-ATS gate (`launcher._v2_supported_ats`). Default (no `APPLYPILOT_V2_ATS` set) = **Greenhouse only**, byte-identical to Phase 3. Set `APPLYPILOT_V2_ATS=greenhouse,ashby,lever` (comma allowlist) to widen the v2 shadow to the Phase-4A front-ends. Workday/LinkedIn/unknown ATSes always run the legacy path regardless of both flags.
- Unset (default): byte-for-byte legacy passthrough — the v2 closure is built but never invoked.

Turn it back off:
```powershell
Remove-Item Env:\APPLYPILOT_V2_ENGINE
```

### 11.2 Shadow A/B (LIVE — real submissions; "safe" = fail-open/fail-closed, NOT dry-run)

With the flag on, `applypilot apply` does not switch over wholesale — every Greenhouse job still gets a legacy-equivalent safety net. That safety net is about WHICH engine ends up submitting and how crashes are handled — it is not a rehearsal mode. The command below is a live apply like any other in this cheatsheet (`--no-live` only disables the terminal dashboard; it does not stop submissions) and WILL submit real applications:
- **Fail-open pre-submit.** Any exception in Parse/Resolve/Fill (before a Submit click) returns the internal `v2_fallback_to_legacy` sentinel and the SAME job re-runs through the legacy LLM-agent path in the same call — counted as a normal legacy apply, not a failure.
- **Fail-closed post-submit.** Once Submit may have fired, a crash returns `needs_review:v2_crashed_post_submit` instead of falling back — it never risks a double submission. The ledger's dangling-INTENT guard reconciles it on the next run.
- Both engines share the SAME safety kernel (submit broker / submission ledger / browser_stream network guard) — v2 constructs none of its own; the worker threads the same objects into whichever engine runs.
- Every `review.jsonl` row carries `tier_used`: `v2_greenhouse` for v2 attempts, `legacy_llm` for legacy — this is exactly what `report --v2-cutover` compares (no new telemetry).

**Rehearse with `--dry-run` first** (see §5) — same as any other live apply, validate before spending real submissions:
```powershell
$env:APPLYPILOT_V2_ENGINE = "1"
& $PY -m applypilot apply --limit 5 --model claude-haiku-4-5-20251001 --headless --no-live --dry-run --job-timeout 720
```

Then run for real exactly as usual (section 5) with the flag set — no other flags change (LIVE — real submissions):
```powershell
$env:APPLYPILOT_V2_ENGINE = "1"
& $PY -m applypilot apply --limit 10 --model claude-haiku-4-5-20251001 --headless --no-live --job-timeout 720 --max-transient-retries 1
```

### 11.3 Reading the cutover gate

```powershell
& $PY -m applypilot report --v2-cutover
```
Prints the normal `report` output plus a 3-leg go/no-go (spec §12) — **all three** must read GO before Greenhouse is permanently cut over to v2:

| Leg | Pass condition |
|---|---|
| §12.1 pass-rate | v2 `pass_rate` >= legacy `pass_rate`, on >=100 LIVE (non-dry-run) `v2_greenhouse` rows |
| §12.2 speed | p50 `duration_ms` over live v2 rows <= 45,000 ms |
| §12.3 safety audit | MANUAL — not derived from `review.jsonl`. The CLI has no flag to pass the audit result in yet, so this leg always prints `????` (not-yet-run) today |

§12.3's manual query trio (the command prints these when unresolved):
1. `submission_ledger.dangling_count() == 0` (reconcile any dangling `v2_crashed_post_submit` INTENT first)
2. no identity double-submitted: `confirmed_count_for_token(token, since) <= 1`
3. flight-recorder / canary provenance: zero canary-field writes, no submit-POST without an open broker ticket

**Do not cut Greenhouse over on pass-rate alone** — the report reads `HOLD` until all three legs are `GO`.

### 11.4 Promoting a flight-recorder bundle to a CI fixture

```powershell
& $PY -m applypilot fixtures promote <run> --out tests/fixtures/v2
```
- `<run>` is either a path to a flight-recorder bundle `.json`, or a bare stem resolved against `$env:APPLYPILOT_DIR\flight\<run>.json`.
- Writes `tests/fixtures/v2/<company>.html` (the real captured DOM) + `<company>.expected.json` (the semantic keys the front-end must recover) — replaces synthetic-only `set_content` tests with a real recorded form.
- The company name is sanitized before it becomes a filename (path-traversal / separator characters are stripped).
- `tests/test_v2_fixture_replay.py` SKIPS ("no promoted fixtures yet") until the first fixture lands in `tests/fixtures/v2/` — that is the 1 skip in the current 730-passed suite, not a failure.

**Gap to know about:** `flight_recorder.FlightRecorder` is a complete, tested module, but as of commit `5f79378` nothing in `orchestrator.run_form_compiler` constructs or calls it yet — no live v2 attempt currently writes a bundle to `$env:APPLYPILOT_DIR\flight\`. Until that wiring lands, there is nothing a real run has produced to promote from.

### 11.5 Known limitations (read before enabling on live jobs)

- **Resume binding gap.** At commit `5f79378` the resolver has no rung for the resume/file field, so on a live Greenhouse form the resume upload falls through every rung to the Oracle — which can never answer a file widget (options are always `[]`) — and parks. Because resume is (almost always) required, this trips the executor's required-completeness interlock and the WHOLE job parks as `needs_review:v2_incomplete_required` before any Submit happens (safe, but no live apply gets through). **A fix has landed as follow-up commit `9a632ae`** ("v2: resume binding — thread prologue-resolved resume_path into resolver") on `main`, ahead of this branch's `5f79378` baseline — confirm it's present on whatever branch you enable the flag on before trusting live A/B pass-rate numbers.
- **radio_group / checkbox / date widgets have no driver.** At commit `5f79378`, `drivers._REGISTRY` only covers `text`, `textarea`, `file`, `react_select`, `native_select`, `typeahead_location`, `phone_intl`. A required field whose widget is `radio_group`/`checkbox`/`date` fails to commit (`no_driver:<kind>`) and parks the form the same way the resume gap does. **Implemented in follow-up commit `85d00f4`** ("v2: radio_group/checkbox/date drivers — close Task 6 registry gap") on branch `p3-drivers-gap` — not yet merged into this branch's Phase 3 baseline (`5f79378`); confirm it has landed before relying on forms that use these widgets.
- **Enumerated custom questions always park.** Options are enumerated LAZILY (never at parse), so a required custom question rendered as a dropdown/react-select reaches the Oracle with `options=[]` and can never be indexed — deliberate park-don't-guess, deferred to Phase 4. Free-text custom questions ARE fully handled today.
- **Flight-recorder is not wired into the orchestrator** — see 11.4 above.
- **react-select fixed-sleep tax.** The promoted combobox drivers still carry `time.sleep()` calls from `prefill.py`: ~0.25s (1 call) when the portal-click path commits cleanly; up to ~1.4s total (5 calls) when it desyncs and falls back to the keyboard-driven path. This is NOT zero-sleep like the rest of the executor (invariant 9 covers the executor's own waits, not the promoted driver internals) — worth watching against the 45s p50 budget on EEO/screening-heavy forms.
