# Autonomous Apply Reliability Loop — Design Spec

| | |
|---|---|
| **Status** | Approved design — ready for implementation plan |
| **Date** | 2026-05-22 |
| **Owner** | adwai (operating on behalf of Nida Shah) |
| **Related** | [CONTEXT.md](../../../CONTEXT.md), [docs/ralph-iterations.md](../../ralph-iterations.md), [docs/OPERATOR_CHEATSHEET.md](../../OPERATOR_CHEATSHEET.md), [CLAUDE.md](../../../CLAUDE.md) |
| **Plugins used** | `ralph-loop` (sequential session re-firing), `superpowers` (brainstorming/writing-plans/executing-plans) |

## 1. Context & motivation

`docs/ralph-iterations.md` documents 13+ rounds of hand-driven reliability work on the apply pipeline: read failures from `review.jsonl` and transcripts → form hypothesis → patch a file → reset a small job set to `apply_status=NULL` → re-run in `--dry-run` → measure → repeat. This loop has driven dry-run pass-rate from ~33% to ~80% but is gated by an operator (adwai) being at the keyboard.

This spec automates that cycle, with two material differences from the existing process:

- **Live submissions, not dry-run.** Each iteration submits a real application. The "fixed-corpus, reset, retry" trick is gone — every successful apply consumes that job permanently. Diagnosis must work on a stream of one-shot evidence.
- **Wide-open editing.** The loop can edit any file in the repo, with a narrow set of carveouts (Section 6) needed to preserve the integrity of the exit condition itself.

The standing memory `feedback_collaboration.md` carries the rule "NEVER autonomously run live applypilot apply." This spec explicitly overrides that rule for the duration of the loop session. The override is recorded so future sessions know the policy is contextual, not permanent.

## 2. Goal & exit condition

**Goal:** drive the live apply pipeline to a state where it produces **5 consecutive verified `apply_status='applied'` submissions** without operator intervention. One failure (`needs_review:*` or `failed:*`) resets the streak to zero.

**Termination phrases** (`--completion-promise`-style; only the first is the success state):

| Phrase | Meaning |
|---|---|
| `STREAK_REACHED` | 5 in a row achieved. Loop's goal met. |
| `QUEUE_EXHAUSTED` | No fresh `score≥8` jobs available even after a discover refresh. |
| `SESSION_CONFLICT` | Another ralph-loop session is already writing this state file. |
| `MAX_ITERATIONS_REACHED` | `--max-iterations 200` hit without convergence (handled via summary block, not actual completion phrase emission). |

`--max-iterations 200` is the hard safety net. At ~3–5 min/iteration, that's ~10–17 hours of wall clock.

## 3. Architecture overview

```
            ┌─────────────────────────────────────────────────────────────┐
            │     ralph-loop Stop hook (re-fires this prompt forever)     │
            └─────────────────────────────┬───────────────────────────────┘
                                          │ same prompt every iteration
                                          ▼
        ┌──────────────────────────────────────────────────────────────────┐
        │  driver_prompt.md                                                │
        │  ──────────────────                                              │
        │  1. Read loop-state.json + recent review.jsonl rows              │
        │  2. Branch (A: rollback / B: count / C: refresh / D: patch /     │
        │     E: apply one job)                                            │
        │  3. Execute exactly one branch                                   │
        │  4. Write iter-NNNN.md, update loop-state.json, exit             │
        └──────────────────────────────────────────────────────────────────┘
                              │            │            │            │
                              ▼            ▼            ▼            ▼
                ┌───────────────┐  ┌───────────┐  ┌──────────┐  ┌──────────┐
                │ loop-state.   │  │ review.   │  │ visual_  │  │ skills/  │
                │   json        │  │  jsonl    │  │ trace/   │  │  *.yaml  │
                └───────────────┘  └───────────┘  └──────────┘  └──────────┘
                  (new, this spec)   (existing)    (existing)    (existing)
```

The loop is one long-running interactive Claude Code session in `E:\auto-apply-pipeline`. ralph-loop's Stop hook intercepts session-end and re-runs the same prompt. Iterations are strictly sequential (one Claude session at a time, by ralph-loop's design); everything that varies between iterations lives in files on disk.

## 4. State files

### 4.1 `E:\applypilot-data\loop-state.json` (new)

Single source of truth for cross-iteration memory. Atomic write (temp file + rename). Schema:

