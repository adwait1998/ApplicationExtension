# Phase 2 — Board Atlas + Freshness-First Discovery (Shadow Mode) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `ats_companies.yaml` with a SQLite-backed **Board Atlas** and a **freshness-first incremental poller** that harvests boards profile-agnostically, polls only the profile-relevant hot/warm rings on an idempotent on-wake/cron TICK, diffs job-id sets so unchanged boards cost one cheap request, and feeds every new posting through the Phase-1 gate before `store_gated`. Runs in **SHADOW MODE** behind an explicit go/no-go: the exit gate is a healthy fresh-eligible queue for the mainstream profile without exceeding per-host politeness budgets. This is the MINIMAL v1 Atlas (spec §13/§14), **not** the full Atlas.

**Architecture:** Additive, following the Phase-1 shape. New tables `boards` and `source_runs` created in `init_db` (CREATE TABLE IF NOT EXISTS, alongside `submission_ledger`/`engine_control`). A new `discovery/atlas/` package: pure helpers (`boards_repo.py` CRUD, `rings.py` ring-assignment + budget math, `politeness.py` token buckets), a `miner.py` (reuses `extract_board_from_url`; DB + bundled Common-Crawl snapshot; live CC batch is a documented optional offline script), a `validator.py` (one cheap `?content=false`-style GET per token, **no** title filter — profile-agnostic membership), and a `poller.py` (per-board id-set diff → gate → `store_gated`, reusing the 3 existing fetchers). Wired as a `applypilot atlas` Typer sub-app (`import`, `mine`, `validate`, `tick`, `report`) **and** as an `atlas` discover sub-source in `pipeline._run_discover`. Every poll writes a `source_runs` row. No daemon, no community publishing, no client ring-2 polling, no forecaster (§14).

**Tech Stack:** Python 3.11, SQLite (WAL, thread-local via `database.get_connection`), `httpx` (`httpx.MockTransport` in tests — NO live network in tests), Typer, pytest. Interpreter `PY = C:\Users\adwai\AppData\Local\Programs\Python\Python312\python.exe` (has pytest + editable applypilot; the `.venv` python does NOT).

**Spec:** `docs/superpowers/specs/2026-07-02-applypilot-v2-design.md` §7.1 (Board Atlas), §7.2 (freshness scheduler), §7.5 (queue-depth controller / adaptive freshness), §13 Phase 2, §14 YAGNI cuts.

**Prerequisite:** Phase 1 complete — `identity.py`, `gate/engine.py gate_job`, `gate/profile_map.py gate_profile`, `database.store_gated`, the `gate` pipeline stage, and gate columns on `jobs` are all landed and committed. (Verified present in the repo at plan-authoring time.)

**Conventions (same as Phase 0/1):**
- Repo root: `e:\auto-apply-pipeline`. Run all commands from there.
- `PY = C:\Users\adwai\AppData\Local\Programs\Python\Python312\python.exe`.
- Commit with one-shot identity, never push: `git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "..."`.
- **The index may contain pre-staged files.** Before EVERY commit run `git diff --cached --stat` and verify only the intended files are staged. If unexpected files are staged, `git reset` first, then re-add only what the task lists.
- TDD: write the failing test first, run it to confirm the failure mode, implement, run to green, commit.

**File structure created by this plan:**
- `src/applypilot/discovery/atlas/__init__.py` — package marker + shared constants (Task 1)
- `src/applypilot/discovery/atlas/boards_repo.py` — `boards` table CRUD (upsert/get_by_ring/mark_checked/mark_dead/get_all) (Task 1)
- `src/applypilot/discovery/atlas/importer.py` — one-time `ats_companies.yaml` → `boards` import (Task 2)
- `src/applypilot/discovery/atlas/miner.py` — token harvest from DB + bundled CC snapshot; `mine_candidates()` (Task 3)
- `src/applypilot/discovery/atlas/validator.py` — profile-agnostic board existence check via httpx (Task 4)
- `src/applypilot/discovery/atlas/politeness.py` — per-host token bucket + backoff, honest UA (Task 4)
- `src/applypilot/discovery/atlas/poller.py` — incremental id-set-diff poller → gate → `store_gated` (Task 5)
- `src/applypilot/discovery/atlas/rings.py` — ring assignment + per-tick budget math (pure functions) (Task 6)
- `src/applypilot/discovery/atlas/source_runs.py` — `source_runs` accounting CRUD (Task 7)
- `src/applypilot/discovery/atlas/tick.py` — the idempotent, resumable TICK orchestrator (Task 8)
- `src/applypilot/discovery/atlas/telemetry.py` — shadow-mode metrics + go/no-go report (Task 9)
- `scripts/mine_common_crawl.py` — documented OFFLINE CC-index batch script (Task 3; optional, not on the client path)
- `src/applypilot/data/atlas_snapshot.jsonl` — bundled snapshot of candidate tokens the client loads (Task 3)
- Modified: `src/applypilot/database.py` (`boards` + `source_runs` CREATE TABLE in `init_db`), `src/applypilot/cli.py` (`atlas` Typer sub-app), `src/applypilot/pipeline.py` (`atlas` discover sub-source)
- Tests: `tests/test_atlas_boards_repo.py`, `tests/test_atlas_import.py`, `tests/test_atlas_miner.py`, `tests/test_atlas_validator.py`, `tests/test_atlas_poller.py`, `tests/test_atlas_rings.py`, `tests/test_atlas_source_runs.py`, `tests/test_atlas_tick.py`, `tests/test_atlas_telemetry.py`

**Key invariants (load-bearing):**
1. **Registry membership is profile-agnostic** (spec §7.1). The validator does a bare existence/id-count check — it MUST NOT run the `title_matches`/`min_matching_jobs` filter that `ats_discovery._validate_jobs` uses. A board with zero design jobs today stays in the Atlas because it may post one tomorrow (and the next user is a data engineer). What a board posts is a *scheduling* input (ring assignment), never a *membership* input.
2. **Gate before store, every source (spec §5.2, Phase-1 pattern).** The poller gates each new posting with `gate_job(row, policy)` and writes via `database.store_gated(conn, row, gate, strategy=...)`, exactly like the 5 Phase-1 discovery sites. It never inserts raw rows.
3. **Incremental (spec §7.2).** Per board, fetch the cheap id-list, hash the sorted set, compare to the stored `job_id_set_hash`. Unchanged → one request, zero content fetches. Only NEW ids get a content fetch + gate + store.
4. **No daemon (spec §7.2/§14).** The TICK is idempotent and resumable; it advances a cursor and can be re-run to catch up. It is invoked by cron/on-wake or as a discover sub-source — never a long-running loop.
5. **Client polls rings 0/1 only (spec §7.2/§14).** Ring-2 (cold) refresh is a server-side snapshot concern, explicitly deferred. The client TICK reads `get_boards_by_ring(conn, rings=(0, 1))`.
6. **Politeness (spec §7.2).** All live HTTP goes through the per-host token bucket at ~4 rps/host with backoff and the honest UA `ApplyPilotBot/1.0`. Tests never hit the network — they inject an `httpx.MockTransport` client and a fake clock.

---

## Task 1: `boards` table + `boards_repo.py` CRUD

**Files:**
- Modify: `src/applypilot/database.py` (`init_db`: add `boards` CREATE TABLE + index)
- Create: `src/applypilot/discovery/atlas/__init__.py`, `src/applypilot/discovery/atlas/boards_repo.py`
- Test: `tests/test_atlas_boards_repo.py`

- [ ] **Step 1: Add the `boards` table to `init_db`**

In `src/applypilot/database.py`, immediately after the `engine_control` CREATE TABLE block (currently ~line 190, before the `conn.commit()` at ~191), add a THIRD standalone table (it is not part of the `jobs` migration path, so — like `submission_ledger` and `engine_control` — it lives in `init_db` directly, NOT in `_ALL_COLUMNS`):

```python
    # Board Atlas (v2 Phase 2). Profile-AGNOSTIC registry of ATS boards:
    # one row per (ats, token). status/ring drive the freshness scheduler;
    # job_id_set_hash powers the incremental poller's cheap unchanged-board
    # short-circuit. Created here (standalone table, not in _ALL_COLUMNS).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS boards (
            ats               TEXT NOT NULL,     -- 'greenhouse' | 'lever' | 'ashby'
            token             TEXT NOT NULL,     -- board slug, lowercased
            company_name      TEXT,              -- best-effort display name
            status            TEXT NOT NULL DEFAULT 'candidate',
                                                 -- 'candidate' (unvalidated) | 'active'
                                                 -- | 'dead' (validation/poll failing)
            ring              INTEGER,           -- 0 hot | 1 warm | 2 cold | NULL unassigned
            first_seen        TEXT NOT NULL,
            last_checked      TEXT,              -- last successful poll/validation
            last_changed      TEXT,              -- last time job_id_set_hash changed
            job_id_set_hash   TEXT,              -- sha1 of sorted job-id set (incremental diff)
            job_count         INTEGER,           -- ids seen at last poll (posting-rate signal)
            new_last_poll     INTEGER DEFAULT 0, -- new ids at last poll (posting-rate signal)
            error_streak      INTEGER NOT NULL DEFAULT 0,
            source            TEXT,              -- 'yaml_import' | 'db_mining' | 'cc_snapshot' | 'search'
            PRIMARY KEY (ats, token)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_boards_ring_status "
        "ON boards(ring, status)"
    )
```

- [ ] **Step 2: Write failing tests**

