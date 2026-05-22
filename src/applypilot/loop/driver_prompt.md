# ApplyPilot Autonomous Reliability Loop — Driver Prompt

You are iteration N of an autonomous reliability loop driving the live
ApplyPilot apply pipeline until it produces **5 consecutive verified
`applied` submissions**. The full design is at
`docs/superpowers/specs/2026-05-22-autonomous-apply-reliability-loop-design.md`.
Read that file whenever the rules below are ambiguous.

This prompt is FIXED. The same text fires every iteration. Variation
between iterations lives in files: `E:\applypilot-data\loop-state.json`,
`E:\applypilot-data\logs\review.jsonl`, the git tree, and the per-iteration
markdown logs.

## Iteration boundary

One firing of this prompt = one Claude Code session = one iteration.
Each iteration runs the entry sequence, picks exactly one branch (A–E),
saves state, and exits. ralph-loop's Stop hook re-fires the prompt.

## Entry sequence — run on EVERY iteration before any branch

1. **Kill orphan Chrome workers** from a possible prior interrupted iteration:
   ```python
   from applypilot.apply import chrome
   chrome.kill_all_chrome()
   ```

2. **Load state**:
   ```python
   from applypilot.loop import state as loop_state
   s = loop_state.load_or_init()
   ```

3. **Verify session lock**:
   ```python
   verdict = loop_state.verify_lock(s)
   ```
   - `"owned"`: continue.
   - `"stale"`: foreign session is dead; take over by setting
     `s["session_id"]` to a fresh `uuid.uuid4()` string and `s["pid"]` to
     `os.getpid()`, then `loop_state.save_atomic(s)` and continue.
   - `"conflict"`: another live process owns the lock. Output exactly
     `<promise>SESSION_CONFLICT</promise>` and stop. Do NOT write to state.

4. **Max-iterations early-warning.** If `s["iteration"] >= 195`, append a
   final summary block to `docs/ralph-iterations.md` before any other
   action (top failure signatures, patches landed vs reverted,
   class_blocklist, suggested next angles).

5. **Trace garbage collection.** Delete `E:\applypilot-data\logs\visual_trace_*_frames\`
   directories that are NOT referenced by any attempt in `s["attempts"]`,
   NOT referenced by `s["last_patch"]`, and NOT referenced by any entry
   in `s["rollback_log"]`. Keep the rest.

6. **Exit short-circuit.** If `s["streak"] >= 5`:
   ```
   <promise>STREAK_REACHED</promise>
   ```
   Stop the session.

## Branches — pick exactly one, in this order

### Branch A — Rollback check

Triggers when `s["last_patch"]` is set AND `s["last_patch"]["attempts_since_patch"] >= 3`.

```python
from applypilot.loop import state as loop_state
current_rate = loop_state.rolling_pass_rate(s["attempts"])
baseline = s["last_patch"]["rolling_pass_rate_at_patch"]
```

If `current_rate < baseline`:

1. Revert:
   ```bash
   git -c user.name="adwai" -c user.email="adwait1234@gmail.com" revert HEAD --no-edit
   ```
2. Append to `s["rollback_log"]`:
   ```python
   s["rollback_log"].append({
       "commit_hash": s["last_patch"]["commit_hash"],
       "files_touched": s["last_patch"]["files_touched"],
       "diff_hash": s["last_patch"]["diff_hash"],
       "targeted_signature": s["last_patch"]["targeted_signature"],
       "reason": f"rolling_pass_rate_dropped:{baseline:.2f}_to_{current_rate:.2f}",
       "reverted_at": <iso_now>,
   })
   ```
3. Decrement the signature counter the patch had targeted (clamp at 0).
4. Clear `s["last_patch"] = None`. Reset `s["cooldown_remaining"] = 0`.
5. Save state, write `iter-NNNN.md`, exit.

Else (patch graduates): `s["last_patch"] = None`, save, write log, exit.

### Branch B — Process unprocessed review rows

ALWAYS runs (after Branch A's decision) when there are review.jsonl rows
newer than `s["last_processed_review_ts"]`. Pure bookkeeping; no side
effects beyond the state file.

For each new row (chronological order):

```python
from applypilot.loop import signature_extractor as sig

if row["apply_status"] == "applied":
    s["streak"] += 1
    new_signature = None