```json
{
  "schema_version": 1,
  "session_id": "<uuid4>",
  "pid": 12345,
  "started_at": "2026-05-22T14:00:00Z",
  "iteration": 47,

  "streak": 3,
  "last_processed_review_ts": "2026-05-22T15:23:11Z",

  "attempts": [
    {
      "url": "https://boards.greenhouse.io/figma/jobs/123",
      "status": "applied",
      "failure_class": null,
      "signature": null,
      "ts": "2026-05-22T15:23:11Z",
      "frames_dir": "logs/visual_trace_20260522_152011_w0_figma_frames",
      "transcript_path": "logs/claude_20260522_152011_w0_figma.txt",
      "verify_json": "logs/verify_20260522_152311_w0_figma.json"
    }
  ],

  "signature_counts": {
    "transient_timeout:prefill_goto:greenhouse.io": 4,
    "react_select_clobber:work_authorization:greenhouse": 2
  },

  "cooldown_remaining": 0,

  "last_patch": {
    "commit_hash": "abc1234",
    "files_touched": ["src/applypilot/apply/prefill.py"],
    "diff_hash": "sha256:...",
    "hypothesis": "pre-fill Page.goto timeout too short for slow Greenhouse loads",
    "expected_outcome": "transient_timeout:prefill_goto count drops to 0 over next 3 attempts",
    "applied_at": "2026-05-22T14:40:00Z",
    "attempts_since_patch": 2,
    "rolling_pass_rate_at_patch": 0.5
  },

  "patch_budgets": {
    "transient_timeout": 1,
    "validation_react_select": 2
  },

  "class_blocklist": [],

  "rollback_log": [
    {
      "commit_hash": "def5678",
      "files_touched": ["src/applypilot/apply/prompt.py"],
      "diff_hash": "sha256:...",
      "reason": "rolling_pass_rate_dropped:0.6_to_0.4",
      "reverted_at": "2026-05-22T13:30:00Z"
    }
  ]
}
```

- `attempts` is a rolling window of the last 10 entries (older are trimmed).
- `signature_counts` is monotonic across iterations (never decremented except on rollback of the patch that targeted that signature).
- `last_patch.diff_hash` + `rollback_log[].diff_hash` together form a **tabu list**: future patches MUST NOT have the same diff_hash as anything in `rollback_log`.
- `class_blocklist` entries are also monotonic per session (only added, never removed).

### 4.2 `E:\applypilot-data\logs\loop-iterations\iter-NNNN.md` (new, written per iteration)

Per-iteration narrative log. One file per iteration, zero-padded 4-digit counter. Lives in the data dir, not under `docs/`, because these are generated runtime artifacts (would bloat git history if committed). Sections:

```markdown
# Iteration NNNN — <ISO timestamp>

## State on entry
- streak: 3
- iteration: 47
- last_processed_review_ts: ...

## Branch taken
**D: DIAGNOSE & PATCH** (or A/B/C/E)

## Action
- Read transcript: logs/claude_*.txt
- Read verifier: logs/verify_*.json
- Read frames: logs/visual_trace_*_frames/{0001, 0042, 0083, 0124, last}.jpg
- Hypothesis: ...
- Patched files: ...
- Commit: abc1234
- Set cooldown: 3

## State on exit
- (updated values)
```

### 4.3 `docs/ralph-iterations.md` (existing, append-only from this loop)

The loop appends one short block per N iterations (configurable; default every 10) summarizing what happened in that block. Continuity with the existing hand-driven log style. The loop NEVER edits existing entries.

### 4.4 Existing artifacts the loop reads (no changes needed)

| Path | Source | Read by |
|---|---|---|
| `E:\applypilot-data\logs\review.jsonl` | launcher.py per-attempt | streak counter, signature seed |
| `logs\claude_<ts>_w<w>_<site>.txt` | launcher.py stream-json | diagnose phase |
| `logs\verify_<ts>_<...>.json` | verifier | diagnose phase, applied-confirmation |
| `logs\visual_trace_<...>_frames\frame_NNNN.jpg` + `manifest.jsonl` | visual_trace.py (always-on this loop) | diagnose phase visual evidence |
| `E:\applypilot-data\applypilot.db` | apply pipeline | queue runway probe |
| `E:\applypilot-data\skills\<company>.yaml` | recorder | skill-drift check |

`APPLYPILOT_VISUAL_TRACE=1` is exported in the loop's environment so every apply produces frames; no changes to `apply/visual_trace.py`.

