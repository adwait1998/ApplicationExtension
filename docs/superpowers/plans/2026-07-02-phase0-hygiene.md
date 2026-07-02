# Phase 0 — Hygiene (Green Baseline) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A green, fully-committed, PII-clean baseline with CI on push — so all Phase 1+ A/B telemetry is trustworthy.

**Architecture:** No new code. Fix the 3 red adapter tests (fixture bug), commit the dirty working tree in coherent slices, harden .gitignore, purge PII strays, scrub a poisoned answer-bank entry, and enable CI on push with a test-gated PyPI publish.

**Tech Stack:** pytest, git, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-07-02-applypilot-v2-design.md` §13 Phase 0.

**Conventions for all tasks:**
- Repo root: `e:\auto-apply-pipeline`. Run all commands from there.
- `PY` = `C:\Users\adwai\AppData\Local\Programs\Python\Python312\python.exe` (the interpreter with pytest + editable applypilot; the `.venv` python does NOT have pytest).
- Commit with one-shot identity, never push: `git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "..."`.
- **The index may contain pre-staged files.** Before EVERY commit run `git diff --cached --stat` and verify only the intended files are staged. If unexpected files are staged, `git reset` first, then re-add only what the task lists.

---

### Task 1: Fix the 3 red greenhouse-adapter tests (fixture fix, not production fix)

The uncommitted "tighten success criteria" edit to `submit_greenhouse()` is CORRECT production code: it returns `submitted=False` when the Submit button is still visible and enabled ~6s after the click (real Greenhouse removes/replaces the form on success). The test fixture is what's wrong — its synthetic submit button only reveals `#done` and stays interactive forever, which is exactly the not-submitted signature the new check catches.

**Files:**
- Modify: `tests/test_greenhouse_adapter.py:92-93`
- (No production files change in this task)

- [ ] **Step 1: Confirm the 3 failures exist**

Run: `& $PY -m pytest tests/test_greenhouse_adapter.py -v`
Expected: 3 FAILED — `test_adapter_fills_pristine_form`, `test_adapter_self_heals_after_id_class_churn`, `test_auto_submit_only_when_fully_resolved` — each with `submit_unconfirmed: form still interactive after click`; the rest pass.

- [ ] **Step 2: Fix the synthetic form's click handler**

In `tests/test_greenhouse_adapter.py` lines 92-93, change:

```javascript
  document.getElementById('sub').addEventListener('click',()=>{
    document.getElementById('done').style.display='block';});
```

to:

```javascript
  document.getElementById('sub').addEventListener('click',()=>{
    document.getElementById('done').style.display='block';
    document.getElementById('sub').remove();});
```

Remove ONLY the button — do not hide the form: `#done` sits inside the form and lines 168/177/256 assert `page.locator("#done").is_visible()`. Playwright's `is_visible()` on the detached button returns False, so `_post_submit_verdict` hits `button_gone → True` on the first 0.25s poll (no 6s stall added to the suite). `test_adapter_self_heals_after_id_class_churn` shares `_FORM` via `_churn()` (which never renames `id="sub"`), so this one edit fixes all three tests.

- [ ] **Step 3: Run the adapter + submit-confirmation tests**

Run: `& $PY -m pytest tests/test_greenhouse_adapter.py tests/test_submit_confirmation.py -v`
Expected: ALL PASS (including the new uncommitted `test_auto_submit_blocks_when_standard_required_combobox_still_missing`).

- [ ] **Step 4: Commit the adapter tightening + fixture fix together**

```powershell
git reset   # clear any pre-staged ralph-loop files first
git add src/applypilot/apply/adapters/greenhouse.py tests/test_greenhouse_adapter.py tests/test_submit_confirmation.py
git diff --cached --stat   # MUST show only these 3 files
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "adapter: confirm submit post-click (button-gone/disabled), fix test fixture to model real GH success"
```

---

### Task 2: Full-suite green checkpoint, then commit the remaining working-tree WIP

The tree has ~30 more modified tracked files (ralph-loop iteration work: prompt guards, prefill, skill runner, config, CLI, tests) plus ~31 untracked source/test files that belong in the repo (webui, stream modules, discovery modules, 19 test files).