```python
# tests/test_atlas_boards_repo.py
from applypilot import database as db
from applypilot.discovery.atlas import boards_repo as repo


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


def test_upsert_inserts_then_updates(tmp_path):
    conn = _conn(tmp_path)
    assert repo.upsert_board(conn, "greenhouse", "chime", company_name="Chime", source="yaml_import") is True
    # second upsert of same PK is an update, not a new row
    assert repo.upsert_board(conn, "greenhouse", "chime", company_name="Chime Inc", source="db_mining") is False
    rows = repo.get_all(conn)
    assert len(rows) == 1
    assert rows[0]["company_name"] == "Chime Inc"        # updated
    assert rows[0]["source"] == "yaml_import"            # source is NOT overwritten (first-seen wins)
    assert rows[0]["status"] == "candidate"
    assert rows[0]["first_seen"]


def test_upsert_is_case_normalized(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "Greenhouse", "Chime", source="yaml_import")
    repo.upsert_board(conn, "greenhouse", "chime", source="db_mining")
    assert len(repo.get_all(conn)) == 1                  # folded to one row


def test_get_boards_by_ring_filters(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "a", source="x")
    repo.upsert_board(conn, "lever", "b", source="x")
    repo.upsert_board(conn, "ashby", "c", source="x")
    repo.set_ring(conn, "greenhouse", "a", 0)
    repo.set_ring(conn, "lever", "b", 1)
    repo.set_ring(conn, "ashby", "c", 2)
    repo.set_status(conn, "greenhouse", "a", "active")
    repo.set_status(conn, "lever", "b", "active")
    repo.set_status(conn, "ashby", "c", "active")
    got = repo.get_boards_by_ring(conn, rings=(0, 1))
    tokens = {(r["ats"], r["token"]) for r in got}
    assert tokens == {("greenhouse", "a"), ("lever", "b")}   # ring 2 excluded


def test_get_boards_by_ring_excludes_dead_and_candidate(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "live", source="x")
    repo.upsert_board(conn, "greenhouse", "dead", source="x")
    repo.upsert_board(conn, "greenhouse", "unval", source="x")
    for t in ("live", "dead", "unval"):
        repo.set_ring(conn, "greenhouse", t, 0)
    repo.set_status(conn, "greenhouse", "live", "active")
    repo.set_status(conn, "greenhouse", "dead", "dead")
    # 'unval' stays 'candidate'
    got = {r["token"] for r in repo.get_boards_by_ring(conn, rings=(0, 1))}
    assert got == {"live"}


def test_mark_checked_records_diff_and_clears_error(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "a", source="x")
    repo.mark_dead(conn, "greenhouse", "a")              # bumps error_streak, sets status dead
    repo.mark_checked(conn, "greenhouse", "a",
                      job_id_set_hash="h1", job_count=10, new_last_poll=10, changed=True)
    row = repo.get(conn, "greenhouse", "a")
    assert row["job_id_set_hash"] == "h1"
    assert row["job_count"] == 10 and row["new_last_poll"] == 10
    assert row["error_streak"] == 0                      # a successful check clears the streak
    assert row["status"] == "active"                     # revived
    assert row["last_checked"] and row["last_changed"]


def test_mark_checked_unchanged_leaves_last_changed(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "a", source="x")
    repo.mark_checked(conn, "greenhouse", "a", job_id_set_hash="h1", job_count=5, new_last_poll=5, changed=True)
    first_changed = repo.get(conn, "greenhouse", "a")["last_changed"]
    repo.mark_checked(conn, "greenhouse", "a", job_id_set_hash="h1", job_count=5, new_last_poll=0, changed=False)
    row = repo.get(conn, "greenhouse", "a")
    assert row["last_changed"] == first_changed          # unchanged poll does not move last_changed
    assert row["last_checked"] >= first_changed          # but last_checked advances


def test_mark_dead_after_streak(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "a", source="x")
    repo.mark_dead(conn, "greenhouse", "a")
    repo.mark_dead(conn, "greenhouse", "a")
    row = repo.get(conn, "greenhouse", "a")
    assert row["error_streak"] == 2 and row["status"] == "dead"
```

- [ ] **Step 3: Run — expect failure** (`ModuleNotFoundError: No module named 'applypilot.discovery.atlas'`)

Run: `& $PY -m pytest tests/test_atlas_boards_repo.py -v`

- [ ] **Step 4: Implement the package + repo**

`src/applypilot/discovery/atlas/__init__.py`:

```python
"""Board Atlas — profile-agnostic ATS board registry + freshness-first
incremental poller (v2 Phase 2). See spec §7.1/§7.2/§7.5.

No daemon: the TICK (tick.py) is idempotent/resumable and runs on cron or
on-wake, or as the 'atlas' discover sub-source. All live HTTP is politeness-
gated (politeness.py); tests inject httpx.MockTransport + a fake clock."""
from __future__ import annotations

ATS_TYPES = ("greenhouse", "lever", "ashby")
USER_AGENT = "Mozilla/5.0 (compatible; ApplyPilotBot/1.0)"
```

`src/applypilot/discovery/atlas/boards_repo.py`:

```python
"""CRUD for the `boards` table (created in database.init_db). Every public fn
takes an explicit sqlite3.Connection so it composes with the thread-local
get_connection() and with in-memory test DBs alike."""
from __future__ import annotations

from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_board(conn, ats: str, token: str, *, company_name: str | None = None,
                 source: str | None = None) -> bool:
    """Insert (status='candidate') or update a board row. Returns True if a new
    row was inserted. `first_seen` and `source` are set once (first sighting
    wins); company_name is refreshed on every upsert. ats/token are lowercased
    so the registry never double-counts case variants."""
    ats, token = ats.lower(), token.lower()
    existing = get(conn, ats, token)
    if existing is None:
        conn.execute(
            "INSERT INTO boards (ats, token, company_name, status, first_seen, "
            "error_streak, source) VALUES (?,?,?,?,?,0,?)",
            (ats, token, company_name, "candidate", _now(), source),
        )
        conn.commit()
        return True
    if company_name is not None:
        conn.execute("UPDATE boards SET company_name = ? WHERE ats = ? AND token = ?",
                     (company_name, ats, token))
        conn.commit()
    return False


def get(conn, ats: str, token: str) -> dict | None:
    row = conn.execute("SELECT * FROM boards WHERE ats = ? AND token = ?",
                       (ats.lower(), token.lower())).fetchone()
    return dict(row) if row else None


def get_all(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM boards ORDER BY ats, token").fetchall()]


def get_boards_by_ring(conn, *, rings: tuple[int, ...] = (0, 1)) -> list[dict]:
    """Active boards in the given rings. The client TICK calls this with (0, 1)
    only (spec §7.2/§14: no client ring-2 polling). Excludes 'candidate' (not
    yet validated) and 'dead' boards."""
    placeholders = ",".join("?" for _ in rings)
    rows = conn.execute(
        f"SELECT * FROM boards WHERE status = 'active' AND ring IN ({placeholders}) "
        "ORDER BY ring, last_checked IS NOT NULL, last_checked",
        tuple(rings),
    ).fetchall()
    return [dict(r) for r in rows]


def set_ring(conn, ats: str, token: str, ring: int) -> None:
    conn.execute("UPDATE boards SET ring = ? WHERE ats = ? AND token = ?",
                 (ring, ats.lower(), token.lower()))
    conn.commit()


def set_status(conn, ats: str, token: str, status: str) -> None:
    conn.execute("UPDATE boards SET status = ? WHERE ats = ? AND token = ?",
                 (status, ats.lower(), token.lower()))
    conn.commit()


def mark_checked(conn, ats: str, token: str, *, job_id_set_hash: str,
                 job_count: int, new_last_poll: int, changed: bool) -> None:
    """Record a successful poll/validation. Clears error_streak, revives the
    board to 'active', advances last_checked; advances last_changed only when
    the id-set actually changed."""
    now = _now()
    if changed:
        conn.execute(
            "UPDATE boards SET status='active', error_streak=0, last_checked=?, "
            "last_changed=?, job_id_set_hash=?, job_count=?, new_last_poll=? "
            "WHERE ats=? AND token=?",
            (now, now, job_id_set_hash, job_count, new_last_poll, ats.lower(), token.lower()),
        )
    else:
        conn.execute(
            "UPDATE boards SET status='active', error_streak=0, last_checked=?, "
            "job_id_set_hash=?, job_count=?, new_last_poll=? WHERE ats=? AND token=?",
            (now, job_id_set_hash, job_count, new_last_poll, ats.lower(), token.lower()),
        )
    conn.commit()


def mark_dead(conn, ats: str, token: str) -> None:
    """Bump error_streak and set status='dead'. A later successful mark_checked
    revives it (spec §7.2: zero-yield/failing sources demote automatically)."""
    conn.execute(
        "UPDATE boards SET error_streak = error_streak + 1, status = 'dead' "
        "WHERE ats = ? AND token = ?",
        (ats.lower(), token.lower()),
    )
    conn.commit()
```

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_atlas_boards_repo.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/database.py src/applypilot/discovery/atlas/__init__.py src/applypilot/discovery/atlas/boards_repo.py tests/test_atlas_boards_repo.py
git diff --cached --stat   # exactly these 4 files
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "atlas: boards table + profile-agnostic CRUD (upsert/get_by_ring/mark_checked/mark_dead)"
```

---

## Task 2: One-time `ats_companies.yaml` → `boards` import (`applypilot atlas import`)

The 56 curated tokens (greenhouse 35, lever 3, ashby 18 — verified against `src/applypilot/config/ats_companies.yaml`) seed the Atlas so Phase 2 never regresses the live queue. Import is idempotent (re-running upserts, never duplicates).

**Files:**
- Create: `src/applypilot/discovery/atlas/importer.py`
- Test: `tests/test_atlas_import.py`

- [ ] **Step 1: READ the registry loader first**

READ `src/applypilot/discovery/ats_discovery.py:115-132` (`load_ats_registry`). It merges package defaults + the user overlay and returns a dict whose `greenhouse`/`lever`/`ashby` keys are already `_clean_token`-normalized lists. The importer reuses it so the import path matches the crawler's view of the registry exactly. Confirm the three keys are present and are lists of strings before writing the implementation.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_atlas_import.py
from applypilot import database as db
from applypilot.discovery.atlas import boards_repo as repo
from applypilot.discovery.atlas import importer


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


def test_import_seeds_boards_from_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "load_ats_registry", lambda: {
        "greenhouse": ["figma", "notion", "chime"],
        "lever": ["plaid", "spotify"],
        "ashby": ["vercel"],
        "title_keywords": ["product designer"],   # non-ATS keys ignored
    })
    conn = _conn(tmp_path)
    n = importer.import_registry(conn)
    assert n == 6
    rows = repo.get_all(conn)
    assert {(r["ats"], r["token"]) for r in rows} == {
        ("greenhouse", "figma"), ("greenhouse", "notion"), ("greenhouse", "chime"),
        ("lever", "plaid"), ("lever", "spotify"), ("ashby", "vercel"),
    }
    assert all(r["source"] == "yaml_import" and r["status"] == "candidate" for r in rows)


def test_import_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "load_ats_registry", lambda: {
        "greenhouse": ["figma"], "lever": [], "ashby": [],
    })
    conn = _conn(tmp_path)
    assert importer.import_registry(conn) == 1
    assert importer.import_registry(conn) == 0     # second run adds nothing new
    assert len(repo.get_all(conn)) == 1


def test_import_real_registry_has_56_boards(tmp_path):
    # Integration: the shipped package ats_companies.yaml (no user overlay in tmp).
    conn = _conn(tmp_path)
    n = importer.import_registry(conn)
    counts = {}
    for r in repo.get_all(conn):
        counts[r["ats"]] = counts.get(r["ats"], 0) + 1
    # Verified counts at plan authoring: gh 35, lever 3, ashby 18 = 56.
    # A user overlay merged by load_ats_registry can only ADD, so assert >=.
    assert counts.get("greenhouse", 0) >= 35
    assert counts.get("lever", 0) >= 3
    assert counts.get("ashby", 0) >= 18
    assert n >= 56
```

Note on `test_import_real_registry_has_56_boards`: it depends on `config.APP_DIR`. If a user overlay `ats_companies.yaml` exists in the real APP_DIR on this machine the counts can exceed 56 (hence `>=`). If the test proves flaky against a live overlay, `monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))` before `_conn` to force a clean APP_DIR — READ `config.py:9` (`APP_DIR = Path(os.environ.get("APPLYPILOT_DIR", ...))`) to confirm the env var name before relying on it.

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_atlas_import.py -v`

- [ ] **Step 4: Implement**

`src/applypilot/discovery/atlas/importer.py`:

```python
"""One-time seed of the boards table from the curated ats_companies.yaml
registry (the 56 tokens). Idempotent — safe to re-run; upsert_board never
duplicates. Membership is profile-agnostic, so ALL registry tokens import
regardless of what they post."""
from __future__ import annotations

from applypilot.discovery.ats_discovery import load_ats_registry
from applypilot.discovery.atlas import ATS_TYPES
from applypilot.discovery.atlas import boards_repo as repo