## 5. Driver prompt — branching algorithm

The driver prompt is a fixed Markdown file at `src/applypilot/loop/driver_prompt.md`, passed to ralph-loop verbatim via `Get-Content -Raw`. Each iteration is the Claude session interpreting this prompt against the current state.

### 5.1 Entry sequence (every iteration, before any branch)

1. Load `loop-state.json` via `applypilot.loop.state.load_or_init()`. If missing, initialize with `session_id`, `pid`, `started_at`, `iteration=1`, `streak=0`, empty collections.
2. **Session-lock check.** If `state.session_id` != current session's id AND `state.pid` is still alive: emit `<promise>SESSION_CONFLICT</promise>` and stop. (No writes; the other session owns the file.)
3. **Max-iterations early-warning.** If `state.iteration >= 195`, this iteration writes a final summary block to `docs/ralph-iterations.md` *before* any other action. Body covers: streak at exit, signature_counts top-5, patches landed vs reverted, class_blocklist, suggested next angles for a follow-up session.
4. **Chrome cleanup.** Call `applypilot.apply.chrome.kill_all_chrome()` if no apply attempt is flagged in-progress in state.
5. **Trace GC.** Delete `logs/visual_trace_*_frames/` for any attempt not in the current rolling window AND not referenced by `last_patch` or `rollback_log`.
6. **Exit short-circuit.** If `state.streak >= 5`: emit `<promise>STREAK_REACHED</promise>` and stop.

### 5.2 Branches (exactly one runs per iteration)

**Branch A — rollback check.** Runs if `state.last_patch` is set AND `last_patch.attempts_since_patch >= 3`.

