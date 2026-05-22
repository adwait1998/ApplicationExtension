# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read first

This repo is a **fork** of `Pickle-Pixel/ApplyPilot` (AGPL-3.0) that is actively driven against a live job queue for **Nida Shah** (Product/UX Designer). The upstream README describes the open-source project; the on-the-ground state of this fork lives in:

- [CONTEXT.md](CONTEXT.md) — fork-vs-upstream code diffs, env vars, paths, queue state, known issues
- [docs/OPERATOR_CHEATSHEET.md](docs/OPERATOR_CHEATSHEET.md) — copy-paste PowerShell commands for the daily flow (discover → score → apply → report)
- [docs/ralph-iterations.md](docs/ralph-iterations.md) — iteration log for reliability work (failure classes, fixes, A/B results)
- [docs/haiku-vs-sonnet.md](docs/haiku-vs-sonnet.md) — model selection notes for apply

CONTEXT.md and the cheatsheet are the authoritative state. Read them before touching the apply pipeline.

## Safety rule (live submissions)

`applypilot apply` (without `--dry-run`) submits real applications under Nida's name. **Never run live apply autonomously.** Always confirm with the user before any non-`--dry-run` invocation, and prefer `--dry-run` or `--limit 1` when validating changes. `applypilot run …` (discovery/scoring/tailoring) is safe.

## Commands

The package is installed editable. Tests, lint, and CLI all assume `pip install -e ".[dev]"` and `playwright install chromium` have been run once.

```powershell
# CLI (uses installed entry point or python -m)
applypilot doctor                  # tier + dependency check (Chrome, claude CLI, profile, resume)
applypilot status                  # DB job counts by stage
applypilot run discover enrich score [--source ats_boards|jobspy|workday|theirstack|smartextract] [--quick --target-ready N]
applypilot apply --dry-run --limit 1 --workers 1 --model claude-haiku-4-5-20251001 --headless --no-live --job-timeout 720
applypilot report                  # reliability + cost summary over logs/review.jsonl

# Tests / lint
pytest tests/ -v                                # all tests
pytest tests/test_skill_replay.py -v            # one file
pytest tests/ -k "scorer" -v                    # by name pattern
pytest tests/ --cov=src/applypilot --cov-report=term-missing
ruff check src/                                 # lint
ruff format src/                                # format (line-length 120, target py311)
```

On this machine, the operator invokes the CLI via `& $PY -m applypilot …` where `$PY = "C:\Users\adwai\AppData\Local\Programs\Python\Python312\python.exe"`. The `applypilot` entry point also works once the package is on PATH.

## Required environment variables (this fork)

These must be set in the user environment (persisted via `[Environment]::SetEnvironmentVariable(..., "User")`):

| Var | Value | Why |
|---|---|---|
| `APPLYPILOT_DIR` | `E:\applypilot-data` | All runtime data lives off C: drive. Default is `~/.applypilot`. |
| `CLAUDE_BIN` | full path to `claude.cmd` / `claude.exe` | Overrides `find_claude_binary()`. The npm-global shim at `C:\Users\adwai\AppData\Roaming\npm\claude.cmd` is what works; the Microsoft Store sandbox version does not. |
| `OLLAMA_MODELS` | `E:\ollama-models` | Keeps local LLM weights off C:. |
| `LLM_URL` | `http://localhost:11434/v1` | Routes scoring through local Ollama (free, no rate limits). |
| `LLM_MODEL` | `gemma3:4b` | Default local scorer. |
| `APPLYPILOT_USE_SKILLS` | `1` | Enables Skill Playbook dispatcher (see Architecture). |

Per-run overrides go in `--llm-provider` / `--llm-model` flags or via `LLM_PROVIDER=claude` (uses Claude Code CLI for score/tailor/cover).

## Architecture

### 6-stage linear pipeline

```
discover → enrich → score → tailor → cover → pdf → APPLY → verify
```

Stages are defined in [src/applypilot/pipeline.py](src/applypilot/pipeline.py) (`STAGE_ORDER`, `STAGE_META`, `_UPSTREAM`). Each stage reads/writes columns on a **single `jobs` table** keyed by URL. The whole schema (all stages) is created up front in [database.py](src/applypilot/database.py) so any stage can run independently — no migration ordering. Adding a column = adding it to `_ALL_COLUMNS`; `ensure_columns()` handles in-place migration on next run.