def import_registry(conn) -> int:
    """Upsert every ats token from the merged registry into boards.
    Returns the count of NEWLY-inserted rows."""
    registry = load_ats_registry()
    added = 0
    for ats in ATS_TYPES:
        for token in registry.get(ats, []) or []:
            if repo.upsert_board(conn, ats, str(token), source="yaml_import"):
                added += 1
    return added
```

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_atlas_import.py -v`
Expected: ALL PASS. (The `atlas import` CLI wiring lands in Task 10.)

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/discovery/atlas/importer.py tests/test_atlas_import.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "atlas: one-time ats_companies.yaml -> boards import (idempotent, 56 seed tokens)"
```

---

## Task 3: Registry miner (DB + bundled Common-Crawl snapshot) + offline CC batch script

Harvest candidate tokens profile-agnostically. Two client-side sources (cheap, no ATS load): mine the existing jobs DB via `extract_board_from_url`, and load a **bundled snapshot JSONL** of tokens produced offline by the CC batch. The live CC mining is a documented OFFLINE script (`scripts/mine_common_crawl.py`) that emits the snapshot — it is NOT run on the client path in v1 (spec §7.1: CC batch is offline, zero ATS load, re-run quarterly; keep the live-mining piece minimal/optional).

**Files:**
- Create: `src/applypilot/discovery/atlas/miner.py`
- Create: `src/applypilot/data/atlas_snapshot.jsonl` (bundled snapshot; start with a small hand-seeded set — see Step 5)
- Create: `scripts/mine_common_crawl.py` (offline batch; documented, not imported by the client)
- Test: `tests/test_atlas_miner.py`

- [ ] **Step 1: READ `extract_board_from_url` + `_candidates_from_db`**

READ `src/applypilot/discovery/ats_discovery.py:71-106` (`extract_board_from_url` → `BoardCandidate(ats, token, source=url)`) and `:156-171` (`_candidates_from_db`, the exact SELECT it runs over `url`/`application_url`). The miner reuses `extract_board_from_url` verbatim — do not re-implement host parsing.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_atlas_miner.py
import json

from applypilot import database as db
from applypilot.discovery.atlas import boards_repo as repo
from applypilot.discovery.atlas import miner


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


def test_mine_from_db_uses_extract_board_from_url(tmp_path):
    conn = _conn(tmp_path)
    conn.executemany(
        "INSERT INTO jobs (url, application_url, discovered_at) VALUES (?,?,datetime('now'))",
        [
            ("https://boards.greenhouse.io/newco/jobs/1", "https://boards.greenhouse.io/newco/jobs/1"),
            ("https://jobs.lever.co/leverco/abc", "https://jobs.lever.co/leverco/abc"),
            ("https://example.com/careers/x", "https://example.com/careers/x"),  # not an ATS
        ],
    )
    conn.commit()
    cands = miner.mine_from_db(conn)
    assert ("greenhouse", "newco") in cands
    assert ("lever", "leverco") in cands
    assert all(a in ("greenhouse", "lever", "ashby") for a, _ in cands)
    assert len(cands) == 2


def test_mine_from_snapshot_reads_jsonl(tmp_path):
    snap = tmp_path / "snap.jsonl"
    snap.write_text(
        json.dumps({"ats": "ashby", "token": "snapco", "company_name": "SnapCo"}) + "\n"
        + json.dumps({"ats": "greenhouse", "token": "gco"}) + "\n"
        + "\n"                                       # blank line tolerated
        + json.dumps({"ats": "notanats", "token": "x"}) + "\n",  # dropped
        encoding="utf-8",
    )
    cands = miner.mine_from_snapshot(snap)
    assert ("ashby", "snapco") in cands
    assert ("greenhouse", "gco") in cands
    assert ("notanats", "x") not in cands
    assert cands[("ashby", "snapco")] == "SnapCo"    # company_name carried when present


def test_mine_candidates_upserts_as_candidate_status(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO jobs (url, application_url, discovered_at) "
                 "VALUES ('https://boards.greenhouse.io/dbco/jobs/9','https://boards.greenhouse.io/dbco/jobs/9',datetime('now'))")
    conn.commit()
    snap = tmp_path / "snap.jsonl"
    snap.write_text(json.dumps({"ats": "lever", "token": "snco"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(miner, "SNAPSHOT_PATH", snap)
    added = miner.mine_candidates(conn)
    assert added == 2
    rows = {(r["ats"], r["token"]): r for r in repo.get_all(conn)}
    assert rows[("greenhouse", "dbco")]["source"] == "db_mining"
    assert rows[("lever", "snco")]["source"] == "cc_snapshot"
    assert all(r["status"] == "candidate" for r in rows.values())   # unvalidated until Task 4
```

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_atlas_miner.py -v`

- [ ] **Step 4: Implement `miner.py`**

```python
"""Profile-agnostic candidate-token harvest for the Board Atlas.

Client-side sources (cheap, no ATS load):
  - mine_from_db: reuse extract_board_from_url over the jobs table.
  - mine_from_snapshot: read the bundled JSONL produced OFFLINE by
    scripts/mine_common_crawl.py (spec §7.1 — CC mining is an offline batch,
    re-run quarterly; the client only loads its output).
mine_candidates() upserts everything as status='candidate'; Task 4's validator
promotes survivors to 'active'."""
from __future__ import annotations

import json
from pathlib import Path

from applypilot.discovery.ats_discovery import extract_board_from_url
from applypilot.discovery.atlas import ATS_TYPES
from applypilot.discovery.atlas import boards_repo as repo

SNAPSHOT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "atlas_snapshot.jsonl"


def mine_from_db(conn) -> set[tuple[str, str]]:
    """Harvest (ats, token) pairs from stored job URLs via extract_board_from_url."""
    rows = conn.execute(
        "SELECT url, application_url FROM jobs "
        "WHERE url LIKE '%greenhouse%' OR application_url LIKE '%greenhouse%' "
        "OR url LIKE '%lever.co%' OR application_url LIKE '%lever.co%' "
        "OR url LIKE '%ashbyhq.com%' OR application_url LIKE '%ashbyhq.com%'"
    ).fetchall()
    out: set[tuple[str, str]] = set()
    for row in rows:
        for value in (row["url"], row["application_url"]):
            cand = extract_board_from_url(value or "")
            if cand and cand.ats in ATS_TYPES and cand.token:
                out.add((cand.ats, cand.token))
    return out


def mine_from_snapshot(path: Path | None = None) -> dict[tuple[str, str], str | None]:
    """Load the bundled CC snapshot. Returns {(ats, token): company_name|None}."""
    path = path or SNAPSHOT_PATH
    out: dict[tuple[str, str], str | None] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ats, token = str(rec.get("ats", "")).lower(), str(rec.get("token", "")).lower()
        if ats not in ATS_TYPES or not token:
            continue
        out[(ats, token)] = rec.get("company_name")
    return out


def mine_candidates(conn, *, include_db: bool = True, include_snapshot: bool = True) -> int:
    """Upsert all mined candidates as status='candidate'. Returns newly-added count."""
    added = 0
    if include_db:
        for ats, token in mine_from_db(conn):
            if repo.upsert_board(conn, ats, token, source="db_mining"):
                added += 1
    if include_snapshot:
        for (ats, token), name in mine_from_snapshot().items():
            if repo.upsert_board(conn, ats, token, company_name=name, source="cc_snapshot"):
                added += 1
    return added
```

- [ ] **Step 5: Create the bundled snapshot + document the offline CC batch**

Create `src/applypilot/data/atlas_snapshot.jsonl` with a small, hand-verified seed (the offline batch expands it later — keeping v1 minimal per §14). One JSON object per line, e.g.:

```jsonl
{"ats": "greenhouse", "token": "figma", "company_name": "Figma"}
{"ats": "ashby", "token": "openai", "company_name": "OpenAI"}
{"ats": "lever", "token": "plaid", "company_name": "Plaid"}
```

(Ship ~20-50 high-signal tokens here; overlap with the YAML import is fine — `upsert_board` folds them. Do not machine-generate thousands in v1.)

Ensure the package ships this data file: READ `pyproject.toml` (or `setup.cfg`/`MANIFEST.in`) and confirm `*.jsonl` under `applypilot/data/` is included in `package-data`/`include-package-data`. If not, add `"applypilot.data": ["*.jsonl"]` (or the equivalent glob) — note the exact edit in the task summary; adapt to whatever build backend the file uses.

Create `scripts/mine_common_crawl.py` as a documented OFFLINE script (NOT imported by the client). Its module docstring must state: it queries the Common Crawl URL index (`http://index.commoncrawl.org/`) for `boards.greenhouse.io`, `job-boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com` host patterns, runs each hit through `extract_board_from_url`, dedupes, and writes `src/applypilot/data/atlas_snapshot.jsonl`. It runs on the maintainer's machine, quarterly, with its own politeness; the client never invokes it. Provide a minimal but real `main()` (argparse over a `--cc-index` crawl id and `--out` path, using `httpx` + `extract_board_from_url`) so the script is runnable, not a stub — but do not wire it into the CLI.

- [ ] **Step 6: Run — expect pass**

Run: `& $PY -m pytest tests/test_atlas_miner.py -v`
Expected: ALL PASS.

- [ ] **Step 7: Commit**

```powershell
git reset
git add src/applypilot/discovery/atlas/miner.py src/applypilot/data/atlas_snapshot.jsonl scripts/mine_common_crawl.py tests/test_atlas_miner.py
# add pyproject.toml too IF you edited package-data in Step 5
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "atlas: registry miner (DB + bundled CC snapshot) + offline CC batch script"
```

---

## Task 4: Per-host politeness + profile-agnostic board validator

Promote `candidate` boards to `active` with ONE cheap existence check per token. **Drop the `min_matching_jobs` title filter** — membership is profile-agnostic (spec §7.1); the check only confirms the board exists and how many postings it has. All HTTP goes through a per-host token bucket (~4 rps/host), honest UA, backoff on 429/5xx.

**Files:**
- Create: `src/applypilot/discovery/atlas/politeness.py`
- Create: `src/applypilot/discovery/atlas/validator.py`
- Test: `tests/test_atlas_validator.py`

- [ ] **Step 1: READ the cheap-check endpoints + the mock pattern**

READ `src/applypilot/discovery/ats_discovery.py:347-409` (`validate_board`) for the exact cheap URLs: Greenhouse `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false`, Lever `https://api.lever.co/v0/postings/{token}?mode=json`, Ashby `https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false`, and how each response's job list is shaped (`data["jobs"]` for gh/ashby, top-level list for lever). READ `tests/test_theirstack_discovery.py:55-90` for the `httpx.Client(transport=httpx.MockTransport(handler))` pattern — the validator must accept an injected `client` so tests never hit the network.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_atlas_validator.py
import httpx

from applypilot import database as db
from applypilot.discovery.atlas import boards_repo as repo
from applypilot.discovery.atlas import politeness, validator


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


# --- politeness (pure, fake clock) ---

