# Phase 4B — UI Apply Control Plane (Operator Console: Batch Panel, Live Monitor, Kill Switch, Autopilot Toggle) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. Each task is self-contained: a fact-swept READ step, verbatim failing tests, a reference implementation adapted to the REAL landed APIs, and a commit block with one-shot identity.

**Goal:** Turn the existing READ-ONLY dashboard (`src/applypilot/webui/`) into the product's **operator control surface** so live applies can be launched, monitored, throttled, and stopped from the UI — the first step toward "all of this becomes an application which normal users can also use." v1 scope (user-approved): **OPERATOR CONSOLE FIRST** — a batch panel (launch dry/live applies with limit/model/workers/site), a live run monitor (worker state + per-job outcomes + cost ticker), a kill switch (graceful skip / stop-all + global pause), and an **AUTOPILOT TOGGLE** (continuous queue operation without per-batch confirmation). Onboarding / profile-editor polish is explicitly **OUT of v1**.

**Architecture:** Additive and quarantined. The UI stays a thin FastAPI app that **shells out to the existing CLI** — it constructs `python -m applypilot apply ...` exactly as an operator would type it, and reads durable records (SQLite, `logs/review.jsonl`, `logs/spend_ledger.jsonl`, the submission ledger). The UI **imports NO apply machinery** and **constructs NO safety-kernel object** (no `SubmitBroker`, no `SubmissionLedger` write path, no `BrowserStateStream`, no Playwright). New modules live under `src/applypilot/webui/`: `settings.py` (persisted operator settings), `control.py` (cap controller — the hard-cap authority), `registry.py` (batch pidfile/registry + reconcile-on-start + batch history), and an autopilot supervisor thread inside `server.py`. `server.py`'s `RunManager` gains a **token-gated** live-apply run kind and a **graceful kill switch**; a new session-token middleware guards every state-changing POST; the CLI `ui` command keeps its 127.0.0.1 binding and refuses non-local binding in v1. Worker observability reuses the EXISTING `apply/dashboard.py:update_state` surface via a small **opt-in file sink** (env-gated; zero cost to the terminal path). The Windows cp1252 console crash in `cli.py:952` (the arrow char) is fixed as the first task.

**The one load-bearing principle (bake into every task):** the UI is an **authorized trigger, never a bypass**. A UI-launched batch runs the EXACT same subprocess an operator would run, so it inherits — unchanged — every existing gate: `queue_policy`/approve-gate, the safety prologue, the submission ledger's two-phase INTENT->CONFIRMED, the submit broker one-shot ticket, CDP network containment, the posting-drift guard, the resume interlock, and canary rules. The UI adds **throttling and orchestration on top**, never a new submission path.

**Tech Stack:** Python 3.11/3.12, FastAPI + uvicorn (the `[ui]` extra: `fastapi>=0.110`, `uvicorn>=0.27`), SQLite (via `applypilot.database`), pytest with `fastapi.testclient.TestClient`. Unit/e2e tests are $0: temp `APPLYPILOT_DIR` (tmp dirs), temp SQLite, temp `review.jsonl`/`spend_ledger.jsonl`, and a **fake pipeline runner** (a tiny stub the RunManager is pointed at instead of a real `python -m applypilot apply`) — NO network, NO real Chrome, NO real Claude subprocess, NO live DB writes. **Tests MUST NEVER touch `E:\applypilot-data`** — every test injects `db_path`/`app_dir` into `create_app(...)` and/or monkeypatches `applypilot.config.APP_DIR` to a tmp dir. Interpreter `PY = C:\Users\adwai\AppData\Local\Programs\Python\Python312\python.exe` (has pytest + editable applypilot + the `[ui]` extra; the `.venv` python does NOT).

**Prerequisite:** Phase 4A complete and landed (`main @ 95e7ac4` or later, tree clean at plan-authoring). Present in the tree and fact-swept for this plan:
- `src/applypilot/webui/server.py` — `create_app(db_path, app_dir)`, `RunManager` (spawns `[sys.executable,"-m","applypilot",*args]`, ONE subprocess, `deque(maxlen=800)` stdout buffer, `_pump`, `stop()`->`terminate()`), `ALLOWED_RUN_KINDS=("discover","score","prune","dryrun_apply")`, `_build_run_args` (hard-codes `--dry-run` for `dryrun_apply`), endpoints `GET /api/summary|/api/jobs|/api/attempts|/api/run/status`, `POST /api/job/action|/api/run|/api/run/stop`. **No auth/CSRF today.**
- `src/applypilot/webui/static/index.html` — GitHub-dark SPA, tabs `Overview|Queue|Triage|Applied|Runs`, `.btn/.btn.danger/.safety/#toast/#console` styles already present; `test_index_has_approve_button_wired` is the served-HTML-assertion test pattern.
- `src/applypilot/cli.py:930-958` — `ui(host="127.0.0.1", port=8765, open_browser)`; `uvicorn.run(create_app(), host=host, port=port)`. **`cli.py:952` prints a literal arrow (U+2192)** -> `UnicodeEncodeError` when stdout is piped under a cp1252 console.
- `src/applypilot/apply/dashboard.py` — in-process `_worker_states: dict[int, WorkerState]`, `update_state(worker_id, **kwargs)`, `add_event`, `_mark_dirty()`, `WorkerState(status,job_title,company,actions,last_action,jobs_applied,jobs_failed,total_cost,...)`. **This dict lives in the apply SUBPROCESS memory — the UI server cannot read it without a durable sink.**
- `src/applypilot/apply/launcher.py` — `worker_loop` (3805; reads spend cap at loop top, `SpendLedger.over_cap()`->`db.set_paused(conn,"budget")`+break), `main`/`apply_main` (~4210; SIGINT handler: 1st Ctrl+C kills claude procs=skip current, 2nd `_stop_event.set()`=stop; `_run_workers()`), `write_review_log` (2145; the `review.jsonl` row schema incl. `cost_usd`, `tier_used`, `status`, `worker_id`, `duration_ms`).
- `src/applypilot/spend_ledger.py` — `SpendLedger(path, daily_cap_usd, monthly_cap_usd)`, `spent_today()` (rolling 24h), `spent_month()`, `over_cap()`, `entries()`.
- `src/applypilot/submission_ledger.py` — `SubmissionLedger(conn)`, `confirm`, `dangling_count()`, `confirmed_count_for_token(token, since_iso)`. **No global "confirmed since <iso>" count yet** — Task 3 adds `confirmed_count_since`.
- `src/applypilot/database.py` — `queue_policy(*, min_score=8, ...)`, `set_paused(conn, reason)`, `paused_reason(conn)`, `engine_control` table; `applypilot resume` clears the pause.
- `src/applypilot/config.py` — `APP_DIR = Path(os.environ.get("APPLYPILOT_DIR", ~/.applypilot))`, `LOG_DIR`, `SPEND_LEDGER_PATH`, `ensure_dirs()`, `DEFAULTS` (has `daily_budget_usd:5.0`, `monthly_budget_usd:50.0`, `max_apply_attempts:3`; **no `max_live_applies_per_day`** — Task 2 adds a settings-level cap, not a `DEFAULTS` change).
- `tests/test_webui.py` — the $0 TestClient pattern (temp DB + temp `review.jsonl`, `create_app(db_path=..., app_dir=...)`, no subprocess spawned).
- `CLAUDE.md:31` ("`applypilot ui` ... Safe by design: cannot launch live applies.") and `CLAUDE.md:18` (live-apply safety rule) — **both are superseded by this plan and MUST be updated (Task 8).** `docs/OPERATOR_CHEATSHEET.md` gains an Operator-Console section (Task 8).

**Conventions (same as Phase 0–4A):**
- Repo root: `e:\auto-apply-pipeline`. Run all commands from there. `PY = C:\Users\adwai\AppData\Local\Programs\Python\Python312\python.exe`.
- Commit with one-shot identity, never push: `git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "..."`.
- **Another workflow may be committing concurrently.** Before EVERY commit: `git reset`, then `git add <only this task's files>`, then `git diff --cached --stat` and verify only intended files are staged. Retry once on a race.
- TDD: write the failing test first, run to confirm the failure mode, implement, run to green, commit.
- **Reuse-verify discipline:** every task that touches an existing module opens with a READ step against the exact file + line range. NEVER construct a safety-kernel object in a UI module. NEVER add a new submission code path — compose the SAME CLI invocation.

**File structure created / modified by this plan:**
- Modify: `src/applypilot/cli.py` — cp1252 fix (Task 1); non-local binding refusal (Task 5).
- Create: `src/applypilot/webui/settings.py` — persisted operator settings under `APP_DIR` (Task 2).
- Create: `src/applypilot/webui/control.py` — cap controller (Task 3).
- Modify: `src/applypilot/submission_ledger.py` — `confirmed_count_since` (Task 3).
- Create: `src/applypilot/webui/registry.py` — batch pidfile/registry + reconcile-on-start + history (Task 4).
- Modify: `src/applypilot/webui/server.py` — token/CSRF middleware, live-apply run kind, kill switch, monitor/runs/settings/autopilot endpoints, autopilot supervisor thread (Tasks 4–7).
- Modify: `src/applypilot/apply/dashboard.py` — opt-in worker-state file sink (Task 6).
- Modify: `src/applypilot/webui/static/index.html` — Operator Console tab + Runs history + banners (Task 7).
- Modify: `CLAUDE.md`, `docs/OPERATOR_CHEATSHEET.md` — supersede "UI cannot launch live applies" + document the console/autopilot/caps (Task 8).
- Tests: `tests/test_ui_console_encoding.py`, `tests/test_ui_settings.py`, `tests/test_ui_control_caps.py`, `tests/test_ui_registry.py`, `tests/test_ui_live_launch.py`, `tests/test_ui_killswitch.py`, `tests/test_ui_monitor.py`, `tests/test_ui_autopilot.py`, `tests/test_ui_frontend.py`, plus extensions to `tests/test_webui.py` (token header on POST).