Discovery has **five sub-sources** (`VALID_SOURCES`) selected by `--source`:
- `ats_boards` — Greenhouse/Lever/Ashby JSON APIs directly ([discovery/ats_boards.py](src/applypilot/discovery/ats_boards.py)). ~50× faster than scraping; ~600 jobs in 17s. Writes `description=full_description` and `application_url=url` so scoring works without an enrichment pass.
- `jobspy` — LinkedIn/Indeed/Glassdoor/ZipRecruiter/Google via `python-jobspy`.
- `workday` — 48 preconfigured Workday tenant portals (`config/employers.yaml`).
- `theirstack` — TheirStack API (opt-in, requires `THEIRSTACK_API_KEY`, consumes credits).
- `smartextract` — disabled by default; set `APPLYPILOT_SMART_EXTRACT=1`.

The companies hit by `ats_boards` are listed in [src/applypilot/config/ats_companies.yaml](src/applypilot/config/ats_companies.yaml). Use `applypilot discover-ats` to grow that registry.

### Tier system (feature gating)

[config.py](src/applypilot/config.py) defines `get_tier()` / `check_tier()`:

- **Tier 1** = Python only → `init`, `run discover`, `run enrich`, `status`, `dashboard`
- **Tier 2** = +LLM (Gemini key / OpenAI key / `LLM_URL` / Claude Code provider) → `run score|tailor|cover|pdf`
- **Tier 3** = +Claude Code CLI + Chrome → `apply`

`find_claude_binary()` resolves the CLI in this order: `CLAUDE_BIN` env → PATH (`shutil.which`) → glob common Windows install roots, newest mtime wins.

### LLM provider router

[llm.py](src/applypilot/llm.py) `_detect_provider()` picks at call time (not import time) based on env vars, in priority order:

1. `LLM_PROVIDER=claude` (or `APPLYPILOT_LLM_PROVIDER`) → spawns Claude Code CLI as subprocess
2. `GEMINI_API_KEY` (unless `LLM_URL` is set) → Gemini OpenAI-compat endpoint
3. `OPENAI_API_KEY` (unless `LLM_URL` is set)
4. `LLM_URL` → local Ollama / llama.cpp

`LLM_MODEL` overrides the model name for any provider. The `--llm-provider auto` flag on `run` clears the env-var override.

### Apply path: Skill Playbook v1 (the big architectural piece)

The apply pipeline has **three tiers of execution**, dispatched per job by [apply/skill_runner.py](src/applypilot/apply/skill_runner.py) when `APPLYPILOT_USE_SKILLS=1`:

1. **Tier 1 — deterministic replay.** [apply/replay.py](src/applypilot/apply/replay.py) runs the saved `<company>.yaml` skill via Playwright sync API directly. No LLM, no MCP, no subprocess. Conservative: drift / missing selector / value-resolution failure → returns `needs_patch`, `drift_detected`, or `failed`.
2. **Tier 2 — LLM patch.** [apply/patcher.py](src/applypilot/apply/patcher.py) (+ `prompt_patch.py`) asks Claude to fill the `unresolved_fields` Tier 1 left behind, then submit.
3. **Tier 3 — full LLM session + recording.** Falls through to the original `run_job` flow in [apply/launcher.py](src/applypilot/apply/launcher.py), with a [SkillRecorder](src/applypilot/apply/recorder.py) attached. On `applied` status the recorder commits a new `<company>.yaml` so the next apply to that company is Tier 1.

Skills live in `$APPLYPILOT_DIR/skills/<company>.yaml`. Drifted skills auto-archive to `skills/_archive/`. With the env var OFF, `dispatch_apply` is a passthrough — byte-for-byte identical to the pre-Skill flow.

### Apply path: pre-fill + LLM agent (Tier 3 internals)

Inside Tier 3 (`run_job` in [apply/launcher.py](src/applypilot/apply/launcher.py)):