def test_token_bucket_rate_limits_per_host():
    t = {"now": 0.0}
    slept = []
    b = politeness.HostRateLimiter(rps=4.0, clock=lambda: t["now"],
                                   sleep=lambda s: (slept.append(s), t.__setitem__("now", t["now"] + s)))
    for _ in range(3):
        b.acquire("boards-api.greenhouse.io")
    # 3 requests at 4 rps on one host => ~0.25s min spacing between the 2nd and 3rd
    assert sum(slept) >= 0.25 - 1e-9
    # a different host is not throttled by the first host's budget
    slept.clear()
    b.acquire("api.lever.co")
    assert sum(slept) == 0


def test_backoff_grows_on_repeated_failures():
    b = politeness.Backoff(base=0.5, cap=8.0)
    assert b.delay(0) == 0.5
    assert b.delay(1) == 1.0
    assert b.delay(3) == 4.0
    assert b.delay(10) == 8.0        # capped


# --- validator (mock transport, no network) ---

def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_validate_existing_greenhouse_board_becomes_active(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "newco", source="db_mining")

    def handler(req):
        assert req.url.host == "boards-api.greenhouse.io"
        assert "content=false" in str(req.url)      # cheap existence check
        return httpx.Response(200, json={"jobs": [{"id": 1}, {"id": 2}, {"id": 3}]})

    res = validator.validate_board(conn, "greenhouse", "newco", client=_client(handler))
    assert res.ok is True and res.job_count == 3
    row = repo.get(conn, "greenhouse", "newco")
    assert row["status"] == "active" and row["job_count"] == 3


def test_validator_does_NOT_title_filter(tmp_path):
    # A board with zero design jobs still validates: membership is profile-agnostic.
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "ashby", "warehouseco", source="cc_snapshot")

    def handler(req):
        return httpx.Response(200, json={"jobs": [{"title": "Forklift Operator"}]})

    res = validator.validate_board(conn, "ashby", "warehouseco", client=_client(handler))
    assert res.ok is True and res.job_count == 1     # kept despite no matching titles
    assert repo.get(conn, "ashby", "warehouseco")["status"] == "active"


def test_validate_dead_board_404_marks_dead(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "lever", "goneco", source="db_mining")

    def handler(req):
        return httpx.Response(404, json={})

    res = validator.validate_board(conn, "lever", "goneco", client=_client(handler))
    assert res.ok is False
    row = repo.get(conn, "lever", "goneco")
    assert row["status"] == "dead" and row["error_streak"] == 1


def test_validate_empty_board_is_active_with_zero(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "emptyco", source="db_mining")

    def handler(req):
        return httpx.Response(200, json={"jobs": []})

    res = validator.validate_board(conn, "greenhouse", "emptyco", client=_client(handler))
    # exists but posts nothing right now -> still a member (may post tomorrow)
    assert res.ok is True and res.job_count == 0
    assert repo.get(conn, "greenhouse", "emptyco")["status"] == "active"
```

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_atlas_validator.py -v`

- [ ] **Step 4: Implement `politeness.py`**

```python
"""Per-host politeness for all Atlas live HTTP (spec §7.2: token buckets,
backoff, honest bot UA, well inside per-host budgets). Pure/injectable clock +
sleep so tests never actually wait."""
from __future__ import annotations

import time
from collections import defaultdict


class HostRateLimiter:
    """Simple per-host minimum-spacing limiter (target ~4 rps/host)."""

    def __init__(self, rps: float = 4.0, *, clock=time.monotonic, sleep=time.sleep):
        self._min_gap = 1.0 / rps
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = defaultdict(lambda: float("-inf"))

    def acquire(self, host: str) -> None:
        now = self._clock()
        wait = self._last[host] + self._min_gap - now
        if wait > 0:
            self._sleep(wait)
            now = self._clock()
        self._last[host] = now


class Backoff:
    """Exponential backoff delay for repeated failures, capped."""

    def __init__(self, base: float = 1.0, cap: float = 60.0):
        self._base = base
        self._cap = cap

    def delay(self, attempt: int) -> float:
        return min(self._cap, self._base * (2 ** attempt))
```

- [ ] **Step 5: Implement `validator.py`**

```python
"""Profile-AGNOSTIC board validator. One cheap existence check per token; a
board is a member iff its public API responds — what it posts is irrelevant to
membership (spec §7.1). Explicitly does NOT run title_matches/min_matching_jobs
(that is the crawler's scheduling concern, not the registry's)."""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from applypilot.discovery.atlas import USER_AGENT
from applypilot.discovery.atlas import boards_repo as repo
from applypilot.discovery.atlas.politeness import HostRateLimiter

_TIMEOUT = 8

# (host, url_template, json-list extractor). Cheap variants only.
_ENDPOINTS = {
    "greenhouse": ("boards-api.greenhouse.io",
                   "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false",
                   lambda d: d.get("jobs", [])),
    "lever": ("api.lever.co",
              "https://api.lever.co/v0/postings/{token}?mode=json",
              lambda d: d if isinstance(d, list) else []),
    "ashby": ("api.ashbyhq.com",
              "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false",
              lambda d: d.get("jobs", [])),
}


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    job_count: int = 0
    error: str | None = None


def validate_board(conn, ats: str, token: str, *, client: httpx.Client | None = None,
                   limiter: HostRateLimiter | None = None) -> ValidationResult:
    spec = _ENDPOINTS.get(ats)
    if spec is None:
        return ValidationResult(False, error="unknown_ats")
    host, tmpl, extract = spec
    owns_client = client is None
    client = client or httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT})
    try:
        if limiter is not None:
            limiter.acquire(host)
        resp = client.get(tmpl.format(token=token), headers={"User-Agent": USER_AGENT})
        if resp.status_code >= 400:
            repo.mark_dead(conn, ats, token)
            return ValidationResult(False, error=f"http_{resp.status_code}")
        jobs = extract(resp.json())
        count = len(jobs)
        # Profile-agnostic: existence alone promotes to 'active'. Ring assignment
        # (Task 6) decides HOW OFTEN to poll based on what it posts.
        repo.set_status(conn, ats, token, "active")
        # id-set hash left for the poller; here we just record existence + count.
        repo.mark_checked(conn, ats, token, job_id_set_hash=repo.get(conn, ats, token).get("job_id_set_hash") or "",
                          job_count=count, new_last_poll=0, changed=False)
        return ValidationResult(True, job_count=count)
    except Exception as exc:  # noqa: BLE001 — one bad board must not kill a batch
        repo.mark_dead(conn, ats, token)
        return ValidationResult(False, error=str(exc))
    finally:
        if owns_client:
            client.close()
```

Note: `mark_checked` sets `status='active'` and clears `error_streak`, so the explicit `set_status(...,'active')` above is belt-and-suspenders and can be dropped if the test `test_validate_existing_greenhouse_board_becomes_active` passes without it — READ your `mark_checked` from Task 1 and simplify accordingly.

- [ ] **Step 6: Run — expect pass**