1. Compute current rolling pass-rate over `state.attempts` (last 10).
2. If `rolling_pass_rate < last_patch.rolling_pass_rate_at_patch`: rollback.
   - `git revert HEAD --no-edit` then commit (loop's identity).
   - Append entry to `state.rollback_log` with `commit_hash`, `files_touched`, `diff_hash`, `reason`.
   - Clear `state.last_patch`. Reset `state.cooldown_remaining = 0`.
   - Subtract 1 from the signature counter the patch had targeted (since that patch's "evidence" is no longer in effect).
3. Else: clear `state.last_patch` (patch graduates as kept).
4. Write `iter-NNNN.md`, save state, exit.

**Branch B — process unprocessed review rows.** Runs first if there are review.jsonl rows newer than `state.last_processed_review_ts`. Pure bookkeeping; produces no side effects beyond state file.

For each new row in order:
- If `apply_status == 'applied'`: `streak += 1`, `signature = null`.
- Else: `streak = 0`, compute `signature = signature_extractor.extract(row, transcript, verify_json)`, `signature_counts[signature] += 1`.
- Append to `state.attempts` (trim to 10).
- Update `state.last_processed_review_ts`.

After processing, if `state.streak >= 5`: emit `<promise>STREAK_REACHED</promise>` and stop.

**Branch C — queue refresh.** Runs if `applypilot status` reports <1 fresh `score≥8` job with `apply_status IS NULL`.

1. Run `applypilot run discover enrich score --quick --target-ready 5` (subprocess, captured output).
2. If still <1 fresh job after refresh: emit `<promise>QUEUE_EXHAUSTED</promise>` (after appending the summary block to `docs/ralph-iterations.md`).
3. Else: write `iter-NNNN.md`, save state, exit.

**Branch D — diagnose & patch.** Runs only if the last entry in `state.attempts` is a failure AND ALL of:
- `failure_class` is (A)-removable (per `applypilot.reporting._IRREDUCIBLE_MARKERS` taxonomy, inverted)
- `failure_class` not in `class_blocklist`
- `signature_counts[signature] >= 3`
- `cooldown_remaining == 0`
- `patch_budgets.get(failure_class, 0) < 5`

Eligibility-fail handling:
- If `cooldown_remaining > 0`: `cooldown_remaining -= 1`, save state, fall through to Branch E.
- If `patch_budgets[failure_class] >= 5`: add `failure_class` to `class_blocklist`, fall through to Branch E.
- Otherwise (e.g. signature_counts < 3): fall through to Branch E.

Eligible action:
1. Read `transcript_path` (full), `verify_json` (full), and 5 selected frames from `frames_dir`:
   - `frame_0001.jpg` (initial page state)
   - Last frame written
   - 3 frames adjacent to the MCP action immediately preceding the failure (selected via `manifest.jsonl`)
2. Optionally consult `skills/<company>.yaml` if one exists for this failure's `site`.
3. Form a hypothesis. **Tabu-list check before patching:** if any entry in `rollback_log` touched the same `files_touched` set AND addressed the same `signature` (recorded in the rollback entry), abandon this angle, set `cooldown_remaining = 1`, fall through to E. The check is conservative — same file-set + same signature = "this approach was tried and reverted." Different angles on the same file are allowed.
4. Edit code via `Edit` / `Write`. Respect carveouts (Section 6) and the function-immutability AST check in `applypilot.loop.safety.is_immutable_path`.
5. Compute `diff_hash = sha256(git diff HEAD)`. (Used for human auditability of `rollback_log` entries, not for the tabu-list check.)
6. `git add` the changed files, commit with structured message:
   ```
   loop iter N: <one-line hypothesis>

   Signature: <signature>
   Class: <failure_class>
   Patch budget: <new value>/5
   Expected outcome: <prediction>
   ```
7. Write to `state.last_patch`: commit_hash, files_touched, diff_hash, hypothesis, expected_outcome, applied_at, attempts_since_patch=0, rolling_pass_rate_at_patch (snapshot now).
8. `cooldown_remaining = 3`. `patch_budgets[failure_class] += 1`.
9. Write `iter-NNNN.md`, save state, exit.

**Branch E — run one apply (default).**

The subprocess + brick-detection is owned by `applypilot.loop.runner.apply_one()` so the prompt stays declarative. The helper:

1. Confirms `APPLYPILOT_VISUAL_TRACE=1` is set (else sets it).
2. Runs `applypilot apply --workers 1 --limit 1 --model claude-haiku-4-5-20251001 --headless --no-live --job-timeout 720 --max-transient-retries 1` as a subprocess. Wall-clock-stamps launch.
3. **Brick-detection sentinel.** If the subprocess exits non-zero **within 10 seconds** of launch (before any real apply work could possibly complete) AND the last commit's message begins with `loop iter N:`:
   - `git revert HEAD --no-edit` and commit.
   - Append to `rollback_log` with `reason="brick_detected"`.
   - Clear `last_patch`. Reset `cooldown_remaining = 0`.
   - Return without counting an attempt against the streak.
4. Otherwise wait for normal completion (apply's internal `--job-timeout 720` is the upper bound).
5. Return the new `review.jsonl` row (Branch B in the *next* iteration counts it).

After `apply_one()` returns, the prompt:
6. If `last_patch` is set, increments `last_patch.attempts_since_patch`.
7. Writes `iter-NNNN.md`, saves state, exits.

**Provider-outage guard (overlays all branches).** If the last 3 entries in `state.attempts` all have `failure_class` in `{cli_error, subprocess_error, anthropic_outage, rate_limited}`, this iteration sleeps 10 minutes and exits without doing anything else. Subsequent iterations re-check.

## 6. Loop-immutable carveouts

These paths the loop MUST NOT edit, even though "wide-open" scope is the user-approved default. Enforced by a pre-edit check in the driver prompt: before any `Edit`/`Write` call, the prompt verifies the target path against this list and aborts the patch if it matches.

| Path | Why immutable |
|---|---|
| `src/applypilot/loop/**` | The loop's own machinery (signature extractor, state writer, driver prompt). The grader can't grade itself. |
| `src/applypilot/apply/launcher.py` — function `_verify_submission_success` | Defines what "applied" means. Streak integrity depends on this. |
| `src/applypilot/apply/launcher.py` — code paths that write `apply_status='applied'` and `applied_at` to the DB | Same reason. |
| `src/applypilot/reporting.py` — `_IRREDUCIBLE_MARKERS` and the (A)/(B) classification | Defines failure-class taxonomy the loop uses for routing. |
| `tests/**` | Tests are the spec, not the target. Loop must not delete or weaken tests to "pass" them. |
| `profile.json`, `resume.pdf`, `.env`, `.git/config` | Nida's identity and credentials. Editing these risks submitting bad data to real employers. |
| `docs/ralph-iterations.md` — existing entries | Loop appends new entries only; never edits past ones. |
| `CLAUDE.md`, `CONTEXT.md` | State documentation, not pipeline code. |
| `docs/superpowers/specs/2026-05-22-autonomous-apply-reliability-loop-design.md` (this file) | Same. |

Function-level carveouts (within editable files) are enforced by AST check in the pre-edit hook: the loop computes the AST diff of any patch to `launcher.py` and rejects writes that touch the immutable functions.

## 7. Mitigations against thrashing and reward-hacking

Locked in via the design above; restated for clarity:

1. **Failure-class triage** — (B)-irreducible classes skipped from Branch D regardless of count.
2. **Signature accumulation** — `signature_counts ≥ 3` gate before Branch D fires.
3. **Hypothesis ledger** — `last_patch.hypothesis` + `expected_outcome` written before the patch; graded after 3 attempts.
4. **Auto-rollback on regression** — rolling pass-rate drop after a patch triggers `git revert`.
5. **Anti-thrash cooldown** — `cooldown_remaining = 3` blocks Branch D after every patch.
6. **Per-class patch budget** — 5 patches per failure class, then the class is added to `class_blocklist`.
7. **Tabu list (diff hash)** — patches whose `diff_hash` matches anything in `rollback_log` are rejected.
8. **Brick-detection sentinel** — sub-10s subprocess failure auto-reverts the last patch.
9. **Provider-outage guard** — 3 consecutive `cli_error`-class failures pause the loop for 10 min.

## 8. Safety rails — terminal states

| Trigger | State emitted | What's written |
|---|---|---|
| `streak == 5` | `<promise>STREAK_REACHED</promise>` | Final summary block in `docs/ralph-iterations.md` + closing `iter-NNNN.md`. |
| Branch C refresh returned 0 fresh jobs | `<promise>QUEUE_EXHAUSTED</promise>` | Same. |
| `state.session_id` mismatch + foreign pid alive | `<promise>SESSION_CONFLICT</promise>` | Nothing written (refuses to corrupt state). |
| `iteration >= 195` reached | Summary block written; loop continues to 200; ralph-loop's `--max-iterations` then halts the session naturally. | The summary documents what to do next. |

## 9. Operational lifecycle

### 9.1 Pre-flight (operator runs once)

```powershell
cd E:\auto-apply-pipeline
applypilot doctor              # confirm Tier 3
applypilot status              # confirm queue has fresh score>=8 jobs
git status                     # confirm clean working tree
Remove-Item E:\applypilot-data\loop-state.json -ErrorAction SilentlyContinue   # only if starting fresh, NOT for resume
```

### 9.2 Start (operator runs once per session)

In an interactive Claude Code session opened in `E:\auto-apply-pipeline`:

```
/ralph-loop "$(Get-Content E:\auto-apply-pipeline\src\applypilot\loop\driver_prompt.md -Raw)" --max-iterations 200 --completion-promise "STREAK_REACHED"
```

PowerShell command substitution loads the (long) driver prompt from the file at invocation time. ralph-loop fires the first iteration immediately.

### 9.3 Monitor (any time, parallel terminal)

```powershell
Get-Content E:\applypilot-data\logs\review.jsonl -Tail 5 -Wait
Get-Content E:\applypilot-data\loop-state.json -Raw | ConvertFrom-Json |
    Format-List streak, iteration, last_patch, class_blocklist, cooldown_remaining
Get-Content docs\ralph-iterations.md -Tail 30
Get-ChildItem E:\applypilot-data\logs\loop-iterations\ | Sort-Object LastWriteTime | Select-Object -Last 5
```

The single signal that matters is `streak` in `loop-state.json`.

### 9.4 Stop

- Clean: `/cancel-ralph` inside the Claude Code session — the current iteration's exit becomes the final exit; state preserved for later resume.
- Hard: Ctrl+C — same outcome; state is whatever the last successful iteration wrote.

### 9.5 Resume after stop

Same start command. The driver prompt detects existing `loop-state.json`, verifies its `session_id` is dead (no living process owns it), and continues with the existing counters.

## 10. Implementation surface

### 10.1 New files (the loop's machinery — these are loop-immutable carveouts)

| Path | Purpose | Approximate size |
|---|---|---|
| `src/applypilot/loop/__init__.py` | Package marker | 1 line |
| `src/applypilot/loop/state.py` | `load_or_init()`, `save_atomic()`, `acquire_session_lock()`, `rolling_pass_rate()`, schema validation. | ~250 LOC |
| `src/applypilot/loop/signature_extractor.py` | Pure function `extract(review_row, transcript_path, verify_json) -> str`. Deterministic mapping from failure evidence to a signature. | ~200 LOC |
| `src/applypilot/loop/driver_prompt.md` | The fixed prompt ralph-loop fires. ~600 lines of structured instructions. | ~600 lines |
| `src/applypilot/loop/safety.py` | `brick_detected()`, `provider_outage_detected()`, `is_immutable_path()`, AST function-immutability check. | ~150 LOC |
| `src/applypilot/loop/runner.py` | Thin Python helper invoked by the driver prompt: subprocess wrapper around `applypilot apply` with brick-detection timing. Mostly a process-management helper. | ~100 LOC |
| `tests/test_loop_state.py` | Round-trip JSON, session lock, atomic write under concurrent writes, schema migration. | ~150 LOC |
| `tests/test_signature_extractor.py` | Each failure class → expected signature, with fixture transcripts/verify JSONs. | ~200 LOC |
| `tests/test_loop_safety.py` | brick detection, immutable-path check, AST immutability check. | ~100 LOC |

### 10.2 Existing files this spec relies on (no edits)

- `src/applypilot/apply/visual_trace.py` — already produces what we need.
- `src/applypilot/apply/launcher.py` — already writes per-attempt artifacts.
- `src/applypilot/reporting.py` — `_IRREDUCIBLE_MARKERS` is the source of truth for (A) vs (B) classification.
- `src/applypilot/database.py` — read-only queries for queue-runway probes.

### 10.3 Existing CLI commands the loop subprocess-invokes

- `applypilot apply --workers 1 --limit 1 --model claude-haiku-4-5-20251001 --headless --no-live --job-timeout 720 --max-transient-retries 1`
- `applypilot run discover enrich score --quick --target-ready 5`
- `applypilot status` (queue-runway probe via parsed output OR direct DB query — implementation may pick whichever is cleaner)

## 11. Out of scope

Explicitly NOT part of this spec, to be considered separately if needed:

- **Closed-loop visual feedback to the apply agent.** Earlier brainstorming considered feeding screenshots back to Sonnet/Haiku during the apply run; rejected. The recording layer remains passive.
- **Parallel ralph-loops.** Session-lock detects accidental concurrency; intentional parallel optimization is not designed for.
- **Cost monitoring.** User is on Claude Max 5x flat-rate; no per-iteration cost cap.
- **Remote/cloud scheduling.** The loop runs in this local Claude Code session only. `/schedule` and cloud routines are out of scope.
- **Tailoring re-enablement.** The current loop optimizes the apply path with the master `resume.pdf`. Re-introducing per-job tailoring is a separate project.
- **New ATS adapters (Lever, Ashby pre-fill).** The loop may produce patches that touch existing adapters; greenfield-adapter design is separate.

## 12. Open questions

1. **Queue-runway probe — DB or CLI?** `applypilot status` parses, but a direct `SELECT COUNT(*) FROM jobs WHERE fit_score >= 8 AND apply_status IS NULL AND discovered_at > ...` is cleaner. Implementation picks one; both are acceptable.
2. **Signature collisions across companies.** A signature like `react_select_clobber:work_authorization` might fire across Greenhouse and Lever with subtly different root causes. The signature extractor includes a site-suffix to reduce this, but cross-company root causes can't be merged into a single fix. Acceptable — the loop will simply patch each separately, and the budget per class limits the cost.
3. **Memory update for live-apply override.** Done as a separate step after the spec is approved: `feedback_collaboration.md` gets a note that live-apply autonomy is permitted under explicit ralph-loop sessions.

## 13. Acceptance criteria for the implementation phase

- `applypilot.loop.state` round-trips JSON, validates schema_version, acquires/releases session lock, performs atomic writes.
- `applypilot.loop.signature_extractor` is deterministic on fixture inputs (same input → same signature, 100% of test cases).
- `applypilot.loop.safety.brick_detected` returns True when given a subprocess that exited non-zero within 10s, False otherwise.
- `applypilot.loop.safety.is_immutable_path` correctly identifies all carveouts in Section 6, including function-level checks for `_verify_submission_success`.
- `driver_prompt.md` exists, references real file paths, and conforms to ralph-loop's prompt format.
- All new tests pass: `pytest tests/test_loop_*.py -v`.
- Ruff clean: `ruff check src/applypilot/loop/`.

End-to-end run-through (executing the loop once and watching the first iteration land an apply attempt + write iter-0001.md) is part of post-implementation validation, not the implementation phase itself.