1. [apply/chrome.py](src/applypilot/apply/chrome.py) launches a per-worker persistent Chrome profile (cookies survive runs) with CDP enabled on `BASE_CDP_PORT + worker_id`.
2. [apply/prefill.py](src/applypilot/apply/prefill.py) CDP-connects and **pre-fills ~14 standard Greenhouse fields + resume upload**, with read-back-after-fill verification for React-controlled values. CDP `Browser.close()` only releases the socket — does **not** kill Chrome.
3. [apply/prompt.py](src/applypilot/apply/prompt.py) `build_prompt()` builds the Sonnet/Haiku instructions. When `prefill_status` indicates fields were pre-filled, the prompt injects HARD RULES telling the model **not to navigate** (a reload wipes pre-fill) and a STALE-SNAPSHOT WARNING so it doesn't bulk-re-fill after react-select re-renders.
4. Claude Code subprocess drives Playwright MCP, handles custom screening questions + submission. Stream-JSON output is parsed line by line; failures are classified into `last_failure_class`.
5. After submit, `_verify_submission_success` ([apply/launcher.py](src/applypilot/apply/launcher.py)) checks success patterns ("thank you for your interest", "we will review your application", etc.) and absence of a remaining "submit application" button. Confidence < `--verify-threshold` → `needs_review:unverified_submission`.
6. Every attempt writes one JSONL row to `$APPLYPILOT_DIR/logs/review.jsonl`; full Claude transcripts go to `logs/claude_*.txt`; verifier evidence to `logs/verify_*.json`.

ATS-specific adapters (currently just Greenhouse) live in [apply/adapters/](src/applypilot/apply/adapters/). Lever / Ashby pre-fill is not yet implemented — they fall straight through to LLM.

### Apply queue gating

Only jobs that satisfy ALL of these are dispatched:
- `fit_score >= --min-score` (default 8 — see `DEFAULTS["min_score"]` in [config.py](src/applypilot/config.py))
- `discovered_at` within `--max-age-hours` (default 24; `0` = drain everything)
- `application_url IS NOT NULL`
- `apply_status IS NULL OR apply_status='failed'`
- `applied_at IS NULL`
- not in `manual_ats` URL patterns from [config/sites.yaml](src/applypilot/config/sites.yaml) (LinkedIn Easy Apply, Indeed Quick Apply, etc. — these are skipped because aggregator quick-apply flows aren't automatable)

Per-site lock in `_run_seen_sites` makes parallel workers cycle through different companies before retrying the same one. Within a process, claimed URLs go into `_run_seen_urls` so a bad URL is not retried by another worker.

### Reliability telemetry

[reporting.py](src/applypilot/reporting.py) is a **pure function** over `logs/review.jsonl` (testable at $0 with synthetic rows). The CLI `applypilot report` is a thin wrapper. It bucketizes failures into:

- **(A) Removable** — stochastic agent failures on stable forms (`transient_*`, `validation_*`, `skill_flow_*`, `no_result_line`, `stuck`). These are the engineering targets.
- **(B) Irreducible** — environment defenses we can't fix in code (`captcha`, `email_verification`, `sso_required`, `expired`, `not_eligible`, `cloudflare`, `policy_*`).

Reliability work is driven by this split — see [docs/ralph-iterations.md](docs/ralph-iterations.md) for the iteration log.

## Conventions specific to this fork

- **PowerShell is the default shell.** Long heredocs use `@"..."@`. Tail logs with `Get-Content -Wait`. See cheatsheet section 3 for inline-Python queries against the SQLite DB.
- **Data path is `E:\applypilot-data`, not `~/.applypilot`.** `APPLYPILOT_DIR` overrides; all `config.py` paths key off it.
- **Stop a live apply with Ctrl+C:** once = skip current job, twice = stop. DB commits are incremental so partial runs are safe.
- **`python-jobspy` install dance.** Pinned numpy in jobspy metadata conflicts with pip's resolver; install with `pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex`. Required even after `pip install -e ".[dev]"`.
- **Resume tailoring is currently disabled in the apply path** — master `resume.pdf` is used as fallback because Gemma 3 4B fabricates content (validator catches it). Re-enable once a stronger local tailor model is wired in.
- **Workday tenants need a pre-registered, email-verified account per tenant.** Profile holds the canonical password; verification is manual.