Run: `& $PY -m pytest tests/test_atlas_validator.py -v`
Expected: ALL PASS. If `test_token_bucket_rate_limits_per_host` fails, check the fake `sleep` also advances the fake clock (the test's `sleep` lambda does).

- [ ] **Step 7: Commit**

```powershell
git reset
git add src/applypilot/discovery/atlas/politeness.py src/applypilot/discovery/atlas/validator.py tests/test_atlas_validator.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "atlas: per-host token-bucket politeness + profile-agnostic board validator (no title filter)"
```

---

## Task 5: Incremental poller — id-set diff → gate → store_gated

Per board: fetch the job-id set (cheap list), hash it, compare to the stored `job_id_set_hash`. Unchanged → one request, done. Changed → content-fetch ONLY the new ids (reuse the 3 existing fetchers), gate each with `gate_job`, and `store_gated`. This is the heart of spec §7.2 (incremental, ~2KB unchanged request).

**Files:**
- Create: `src/applypilot/discovery/atlas/poller.py`
- Test: `tests/test_atlas_poller.py`

- [ ] **Step 1: READ the 3 fetchers + the id fields + gate/store**

READ `src/applypilot/discovery/ats_boards.py:50-174` for the exact JSON shapes. The **id fields** for the cheap id-set are: Greenhouse `j["id"]` (int; also has `absolute_url`), Lever `j["id"]` (uuid string; `hostedUrl`), Ashby `j["id"]` (uuid; `jobUrl`). Confirm each has an `id` field by reading the fetchers (they read `absolute_url`/`hostedUrl`/`jobUrl` — verify `id` presence; if Ashby's board API omits `id`, fall back to hashing the `jobUrl` set — READ the response shape and adapt, noting which key you used). READ `src/applypilot/database.py:563-591` (`store_gated` signature and which `job` keys it reads: `url, title, salary, description, full_description, application_url, location, site, posted_at, detail_scraped_at, detail_error`) and `src/applypilot/gate/engine.py` `gate_job(job, profile)` + `src/applypilot/gate/profile_map.py gate_profile(profile, search_cfg)`.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_atlas_poller.py
import httpx

from applypilot import database as db
from applypilot.discovery.atlas import boards_repo as repo
from applypilot.discovery.atlas import poller


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


POLICY = {"geo": {"remote_ok": True, "remote_scope": "US", "onsite_regions": ["us-ca"]},
          "seniority": {"accept_bands": ["senior"], "ic_only": True},
          "needs_sponsorship": False, "workday_accounts": []}


def _gh_full(ids_titles):
    # Greenhouse content=true response shape (subset the fetcher reads).
    return {"jobs": [
        {"id": i, "title": t, "absolute_url": f"https://boards.greenhouse.io/pollco/jobs/{i}",
         "location": {"name": "Remote - US"}, "content": "<p>Design systems.</p>",
         "first_published": "2026-07-01T00:00:00Z"}
        for i, t in ids_titles]}


def test_first_poll_stores_all_new_ids(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "pollco", source="x")
    repo.set_status(conn, "greenhouse", "pollco", "active")

    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json=_gh_full([(1, "Senior Product Designer"),
                                                  (2, "Staff Product Designer")]))

    res = poller.poll_board(conn, repo.get(conn, "greenhouse", "pollco"), POLICY, client=_client(handler))
    assert res.new_ids == 2 and res.stored == 2
    stored = conn.execute("SELECT COUNT(*) FROM jobs WHERE strategy = 'atlas:greenhouse'").fetchone()[0]
    assert stored == 2
    row = repo.get(conn, "greenhouse", "pollco")
    assert row["job_id_set_hash"] and row["job_count"] == 2


def test_unchanged_board_short_circuits_no_content_fetch(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "pollco", source="x")
    repo.set_status(conn, "greenhouse", "pollco", "active")
    resp_json = _gh_full([(1, "Senior Product Designer")])

    def handler(req):
        # cheap id-list variant and full variant both answerable; assert we only
        # ask for content on the FIRST poll, not the second.
        return httpx.Response(200, json=resp_json)

    poller.poll_board(conn, repo.get(conn, "greenhouse", "pollco"), POLICY, client=_client(handler))
    before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    res2 = poller.poll_board(conn, repo.get(conn, "greenhouse", "pollco"), POLICY, client=_client(handler))
    after = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert res2.new_ids == 0 and res2.stored == 0
    assert before == after                              # no new rows on unchanged poll


def test_only_new_ids_stored_on_delta(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "pollco", source="x")
    repo.set_status(conn, "greenhouse", "pollco", "active")

    state = {"jobs": _gh_full([(1, "Senior Product Designer")])}

    def handler(req):
        return httpx.Response(200, json=state["jobs"])

    poller.poll_board(conn, repo.get(conn, "greenhouse", "pollco"), POLICY, client=_client(handler))
    # a new posting appears
    state["jobs"] = _gh_full([(1, "Senior Product Designer"), (2, "Staff Product Designer")])
    res = poller.poll_board(conn, repo.get(conn, "greenhouse", "pollco"), POLICY, client=_client(handler))
    assert res.new_ids == 1 and res.stored == 1         # only id=2 is new


def test_ineligible_new_job_is_stored_but_gated(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "pollco", source="x")
    repo.set_status(conn, "greenhouse", "pollco", "active")

    def handler(req):
        return httpx.Response(200, json=_gh_full([(9, "Engineering Manager")]))  # mgmt -> ineligible

    poller.poll_board(conn, repo.get(conn, "greenhouse", "pollco"), POLICY, client=_client(handler))
    row = conn.execute("SELECT gate_result FROM jobs WHERE strategy='atlas:greenhouse'").fetchone()
    assert row is not None                              # always stored (audit)
    assert row["gate_result"] == "ineligible"          # but gated at ingest
```

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_atlas_poller.py -v`

- [ ] **Step 4: Implement `poller.py`**

Design: for v1 minimality, fetch the FULL board response once (the existing fetchers already return content), compute the id-set from it, and if the set is unchanged short-circuit before doing any per-row gate/store work. This satisfies "unchanged board = cheap, no content processing" without a separate id-only endpoint round-trip (Greenhouse's `content=false` id-only optimization is noted as a future refinement in the Open Decisions). Reuse the existing per-ATS parsing by calling a thin fetch that mirrors `ats_boards._fetch_*` but WITHOUT the title/location filters (profile-agnostic — the gate decides eligibility, not discovery).

```python
"""Incremental Atlas poller (spec §7.2). Per board: fetch id-set, hash it, diff
against stored job_id_set_hash. Unchanged -> stop. Changed -> gate + store only
the NEW ids. Reuses the ATS JSON shapes from ats_boards but drops the discovery
title/location prefilter: membership + eligibility are the gate's job, so every
posting is gated and stored (audit), only eligible+auto rows become queue-visible."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

import httpx

from applypilot import database as _db
from applypilot.discovery.atlas import USER_AGENT
from applypilot.discovery.atlas import boards_repo as repo
from applypilot.discovery.atlas.politeness import HostRateLimiter
from applypilot.gate.engine import gate_job

_TIMEOUT = 20


@dataclass(frozen=True)
class PollResult:
    ok: bool
    total_ids: int = 0
    new_ids: int = 0
    stored: int = 0
    changed: bool = False
    error: str | None = None


def _hash_ids(ids) -> str:
    payload = "\n".join(sorted(str(i) for i in ids))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _fetch_all(ats: str, token: str, client: httpx.Client) -> list[dict]:
    """Return normalized rows [{id, url, title, location, description, posted_at}]
    for ALL postings (no prefilter). URL/field mapping mirrors ats_boards._fetch_*."""
    if ats == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        data = client.get(url, headers={"User-Agent": USER_AGENT}).json()
        out = []
        for j in data.get("jobs", []):
            desc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", j.get("content", "") or "")).strip()[:5000]
            out.append({"id": j.get("id"), "url": j.get("absolute_url"), "title": j.get("title", ""),
                        "location": (j.get("location") or {}).get("name", ""), "description": desc,
                        "posted_at": j.get("first_published") or j.get("updated_at")})
        return out
    if ats == "lever":
        url = f"https://api.lever.co/v0/postings/{token}?mode=json"
        data = client.get(url, headers={"User-Agent": USER_AGENT}).json()
        out = []
        for j in data or []:
            out.append({"id": j.get("id"), "url": j.get("hostedUrl"), "title": j.get("text", ""),
                        "location": (j.get("categories") or {}).get("location", ""),
                        "description": (j.get("descriptionPlain") or j.get("description", ""))[:5000],
                        "posted_at": j.get("createdAt")})
        return out
    if ats == "ashby":
        url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
        data = client.get(url, headers={"User-Agent": USER_AGENT}).json()
        out = []
        for j in data.get("jobs", []):
            out.append({"id": j.get("id") or j.get("jobUrl"), "url": j.get("jobUrl"),
                        "title": j.get("title", ""), "location": j.get("location", "") or "",
                        "description": (j.get("descriptionPlain") or j.get("descriptionHtml", ""))[:5000],
                        "posted_at": j.get("publishedAt")})
        return out
    return []


def poll_board(conn, board: dict, policy: dict, *, client: httpx.Client | None = None,
               limiter: HostRateLimiter | None = None) -> PollResult:
    ats, token = board["ats"], board["token"]
    site_label = f"{board.get('company_name') or token} ({ats})"
    strategy = f"atlas:{ats}"
    owns = client is None
    client = client or httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT})
    try:
        if limiter is not None:
            limiter.acquire({"greenhouse": "boards-api.greenhouse.io",
                             "lever": "api.lever.co", "ashby": "api.ashbyhq.com"}[ats])
        rows = _fetch_all(ats, token, client)
        ids = [r["id"] for r in rows if r.get("id") is not None]
        new_hash = _hash_ids(ids)
        old_hash = board.get("job_id_set_hash") or ""
        changed = new_hash != old_hash
        if not changed:
            repo.mark_checked(conn, ats, token, job_id_set_hash=new_hash,
                              job_count=len(ids), new_last_poll=0, changed=False)
            return PollResult(True, total_ids=len(ids), new_ids=0, stored=0, changed=False)

        # Determine which ids are NEW vs already-stored (by identity/url in jobs).
        stored = 0
        new_ids = 0
        existing_urls = {r[0] for r in conn.execute(
            "SELECT url FROM jobs WHERE strategy = ?", (strategy,)).fetchall()}
        for r in rows:
            if not r.get("url") or r["url"] in existing_urls:
                continue
            new_ids += 1
            job_row = {"url": r["url"], "title": r["title"], "salary": None,
                       "description": r["description"], "full_description": r["description"],
                       "application_url": r["url"], "location": r["location"],
                       "site": site_label, "posted_at": r.get("posted_at")}
            try:
                if _db.store_gated(conn, job_row, gate_job(job_row, policy), strategy=strategy):
                    stored += 1
            except Exception:  # noqa: BLE001 — one bad row never kills the board
                continue
        repo.mark_checked(conn, ats, token, job_id_set_hash=new_hash,
                          job_count=len(ids), new_last_poll=new_ids, changed=True)
        return PollResult(True, total_ids=len(ids), new_ids=new_ids, stored=stored, changed=True)
    except Exception as exc:  # noqa: BLE001
        repo.mark_dead(conn, ats, token)
        return PollResult(False, error=str(exc))
    finally:
        if owns:
            client.close()
```

Note on the gate `full_description`: Atlas rows arrive with content already (like `ats_boards`), so `full_description` is set and the gate runs on the real text at ingest — no re-gate needed. `detail_scraped_at` is left NULL here; if you want these rows to skip the enrich stage, set `detail_scraped_at` in `job_row` (READ how `ats_boards` sets it at line 270 and match — it sets `detail_scraped_at=now`). Match `ats_boards`' choice so Atlas rows and crawler rows behave identically in the pipeline.

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_atlas_poller.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/discovery/atlas/poller.py tests/test_atlas_poller.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "atlas: incremental poller (id-set diff -> gate_job -> store_gated), reuses 3 ATS shapes"
```

---

## Task 6: Ring scheduler — assignment + per-tick budget (pure functions)

Assign each active board a ring from its posting-activity + profile-relevance signals; compute the per-tick request budget. Rings 0 (hot, 30-60 min) / 1 (warm, 6-12h) / 2 (cold, snapshot-only, client never polls). All pure functions — no I/O, fully unit-testable (spec §7.2/§7.5 adaptive freshness).

**Files:**
- Create: `src/applypilot/discovery/atlas/rings.py`
- Test: `tests/test_atlas_rings.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_atlas_rings.py
from applypilot.discovery.atlas import rings


def test_recently_changed_board_is_hot():
    # posted something new in the last poll AND changed recently -> ring 0
    b = {"new_last_poll": 3, "job_count": 40, "last_changed": "2026-07-03T00:00:00Z"}
    assert rings.assign_ring(b, now="2026-07-03T02:00:00Z") == 0


def test_stale_but_active_board_is_warm():
    b = {"new_last_poll": 0, "job_count": 12, "last_changed": "2026-06-20T00:00:00Z"}
    assert rings.assign_ring(b, now="2026-07-03T00:00:00Z") == 1


def test_long_dormant_board_is_cold():
    b = {"new_last_poll": 0, "job_count": 0, "last_changed": "2026-01-01T00:00:00Z"}
    assert rings.assign_ring(b, now="2026-07-03T00:00:00Z") == 2


def test_never_changed_board_defaults_warm_not_cold():
    # freshly validated, never observed a change yet -> warm (give it a chance)
    b = {"new_last_poll": 0, "job_count": 5, "last_changed": None}
    assert rings.assign_ring(b, now="2026-07-03T00:00:00Z") == 1


def test_is_due_respects_ring_cadence():
    # hot board checked 45 min ago is due (cadence 30-60 min -> due at >=30m in v1)
    assert rings.is_due({"ring": 0, "last_checked": "2026-07-03T00:00:00Z"},
                        now="2026-07-03T00:45:00Z") is True
    # warm board checked 2h ago is NOT due (cadence >=6h)
    assert rings.is_due({"ring": 1, "last_checked": "2026-07-03T00:00:00Z"},
                        now="2026-07-03T02:00:00Z") is False
    # never-checked board is always due
    assert rings.is_due({"ring": 0, "last_checked": None}, now="2026-07-03T00:00:00Z") is True


def test_select_due_within_budget_prioritizes_hot():
    boards = [
        {"ats": "greenhouse", "token": "hot1", "ring": 0, "last_checked": None},
        {"ats": "greenhouse", "token": "hot2", "ring": 0, "last_checked": None},
        {"ats": "lever", "token": "warm1", "ring": 1, "last_checked": None},
    ]
    picked = rings.select_due(boards, now="2026-07-03T00:00:00Z", budget=2)
    assert len(picked) == 2
    assert {p["token"] for p in picked} == {"hot1", "hot2"}   # hot before warm under budget


def test_daily_request_estimate_within_politeness():
    # 5000 ring-0/1 boards, once each, is well under per-host budgets.
    est = rings.estimate_daily_requests(hot=1000, warm=4000)
    # hot polled ~24x/day (hourly), warm ~2x/day => ~24000 + 8000
    assert 25000 <= est <= 40000
```

- [ ] **Step 2: Run — expect failure**

Run: `& $PY -m pytest tests/test_atlas_rings.py -v`

- [ ] **Step 3: Implement `rings.py`**

```python
"""Ring assignment + per-tick selection/budget math (pure; spec §7.2/§7.5).
Ring 0 = hot (poll ~hourly), 1 = warm (~6-12h), 2 = cold (snapshot-only; the
CLIENT never polls ring 2 — spec §14). Adaptive: a board that just posted is
hot; a long-dormant one decays to cold; a freshly-validated board with no
change history gets a warm chance rather than being buried cold."""
from __future__ import annotations

from datetime import datetime, timezone

# Minimum spacing (hours) before a ring is due again. v1 uses the low end of
# the spec bands (30 min hot, 6h warm) so the queue stays fresh.
_CADENCE_H = {0: 0.5, 1: 6.0, 2: 24.0}
_HOT_RECENCY_H = 72        # changed within 3 days -> hot
_COLD_DORMANCY_H = 24 * 45  # no change in 45 days AND empty -> cold


def _parse(ts: str | None):
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def _hours_since(ts: str | None, now: str) -> float | None:
    dt = _parse(ts)
    if dt is None:
        return None
    return (_parse(now) - dt).total_seconds() / 3600.0


def assign_ring(board: dict, *, now: str) -> int:
    changed_ago = _hours_since(board.get("last_changed"), now)
    posted_recently = (board.get("new_last_poll") or 0) > 0
    if posted_recently or (changed_ago is not None and changed_ago <= _HOT_RECENCY_H):
        return 0
    if changed_ago is None:
        return 1                                  # never-changed-yet: warm chance
    if changed_ago >= _COLD_DORMANCY_H and (board.get("job_count") or 0) == 0:
        return 2
    return 1


def is_due(board: dict, *, now: str) -> bool:
    ring = board.get("ring")
    if ring is None:
        return True
    since = _hours_since(board.get("last_checked"), now)
    if since is None:
        return True
    return since >= _CADENCE_H.get(ring, 6.0)


def select_due(boards: list[dict], *, now: str, budget: int) -> list[dict]:
    """Due boards, hot-first, capped at `budget` requests this tick."""
    due = [b for b in boards if is_due(b, now=now)]
    due.sort(key=lambda b: (b.get("ring", 9),
                            b.get("last_checked") or ""))    # oldest-checked first within ring
    return due[:budget]


def estimate_daily_requests(*, hot: int, warm: int) -> int:
    """Rough daily request volume for a ring-0/1 subset (one req/poll thanks to
    id-set diffing). Hot ~hourly (24/day), warm ~2/day."""
    return hot * 24 + warm * 2
```

- [ ] **Step 4: Run — expect pass**

Run: `& $PY -m pytest tests/test_atlas_rings.py -v`
Expected: ALL PASS. If `test_daily_request_estimate_within_politeness` fails, reconcile the multipliers with the spec bands; the assertion band is intentionally loose.

- [ ] **Step 5: Commit**

```powershell
git reset
git add src/applypilot/discovery/atlas/rings.py tests/test_atlas_rings.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "atlas: ring assignment + due-selection + budget math (pure fns, adaptive freshness)"
```

---

## Task 7: `source_runs` accounting table

Every Atlas run records requests/jobs_seen/jobs_new/jobs_eligible + cost so zero-yield sources demote and cost joins down-funnel (spec §7.2: per-source accounting from day one).

**Files:**
- Modify: `src/applypilot/database.py` (`init_db`: add `source_runs` CREATE TABLE)
- Create: `src/applypilot/discovery/atlas/source_runs.py`
- Test: `tests/test_atlas_source_runs.py`

- [ ] **Step 1: Add the `source_runs` table to `init_db`**

In `database.py`, directly after the `boards` table block from Task 1 (still before `conn.commit()`), add:

```python
    # Per-source run accounting (v2 Phase 2, spec §7.2). One row per Atlas tick
    # (or per source per tick). Requests/yield/cost join down-funnel so zero-yield
    # sources demote automatically. Standalone table (not in _ALL_COLUMNS).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_runs (
            run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            source         TEXT NOT NULL,        -- 'atlas' | 'atlas:greenhouse' | ...
            started_at     TEXT NOT NULL,
            finished_at    TEXT,
            boards_polled  INTEGER DEFAULT 0,
            requests       INTEGER DEFAULT 0,
            jobs_seen      INTEGER DEFAULT 0,
            jobs_new       INTEGER DEFAULT 0,
            jobs_eligible  INTEGER DEFAULT 0,
            cost_usd       REAL DEFAULT 0.0,
            error          TEXT
        )
    """)
```

- [ ] **Step 2: Write failing tests**

```python
# tests/test_atlas_source_runs.py
from applypilot import database as db
from applypilot.discovery.atlas import source_runs as sr


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


def test_open_and_finish_run(tmp_path):
    conn = _conn(tmp_path)
    run_id = sr.open_run(conn, source="atlas")
    assert isinstance(run_id, int)
    sr.finish_run(conn, run_id, boards_polled=10, requests=10, jobs_seen=120,
                  jobs_new=8, jobs_eligible=3, cost_usd=0.0)
    row = sr.get_run(conn, run_id)
    assert row["boards_polled"] == 10 and row["jobs_new"] == 8 and row["jobs_eligible"] == 3
    assert row["finished_at"] is not None and row["error"] is None


def test_finish_run_records_error(tmp_path):
    conn = _conn(tmp_path)
    run_id = sr.open_run(conn, source="atlas:lever")
    sr.finish_run(conn, run_id, error="429 storm")
    assert sr.get_run(conn, run_id)["error"] == "429 storm"


def test_recent_runs_ordered_newest_first(tmp_path):
    conn = _conn(tmp_path)
    a = sr.open_run(conn, source="atlas"); sr.finish_run(conn, a)
    b = sr.open_run(conn, source="atlas"); sr.finish_run(conn, b)
    runs = sr.recent_runs(conn, source="atlas", limit=5)
    assert [r["run_id"] for r in runs][:2] == [b, a]
```

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_atlas_source_runs.py -v`

- [ ] **Step 4: Implement `source_runs.py`**

```python
"""source_runs accounting (spec §7.2). open_run at tick start, finish_run at
end with the tallies. Cost is 0.0 in Phase 2 (Atlas polling is $0 public JSON);
the column exists so down-funnel scoring cost can be joined later."""
from __future__ import annotations

from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_run(conn, *, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO source_runs (source, started_at) VALUES (?, ?)",
        (source, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn, run_id: int, *, boards_polled: int = 0, requests: int = 0,
               jobs_seen: int = 0, jobs_new: int = 0, jobs_eligible: int = 0,
               cost_usd: float = 0.0, error: str | None = None) -> None:
    conn.execute(
        "UPDATE source_runs SET finished_at=?, boards_polled=?, requests=?, "
        "jobs_seen=?, jobs_new=?, jobs_eligible=?, cost_usd=?, error=? WHERE run_id=?",
        (_now(), boards_polled, requests, jobs_seen, jobs_new, jobs_eligible,
         cost_usd, error, run_id),
    )
    conn.commit()


def get_run(conn, run_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM source_runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def recent_runs(conn, *, source: str | None = None, limit: int = 20) -> list[dict]:
    if source:
        rows = conn.execute(
            "SELECT * FROM source_runs WHERE source = ? ORDER BY run_id DESC LIMIT ?",
            (source, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM source_runs ORDER BY run_id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_atlas_source_runs.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/database.py src/applypilot/discovery/atlas/source_runs.py tests/test_atlas_source_runs.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "atlas: source_runs accounting table + CRUD (requests/yield/cost per run)"
```

---

## Task 8: The idempotent, resumable TICK orchestrator

Compose the pieces into one on-wake/cron TICK: (re)assign rings for active boards, select due ring-0/1 boards within budget, poll each through the politeness limiter, tally into `source_runs`. Idempotent (re-running just polls whatever's due now) and resumable (each poll commits + `mark_checked` on its own, so a mid-tick crash loses no progress). No daemon (spec §7.2/§14).

**Files:**
- Create: `src/applypilot/discovery/atlas/tick.py`
- Test: `tests/test_atlas_tick.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_atlas_tick.py
import httpx

from applypilot import database as db
from applypilot.discovery.atlas import boards_repo as repo
from applypilot.discovery.atlas import source_runs as sr
from applypilot.discovery.atlas import tick


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


POLICY = {"geo": {"remote_ok": True, "remote_scope": "US", "onsite_regions": ["us-ca"]},
          "seniority": {"accept_bands": ["senior"], "ic_only": True},
          "needs_sponsorship": False, "workday_accounts": []}


def _gh(ids):
    return {"jobs": [{"id": i, "title": "Senior Product Designer",
                      "absolute_url": f"https://boards.greenhouse.io/tickco/jobs/{i}",
                      "location": {"name": "Remote - US"}, "content": "design",
                      "first_published": "2026-07-01T00:00:00Z"} for i in ids]}


def test_tick_polls_only_ring01_and_records_source_run(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "tickco", source="x")
    repo.set_status(conn, "greenhouse", "tickco", "active")
    repo.set_ring(conn, "greenhouse", "tickco", 0)
    # a cold ring-2 board must be ignored by the client tick
    repo.upsert_board(conn, "greenhouse", "coldco", source="x")
    repo.set_status(conn, "greenhouse", "coldco", "active")
    repo.set_ring(conn, "greenhouse", "coldco", 2)

    polled = []

    def handler(req):
        polled.append(str(req.url))
        return httpx.Response(200, json=_gh([1, 2]))

    res = tick.run_tick(conn, POLICY, client=_client(handler), budget=100)
    assert res["boards_polled"] == 1                    # only tickco (ring 0)
    assert all("tickco" in u for u in polled)           # coldco never fetched
    assert res["jobs_new"] == 2
    runs = sr.recent_runs(conn, source="atlas", limit=1)
    assert runs and runs[0]["boards_polled"] == 1 and runs[0]["finished_at"]


def test_tick_is_idempotent_second_run_no_new_jobs(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "tickco", source="x")
    repo.set_status(conn, "greenhouse", "tickco", "active")
    repo.set_ring(conn, "greenhouse", "tickco", 0)

    def handler(req):
        return httpx.Response(200, json=_gh([1]))

    tick.run_tick(conn, POLICY, client=_client(handler), budget=100)
    before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    # second tick: board reassigned; unchanged id-set -> no new rows.
    tick.run_tick(conn, POLICY, client=_client(handler), budget=100, force_due=True)
    after = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert before == after


def test_tick_reassigns_rings_before_selecting(tmp_path):
    conn = _conn(tmp_path)
    # board with no ring yet must get assigned and (being fresh) become warm/hot -> polled
    repo.upsert_board(conn, "greenhouse", "newco", source="x")
    repo.set_status(conn, "greenhouse", "newco", "active")   # ring is NULL

    def handler(req):
        return httpx.Response(200, json=_gh([1]))

    res = tick.run_tick(conn, POLICY, client=_client(handler), budget=100)
    assert res["boards_polled"] == 1
    assert repo.get(conn, "greenhouse", "newco")["ring"] in (0, 1)
```

- [ ] **Step 2: Run — expect failure**

Run: `& $PY -m pytest tests/test_atlas_tick.py -v`

- [ ] **Step 3: Implement `tick.py`**

```python
"""The Atlas TICK: idempotent, resumable, no-daemon catch-up poll (spec §7.2).
Reassign rings for active boards, select due ring-0/1 boards within budget,
poll each (id-set diff -> gate -> store), tally into source_runs. Safe to run
on cron/on-wake or as the 'atlas' discover sub-source."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from applypilot.discovery.atlas import USER_AGENT
from applypilot.discovery.atlas import boards_repo as repo
from applypilot.discovery.atlas import rings
from applypilot.discovery.atlas import source_runs as sr
from applypilot.discovery.atlas.poller import poll_board
from applypilot.discovery.atlas.politeness import HostRateLimiter

# Client budget: ring-0/1 only, well inside per-host politeness (spec §7.2).
_DEFAULT_BUDGET = 6000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_tick(conn, policy: dict, *, client: httpx.Client | None = None,
             budget: int = _DEFAULT_BUDGET, force_due: bool = False,
             limiter: HostRateLimiter | None = None) -> dict:
    now = _now()
    owns = client is None
    client = client or httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT})
    limiter = limiter or HostRateLimiter(rps=4.0)
    run_id = sr.open_run(conn, source="atlas")
    boards_polled = requests = jobs_seen = jobs_new = jobs_eligible = 0
    error = None
    try:
        # 1. Reassign rings for every active board from its current signals.
        active = repo.get_boards_by_ring(conn, rings=(0, 1, 2))
        for b in active:
            repo.set_ring(conn, b["ats"], b["token"], rings.assign_ring(b, now=now))
        # 2. Select due ring-0/1 boards within budget (hot-first).
        candidates = repo.get_boards_by_ring(conn, rings=(0, 1))
        if force_due:
            for b in candidates:
                b["last_checked"] = None
        due = rings.select_due(candidates, now=now, budget=budget)
        # 3. Poll each (commits per board -> resumable).
        for b in due:
            res = poll_board(conn, repo.get(conn, b["ats"], b["token"]), policy,
                             client=client, limiter=limiter)
            boards_polled += 1
            requests += 1
            if res.ok:
                jobs_seen += res.total_ids
                jobs_new += res.new_ids
        # eligible count for this tick's newly-stored atlas rows
        jobs_eligible = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE strategy LIKE 'atlas:%' "
            "AND gate_result = 'eligible' AND gated_at >= ?", (now,)).fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    finally:
        sr.finish_run(conn, run_id, boards_polled=boards_polled, requests=requests,
                      jobs_seen=jobs_seen, jobs_new=jobs_new, jobs_eligible=jobs_eligible,
                      cost_usd=0.0, error=error)
        if owns:
            client.close()
    return {"run_id": run_id, "boards_polled": boards_polled, "requests": requests,
            "jobs_seen": jobs_seen, "jobs_new": jobs_new, "jobs_eligible": jobs_eligible,
            "error": error}