**Key invariants (load-bearing — carried from the safety kernel, extended for the control plane):**
1. **The UI is an authorized trigger, never a bypass.** A UI-launched batch (dry OR live) is the SAME subprocess an operator types (`python -m applypilot apply ...`). No UI module imports `apply.launcher`/`submit_broker`/`browser_stream`/Playwright, and none constructs a `SubmitBroker`/`SubmissionLedger`(write)/`BrowserStateStream`. Every existing gate applies unchanged.
2. **Hard caps are enforced in the run-controller regardless of autopilot.** `control.py` computes `live_applies_today` (COUNT of CONFIRMED submission-ledger rows in the rolling 24h) and `spend_today` (`SpendLedger.spent_today()`), compares to the persisted settings caps, and is consulted BEFORE every live launch AND on every autopilot tick. Breach ⇒ manual live launch refuses (HTTP 409 + reason); autopilot pauses + raises a banner. Dry-run batches are never blocked by these caps (they submit nothing).
3. **Autopilot defaults OFF, survives restarts, is one explicit click.** The `autopilot_enabled` flag lives in the persisted settings file under `APP_DIR` (NOT an env var), default `False`. Flipping it is a single token-gated POST; the supervisor reads it fresh each tick.
4. **127.0.0.1-only + per-session token on every state-changing POST.** The server binds localhost only; a `secrets`-minted per-session token is required (header) on ALL POSTs (`/api/run`, `/api/run/stop`, `/api/job/action`, `/api/settings`, `/api/autopilot`, `/api/pause`). GET reads are localhost-gated by binding. Non-local binding is refused by the `ui` command in v1.
5. **Kill switch mirrors the CLI's Ctrl+C semantics.** Stop = graceful (skip current job / stop-all) driven by the SAME signal path `apply_main` already handles; global "pause everything" additionally sets `db.set_paused` and disables autopilot. Escalate to terminate/process-tree-kill only after a grace window.
6. **Observability reuses `update_state`, off the terminal hot path.** The apply subprocess writes its `_worker_states` snapshot to a JSON file ONLY when the UI-set `APPLYPILOT_STATE_FILE` env is present; unset (normal terminal `apply`) ⇒ zero file writes, byte-identical behavior. The monitor also tails `review.jsonl` for per-job outcomes and reads `spend_ledger.jsonl` for the cost ticker. Batch history is persisted in the registry so the Runs page shows past batches with outcomes.
7. **No orphaned live batch after a server restart.** Each batch writes a registry record + pidfile under `APP_DIR/ui_runs/`. On `create_app`, reconcile: a record with a live pid is ADOPTED (re-attached to the RunManager, monitor resumes); a dead pid is closed as `orphaned_exited`. A live batch is never left running-but-untracked, and the UI never spawns a second concurrent live batch.
8. **Fail-safe defaults.** Any control/registry/settings read error resolves to the SAFE side: caps treated as breached (refuse), autopilot treated as OFF, binding treated as refused. A UI fault can never widen what submits.

---

## Ordering and dependency notes

- **Task 1** (cp1252 fix) is independent — land it first (unblocks piping the `ui` banner in CI/PowerShell).
- **Task 2** (settings) is the foundation for 3, 5, 7. **Task 3** (caps) depends on 2 and adds the ledger method. **Task 4** (registry) depends only on `APP_DIR`. **Task 5** (live launch + token + kill switch + binding) depends on 2, 3, 4. **Task 6** (observability) depends on 5 (state-file env is set at spawn) and 4 (runs history). **Task 7** (autopilot + global pause + frontend) depends on 2, 3, 5, 6. **Task 8** (docs) depends on the behavior landing in 5–7. **Task 9** is verification.
- Each task is independently committable and sized for one implementer-agent session.

---

## Task 1: Fix the Windows cp1252 console crash in `applypilot ui` (the arrow char)