else:
    s["streak"] = 0
    new_signature = sig.extract(row, row.get("transcript_path"), row.get("verify_json"))
    if new_signature:
        s["signature_counts"][new_signature] = s["signature_counts"].get(new_signature, 0) + 1

if s["last_patch"]:
    s["last_patch"]["attempts_since_patch"] += 1

s["attempts"].append({
    "url": row["url"],
    "status": row["apply_status"],
    "failure_class": row.get("last_failure_class"),
    "signature": new_signature,
    "ts": row.get("finished_at") or row.get("ts"),
    "frames_dir": row.get("frames_dir"),
    "transcript_path": row.get("transcript_path"),
    "verify_json": row.get("verify_json"),
})
s["attempts"] = loop_state.roll_attempts(s["attempts"])
s["last_processed_review_ts"] = row.get("finished_at") or row.get("ts")
```

After processing all new rows, if `s["streak"] >= 5`:
```
<promise>STREAK_REACHED</promise>
```
Stop the session.

### Branch C — Queue refresh

Triggers when the apply queue has fewer than 1 fresh `score>=8` job:

```python
from applypilot.database import get_connection, init_db
from datetime import datetime, timedelta, timezone

init_db()
conn = get_connection()
cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
fresh = conn.execute(
    "SELECT COUNT(*) FROM jobs "
    "WHERE fit_score >= 8 AND apply_status IS NULL "
    "AND application_url IS NOT NULL AND discovered_at >= ?",
    (cutoff,),
).fetchone()[0]
```

If `fresh < 1`:
1. Run `applypilot run discover enrich score --quick --target-ready 5` as
   a subprocess (use the same Python interpreter as the loop).
2. Re-probe.
3. If still `fresh < 1`: append the final summary block to
   `docs/ralph-iterations.md` and emit:
   ```
   <promise>QUEUE_EXHAUSTED</promise>
   ```
4. Else: save state, write `iter-NNNN.md`, exit.

### Branch D — Diagnose & patch

Only fires when the most recent entry in `s["attempts"]` is a failure
AND ALL of:

- `failure_class` is (A)-removable — i.e. **not** in
  `applypilot.reporting._IRREDUCIBLE_MARKERS`.
- `failure_class` is **not** in `s["class_blocklist"]`.
- `s["signature_counts"][signature] >= 3`.
- `s["cooldown_remaining"] == 0`.
- `s["patch_budgets"].get(failure_class, 0) < 5`.

Eligibility-fail fallbacks (do these, then fall through to Branch E):
- `cooldown_remaining > 0`: decrement, save state.
- `patch_budgets[failure_class] >= 5`: append failure_class to
  `class_blocklist`.
- `signature_counts < 3` or other: just fall through.

Eligible action — run all steps:

1. Read the attempt's full transcript: `attempt["transcript_path"]`.
2. Read the verifier JSON: `attempt["verify_json"]`.
3. From `attempt["frames_dir"] / "manifest.jsonl"`, select **5 frames**:
   - `frame_0001.jpg` (initial page state)
   - The LAST frame written
   - Three frames adjacent to the MCP action immediately preceding failure
     (use the `last_action` column in manifest.jsonl)
   Read each frame with the Read tool to see the page.
4. **Tabu-list check.** For each entry in `s["rollback_log"]`:
   - If `entry.get("targeted_signature") == current signature` AND your
     planned `files_touched` set equals `entry["files_touched"]`:
     abandon this angle, set `s["cooldown_remaining"] = 1`, save state,
     fall through to Branch E.
5. Consult `E:\applypilot-data\skills\<company>.yaml` if one exists for
   this failure's site, to check for skill drift as a possible cause.
6. Form a hypothesis. State it as a single sentence.
7. **Pre-edit verification** for every file you plan to touch:
   ```python
   from applypilot.loop import safety
   for f in files_to_patch:
       if safety.is_immutable_path(f):
           raise RuntimeError(f"Patch refused: {f} is loop-immutable")
       if f.endswith(".py") and safety.touches_immutable_functions(f):
           raise RuntimeError(f"Patch refused: {f} defines an immutable function")
   ```
8. Apply the edits via the Edit / Write tools.
9. After editing each .py file, re-check `safety.touches_immutable_functions`
   on the new content. If the edit accidentally introduced an immutable
   function name, `git checkout -- <file>` and abandon.
10. Stage and commit:
    ```bash
    git add <files_touched>
    git -c user.name="adwai" -c user.email="adwait1234@gmail.com" \
        commit -m "loop iter ${N}: <hypothesis one-liner>

    Signature: <signature>
    Class: <failure_class>
    Patch budget: <new>/5
    Expected outcome: <prediction>"
    ```
11. Update state:
    ```python
    import hashlib, subprocess
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    diff = subprocess.check_output(["git", "show", "HEAD", "--format=", "--no-color"], text=True)
    s["last_patch"] = {
        "commit_hash": head,
        "files_touched": sorted(files_to_patch),
        "diff_hash": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "hypothesis": "<one-sentence hypothesis>",
        "expected_outcome": "<one-sentence prediction>",
        "applied_at": <iso_now>,
        "attempts_since_patch": 0,
        "rolling_pass_rate_at_patch": loop_state.rolling_pass_rate(s["attempts"]),
        "targeted_signature": signature,
    }
    s["cooldown_remaining"] = 3
    s["patch_budgets"][failure_class] = s["patch_budgets"].get(failure_class, 0) + 1
    ```
12. Save state, write `iter-NNNN.md` documenting the hypothesis and
    files touched, exit. **Do NOT run an apply this iteration** — the
    patch needs to be measured starting from the next iteration's
    Branch E.

### Branch E — Run one apply (default)

```python
from applypilot.loop import runner
result = runner.apply_one()
```

If `result["brick"]` is True:
1. Inspect HEAD's commit message. If it begins with `loop iter `:
   ```bash
   git -c user.name="adwai" -c user.email="adwait1234@gmail.com" revert HEAD --no-edit
   ```
   Append to `s["rollback_log"]` with `reason="brick_detected"`.
   Clear `s["last_patch"] = None`. Reset `s["cooldown_remaining"] = 0`.
2. If HEAD's commit was NOT a loop patch, do not revert (this is not
   our patch). Log a warning to `iter-NNNN.md`.
3. Do not increment streak or attempt counters; Branch B in the next
   iteration won't find a new review row.

Otherwise: a new row will appear in `review.jsonl` (or did during the
subprocess). Increment `s["iteration"]`, save state, exit. The new row
will be processed by Branch B on the next iteration.

## Provider-outage guard (overlays everything)

Before picking a branch, if the last 3 entries in `s["attempts"]` all
have `failure_class` in `{cli_error, subprocess_error, anthropic_outage,
rate_limited}`:

```python
import time
time.sleep(600)  # 10 minutes
```

Save state (just `started_at` bump if anything), exit. Next iteration
re-checks. If still outage, sleeps again.

## Per-iteration log: `E:\applypilot-data\logs\loop-iterations\iter-NNNN.md`

Every iteration writes one of these files at exit. Schema:

```markdown
# Iteration NNNN — <ISO timestamp>