```

- [ ] **Step 4: Run — expect pass**

Run: `& $PY -m pytest tests/test_atlas_tick.py -v`
Expected: ALL PASS. Note `test_tick_is_idempotent...` uses `force_due=True` on the second call so the tick re-selects the board despite the cadence gate; the id-set is unchanged, so `poll_board` short-circuits and stores nothing — proving idempotency.

- [ ] **Step 5: Commit**

```powershell
git reset
git add src/applypilot/discovery/atlas/tick.py tests/test_atlas_tick.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "atlas: idempotent resumable TICK (reassign rings -> select due 0/1 -> poll -> source_runs)"
```

---

## Task 9: Shadow-mode telemetry + go/no-go report

Measure the three shadow-mode signals (spec §13 Phase 2 exit): per-ATS board coverage, poll request volume, fresh-eligible-queue-depth. Render a `report` the operator reads to make the go/no-go call.

**Files:**
- Create: `src/applypilot/discovery/atlas/telemetry.py`
- Test: `tests/test_atlas_telemetry.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_atlas_telemetry.py
from applypilot import database as db
from applypilot.discovery.atlas import boards_repo as repo
from applypilot.discovery.atlas import source_runs as sr
from applypilot.discovery.atlas import telemetry


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


def test_board_coverage_by_ats_and_status(tmp_path):
    conn = _conn(tmp_path)
    repo.upsert_board(conn, "greenhouse", "a", source="x"); repo.set_status(conn, "greenhouse", "a", "active")
    repo.upsert_board(conn, "greenhouse", "b", source="x")  # candidate
    repo.upsert_board(conn, "lever", "c", source="x"); repo.set_status(conn, "lever", "c", "dead")
    cov = telemetry.board_coverage(conn)
    assert cov["greenhouse"]["active"] == 1 and cov["greenhouse"]["candidate"] == 1
    assert cov["lever"]["dead"] == 1