**Files:**
- Modify (commit as-is, no edits): all remaining `M` files from `git status --short` EXCEPT any file in Task 3's PII list
- Add: `src/applypilot/webui/`, `src/applypilot/apply/browser_stream.py`, `src/applypilot/apply/stream_executor.py`, `src/applypilot/apply/stream_mcp_server.py`, `src/applypilot/apply/visual_trace.py`, `src/applypilot/discovery/ats_discovery.py`, `src/applypilot/discovery/freshness.py`, `src/applypilot/discovery/theirstack.py`, `src/applypilot/discovery/title_filter.py`, `src/applypilot/enrichment/linkedin_cua_probe.py`, `src/applypilot/enrichment/linkedin_outbound.py`, `src/applypilot/freshness.py`, `src/applypilot/config/ats_seed_companies.yaml`, `scripts/refresh_and_validate.ps1`, and all 19 untracked `tests/test_*.py`

- [ ] **Step 1: Run the FULL suite including untracked tests**

Run: `& $PY -m pytest tests/ -v --tb=short`
Expected: ALL PASS (~470+ tests; 453 previously collected + 19 new files' tests). **If anything fails: STOP, report the failure to the user, do not commit.** (Exception: if a failure is clearly environment-only — e.g. a missing optional dependency on this machine — note it and continue.)

- [ ] **Step 2: Stage the modified tracked files + the should-commit untracked files**

```powershell
git add -u   # all modified tracked files (PII files are gitignored, -u can't add untracked)
git add src/applypilot/webui src/applypilot/apply/browser_stream.py src/applypilot/apply/stream_executor.py src/applypilot/apply/stream_mcp_server.py src/applypilot/apply/visual_trace.py src/applypilot/discovery/ats_discovery.py src/applypilot/discovery/freshness.py src/applypilot/discovery/theirstack.py src/applypilot/discovery/title_filter.py src/applypilot/enrichment/linkedin_cua_probe.py src/applypilot/enrichment/linkedin_outbound.py src/applypilot/freshness.py src/applypilot/config/ats_seed_companies.yaml scripts/refresh_and_validate.ps1 tests/
git diff --cached --stat
```

Verify the staged list contains NO file from Task 3's PII/stray list (`answer_bank.json`, `searches.yaml`, `loop-state.json`, `feature.md`, `ats_companies.yaml` at repo root, `resume.pdf.bak.*`, `.tmp-applypilot-cli/`, `.gh_validation.sh`, `applypilot.db-wal`, `.fuse_hidden*`). If any is staged, `git reset <file>` it.

- [ ] **Step 3: Commit**

```powershell
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "checkpoint: commit loop-iteration WIP + webui/stream/discovery modules + 19 test files

Working tree had accumulated uncommitted reliability work (submit guards,
prompt hardening, stream MCP server, web UI). Committing as the Phase 0
green baseline for ApplyPilot v2 (spec 2026-07-02)."
```

---

### Task 3: PII purge + .gitignore hardening + answer-bank scrub

**Files:**
- Modify: `.gitignore`
- Delete: `resume.pdf.bak.20260514_125304`, `.fuse_hidden0000000f00000001`, `.gh_validation.sh`, `.tmp-applypilot-cli/` (entire dir)
- Scrub (data, keep file): `E:\applypilot-data\answer_bank.json` and repo-root `answer_bank.json`

- [ ] **Step 1: Append to `.gitignore`**

Add this block at the end of the "# User data (NEVER commit)" section:

```gitignore
resume.pdf.bak.*
answer_bank.json
searches.yaml
!src/applypilot/config/searches.example.yaml
ats_companies.yaml
!src/applypilot/config/ats_companies.yaml
```

And this block under "# Runtime artifacts":

```gitignore
*.db-wal
*.db-shm
*.db-journal
loop-state.json
feature.md
.tmp-applypilot-cli/
.fuse_hidden*
```

(Note: the existing `resume.pdf` and `*.db` rules do NOT match `resume.pdf.bak.20260514_125304` or `applypilot.db-wal` — that's why these are needed. The `!` negations keep the two package-shipped YAMLs under `src/applypilot/config/` committable while ignoring the repo-root runtime copies.)

- [ ] **Step 2: Delete the PII strays**

```powershell
Remove-Item "resume.pdf.bak.20260514_125304" -Force
Remove-Item ".fuse_hidden0000000f00000001" -Force -ErrorAction SilentlyContinue
Remove-Item ".gh_validation.sh" -Force
Remove-Item ".tmp-applypilot-cli" -Recurse -Force
```

(`.gh_validation.sh` is an ad-hoc live-apply script with a hardcoded local Python path; `scripts/refresh_and_validate.ps1` is the maintained runbook.)

- [ ] **Step 3: Scrub poisoned canary entries from the live answer bank**

The bank contains at least one entry answering a citizenship/sponsorship question with *"I am a citizen of the United States"* — false for Nida (needs sponsorship) and reputation-damaging if replayed on a live application. Back up, then remove all entries whose question matches sponsorship/citizenship/visa/authorization markers (Phase 1 will make this class structurally impossible; this protects live runs NOW):

```powershell
& $PY -c @"
import json, re, shutil, pathlib
for p in [pathlib.Path(r'E:\applypilot-data\answer_bank.json'), pathlib.Path(r'e:\auto-apply-pipeline\answer_bank.json')]:
    if not p.exists(): continue
    shutil.copy(p, str(p) + '.pre-scrub.bak')
    entries = json.loads(p.read_text(encoding='utf-8'))
    pat = re.compile(r'sponsor|visa|citizen|authoriz|legally able|eligible to work|clearance', re.I)
    kept = [e for e in entries if not pat.search(e.get('q',''))]
    removed = [e.get('q') for e in entries if pat.search(e.get('q',''))]
    p.write_text(json.dumps(kept, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'{p}: removed {len(removed)} entries:')
    [print('  -', q) for q in removed]
"@
```

Expected: at least 1 removed entry per file (the citizenship answer). Report the removed questions to the user in the task summary.

- [ ] **Step 4: Verify git no longer sees the strays, commit**

Run: `git status --short`
Expected: no `??` lines for any file in this task's list (searches.yaml, loop-state.json, feature.md, ats_companies.yaml, answer_bank.json now ignored; strays deleted).

```powershell
git reset
git add .gitignore
git diff --cached --stat   # only .gitignore
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "hygiene: gitignore runtime/PII strays; purge resume backup, tmp data tree, ad-hoc script"
```

---

### Task 4: CI on push + test-gated PyPI publish

**Files:**
- Modify: `.github/workflows/ci.yml:3-4`
- Modify: `.github/workflows/publish.yml:8-13`

- [ ] **Step 1: Run the CI steps locally first**

Run: `ruff check src/`
Expected: clean. If there are violations, fix mechanical ones (unused imports, whitespace) in the files reported; for any non-trivial finding, add a targeted `# noqa: <code>` with a one-word reason and note it in the task summary. Re-run until clean.

Run: `& $PY -m pytest tests/ -q`
Expected: all pass (verified in Task 2; re-run to catch ruff-fix regressions).

- [ ] **Step 2: Enable CI on push/PR + make it callable**

In `.github/workflows/ci.yml`, replace lines 3-4:

```yaml
on:
  workflow_dispatch:  # manual trigger only
```

with:

```yaml
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:
  workflow_call:
```

- [ ] **Step 3: Gate publish on the test matrix**

In `.github/workflows/publish.yml`, replace the `jobs:` header block (lines 8-13):

```yaml
jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write  # required for OIDC trusted publishing
```

with:

```yaml
jobs:
  test:
    uses: ./.github/workflows/ci.yml

  publish:
    needs: test
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write  # required for OIDC trusted publishing
```

- [ ] **Step 4: Commit**

```powershell
git reset
git add .github/workflows/ci.yml .github/workflows/publish.yml
git diff --cached --stat   # only the 2 workflow files
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "ci: run on push/PR; gate PyPI publish on the test matrix"
```

Note: CI runs on GitHub's runners on the next push. Pushing is the user's call (memory guardrail: never push autonomously) — flag in the final summary that the first CI run happens whenever they push.

---

### Task 5: Final verification

- [ ] **Step 1: Full suite green**

Run: `& $PY -m pytest tests/ -q`
Expected: ALL PASS, 0 failed.

- [ ] **Step 2: Tree clean**

Run: `git status --short`
Expected: EMPTY output (no modified, no untracked). If anything remains, resolve it per the categories above and re-run.

- [ ] **Step 3: Report**

Summarize to the user: commits made, tests passing count, PII removed (including the scrubbed answer-bank questions), and the reminder that CI activates on their next push.