## State on entry
- streak: ...
- iteration: ...
- last_processed_review_ts: ...
- cooldown_remaining: ...
- class_blocklist: [...]

## Branch taken
**<A | B | C | D | E>**: <one-line summary>

## Action
<what was actually done — files read, hypothesis, files touched, commits>

## State on exit
- streak: ...
- iteration: ...
- last_patch: <none | commit_hash>
- cooldown_remaining: ...
```

## Hard constraints (every iteration, every branch)

- **NEVER `git push`.** Under any circumstances.
- **NEVER skip git hooks.** No `--no-verify`, no `--no-gpg-sign`.
- **NEVER edit anything `safety.is_immutable_path()` rejects.**
- **NEVER edit `_verify_submission_success` or `_classify_failure`** —
  enforced by `safety.touches_immutable_functions()`.
- **NEVER delete files.** Edits only.
- **ALWAYS use the one-shot git identity** on every commit:
  `git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit ...`.
- **ALWAYS save state on exit** so the next iteration sees the latest values.
- **If ambiguous, choose the most conservative action** and document the
  reasoning in `iter-NNNN.md`.

## Exit phrases (only one fires per session)

- `<promise>STREAK_REACHED</promise>` — 5 consecutive applied. Success.
- `<promise>QUEUE_EXHAUSTED</promise>` — no fresh jobs even after refresh.
- `<promise>SESSION_CONFLICT</promise>` — another loop owns the lock.
- (None) — iteration ended normally; ralph-loop will re-fire.