def test_fresh_eligible_depth_counts_recent_eligible_atlas_rows(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO jobs (url, strategy, gate_result, gated_at, fit_score) "
                 "VALUES ('u1','atlas:greenhouse','eligible',datetime('now'),NULL)")
    conn.execute("INSERT INTO jobs (url, strategy, gate_result, gated_at, fit_score) "
                 "VALUES ('u2','atlas:ashby','ineligible',datetime('now'),NULL)")
    conn.execute("INSERT INTO jobs (url, strategy, gate_result, gated_at, fit_score) "
                 "VALUES ('u3','ats_api:greenhouse','eligible',datetime('now'),NULL)")
    conn.commit()
    depth = telemetry.fresh_eligible_depth(conn, source_prefix="atlas:")
    assert depth == 1                                    # only u1 (eligible + atlas)


def test_poll_volume_sums_requests(tmp_path):
    conn = _conn(tmp_path)
    r = sr.open_run(conn, source="atlas"); sr.finish_run(conn, r, requests=120, boards_polled=100)
    r2 = sr.open_run(conn, source="atlas"); sr.finish_run(conn, r2, requests=80, boards_polled=70)
    vol = telemetry.poll_volume(conn)
    assert vol["total_requests"] == 200 and vol["runs"] == 2


def test_go_no_go_verdict(tmp_path):
    conn = _conn(tmp_path)
    # healthy: eligible depth >= threshold, no run errored, requests modest
    conn.execute("INSERT INTO jobs (url, strategy, gate_result, gated_at) "
                 "VALUES ('u1','atlas:greenhouse','eligible',datetime('now'))")
    for u in ("u2", "u3", "u4", "u5"):
        conn.execute("INSERT INTO jobs (url, strategy, gate_result, gated_at) "
                     f"VALUES ('{u}','atlas:greenhouse','eligible',datetime('now'))")
    conn.commit()
    r = sr.open_run(conn, source="atlas"); sr.finish_run(conn, r, requests=100, boards_polled=100)
    verdict = telemetry.go_no_go(conn, min_fresh_eligible=5, max_error_runs=0)
    assert verdict["go"] is True
    assert "fresh_eligible_depth" in verdict["signals"]
```

- [ ] **Step 2: Run — expect failure**

Run: `& $PY -m pytest tests/test_atlas_telemetry.py -v`

- [ ] **Step 3: Implement `telemetry.py`**

```python
"""Shadow-mode telemetry + go/no-go (spec §13 Phase 2 exit). Three signals:
per-ATS board coverage, poll request volume, fresh-eligible-queue-depth. The
Phase-2 gate is: Atlas produces a healthy fresh-eligible queue for the mainstream
profile WITHOUT exceeding politeness budgets. (Ashby/Lever parse-gap rates are a
Phase-3 concern — NOT gated here.)"""
from __future__ import annotations


def board_coverage(conn) -> dict:
    """{ats: {status: count}} across the boards registry."""
    out: dict[str, dict[str, int]] = {}
    for ats, status, n in conn.execute(
            "SELECT ats, status, COUNT(*) FROM boards GROUP BY ats, status").fetchall():
        out.setdefault(ats, {})[status] = n
    return out


def fresh_eligible_depth(conn, *, source_prefix: str = "atlas:") -> int:
    """Eligible, not-yet-scored rows from Atlas — the queue the funnel feeds on."""
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE strategy LIKE ? "
        "AND gate_result = 'eligible'", (source_prefix + "%",)).fetchone()[0]


def poll_volume(conn) -> dict:
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(requests),0), COALESCE(SUM(boards_polled),0) "
        "FROM source_runs WHERE source LIKE 'atlas%'").fetchone()
    return {"runs": row[0], "total_requests": row[1], "total_boards_polled": row[2]}


def go_no_go(conn, *, min_fresh_eligible: int = 5, max_error_runs: int = 0) -> dict:
    depth = fresh_eligible_depth(conn)
    coverage = board_coverage(conn)
    volume = poll_volume(conn)
    error_runs = conn.execute(
        "SELECT COUNT(*) FROM source_runs WHERE source LIKE 'atlas%' AND error IS NOT NULL"
    ).fetchone()[0]
    go = depth >= min_fresh_eligible and error_runs <= max_error_runs
    return {
        "go": bool(go),
        "signals": {
            "fresh_eligible_depth": depth,
            "board_coverage": coverage,
            "poll_volume": volume,
            "error_runs": error_runs,
        },
        "criteria": {
            "min_fresh_eligible": min_fresh_eligible,
            "max_error_runs": max_error_runs,
        },
    }
```

- [ ] **Step 4: Run — expect pass**

Run: `& $PY -m pytest tests/test_atlas_telemetry.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Document the go/no-go criteria (in the module docstring + task summary)**

The Phase-2 → Phase-3 go/no-go, per spec §13 (Phase 2 exit is about the FUNNEL, not apply-engine parse rates):
- **GO if:** fresh-eligible Atlas queue depth ≥ 5 (median daily) for the mainstream profile over the shadow window (mirrors acceptance-gate item 5, §12); AND zero `source_runs` with a politeness/vendor error (429/auth-wall) — the vendor-behavior canary is clean; AND poll volume stays within the per-host budget (`estimate_daily_requests` for the polled ring-0/1 count is comfortably under ~4 rps/host sustained).
- **NO-GO / iterate if:** the queue starves (depth < 5) → widen the freshness window / promote warm boards (spec §7.5 levers) before proceeding; OR any host shows a 429/auth-wall streak → back off, re-tune politeness, re-run shadow.
- **Explicitly NOT gated in Phase 2:** Ashby/Lever DOM parse-gap and fingerprint-hit rates — those are Phase 3 (apply engine) exit criteria. Phase 2 proves the funnel produces fresh eligible supply politely; Phase 3 proves the engine can apply to it.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/discovery/atlas/telemetry.py tests/test_atlas_telemetry.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "atlas: shadow-mode telemetry (coverage/volume/fresh-eligible depth) + go/no-go verdict"
```

---

## Task 10: Wire `applypilot atlas` CLI sub-app + the `atlas` discover sub-source

Expose the Atlas via a Typer sub-app (`import`, `mine`, `validate`, `tick`, `report`) AND register `atlas` as a discover sub-source so a normal `discover` run can include a tick. Both call the SAME functions — the CLI is a thin shell.

**Files:**
- Modify: `src/applypilot/cli.py` (add an `atlas` Typer sub-app via `app.add_typer`)
- Modify: `src/applypilot/pipeline.py` (`_run_discover`: add `atlas` to the sources set + a guarded call)
- Test: `tests/test_atlas_cli.py`

- [ ] **Step 1: READ the CLI + pipeline wiring points**

READ `src/applypilot/cli.py:22-31` (`app = typer.Typer(...)`, `VALID_STAGES`) and `:255-343` (`discover_ats` command — the style for `_bootstrap()`, `console.print`, Rich `Table`). READ `src/applypilot/pipeline.py:169-235` (`_run_discover` — the `enabled` set and the per-source `try/except` + `stats[...]` pattern). Match both styles exactly.

- [ ] **Step 2: Write failing tests (CLI via Typer's CliRunner + injected DB)**

```python
# tests/test_atlas_cli.py
from typer.testing import CliRunner