`cli.py:952` prints `...dashboard[/bold] -> {url}...`. Under a cp1252 console with stdout piped (the operator's PowerShell redirect, and CI capture), rich encodes to cp1252 and the arrow (U+2192) raises `UnicodeEncodeError`, killing `applypilot ui` before uvicorn starts. Replace the one glyph with ASCII `->` and pin it with a test so it can't regress.

**Files:**
- Modify: `src/applypilot/cli.py` (line ~952)
- Test: `tests/test_ui_console_encoding.py`

- [ ] **Step 1: READ the print site**

READ `src/applypilot/cli.py:930-958` — the `ui` command. Confirm the exact string at L952 contains the arrow (U+2192) and that it is the only non-ASCII in the `ui` banner. Grep the `ui` function body for any other non-ASCII.

- [ ] **Step 2: Write failing test**

```python
# tests/test_ui_console_encoding.py
import io
from pathlib import Path

from rich.console import Console

CLI = Path("src/applypilot/cli.py")


def test_ui_banner_line_is_cp1252_safe():
    """The `applypilot ui` banner must render on a cp1252 console (Windows,
    piped stdout). A literal arrow (U+2192) crashes there — assert the source
    line is ASCII-encodable so the regression is caught at the source."""
    src = CLI.read_text(encoding="utf-8")
    line = next(l for l in src.splitlines() if "ApplyPilot dashboard" in l)
    line.encode("cp1252")            # raises UnicodeEncodeError on U+2192
    assert "→" not in line      # no arrow glyph


def test_rich_render_of_banner_encodes_cp1252():
    buf = io.StringIO()
    c = Console(file=buf, force_terminal=False)
    c.print("[bold]ApplyPilot dashboard[/bold] -> http://127.0.0.1:8765  (Ctrl+C to stop)")
    buf.getvalue().encode("cp1252")  # must not raise
```

- [ ] **Step 3: Run — expect failure** (`UnicodeEncodeError` / arrow present)

Run: `& $PY -m pytest tests/test_ui_console_encoding.py -v`

- [ ] **Step 4: Fix**

In `src/applypilot/cli.py:952` replace the arrow with `->`:

```python
    console.print(f"[bold]ApplyPilot dashboard[/bold] -> {url}  (Ctrl+C to stop)")
```

Sweep the `ui` function for any other non-ASCII and ASCII-ify (there should be none).

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_ui_console_encoding.py -v` → ALL PASS.
Run: `& $PY -m pytest tests/test_webui.py -q` → no regressions.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/cli.py tests/test_ui_console_encoding.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "cli: fix cp1252 UnicodeEncodeError in 'applypilot ui' banner (U+2192 arrow -> ASCII) so piped/Windows-console launch no longer crashes"
```

---

## Task 2: Persisted operator settings (`webui/settings.py`) — autopilot OFF default, caps, batch defaults, survives restarts

A single JSON settings file under `APP_DIR` (NOT env vars) that holds the control-plane state: `autopilot_enabled` (default `False`), the hard caps (`max_live_applies_per_day`, `spend_cap_usd_per_day`), and batch defaults (limit/model/workers/headless/site/max_age_hours/min_score). Load returns a fully-defaulted dict even when the file is absent or corrupt (fail-safe: autopilot OFF). Save is atomic. This is the foundation Tasks 3/5/7 read.

**Files:**
- Create: `src/applypilot/webui/settings.py`
- Test: `tests/test_ui_settings.py`

- [ ] **Step 1: READ the config path surface**

READ `src/applypilot/config.py:1-33,167-191` — `APP_DIR` (env `APPLYPILOT_DIR`), `DEFAULTS` (`daily_budget_usd`, `max_apply_attempts`, `min_score`), `ensure_dirs()`. The settings file path is `APP_DIR / "ui_settings.json"`. Confirm there is NO existing settings module.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_ui_settings.py
from applypilot.webui import settings as st


def test_defaults_autopilot_off(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    s = st.load_settings()
    assert s["autopilot_enabled"] is False               # constraint 3
    assert s["max_live_applies_per_day"] >= 1
    assert s["spend_cap_usd_per_day"] > 0
    assert s["batch"]["limit"] >= 1 and s["batch"]["dry_run"] in (True, False)


def test_save_then_load_roundtrip_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    st.save_settings({"autopilot_enabled": True, "max_live_applies_per_day": 7,
                      "spend_cap_usd_per_day": 3.5,
                      "batch": {"limit": 10, "model": "claude-haiku-4-5-20251001",
                                "workers": 2, "headless": True, "dry_run": False,
                                "min_score": 8, "max_age_hours": 24, "site_contains": None}})
    s2 = st.load_settings()
    assert s2["autopilot_enabled"] is True and s2["max_live_applies_per_day"] == 7
    assert (tmp_path / "ui_settings.json").exists()


def test_partial_update_merges_and_keeps_unknown_safe(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    st.save_settings(st.load_settings())
    merged = st.update_settings({"autopilot_enabled": True})
    assert merged["autopilot_enabled"] is True
    assert merged["max_live_applies_per_day"] == st.DEFAULT_SETTINGS["max_live_applies_per_day"]


def test_corrupt_file_falls_back_to_safe_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    (tmp_path / "ui_settings.json").write_text("{ not json", encoding="utf-8")
    s = st.load_settings()
    assert s["autopilot_enabled"] is False               # fail-safe (invariant 8)


def test_validation_rejects_absurd_caps(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    import pytest
    with pytest.raises(ValueError):
        st.update_settings({"max_live_applies_per_day": -1})
    with pytest.raises(ValueError):
        st.update_settings({"spend_cap_usd_per_day": 0})
```

- [ ] **Step 3: Run — expect failure** (`ModuleNotFoundError: applypilot.webui.settings`)

Run: `& $PY -m pytest tests/test_ui_settings.py -v`

- [ ] **Step 4: Implement `settings.py`**

```python
"""Persisted operator settings for the UI control plane. Lives under APP_DIR
(NOT env vars) so autopilot state + caps survive restarts. Every read fully
defaults; a missing/corrupt file resolves to the SAFE side (autopilot OFF)."""
from __future__ import annotations

import json
import os
import tempfile

DEFAULT_SETTINGS: dict = {
    "autopilot_enabled": False,                 # constraint 3: OFF by default
    "max_live_applies_per_day": 20,             # hard cap (CONFIRMED ledger rows / rolling day)
    "spend_cap_usd_per_day": 5.0,               # hard cap (reuses SpendLedger.spent_today)
    "batch": {
        "limit": 10,
        "model": "claude-haiku-4-5-20251001",
        "workers": 1,
        "headless": True,
        "dry_run": True,                        # a fresh install defaults to REHEARSAL
        "min_score": 8,
        "max_age_hours": 24,
        "site_contains": None,
    },
}


def _path():
    from applypilot import config
    return config.APP_DIR / "ui_settings.json"


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings() -> dict:
    p = _path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        if not isinstance(raw, dict):
            raw = {}
    except Exception:                            # noqa: BLE001 — fail-safe (invariant 8)
        raw = {}
    return _deep_merge(DEFAULT_SETTINGS, raw)


def _validate(s: dict) -> None:
    if int(s["max_live_applies_per_day"]) < 0:
        raise ValueError("max_live_applies_per_day must be >= 0")
    if float(s["spend_cap_usd_per_day"]) <= 0:
        raise ValueError("spend_cap_usd_per_day must be > 0")
    b = s.get("batch", {})
    if int(b.get("limit", 1)) < 1:
        raise ValueError("batch.limit must be >= 1")
    if int(b.get("workers", 1)) < 1:
        raise ValueError("batch.workers must be >= 1")


def save_settings(s: dict) -> dict:
    merged = _deep_merge(DEFAULT_SETTINGS, s or {})
    _validate(merged)
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return merged


def update_settings(patch: dict) -> dict:
    return save_settings(_deep_merge(load_settings(), patch or {}))
```

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_ui_settings.py -v` → ALL PASS.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/webui/settings.py tests/test_ui_settings.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "webui: persisted operator settings (ui_settings.json under APP_DIR) — autopilot OFF default, per-day live/spend caps, batch defaults; atomic write, fail-safe load"
```

---

## Task 3: Cap controller (`webui/control.py`) — hard-cap authority for live applies (count + spend), enforced regardless of autopilot

The single place that answers "may a live apply run right now?" It reads DURABLE records: CONFIRMED submission-ledger rows in the rolling 24h (a new `SubmissionLedger.confirmed_count_since`) and `SpendLedger.spent_today()`, compares to the persisted caps (Task 2), and returns a structured decision (allow/deny + reason + banner + the numbers for the ticker). Consulted before every live launch (Task 5) and on every autopilot tick (Task 7). Dry-run is never blocked.

**Files:**
- Modify: `src/applypilot/submission_ledger.py` (add `confirmed_count_since`)
- Create: `src/applypilot/webui/control.py`
- Test: `tests/test_ui_control_caps.py`

- [ ] **Step 1: READ the ledgers + settings**

READ `src/applypilot/submission_ledger.py:44-59` — `has_confirmed`, `confirmed_count_for_token(board_token, since_iso)` (the `updated_at >= ?` filter pattern), `dangling_count()`. READ `src/applypilot/spend_ledger.py:30-74` — `SpendLedger(path, daily_cap_usd, ...)`, `spent_today()` (rolling `time.time()-86400`), `over_cap()`. READ `src/applypilot/config.py:24-25` — `SPEND_LEDGER_PATH`. READ `src/applypilot/webui/settings.py` (Task 2) — `load_settings()`.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_ui_control_caps.py
import sqlite3

import pytest

from applypilot.webui import control as ctl
from applypilot.submission_ledger import SubmissionLedger


def _ledger_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE submission_ledger (identity_id TEXT, state TEXT, "
                 "worker_id INT, reason TEXT, confidence REAL, created_at TEXT, updated_at TEXT)")
    conn.commit()
    return conn


def test_confirmed_count_since_counts_rolling_day():
    conn = _ledger_conn()
    led = SubmissionLedger(conn)
    conn.execute("INSERT INTO submission_ledger (identity_id,state,updated_at) VALUES "
                 "('a:acme:1','confirmed','2026-07-24T10:00:00+00:00')")
    conn.execute("INSERT INTO submission_ledger (identity_id,state,updated_at) VALUES "
                 "('a:acme:2','confirmed','2020-01-01T00:00:00+00:00')")
    conn.execute("INSERT INTO submission_ledger (identity_id,state,updated_at) VALUES "
                 "('a:acme:3','intent','2026-07-24T10:00:00+00:00')")
    conn.commit()
    assert led.confirmed_count_since("2026-07-24T00:00:00+00:00") == 1


def _fake_spend(tmp_path, dollars):
    import json, time
    p = tmp_path / "spend.jsonl"
    p.write_text(json.dumps({"ts": time.time(), "stage": "apply", "model": "x",
                             "tokens_in": 1, "tokens_out": 1, "cost_usd": dollars}) + "\n",
                 encoding="utf-8")
    return p


def test_allows_when_under_both_caps(tmp_path):
    conn = _ledger_conn()
    dec = ctl.cap_status(ledger=SubmissionLedger(conn),
                         spend_path=_fake_spend(tmp_path, 1.0),
                         settings={"max_live_applies_per_day": 20, "spend_cap_usd_per_day": 5.0})
    assert dec["blocked"] is False
    assert dec["live_today"] == 0 and dec["spend_today"] == pytest.approx(1.0)


def test_blocks_on_live_count_cap(tmp_path):
    conn = _ledger_conn()
    for i in range(3):
        conn.execute("INSERT INTO submission_ledger (identity_id,state,updated_at) VALUES "
                     f"('a:acme:{i}','confirmed',strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))")
    conn.commit()
    dec = ctl.cap_status(ledger=SubmissionLedger(conn),
                         spend_path=_fake_spend(tmp_path, 0.0),
                         settings={"max_live_applies_per_day": 3, "spend_cap_usd_per_day": 5.0})
    assert dec["blocked"] is True and dec["over_live"] is True
    assert "live applies" in dec["reason"].lower()


def test_blocks_on_spend_cap(tmp_path):
    conn = _ledger_conn()
    dec = ctl.cap_status(ledger=SubmissionLedger(conn),
                         spend_path=_fake_spend(tmp_path, 9.99),
                         settings={"max_live_applies_per_day": 20, "spend_cap_usd_per_day": 5.0})
    assert dec["blocked"] is True and dec["over_spend"] is True


def test_dry_run_is_never_blocked(tmp_path):
    conn = _ledger_conn()
    conn.execute("INSERT INTO submission_ledger (identity_id,state,updated_at) VALUES "
                 "('a:acme:1','confirmed',strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))")
    conn.commit()
    allowed, reason = ctl.may_launch(dry_run=True,
                                     ledger=SubmissionLedger(conn),
                                     spend_path=_fake_spend(tmp_path, 999.0),
                                     settings={"max_live_applies_per_day": 0,
                                               "spend_cap_usd_per_day": 0.01})
    assert allowed is True and reason is None


def test_live_launch_refused_on_breach_with_reason(tmp_path):
    conn = _ledger_conn()
    allowed, reason = ctl.may_launch(dry_run=False,
                                     ledger=SubmissionLedger(conn),
                                     spend_path=_fake_spend(tmp_path, 9.0),
                                     settings={"max_live_applies_per_day": 20,
                                               "spend_cap_usd_per_day": 5.0})
    assert allowed is False and reason and "spend" in reason.lower()
```

- [ ] **Step 3: Run — expect failure** (`AttributeError`/`ModuleNotFoundError`)

Run: `& $PY -m pytest tests/test_ui_control_caps.py -v`

- [ ] **Step 4: Implement**

Add to `src/applypilot/submission_ledger.py` (mirror `confirmed_count_for_token`'s `updated_at >= ?` filter):

```python
    def confirmed_count_since(self, since_iso: str) -> int:
        """Count CONFIRMED submissions since an ISO timestamp — the rolling-day
        live-apply cap basis for the UI control plane (one confirmed row = one
        real application). Reuses the same durable ledger the CLI writes."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM submission_ledger WHERE state='confirmed' "
            "AND updated_at >= ?", (since_iso,)).fetchone()[0]
```

Create `src/applypilot/webui/control.py`:

```python
"""Cap controller — the hard-cap authority for live applies. Reads DURABLE
records (submission ledger CONFIRMED count + spend ledger) and the persisted
settings caps; enforced BEFORE every live launch and on every autopilot tick
(constraint 2). Dry-run is never blocked. Fail-safe: on any read error, treat
as blocked (invariant 8)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _rolling_day_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()


def cap_status(*, ledger, spend_path, settings) -> dict:
    live_cap = int(settings["max_live_applies_per_day"])
    spend_cap = float(settings["spend_cap_usd_per_day"])
    try:
        live_today = int(ledger.confirmed_count_since(_rolling_day_iso())) if ledger else 0
    except Exception:                                    # noqa: BLE001 — fail-safe
        live_today, live_cap = 1, 0                      # force blocked
    try:
        from applypilot.spend_ledger import SpendLedger
        spend_today = SpendLedger(spend_path).spent_today()
    except Exception:                                    # noqa: BLE001 — fail-safe
        spend_today, spend_cap = spend_cap + 1.0, spend_cap
    over_live = live_today >= live_cap
    over_spend = spend_today >= spend_cap
    blocked = over_live or over_spend
    reason = None
    if over_live:
        reason = f"daily live-applies cap reached ({live_today}/{live_cap})"
    elif over_spend:
        reason = f"daily spend cap reached (${spend_today:.2f}/${spend_cap:.2f})"
    return {
        "blocked": blocked, "over_live": over_live, "over_spend": over_spend,
        "live_today": live_today, "live_cap": live_cap,
        "spend_today": round(spend_today, 4), "spend_cap": spend_cap,
        "reason": reason,
        "banner": (f"Autopilot paused — {reason}" if blocked else None),
    }


def may_launch(*, dry_run, ledger, spend_path, settings) -> tuple[bool, str | None]:
    """Gate a launch. Dry-run always allowed (submits nothing). Live: refused
    with a human reason when either cap is breached."""
    if dry_run:
        return True, None
    dec = cap_status(ledger=ledger, spend_path=spend_path, settings=settings)
    if dec["blocked"]:
        return False, dec["reason"]
    return True, None
```

Notes / adapt warnings:
- `cap_status` accepts an already-constructed `ledger` + a `spend_path` for testability; the server wires `SubmissionLedger(get_connection())` + `config.SPEND_LEDGER_PATH` + `settings.load_settings()` (Task 5/7).
- Rolling-day is UTC-consistent with `write_review_log`/ledger timestamps (both ISO-UTC). The `SpendLedger.spent_today()` window is `time.time()-86400` (also rolling 24h) — the two caps use the same rolling window.

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_ui_control_caps.py -v` → ALL PASS.
Run: `& $PY -m pytest tests/ -k "ledger or spend" -q` → no regressions.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/submission_ledger.py src/applypilot/webui/control.py tests/test_ui_control_caps.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "webui: cap controller (control.py) + SubmissionLedger.confirmed_count_since — hard per-day live-count + spend caps from durable records; dry-run never blocked; fail-safe to blocked"
```

---

## Task 4: Batch registry + pidfile + reconcile-on-start (`webui/registry.py`) — no orphaned live batch, persisted Runs history

Each launched batch gets a durable record (`APP_DIR/ui_runs/<id>.json`) plus a `current.pid` pidfile. On `create_app`, reconcile: adopt a still-alive batch (re-attach so the monitor resumes) or close a dead one as `orphaned_exited`. This registry is ALSO the Runs-history store (Task 7 lists it). Windows-safe process-alive check + a single-active-live-batch guard.

**Files:**
- Create: `src/applypilot/webui/registry.py`
- Test: `tests/test_ui_registry.py`

- [ ] **Step 1: READ the run/dir surface**

READ `src/applypilot/config.py:9,91-94` — `APP_DIR`, `ensure_dirs`. READ `src/applypilot/webui/server.py:78-142` — `RunManager` (what it tracks: `kind`, `started_at`, `returncode`, `running` via `_proc.poll()`). The registry stores what survives a process death: `pid`, `kind`, `dry_run`, `args`, `started_at`, `finished_at`, `returncode`, `outcome`. Confirm no existing `ui_runs` concept.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_ui_registry.py
import os

from applypilot.webui import registry as reg


def test_open_writes_record_and_pidfile(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    rec = reg.open_batch(kind="live_apply", dry_run=False,
                         args=["apply", "--limit", "10"], pid=os.getpid())
    assert rec["id"] and rec["finished_at"] is None and rec["pid"] == os.getpid()
    d = tmp_path / "ui_runs"
    assert (d / f"{rec['id']}.json").exists()
    assert (d / "current.pid").read_text(encoding="utf-8").strip() == rec["id"]


def test_close_batch_records_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    rec = reg.open_batch(kind="live_apply", dry_run=False, args=[], pid=os.getpid())
    reg.close_batch(rec["id"], returncode=0, outcome={"applied": 3, "failed": 1, "needs_review": 2})
    got = reg.get_batch(rec["id"])
    assert got["returncode"] == 0 and got["outcome"]["applied"] == 3
    assert got["finished_at"] is not None
    assert not (tmp_path / "ui_runs" / "current.pid").exists()


def test_history_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    a = reg.open_batch(kind="live_apply", dry_run=True, args=[], pid=os.getpid())
    reg.close_batch(a["id"], returncode=0, outcome={})
    b = reg.open_batch(kind="live_apply", dry_run=False, args=[], pid=os.getpid())
    reg.close_batch(b["id"], returncode=0, outcome={})
    hist = reg.history(limit=10)
    assert [h["id"] for h in hist][:2] == [b["id"], a["id"]]


def test_reconcile_adopts_live_pid(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    rec = reg.open_batch(kind="live_apply", dry_run=False, args=[], pid=os.getpid())
    result = reg.reconcile_on_start()
    assert result["adopted"] and result["adopted"]["id"] == rec["id"]
    assert reg.get_batch(rec["id"])["finished_at"] is None


def test_reconcile_closes_dead_pid_as_orphan(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    rec = reg.open_batch(kind="live_apply", dry_run=False, args=[], pid=2_000_000_000)
    result = reg.reconcile_on_start()
    assert result["adopted"] is None
    got = reg.get_batch(rec["id"])
    assert got["finished_at"] is not None and got["returncode"] == reg.ORPHANED
    assert not (tmp_path / "ui_runs" / "current.pid").exists()


def test_pid_alive_is_windows_safe():
    assert reg.pid_alive(os.getpid()) is True
    assert reg.pid_alive(2_000_000_000) is False
```

- [ ] **Step 3: Run — expect failure** (`ModuleNotFoundError: applypilot.webui.registry`)

Run: `& $PY -m pytest tests/test_ui_registry.py -v`

- [ ] **Step 4: Implement `registry.py`**

```python
"""Durable batch registry + pidfile so a UI-launched batch is never orphaned
across a server restart (constraint 7), and so the Runs page has history. One
JSON record per batch under APP_DIR/ui_runs/; current.pid names the active
batch. reconcile_on_start() adopts a live batch or closes a dead one."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone

ORPHANED = -999                                  # returncode sentinel for a lost batch


def _dir():
    from applypilot import config
    d = config.APP_DIR / "ui_runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path, obj) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def pid_alive(pid: int) -> bool:
    """Windows-safe liveness. On Windows use OpenProcess via ctypes; on POSIX
    use os.kill(pid, 0)."""
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k = ctypes.windll.kernel32
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong(0)
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def open_batch(*, kind: str, dry_run: bool, args: list[str], pid: int) -> dict:
    rec = {
        "id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6],
        "kind": kind, "dry_run": bool(dry_run), "args": list(args), "pid": int(pid),
        "started_at": _now(), "finished_at": None, "returncode": None, "outcome": None,
    }
    d = _dir()
    _atomic_write(d / f"{rec['id']}.json", rec)
    (d / "current.pid").write_text(rec["id"], encoding="utf-8")
    return rec


def get_batch(batch_id: str) -> dict | None:
    p = _dir() / f"{batch_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                            # noqa: BLE001
        return None


def close_batch(batch_id: str, *, returncode: int | None, outcome: dict | None) -> None:
    rec = get_batch(batch_id)
    if rec is None:
        return
    rec["finished_at"] = _now()
    rec["returncode"] = returncode
    rec["outcome"] = outcome or {}
    _atomic_write(_dir() / f"{batch_id}.json", rec)
    cur = _dir() / "current.pid"
    try:
        if cur.exists() and cur.read_text(encoding="utf-8").strip() == batch_id:
            cur.unlink()
    except OSError:
        pass


def history(*, limit: int = 50) -> list[dict]:
    recs = []
    for p in _dir().glob("*.json"):
        try:
            recs.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:                        # noqa: BLE001
            continue
    recs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return recs[:limit]


def active_batch() -> dict | None:
    cur = _dir() / "current.pid"
    if not cur.exists():
        return None
    return get_batch(cur.read_text(encoding="utf-8").strip())


def reconcile_on_start() -> dict:
    """On server boot: if a batch was mid-flight, ADOPT it when its pid is still
    alive (monitor re-attaches), else close it as orphaned. Returns
    {'adopted': <rec|None>, 'orphaned': [<id>...]}. Never leaves a live batch
    untracked and never lets the server think it can start a second one."""
    active = active_batch()
    orphaned = []
    adopted = None
    if active and active.get("finished_at") is None:
        if pid_alive(active.get("pid", 0)):
            adopted = active
        else:
            close_batch(active["id"], returncode=ORPHANED, outcome={"note": "orphaned_on_restart"})
            orphaned.append(active["id"])
    return {"adopted": adopted, "orphaned": orphaned}
```

Notes / adapt warnings:
- `outcome` is filled by the RunManager on batch end (Task 5) from the review.jsonl rows written during the batch window, or from the subprocess's final `Done: N applied, M failed` summary line — implement whichever is more robust; the review.jsonl derivation is preferred (already durable). Wire it in Task 5.
- The `id` is time-sortable + uuid-suffixed so `history` newest-first is a stable sort.

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_ui_registry.py -v` → ALL PASS.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/webui/registry.py tests/test_ui_registry.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "webui: batch registry + pidfile + reconcile-on-start (ui_runs/) — adopt live batch or close orphan on restart; Windows-safe pid liveness; persisted Runs history"
```

---

## Task 5: Live-apply launch via the SAME CLI path + per-session token/CSRF + graceful kill switch + localhost-only binding

The core. Extend `RunManager` with a `live_apply` (and keep `dryrun_apply`) run kind that composes the EXACT `python -m applypilot apply ...` an operator types, from the persisted batch settings — inheriting every gate (invariant 1). Guard ALL state-changing POSTs with a per-session token (constraint 4). Refuse a live launch when caps are breached (invariant 2). Make stop graceful, mirroring Ctrl+C (skip current / stop-all; invariant 5). Register the batch (Task 4) and set the state-file env (for Task 6). Refuse non-local binding in the `ui` command.

**Files:**
- Modify: `src/applypilot/webui/server.py`
- Modify: `src/applypilot/cli.py` (non-local binding refusal)
- Modify: `tests/test_webui.py` (existing POSTs now send the token)
- Test: `tests/test_ui_live_launch.py`, `tests/test_ui_killswitch.py`

- [ ] **Step 1: READ the RunManager + spawn + stop + the apply arg surface**

READ `src/applypilot/webui/server.py:41-142` (`RunRequest`, `_build_run_args`, `ALLOWED_RUN_KINDS`, `RunManager.start/_pump/stop/status`), `:394-411` (`/api/run`, `/api/run/stop`). READ `src/applypilot/cli.py:587-814` — the `apply` command's flags (`--limit/--workers/--model/--dry-run/--headless/--no-live/--min-score/--max-age-hours/--site-contains/--job-timeout/--max-transient-retries`) so the composed args are a valid subset. READ `src/applypilot/apply/launcher.py:4272-4295` — the SIGINT handler (1 = skip current via `_kill_process_tree` on claude procs; 2 = `_stop_event.set()`); note it registers `signal.signal(signal.SIGINT, _sigint_handler)` only. READ `webui/server.py:107-115` — the current `subprocess.Popen(...)` (no `creationflags`).

- [ ] **Step 2: Write failing tests**

```python
# tests/test_ui_live_launch.py
from __future__ import annotations

import sqlite3

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from applypilot.webui import server as srv  # noqa: E402
from applypilot.webui.server import _build_run_args, ALLOWED_RUN_KINDS, create_app  # noqa: E402


def test_live_apply_is_now_an_allowed_kind_but_gated():
    assert "live_apply" in ALLOWED_RUN_KINDS


def test_live_apply_args_are_the_same_cli_path_no_dry_run():
    args = _build_run_args("live_apply", {"limit": 10, "model": "claude-haiku-4-5-20251001",
                                          "workers": 2, "headless": True, "min_score": 8,
                                          "max_age_hours": 24, "site_contains": None})
    assert args[0] == "apply"
    assert "--dry-run" not in args
    assert "--limit" in args and args[args.index("--limit") + 1] == "10"
    assert "--workers" in args and args[args.index("--workers") + 1] == "2"


def test_dryrun_apply_still_forces_dry_run():
    args = _build_run_args("dryrun_apply", {"limit": 3})
    assert "--dry-run" in args


class _FakeLedger:
    def __init__(self, n): self._n = n
    def confirmed_count_since(self, since): return self._n


def _client(tmp_path, monkeypatch, *, confirmed=0, spend=0.0, caps=(20, 5.0)):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    db = tmp_path / "applypilot.db"
    sqlite3.connect(db).close()
    (tmp_path / "logs").mkdir(exist_ok=True)
    from applypilot.webui import settings as st
    st.save_settings({"max_live_applies_per_day": caps[0], "spend_cap_usd_per_day": caps[1],
                      "batch": {"limit": 5, "model": "m", "workers": 1, "headless": True,
                                "dry_run": False, "min_score": 8, "max_age_hours": 24,
                                "site_contains": None}})
    started = {}
    def _fake_start(self, kind, params, *, state_file=None):
        started["kind"] = kind
        started["args"] = _build_run_args(kind, params)
    monkeypatch.setattr(srv.RunManager, "start", _fake_start)
    monkeypatch.setattr(srv, "_cap_inputs", lambda app_dir: (_FakeLedger(confirmed), tmp_path / "spend.jsonl"))
    import json, time
    (tmp_path / "spend.jsonl").write_text(json.dumps(
        {"ts": time.time(), "cost_usd": spend, "model": "x", "tokens_in": 1, "tokens_out": 1,
         "stage": "apply"}) + "\n", encoding="utf-8")
    monkeypatch.setenv("APPLYPILOT_UI_NO_SUPERVISOR", "1")
    app = create_app(db_path=db, app_dir=tmp_path)
    return TestClient(app), started


def _tok(client):
    return client.get("/api/session").json()["token"]


def test_live_launch_requires_token(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.post("/api/run", json={"kind": "live_apply"})
    assert r.status_code == 403


def test_live_launch_allowed_under_caps(tmp_path, monkeypatch):
    client, started = _client(tmp_path, monkeypatch, confirmed=0, spend=0.0)
    r = client.post("/api/run", json={"kind": "live_apply"},
                    headers={"X-ApplyPilot-Token": _tok(client)})
    assert r.status_code == 200
    assert started["kind"] == "live_apply" and "--dry-run" not in started["args"]


def test_live_launch_refused_when_cap_breached(tmp_path, monkeypatch):
    client, started = _client(tmp_path, monkeypatch, confirmed=99, spend=0.0, caps=(20, 5.0))
    r = client.post("/api/run", json={"kind": "live_apply"},
                    headers={"X-ApplyPilot-Token": _tok(client)})
    assert r.status_code == 409
    assert "cap" in r.json()["detail"].lower()
    assert started == {}


def test_dry_run_launch_not_blocked_by_caps(tmp_path, monkeypatch):
    client, started = _client(tmp_path, monkeypatch, confirmed=99, spend=999.0, caps=(1, 0.5))
    r = client.post("/api/run", json={"kind": "dryrun_apply"},
                    headers={"X-ApplyPilot-Token": _tok(client)})
    assert r.status_code == 200 and started["kind"] == "dryrun_apply"
```

```python
# tests/test_ui_killswitch.py
from applypilot.webui.server import RunManager


class _FakeProc:
    def __init__(self):
        self.pid = 4321
        self._alive = True
        self.signals = []
        self.terminated = False
    def poll(self):
        return None if self._alive else 0
    def send_signal(self, sig):
        self.signals.append(sig)
    def terminate(self):
        self.terminated = True
        self._alive = False
    def wait(self, timeout=None):
        self._alive = False
        return 0


def test_stop_graceful_skip_sends_one_interrupt():
    rm = RunManager()
    rm._proc = _FakeProc()
    rm.stop(mode="skip")
    assert len(rm._proc.signals) == 1                    # one Ctrl+C => skip current


def test_stop_all_escalates_then_terminates():
    rm = RunManager()
    p = _FakeProc()
    rm._proc = p
    rm.stop(mode="all", grace_s=0.0)                     # 0 grace => immediate escalate
    assert len(p.signals) >= 1
    assert p.terminated is True
```

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_ui_live_launch.py tests/test_ui_killswitch.py -v`

- [ ] **Step 4: Implement**

In `src/applypilot/webui/server.py`:

(a) Extend the allowlist + arg builder — `live_apply` composes the SAME CLI path (invariant 1), no `--dry-run`:

```python
ALLOWED_RUN_KINDS = ("discover", "score", "prune", "dryrun_apply", "live_apply")


def _apply_args(params: dict, *, dry_run: bool) -> list[str]:
    args = ["apply",
            "--limit", str(max(1, int(params.get("limit") or 1))),
            "--workers", str(max(1, int(params.get("workers") or 1))),
            "--model", str(params.get("model") or "claude-haiku-4-5-20251001"),
            "--min-score", str(int(params.get("min_score") or 8)),
            "--max-age-hours", str(int(params.get("max_age_hours") if params.get("max_age_hours") is not None else 24)),
            "--no-live"]
    if params.get("headless", True):
        args.append("--headless")
    if dry_run:
        args.append("--dry-run")                          # rehearsal: never clicks Submit
    site = params.get("site_contains")
    if site:
        args += ["--site-contains", str(site)]
    return args
```

Then in `_build_run_args`, route `dryrun_apply` -> `_apply_args(params, dry_run=True)` (keep the <=5 cap for rehearsal if desired) and `live_apply` -> `_apply_args(params, dry_run=False)`.

(b) `RunManager.start` spawns in a new process group (so signals target the child group, not the UI server) and sets the state-file env for Task 6; wire the registry:

```python
    def start(self, kind: str, params: dict, *, state_file=None) -> None:
        if kind not in ALLOWED_RUN_KINDS:
            raise ValueError(f"run kind not allowed: {kind}")
        with self._lock:
            if self.running:
                raise RuntimeError("a run is already in progress")
            args = _build_run_args(kind, params)
            cmd = [sys.executable, "-m", "applypilot", *args]
            env = dict(os.environ)
            if state_file is not None:
                env["APPLYPILOT_STATE_FILE"] = str(state_file)   # Task 6 opt-in sink
            creationflags = 0
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            self.kind = kind
            self.dry_run = ("--dry-run" in args)
            self.returncode = None
            self.started_at = datetime.now(timezone.utc).isoformat()
            self.lines.clear()
            self.lines.append(f"$ {' '.join(cmd)}")
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", bufsize=1, env=env,
                creationflags=creationflags,
            )
            from applypilot.webui import registry as reg
            self.batch = reg.open_batch(kind=kind, dry_run=self.dry_run, args=args, pid=self._proc.pid)
            self._thread = threading.Thread(target=self._pump, daemon=True)
            self._thread.start()
```

(c) Graceful kill switch mirroring Ctrl+C (invariant 5). On Windows a new-process-group child receives `CTRL_BREAK_EVENT`; the launcher must also honor it (small launcher edit below). POSIX uses SIGINT:

```python
    def stop(self, *, mode: str = "all", grace_s: float = 8.0) -> None:
        """mode='skip' => one interrupt (skip current job, CLI's 1st Ctrl+C).
        mode='all'  => two interrupts (STOP, CLI's 2nd Ctrl+C), then escalate to
        terminate + process-tree kill if it doesn't exit within grace_s."""
        import time as _t
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return
            sig = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT
            try:
                proc.send_signal(sig)
                self.lines.append("[stop: skip current job]" if mode == "skip" else "[stop: stopping]")
                if mode == "all":
                    _t.sleep(min(0.4, grace_s))
                    if proc.poll() is None:
                        proc.send_signal(sig)            # 2nd interrupt => STOP branch
            except Exception:                            # noqa: BLE001
                pass
        if mode == "all":
            deadline = _t.time() + grace_s
            while proc.poll() is None and _t.time() < deadline:
                _t.sleep(0.2)
            if proc.poll() is None:
                try:
                    proc.terminate()                     # escalate — hard stop
                    self.lines.append("[stop: terminated]")
                except Exception:                        # noqa: BLE001
                    pass
```

(d) On batch end, `_pump` closes the registry record with an outcome derived from review.jsonl rows written during `[started_at, now]`:

```python
    def _pump(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            self.lines.append(line.rstrip("\n"))
        proc.wait()
        self.returncode = proc.returncode
        self.lines.append(f"[exit code {proc.returncode}]")
        try:
            from applypilot.webui import registry as reg
            outcome = _outcome_since(self.started_at)     # count review.jsonl rows this batch
            reg.close_batch(self.batch["id"], returncode=proc.returncode, outcome=outcome)
        except Exception:                                # noqa: BLE001
            pass
```

(e) Per-session token + CSRF. Mint on `create_app`; require on every POST; expose via a localhost-only `GET /api/session`:

```python
    import secrets
    app.state.session_token = secrets.token_urlsafe(32)

    @app.middleware("http")
    async def _csrf_guard(request, call_next):
        if request.method == "POST":
            tok = request.headers.get("X-ApplyPilot-Token", "")
            if not secrets.compare_digest(tok, app.state.session_token):
                return JSONResponse({"detail": "missing or invalid session token"}, status_code=403)
        return await call_next(request)

    @app.get("/api/session")
    def session() -> dict:
        return {"token": app.state.session_token}
```

(f) The `/api/run` endpoint consults the cap controller for live launches and wires the state-file env + a helper the tests monkeypatch:

```python
    def _cap_inputs(app_dir):
        from applypilot.submission_ledger import SubmissionLedger
        from applypilot.database import get_connection
        from applypilot import config
        return SubmissionLedger(get_connection()), config.SPEND_LEDGER_PATH

    srv_cap_inputs = _cap_inputs   # module-visible for monkeypatch in tests

    @app.post("/api/run")
    def start_run(body: RunRequest) -> dict:
        from applypilot.webui import settings as st, control as ctl
        s = st.load_settings()
        params = {**s.get("batch", {}), **{k: v for k, v in body.model_dump().items() if v is not None}}
        dry = body.kind != "live_apply"
        if body.kind in ("live_apply", "dryrun_apply"):
            ledger, spend_path = _cap_inputs(app_dir)
            allowed, reason = ctl.may_launch(dry_run=dry, ledger=ledger,
                                             spend_path=spend_path, settings=s)
            if not allowed:
                raise HTTPException(409, f"live apply refused: {reason}")
        state_file = Path(app_dir) / "logs" / "worker_state.json"
        try:
            runs.start(body.kind, params, state_file=state_file)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "kind": body.kind, "dry_run": dry}
```

(Adapt: expose `_cap_inputs` at module scope, or as `srv._cap_inputs`, so `test_ui_live_launch` can monkeypatch it. Also add `dry_run`/`batch` attrs to `RunManager.__init__`.)

(g) `/api/run/stop` gains a `mode`:

```python
    class StopRequest(BaseModel):
        mode: str = "all"                                # "skip" | "all"

    @app.post("/api/run/stop")
    def run_stop(body: StopRequest) -> dict:
        runs.stop(mode=body.mode)
        return runs.status()
```

In `src/applypilot/apply/launcher.py` `main`/`apply_main` (~L4295), register the Windows console-break handler so a UI-sent `CTRL_BREAK_EVENT` triggers the SAME skip/stop counter as Ctrl+C:

```python
    signal.signal(signal.SIGINT, _sigint_handler)
    if hasattr(signal, "SIGBREAK"):                      # Windows: CTRL_BREAK_EVENT
        signal.signal(signal.SIGBREAK, _sigint_handler)
```

In `src/applypilot/cli.py` `ui` command, refuse non-local binding in v1 (constraint 4):

```python
    if host not in {"127.0.0.1", "localhost", "::1"}:
        console.print("[red]Non-local binding is unsupported in v1.[/red] The control "
                      "plane can launch LIVE applies; bind 127.0.0.1 only.")
        raise typer.Exit(2)
```

Update `tests/test_webui.py`: the existing POST tests must send the token. Add a fixture helper that GETs `/api/session` and route `job/action`/`run` POSTs through a wrapper that sets the `X-ApplyPilot-Token` header. Add one test `test_post_without_token_is_403`.

Notes / adapt warnings:
- The `_outcome_since(started_at)` helper counts `review.jsonl` rows with `ts >= started_at`, bucketing `status` into applied/failed/needs_review — reuse the parse loop already in `/api/attempts`. Keep it defensive (missing file -> `{}`).
- The kill-switch tests use a `_FakeProc`; real signal delivery is NOT exercised in CI (Windows console-signal semantics can't be unit-tested cleanly). The e2e smoke (Task 9) uses a fake runner script that installs a SIGBREAK/SIGINT handler to prove the graceful path end-to-end at $0.
- **Posture:** `_build_run_args`/`_apply_args` construct a CLI argv only. Grep-proof in Task 9 that `server.py` imports NOTHING from `apply.launcher`/`submit_broker`/`browser_stream` and constructs no kernel object.

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_ui_live_launch.py tests/test_ui_killswitch.py tests/test_webui.py -v` → ALL PASS.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/webui/server.py src/applypilot/cli.py src/applypilot/apply/launcher.py tests/test_ui_live_launch.py tests/test_ui_killswitch.py tests/test_webui.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "webui: live-apply launch via the SAME apply CLI path (no new submission pathway) + per-session token/CSRF on POST + cap-gated refusal + graceful kill switch (skip/stop-all mirroring Ctrl+C) + localhost-only binding"
```

---

## Task 6: Observability — worker-state file sink (reuse `update_state`) + monitor endpoint + cost ticker + runs history

The run monitor needs the apply subprocess's live worker state, but `_worker_states` lives in that subprocess's memory. Add an **opt-in** file sink: when `APPLYPILOT_STATE_FILE` is set (the UI sets it at spawn, Task 5), `dashboard.update_state`/`add_event` also flush a JSON snapshot atomically; unset => zero cost (terminal `apply` unchanged). Add `GET /api/monitor` (worker state + recent review.jsonl outcomes + cost ticker + cap status + autopilot flag) and `GET /api/runs` (registry history).

**Files:**
- Modify: `src/applypilot/apply/dashboard.py`
- Modify: `src/applypilot/webui/server.py`
- Test: `tests/test_ui_monitor.py`

- [ ] **Step 1: READ the state surface**

READ `src/applypilot/apply/dashboard.py:22-104` — `WorkerState` fields, `update_state`, `add_event`, `_mark_dirty`, `_events`, `_lock`. READ `webui/server.py` summary/attempts loops (the review.jsonl parse) + Task 4 `registry.history`. Confirm `update_state` currently only mutates the in-memory dict.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_ui_monitor.py
import json

import pytest

from applypilot.apply import dashboard as dash


def test_state_sink_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("APPLYPILOT_STATE_FILE", raising=False)
    dash.init_worker(0)
    dash.update_state(0, status="applying", job_title="Designer", company="acme")
    assert not list(tmp_path.glob("*.json"))             # no file written when env unset


def test_state_sink_writes_snapshot_when_enabled(tmp_path, monkeypatch):
    sink = tmp_path / "worker_state.json"
    monkeypatch.setenv("APPLYPILOT_STATE_FILE", str(sink))
    dash.init_worker(0)
    dash.update_state(0, status="applying", job_title="Designer", company="acme",
                      last_action="filling", total_cost=0.12)
    dash.add_event("[W0] launched")
    data = json.loads(sink.read_text(encoding="utf-8"))
    w = [x for x in data["workers"] if x["worker_id"] == 0][0]
    assert w["status"] == "applying" and w["company"] == "acme"
    assert data["totals"]["cost"] >= 0.12
    assert any("launched" in e for e in data["events"])


def test_state_sink_fault_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_STATE_FILE", str(tmp_path / "nope" / "deep" / "s.json"))
    dash.init_worker(0)
    dash.update_state(0, status="idle")                  # must not raise


# --- monitor endpoint ---

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from applypilot.webui.server import create_app  # noqa: E402


def test_monitor_endpoint_shape(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv("APPLYPILOT_UI_NO_SUPERVISOR", "1")
    db = tmp_path / "applypilot.db"; sqlite3.connect(db).close()
    logs = tmp_path / "logs"; logs.mkdir()
    (logs / "worker_state.json").write_text(json.dumps({
        "workers": [{"worker_id": 0, "status": "applying", "job_title": "Designer",
                     "company": "acme", "last_action": "fill", "jobs_applied": 1,
                     "jobs_failed": 0, "total_cost": 0.2}],
        "events": ["[W0] launched"], "totals": {"applied": 1, "failed": 0, "cost": 0.2},
    }), encoding="utf-8")
    (logs / "review.jsonl").write_text(json.dumps(
        {"ts": "2026-07-24T10:00:00", "status": "applied", "title": "Designer",
         "site": "acme", "cost_usd": 0.2}) + "\n", encoding="utf-8")
    m = TestClient(create_app(db_path=db, app_dir=tmp_path)).get("/api/monitor").json()
    assert m["workers"][0]["status"] == "applying"
    assert m["cost"]["spend_today"] is not None
    assert "caps" in m and "autopilot_enabled" in m
    assert m["recent_outcomes"][0]["status"] == "applied"
```

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_ui_monitor.py -v`

- [ ] **Step 4: Implement**

In `src/applypilot/apply/dashboard.py`, add a guarded flush and call it from `update_state`/`add_event`/`init_worker` (all under `_mark_dirty`). Keep it fully exception-guarded so a sink fault never touches the apply loop:

```python
import json as _json
import os as _os
import tempfile as _tempfile

_last_flush = 0.0
_FLUSH_MIN_INTERVAL = 0.4          # throttle disk churn

def _snapshot() -> dict:
    with _lock:
        workers = [vars(s).copy() for s in _worker_states.values()]
        events = list(_events)
    for w in workers:
        w.pop("log_file", None)     # Path is not JSON-serializable / not needed
    applied = sum(w.get("jobs_applied", 0) for w in workers)
    failed = sum(w.get("jobs_failed", 0) for w in workers)
    cost = sum(w.get("total_cost", 0.0) for w in workers)
    return {"workers": workers, "events": events,
            "totals": {"applied": applied, "failed": failed, "cost": round(cost, 4)}}

def _maybe_flush_state(force: bool = False) -> None:
    path = _os.environ.get("APPLYPILOT_STATE_FILE")
    if not path:
        return                      # OFF by default => zero cost (invariant 6)
    global _last_flush
    now = time.time()
    if not force and (now - _last_flush) < _FLUSH_MIN_INTERVAL:
        return
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = _tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            _json.dump(_snapshot(), f)
        _os.replace(tmp, p)
        _last_flush = now
    except Exception:               # noqa: BLE001 — never break the apply loop
        pass
```

Call `_maybe_flush_state()` at the end of `update_state`, `add_event`, and `init_worker` (right after `_mark_dirty()`), and `_maybe_flush_state(force=True)` from the terminal `update_state(status="done")` sites if you want a final snapshot (optional).

In `src/applypilot/webui/server.py`, add the endpoints:

```python
    @app.get("/api/monitor")
    def monitor() -> dict:
        state = {"workers": [], "events": [], "totals": {"applied": 0, "failed": 0, "cost": 0.0}}
        sf = Path(app_dir) / "logs" / "worker_state.json"
        if sf.exists():
            try:
                state = json.loads(sf.read_text(encoding="utf-8"))
            except Exception:                            # noqa: BLE001
                pass
        outcomes = []
        if review_log.exists():
            with review_log.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            outcomes.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        from applypilot.webui import settings as st, control as ctl
        s = st.load_settings()
        ledger, spend_path = _cap_inputs(app_dir)
        caps = ctl.cap_status(ledger=ledger, spend_path=spend_path, settings=s)
        return {
            "running": runs.running, "kind": runs.kind, "dry_run": getattr(runs, "dry_run", None),
            "workers": state.get("workers", []), "events": state.get("events", []),
            "totals": state.get("totals", {}),
            "recent_outcomes": outcomes[-30:][::-1],
            "cost": {"spend_today": caps["spend_today"], "spend_cap": caps["spend_cap"],
                     "batch_cost": state.get("totals", {}).get("cost", 0.0)},
            "caps": caps, "autopilot_enabled": bool(s.get("autopilot_enabled")),
            "console": runs.status()["lines"],
        }

    @app.get("/api/runs")
    def runs_history(limit: int = 50) -> dict:
        from applypilot.webui import registry as reg
        return {"runs": reg.history(limit=max(1, min(limit, 200)))}
```

Also call `registry.reconcile_on_start()` once inside `create_app` (constraint 7) and stash the adopted batch on `runs` if present (so `runs.running` reflects an adopted live batch — at minimum reflect its pidfile; full stdout re-attach is best-effort, note in summary).

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_ui_monitor.py tests/test_webui.py -q` → ALL PASS.
Run: `& $PY -m pytest tests/ -k "dashboard" -q` → no regressions.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/apply/dashboard.py src/applypilot/webui/server.py tests/test_ui_monitor.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "webui: run-monitor observability — opt-in worker-state file sink (reuses update_state; env-gated, zero terminal cost) + /api/monitor (worker state + review.jsonl outcomes + cost ticker + caps) + /api/runs history + reconcile-on-start"
```

---

## Task 7: Autopilot supervisor + global pause-everything + toggle endpoint (cap-aware auto-pause)

The autopilot toggle (default OFF, Task 2) that runs the queue continuously without per-batch confirmation. A single supervisor thread inside the server: when `autopilot_enabled` AND not globally paused AND no batch running AND caps NOT breached AND the queue has eligible jobs -> launch the next live batch (same path as Task 5). On cap breach it flips autopilot OFF-effective (pauses) and surfaces a banner. A global `POST /api/pause` disables autopilot, stops the current batch (`mode="all"`), and sets `db.set_paused` (belt-and-suspenders with the engine's own pause).

**Files:**
- Modify: `src/applypilot/webui/server.py`
- Test: `tests/test_ui_autopilot.py`

- [ ] **Step 1: READ the pause + queue surfaces**

READ `src/applypilot/database.py:289-306` — `set_paused`/`paused_reason`. READ `webui/server.py` summary's `queue_policy` eligible count (there is already an eligible SELECT) — the supervisor reuses it to decide "is there anything to run". READ Task 2 `settings`, Task 3 `control`, Task 5 `runs.start`.

- [ ] **Step 2: Write failing tests** (the DECISION function is pure + unit-testable; the thread is not run in tests)

```python
# tests/test_ui_autopilot.py
import sqlite3

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from applypilot.webui import server as srv  # noqa: E402
from applypilot.webui.server import create_app  # noqa: E402


def test_autopilot_decision_off_is_noop():
    d = srv.autopilot_decision(enabled=False, running=False, blocked=False, eligible=5)
    assert d["action"] == "idle" and d["reason"]


def test_autopilot_decision_launches_when_clear():
    d = srv.autopilot_decision(enabled=True, running=False, blocked=False, eligible=5)
    assert d["action"] == "launch"


def test_autopilot_decision_waits_while_running():
    d = srv.autopilot_decision(enabled=True, running=True, blocked=False, eligible=5)
    assert d["action"] == "wait"


def test_autopilot_decision_pauses_on_cap_breach():
    d = srv.autopilot_decision(enabled=True, running=False, blocked=True, eligible=5)
    assert d["action"] == "pause" and "cap" in d["reason"].lower()


def test_autopilot_decision_idle_when_queue_empty():
    d = srv.autopilot_decision(enabled=True, running=False, blocked=False, eligible=0)
    assert d["action"] == "idle"


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_UI_NO_SUPERVISOR", "1")
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    db = tmp_path / "applypilot.db"; sqlite3.connect(db).close()
    (tmp_path / "logs").mkdir(exist_ok=True)
    return TestClient(create_app(db_path=db, app_dir=tmp_path))


def _tok(c): return c.get("/api/session").json()["token"]


def test_autopilot_toggle_requires_token_and_persists(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    assert c.post("/api/autopilot", json={"enabled": True}).status_code == 403
    r = c.post("/api/autopilot", json={"enabled": True}, headers={"X-ApplyPilot-Token": _tok(c)})
    assert r.status_code == 200 and r.json()["autopilot_enabled"] is True
    from applypilot.webui import settings as st
    assert st.load_settings()["autopilot_enabled"] is True


def test_global_pause_disables_autopilot(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    c.post("/api/autopilot", json={"enabled": True}, headers={"X-ApplyPilot-Token": _tok(c)})
    r = c.post("/api/pause", json={}, headers={"X-ApplyPilot-Token": _tok(c)})
    assert r.status_code == 200
    from applypilot.webui import settings as st
    assert st.load_settings()["autopilot_enabled"] is False
```

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_ui_autopilot.py -v`

- [ ] **Step 4: Implement**

Add the pure decision function + endpoints + a daemon supervisor to `server.py`:

```python
def autopilot_decision(*, enabled: bool, running: bool, blocked: bool, eligible: int) -> dict:
    """Pure policy for one supervisor tick. Order matters: OFF wins; a cap
    breach pauses even if enabled; a running batch waits; empty queue idles."""
    if not enabled:
        return {"action": "idle", "reason": "autopilot off"}
    if blocked:
        return {"action": "pause", "reason": "hard cap reached"}
    if running:
        return {"action": "wait", "reason": "batch in progress"}
    if eligible <= 0:
        return {"action": "idle", "reason": "queue empty"}
    return {"action": "launch", "reason": "clear to launch"}
```

Endpoints (token-gated by the middleware from Task 5):

```python
    class AutopilotRequest(BaseModel):
        enabled: bool

    @app.post("/api/autopilot")
    def set_autopilot(body: AutopilotRequest) -> dict:
        from applypilot.webui import settings as st
        s = st.update_settings({"autopilot_enabled": bool(body.enabled)})
        return {"ok": True, "autopilot_enabled": s["autopilot_enabled"]}

    @app.post("/api/pause")
    def pause_everything(body: dict | None = None) -> dict:
        from applypilot.webui import settings as st
        st.update_settings({"autopilot_enabled": False})
        runs.stop(mode="all")
        try:
            from applypilot import database as db
            db.set_paused(db.get_connection(), "ui_pause")
        except Exception:                                # noqa: BLE001
            pass
        return {"ok": True, "paused": True}
```

Supervisor thread (started in `create_app`, opt-out via an env for tests so the TestClient doesn't spin it):

```python
    def _eligible_count() -> int:
        from applypilot.database import queue_policy
        from applypilot.webui import settings as st
        frag, params = queue_policy(min_score=int(st.load_settings()["batch"].get("min_score", 8)))
        conn = _connect(db_path)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {frag} AND application_url IS NOT NULL",
                                params).fetchone()[0]
        finally:
            conn.close()

    def _supervisor_tick() -> None:
        from applypilot.webui import settings as st, control as ctl
        s = st.load_settings()
        ledger, spend_path = _cap_inputs(app_dir)
        caps = ctl.cap_status(ledger=ledger, spend_path=spend_path, settings=s)
        d = autopilot_decision(enabled=bool(s.get("autopilot_enabled")),
                               running=runs.running, blocked=caps["blocked"],
                               eligible=_eligible_count())
        if d["action"] == "launch":
            runs.start("live_apply", s.get("batch", {}),
                       state_file=Path(app_dir) / "logs" / "worker_state.json")
        elif d["action"] == "pause":
            st.update_settings({"autopilot_enabled": False})   # cap breach => auto-pause

    if os.environ.get("APPLYPILOT_UI_NO_SUPERVISOR") != "1":
        def _loop():
            while True:
                try:
                    _supervisor_tick()
                except Exception:                        # noqa: BLE001
                    pass
                time.sleep(float(os.environ.get("APPLYPILOT_UI_TICK_S", "5")))
        threading.Thread(target=_loop, daemon=True).start()
```

Set `APPLYPILOT_UI_NO_SUPERVISOR=1` in the test fixtures' env (or `create_app` accepts a `supervisor=False` kwarg) so unit tests never auto-launch. Note this in the summary.

Notes / adapt warnings:
- The batch defaults the supervisor launches with come from `settings["batch"]` — the operator sets these once; autopilot honors them each cycle. `batch.dry_run` in autopilot is honored too (an operator can run autopilot in rehearsal).
- Auto-pause on cap breach writes `autopilot_enabled=False` so the banner + persisted state agree; the operator re-enables explicitly after raising a cap or a new day rolls the count.
- `_cap_inputs` is the module-level helper from Task 5 (adapt names).

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_ui_autopilot.py -v` → ALL PASS.
Run: `& $PY -m pytest tests/test_webui.py tests/test_ui_*.py -q` → no regressions.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/webui/server.py tests/test_ui_autopilot.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "webui: autopilot supervisor (continuous queue operation, default OFF, cap-aware auto-pause) + /api/autopilot toggle + global /api/pause (disables autopilot, stops batch, sets engine pause)"
```

---

## Task 8: Frontend Operator Console + Runs history + docs supersede

Wire the SPA: a new **Console** tab (batch panel: model/limit/workers/site/dry-vs-live + Launch; live monitor: worker rows, per-job outcomes, cost ticker; Stop-skip / Stop-all buttons; the AUTOPILOT toggle; a cap/pause banner) and enrich the existing **Runs** tab from `/api/runs`. All state-changing calls send the session token (fetched once from `/api/session`). Then supersede the "UI cannot launch live applies" docs.

**Files:**
- Modify: `src/applypilot/webui/static/index.html`
- Modify: `CLAUDE.md`, `docs/OPERATOR_CHEATSHEET.md`
- Test: `tests/test_ui_frontend.py`

- [ ] **Step 1: READ the SPA + docs lines**

READ `src/applypilot/webui/static/index.html:73-80` (nav tabs) + the existing JS `fetch`/tab-render pattern + `#toast` + `.safety` banner styles (already present). READ `CLAUDE.md:16-18` (safety rule) + `:31` (the ui line). READ `docs/OPERATOR_CHEATSHEET.md:174-183` (section 6 watch a run) + section 5 (apply). Confirm the served-HTML-assertion test style in `tests/test_webui.py:250-254`.

- [ ] **Step 2: Write failing tests** (served-HTML assertions + token flow)

```python
# tests/test_ui_frontend.py
import sqlite3

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from applypilot.webui.server import create_app  # noqa: E402


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_UI_NO_SUPERVISOR", "1")
    db = tmp_path / "applypilot.db"; sqlite3.connect(db).close()
    (tmp_path / "logs").mkdir(exist_ok=True)
    return TestClient(create_app(db_path=db, app_dir=tmp_path))


def test_console_tab_and_controls_present(tmp_path, monkeypatch):
    html = _client(tmp_path, monkeypatch).get("/").text
    assert 'data-tab="console"' in html                  # new Operator Console tab
    assert "live_apply" in html and "dryrun_apply" in html
    assert "autopilot" in html.lower()
    assert "/api/monitor" in html and "/api/run/stop" in html
    assert "/api/session" in html                        # token is fetched


def test_console_has_stop_and_pause(tmp_path, monkeypatch):
    html = _client(tmp_path, monkeypatch).get("/").text
    assert "skip" in html and "/api/pause" in html


def test_session_token_served(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    assert len(c.get("/api/session").json()["token"]) > 20
```

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_ui_frontend.py -v`

- [ ] **Step 4: Implement the SPA changes + docs**

In `index.html`:
- Add `<button data-tab="console">Console</button>` to `<nav>`.
- Add a `console` view: a batch form (model select incl. `claude-haiku-4-5-20251001`/`sonnet`, limit, workers, site-contains, a Dry-run/Live segmented control -> maps to `dryrun_apply`/`live_apply`), a **Launch** button, a **Stop (skip)** + **Stop all** + **Pause everything** button row, an **Autopilot** toggle, a `.safety` banner element (shows `caps.banner`/refusal reasons), a worker-state table (from `/api/monitor` `workers`), a per-job outcomes list (`recent_outcomes`), and a cost ticker (`cost.spend_today`/`spend_cap` + `batch_cost`).
- JS: on load, fetch `/api/session` once and store the token; a `post(url, body)` helper that sets `headers: {'X-ApplyPilot-Token': TOKEN, 'Content-Type':'application/json'}`; poll `/api/monitor` every ~2s while the console tab is active; render the banner from `caps.banner`; wire Launch->`POST /api/run {kind, ...batch}`, Stop->`POST /api/run/stop {mode}`, Pause->`POST /api/pause`, Autopilot->`POST /api/autopilot {enabled}` and reflect `autopilot_enabled` from `/api/monitor`. Handle a 409 (cap refusal) by showing `detail` in the banner/toast.
- Enrich the **Runs** tab: fetch `/api/runs` and render id/kind/dry_run/started/finished/returncode/outcome counts.

In `CLAUDE.md`:
- Replace L31's "Safe by design: cannot launch live applies." with the new posture, e.g.: "`applypilot ui` — local operator console at http://127.0.0.1:8765. **Can launch LIVE applies** through the same gated pipeline as the CLI, throttled by per-day live-count + spend caps and an explicit, default-OFF autopilot toggle; binds 127.0.0.1 only with a per-session token on state-changing actions."
- Update the L16-18 safety rule to acknowledge the UI is now an authorized live trigger: live applies remain gated by caps + explicit autopilot; the UI never bypasses `queue_policy`/broker/ledger; still confirm before enabling autopilot for a real run.

In `docs/OPERATOR_CHEATSHEET.md`:
- Add a "12. Operator Console (UI)" section: `applypilot ui` -> open http://127.0.0.1:8765 -> Console tab; set the batch (model/limit/workers), Launch a dry-run to rehearse, then Live; watch worker rows + cost ticker; Stop (skip/all); enable Autopilot for continuous operation; note the per-day caps live in `ui_settings.json` (`max_live_applies_per_day`, `spend_cap_usd_per_day`) and that a cap breach auto-pauses autopilot with a banner. Note non-local binding is unsupported in v1.

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_ui_frontend.py tests/test_webui.py -q` → ALL PASS.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/webui/static/index.html CLAUDE.md docs/OPERATOR_CHEATSHEET.md tests/test_ui_frontend.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "webui+docs: Operator Console (batch panel + live monitor + stop + autopilot toggle + cap banner) & Runs history; supersede 'UI cannot launch live applies' in CLAUDE.md + OPERATOR_CHEATSHEET"
```

---

## Task 9: Phase 4B verification

- [ ] **Step 1: Full suite green**

Run: `& $PY -m pytest tests/ -q`
Expected: ALL PASS, 0 failed. Investigate any regression, especially `-k "webui or ui_ or ledger or dashboard"`. The token-gating change to `test_webui.py` is the highest-risk edit — a leftover un-tokened POST is a test bug, not a product bug, but confirm the middleware fires (a POST with no token is 403).

- [ ] **Step 2: Lint the new/changed modules**

Run: `ruff check src/applypilot/webui src/applypilot/apply/dashboard.py src/applypilot/cli.py`
Expected: clean. Keep the deliberate `# noqa: BLE001` markers on the fail-safe/best-effort `except Exception` blocks (invariants 6/8) with a one-word reason.

- [ ] **Step 3: Prove flag-off / no-op posture (by reading + targeted tests)**

- **Autopilot OFF by default:** `settings.load_settings()` on a fresh tmp `APP_DIR` returns `autopilot_enabled=False`; `autopilot_decision(enabled=False, ...)` is always `idle`. (Task 2/7 tests.)
- **State sink zero-cost when unset:** with `APPLYPILOT_STATE_FILE` unset, `update_state` writes no file — terminal `applypilot apply` is byte-identical. (Task 6 test.)
- **Dry-run never blocked, live always cap-checked:** `may_launch(dry_run=True, ...)` is `(True, None)` even at breach; `may_launch(dry_run=False, ...)` refuses at breach. (Task 3 tests.)
- **Token required on every POST:** a POST with no/invalid token is 403. (Task 5/7 tests.)

- [ ] **Step 4: Cap-enforcement proof (synthetic, $0)**

Write a scratch script (scratchpad dir, NOT the repo) that, against a `TestClient` with a temp `APP_DIR`:
1. Seeds the submission ledger with N=cap CONFIRMED rows in the last 24h -> `POST /api/run {kind:"live_apply"}` returns **409** with a cap reason; the fake RunManager records NO spawn.
2. Seeds `spend_ledger.jsonl` above the spend cap -> live launch **409**; dry-run launch **200**.
3. Below both caps -> live launch **200** and the composed args contain `apply` and NOT `--dry-run`.
4. `POST /api/pause` flips `autopilot_enabled` to False in the persisted file and calls `runs.stop(mode="all")`.
Do NOT commit the scratch script.

- [ ] **Step 5: E2E smoke against a FAKE pipeline runner (no network, no real Chrome, no live DB, never touches E:\applypilot-data)**

Write a scratch script that points `RunManager` at a tiny fake `apply` entry (a stub that: prints a few lines, writes a couple of synthetic `review.jsonl` rows into the temp `APP_DIR/logs`, writes a `worker_state.json` snapshot, installs a SIGINT/SIGBREAK handler that exits on the 2nd signal, then sleeps). Drive: Launch (live) -> `/api/monitor` shows the worker row + cost ticker + a `recent_outcome` -> `POST /api/run/stop {mode:"all"}` graceful-stops it -> `/api/runs` shows the finished batch with an outcome. Then simulate a restart: leave a stale registry record with a dead pid and confirm `create_app` -> `reconcile_on_start` closes it as `orphaned_exited` (and one with THIS pid is adopted). Confirm the whole console composes at $0. Do NOT commit the scratch script.

- [ ] **Step 6: Posture greps (no submission-path construction in UI modules)**

Confirm by grepping the UI modules:
- `webui/server.py`, `webui/control.py`, `webui/registry.py`, `webui/settings.py` import NOTHING from `apply.launcher`, `apply.submit_broker`, `apply.browser_stream`, `apply.v2`, or `playwright`, and construct NO `SubmitBroker`/`SubmissionLedger(...)` **write** path (the only ledger use is the READ `confirmed_count_since`). The ONLY way the UI causes a submission is composing an argv for `python -m applypilot apply` (invariant 1).

Run:
`rg -n "SubmitBroker|BrowserStateStream|record_intent|route\.fetch|playwright|from applypilot.apply.launcher" src/applypilot/webui` → expect NO hits (except possibly a comment). `rg -n "\"apply\"|'apply'" src/applypilot/webui/server.py` → the ONLY apply reference is the CLI argv builder.

- [ ] **Step 7: Tree clean + report**

Run: `git status --short` → EMPTY (all Phase-4B work committed; no stray scratch files).

Summarize to the user: commits made; total new tests + pass count; the synthetic cap-enforcement + e2e-fake-runner smoke result; the reminder that the UI is an **authorized trigger, not a bypass** — every UI batch is the SAME gated `apply` subprocess, throttled by per-day live-count + spend caps, with autopilot default-OFF and a token on every state change. Note the two follow-ups deferred to v2 (below) and that the docs now say the UI CAN launch live applies.

---

## Open decisions for the controller

- **Kill-switch transport on Windows.** Recommended (encoded in Task 5): launch the batch in a **new process group** and send `CTRL_BREAK_EVENT` (mapped to the launcher's SIGBREAK handler mirroring SIGINT) for graceful skip/stop, escalating to `terminate()` + process-tree kill after a grace window. The alternative — a cooperative stop-flag file the worker polls — is more portable but does NOT reproduce the "skip current job" (1st Ctrl+C) semantic without also killing the in-flight claude/chrome. If cross-platform parity matters more than fidelity, add the stop-flag file as a second, belt-and-suspenders signal; recommended default is the signal path plus `db.set_paused` on global pause.
- **`max_live_applies_per_day` basis: CONFIRMED ledger rows vs review.jsonl `applied`.** Recommended (Task 3): count CONFIRMED submission-ledger rows — that is the durable, double-submit-safe truth (a `needs_review:unverified` is NOT a confirmed submission and should not consume the cap). review.jsonl `applied` rows are a looser proxy. If the operator wants the cap to bite on any attempt (not just confirmed), switch the basis — one-line change in `control.py`.
- **Autopilot batch size / cadence.** Recommended: autopilot launches `settings.batch` each cycle (operator-tuned once), one batch at a time, ~5s supervisor tick. A "drain continuously with `--continuous`" mode is possible (single long-lived subprocess) but complicates the kill switch + cap re-check granularity; the batch-at-a-time loop re-checks caps between batches, which is the safer default for v1.
- **Adopted-batch stdout re-attach after restart.** Recommended: on reconcile-adopt, the monitor resumes from the `worker_state.json` sink + `review.jsonl` (both durable); the live stdout console for that batch is best-effort (a restarted server cannot recover the child's already-emitted pipe). Acceptable for v1 — the durable telemetry is the source of truth.
- **Non-local binding.** Recommended (Task 5): refuse outright in v1 with a clear message. If the operator genuinely needs LAN access later, that is a v2 item requiring real auth (not just the CSRF token) + TLS — explicitly out of scope now.
- **Deferred to v2 (flag in the final report):** onboarding/profile-editor polish (explicitly out of v1); real multi-user auth + non-local binding; a per-batch stdout re-attach after restart; surfacing dangling-INTENT reconciliation actions in the UI (today the CLI/next-run handles it).