from applypilot import database as db
from applypilot.cli import app
from applypilot.discovery.atlas import boards_repo as repo

runner = CliRunner()


def test_atlas_import_cli_seeds_boards(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))   # clean APP_DIR -> tmp DB
    result = runner.invoke(app, ["atlas", "import"])
    assert result.exit_code == 0
    conn = db.get_connection()
    # 56 curated tokens land (>= to tolerate any user overlay).
    assert len(repo.get_all(conn)) >= 56


def test_atlas_report_cli_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    runner.invoke(app, ["atlas", "import"])
    result = runner.invoke(app, ["atlas", "report"])
    assert result.exit_code == 0
    assert "coverage" in result.stdout.lower() or "boards" in result.stdout.lower()
```

Note: these tests rely on `APPLYPILOT_DIR` redirecting `config.APP_DIR`/`DB_PATH` to `tmp_path` so the CLI's `_bootstrap()` inits a throwaway DB. VERIFY `config.py:9,12` read the env var at import/call time; if `DB_PATH` is a module constant captured at import (it is: `DB_PATH = APP_DIR / "applypilot.db"`), the env var must be set BEFORE `applypilot.config` is first imported. `CliRunner` imports happen at test-module load, so if the redirect doesn't take, fall back to `monkeypatch.setattr(database, "DB_PATH", tmp_path / "applypilot.db")` and `monkeypatch.setattr(config, "APP_DIR", tmp_path)` — READ how `test_theirstack_discovery.py:40-42` monkeypatches `database.DB_PATH` and mirror it.

- [ ] **Step 3: Run — expect failure** (`No such command 'atlas'`)

Run: `& $PY -m pytest tests/test_atlas_cli.py -v`

- [ ] **Step 4: Add the `atlas` Typer sub-app to `cli.py`**

Near the other command definitions, add a sub-app and register it (Typer's canonical nesting):

```python
atlas_app = typer.Typer(help="Board Atlas — discovery at scale (v2 Phase 2, shadow mode).")
app.add_typer(atlas_app, name="atlas")


@atlas_app.command("import")
def atlas_import() -> None:
    """One-time seed of the boards registry from ats_companies.yaml (56 tokens)."""
    _bootstrap()
    from applypilot import database as db
    from applypilot.discovery.atlas.importer import import_registry
    n = import_registry(db.get_connection())
    console.print(f"Imported [bold]{n}[/bold] new boards from the ATS registry.")


@atlas_app.command("mine")
def atlas_mine() -> None:
    """Harvest candidate board tokens from the jobs DB + bundled CC snapshot."""
    _bootstrap()
    from applypilot import database as db
    from applypilot.discovery.atlas.miner import mine_candidates
    n = mine_candidates(db.get_connection())
    console.print(f"Mined [bold]{n}[/bold] new candidate boards (status=candidate).")


@atlas_app.command("validate")
def atlas_validate(
    limit: int = typer.Option(500, "--limit", help="Max candidate boards to validate this run."),
) -> None:
    """Promote surviving candidate boards to 'active' via one cheap existence check each."""
    _bootstrap()
    from applypilot import database as db
    from applypilot.discovery.atlas import validator
    from applypilot.discovery.atlas.politeness import HostRateLimiter
    conn = db.get_connection()
    limiter = HostRateLimiter(rps=4.0)
    cands = conn.execute(
        "SELECT ats, token FROM boards WHERE status = 'candidate' LIMIT ?", (limit,)).fetchall()
    ok = 0
    for r in cands:
        if validator.validate_board(conn, r["ats"], r["token"], limiter=limiter).ok:
            ok += 1
    console.print(f"Validated [bold]{ok}[/bold]/{len(cands)} candidate boards to active.")


@atlas_app.command("tick")
def atlas_tick(
    budget: int = typer.Option(6000, "--budget", help="Max board polls this tick (ring 0/1)."),
) -> None:
    """Run one idempotent freshness TICK (poll due ring-0/1 boards; no daemon)."""
    _bootstrap()
    from applypilot import database as db
    from applypilot.discovery.atlas.tick import run_tick
    from applypilot.gate.profile_map import gate_profile
    from applypilot.config import load_profile, load_search_config
    policy = gate_profile(load_profile(), load_search_config())
    res = run_tick(db.get_connection(), policy, budget=budget)
    console.print(
        f"Atlas tick: polled [bold]{res['boards_polled']}[/bold] boards, "
        f"{res['jobs_new']} new jobs, {res['jobs_eligible']} eligible"
        + (f" [red](error: {res['error']})[/red]" if res.get("error") else "")
    )


@atlas_app.command("report")
def atlas_report() -> None:
    """Shadow-mode telemetry + go/no-go for the Phase 2 exit."""
    _bootstrap()
    from applypilot import database as db
    from applypilot.discovery.atlas import telemetry
    conn = db.get_connection()
    verdict = telemetry.go_no_go(conn)
    cov = verdict["signals"]["board_coverage"]
    table = Table(title="Atlas board coverage", header_style="bold cyan")
    table.add_column("ATS"); table.add_column("active", justify="right")
    table.add_column("candidate", justify="right"); table.add_column("dead", justify="right")
    for ats, d in sorted(cov.items()):
        table.add_row(ats, str(d.get("active", 0)), str(d.get("candidate", 0)), str(d.get("dead", 0)))
    console.print(table)
    vol = verdict["signals"]["poll_volume"]
    console.print(f"Poll volume: {vol['total_requests']} requests over {vol['runs']} runs")
    console.print(f"Fresh-eligible depth: [bold]{verdict['signals']['fresh_eligible_depth']}[/bold]")
    console.print(f"Go/no-go: {'[green]GO[/green]' if verdict['go'] else '[yellow]NO-GO (iterate)[/yellow]'}")
```

- [ ] **Step 5: Register `atlas` as a discover sub-source in `pipeline.py`**

In `_run_discover`, after the existing `ats_boards` block (~line 227+), add a guarded block mirroring the others (opt-in via env so shadow mode doesn't disturb the live crawler until the operator enables it):

```python
    # Board Atlas freshness tick (v2 Phase 2, shadow). Opt-in until go/no-go.
    if "atlas" in enabled or os.environ.get("APPLYPILOT_ATLAS_ENABLED") == "1":
        console.print("  [cyan]Board Atlas tick (ring 0/1)...[/cyan]")
        try:
            from applypilot import database as _db
            from applypilot.discovery.atlas.tick import run_tick
            from applypilot.gate.profile_map import gate_profile
            from applypilot.config import load_profile, load_search_config
            policy = gate_profile(load_profile(), load_search_config())
            res = run_tick(_db.get_connection(), policy)
            stats["atlas"] = f"ok: {res['jobs_new']} new, {res['jobs_eligible']} eligible"
        except Exception as e:
            log.error("Atlas tick failed: %s", e)
            console.print(f"  [red]Atlas error:[/red] {e}")
            stats["atlas"] = f"error: {e}"
    else:
        stats["atlas"] = "skipped"
```

(No change to `STAGE_ORDER`/`VALID_STAGES` — `atlas` is a discover *sub-source*, not a top-level stage.)

- [ ] **Step 6: Run — expect pass, then full suite**

Run: `& $PY -m pytest tests/test_atlas_cli.py -v`
Expected: ALL PASS.
Run: `& $PY -m pytest tests/ -q`
Expected: no regressions.

- [ ] **Step 7: Commit**

```powershell
git reset
git add src/applypilot/cli.py src/applypilot/pipeline.py tests/test_atlas_cli.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "atlas: 'applypilot atlas' CLI (import/mine/validate/tick/report) + opt-in discover sub-source"
```

---

## Task 11: Phase 2 verification

- [ ] **Step 1: Full suite green**

Run: `& $PY -m pytest tests/ -q`
Expected: ALL PASS, 0 failed. Investigate any regression before proceeding.

- [ ] **Step 2: Lint the new package**

Run: `ruff check src/applypilot/discovery/atlas scripts/mine_common_crawl.py`
Expected: clean. Fix mechanical findings (unused imports, whitespace); for anything non-trivial add a targeted `# noqa: <code>` with a one-word reason and note it in the summary.

- [ ] **Step 3: End-to-end smoke on a throwaway DB (no network — MockTransport only)**

Write a scratch script (in the scratchpad dir, NOT the repo) that, against a `tmp` DB: imports the registry, mines, validates two boards via a `MockTransport` client, runs a tick, and prints `telemetry.go_no_go`. Confirm: boards land, a tick stores gated rows, `source_runs` has a finished row, and the report renders. This proves the pieces compose without a live crawl. Do NOT commit the scratch script.

- [ ] **Step 4: Confirm shadow-mode is non-disruptive**

Verify (by reading, not running a live crawl) that: `atlas` is `skipped` in `_run_discover` unless `APPLYPILOT_ATLAS_ENABLED=1` or explicitly in `sources`; and that the live `ats_boards` crawler path is untouched. Atlas runs alongside, not instead of, the existing crawler until the go/no-go passes.

- [ ] **Step 5: Tree clean + report**

Run: `git status --short`
Expected: EMPTY (all Atlas work committed; no stray scratch files).

Summarize to the user: commits made, total new tests + pass count, the board-coverage/poll-volume/fresh-eligible numbers from a MockTransport smoke run, the documented go/no-go criteria, and the reminder that Atlas ships in SHADOW MODE (opt-in) — the live crawler still runs until the operator flips `APPLYPILOT_ATLAS_ENABLED=1` and the go/no-go clears.

---

## Open decisions for the controller

- **Common Crawl mining aggressiveness in v1.** Recommended default: keep it OFFLINE and minimal — `scripts/mine_common_crawl.py` is a documented maintainer script that emits `atlas_snapshot.jsonl`, and v1 ships a small hand-verified snapshot (~20-50 tokens) plus DB mining. The "thousands of candidates" quarterly batch is real but runs on the maintainer's machine, not the client, and does not block Phase 2's go/no-go. Rationale: §14 defers the heavy registry infra; the 56 imported + DB-mined tokens already exercise the whole pipeline.
- **Tick delivery: cron CLI vs discover sub-source.** Recommended default: ship BOTH but keep the sub-source OPT-IN (`APPLYPILOT_ATLAS_ENABLED=1`) during shadow mode (Task 10 does this). `applypilot atlas tick` is the primary shadow-mode entry (operator/cron-driven, isolated telemetry); the discover sub-source is how it graduates into the normal pipeline after go/no-go. This lets shadow measurement happen without perturbing the live crawler.
- **Incremental fetch: full-content fetch vs id-only pre-check.** The poller (Task 5) fetches the full board once and diffs the id-set from it, rather than doing a separate `?content=false` id-only request first. Recommended default: keep the single-fetch design for v1 (simpler, one request/board, and unchanged boards still skip all gate/store work). Revisit the two-request id-only pre-check only if shadow telemetry shows content-fetch bandwidth is a real per-host budget problem — Greenhouse's `content=false` variant makes it a clean future optimization, noted but not built.
- **Ring cadence exact values.** v1 uses the aggressive end of the spec bands (hot = 30 min, warm = 6 h) in `rings._CADENCE_H`. Recommended default: keep these for shadow mode to maximize freshness signal, and let the go/no-go poll-volume number decide whether to relax toward the 60 min / 12 h end if any host approaches its politeness budget.
