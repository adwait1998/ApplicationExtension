# Multi-Profile Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one ApplyPilot installation run independent job searches for several people, with per-person data isolated on the filesystem and learned ATS knowledge shared.

**Architecture:** The bound profile is resolved **once per process, before any `applypilot` module is imported** — `__main__.py` scans `sys.argv` for `--profile`, resolves an id, sets `APPLYPILOT_DIR` to that profile's directory, then imports the CLI. Because `config.py` computes its path constants at import time, every downstream module keeps working untouched. The four profile-agnostic tables move to a shared SQLite file that is `ATTACH`ed to the profile database, so unqualified table names still resolve and no SQL or function signature changes anywhere.

**Tech Stack:** Python 3.12, SQLite (WAL), Typer CLI, pytest. Interpreter `PY = C:\Users\adwai\AppData\Local\Programs\Python\Python312\python.exe` (has pytest + the editable install; the `.venv` python does NOT).

**Spec:** `docs/superpowers/specs/2026-09-18-multi-profile-design.md`

**Prerequisite:** `main` at `a827ad1` or later, tree clean, suite green at **963 passed, 1 skipped**.

---

## Conventions (every task)

- Repo root `E:\auto-apply-pipeline`; run all commands from there.
- Commit with one-shot identity, never push:
  `git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "..."`
- Before EVERY commit: `git reset`, then `git add <only this task's files>`, then
  `git diff --cached --stat` and verify only intended files are staged.
- TDD: write the failing test first, run it to confirm the failure mode, implement, run to green, commit.
- **Never** run a live `applypilot apply`. All validation here is synthetic and $0.
- **Tests must never touch `E:\applypilot-data`.** Every test uses `tmp_path`.

## The load-bearing invariant

Isolation between profiles is a **filesystem property**, not a query predicate. No task may
introduce a code path where profile A's process can write under profile B's directory. If a
task seems to require one, stop and raise it rather than working around it.

## File structure

- Create: `src/applypilot/profiles.py` — root + profile resolution. Imports **nothing** from `applypilot`.
- Modify: `src/applypilot/__main__.py` — pre-import binding hook.
- Modify: `src/applypilot/config.py` — add `ROOT`, `SHARED_DIR`, `ATLAS_DB_PATH`.
- Modify: `src/applypilot/database.py` — attach the shared atlas DB; create atlas tables there.
- Modify: `src/applypilot/apply/launcher.py` — `profile_id` assertion in `_safety_prologue`.
- Modify: `src/applypilot/webui/registry.py` — registry to `shared/ui_runs`, records tagged with profile.
- Create: `src/applypilot/webui/shared_settings.py` — global settings (spend cap, autopilot).
- Modify: `src/applypilot/cli.py` — `--profile` global option, `profile` sub-commands, `ui` wiring.
- Create: `src/applypilot/migrate_profiles.py` — one-shot idempotent migration.
- Tests: `tests/test_profiles.py`, `tests/test_profile_binding.py`, `tests/test_atlas_split.py`,
  `tests/test_profile_isolation.py`, `tests/test_profile_migrate.py`, `tests/test_shared_settings.py`,
  plus edits to `tests/test_ui_registry.py`.

## Ordering

Task 1 (`profiles.py`) is the foundation for everything. Task 2 (binding) depends on 1.
Task 3 (config) depends on 1. Task 4 (atlas split) depends on 3. Tasks 5–7 depend on 1–3.
Task 8 (CLI) depends on 1–3. Task 9 (migration) depends on 1–4. Task 10 (isolation tests)
depends on everything. Task 11 is verification.

---

## Task 1: `profiles.py` — root and profile resolution

This module is imported by `__main__.py` **before** `applypilot.config`, so it must not
import anything from `applypilot`. Standard library only.

**Files:**
- Create: `src/applypilot/profiles.py`
- Test: `tests/test_profiles.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_profiles.py
import pytest

from applypilot import profiles


def test_valid_and_invalid_ids():
    assert profiles.is_valid_id("nida")
    assert profiles.is_valid_id("adwait_2")
    assert profiles.is_valid_id("a-b")
    for bad in ("", "Nida", "a b", "../etc", "a/b", "a.b", "x" * 65):
        assert not profiles.is_valid_id(bad), bad


def test_data_root_prefers_explicit_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ROOT", str(tmp_path))
    monkeypatch.delenv("APPLYPILOT_DIR", raising=False)
    assert profiles.data_root() == tmp_path


def test_data_root_falls_back_to_appdir_env(tmp_path, monkeypatch):
    monkeypatch.delenv("APPLYPILOT_ROOT", raising=False)
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    assert profiles.data_root() == tmp_path


def test_legacy_layout_detected_when_profile_json_present(tmp_path):
    (tmp_path / "profile.json").write_text("{}", encoding="utf-8")
    assert profiles.is_legacy_layout(tmp_path)


def test_multi_layout_when_profiles_dir_present(tmp_path):
    (tmp_path / "profiles" / "nida").mkdir(parents=True)
    assert not profiles.is_legacy_layout(tmp_path)


def test_list_and_dir(tmp_path):
    for p in ("nida", "adwait"):
        (tmp_path / "profiles" / p).mkdir(parents=True)
    assert profiles.list_profiles(tmp_path) == ["adwait", "nida"]   # sorted
    assert profiles.profile_dir(tmp_path, "nida") == tmp_path / "profiles" / "nida"


def test_profile_dir_rejects_traversal(tmp_path):
    with pytest.raises(ValueError):
        profiles.profile_dir(tmp_path, "../escape")


def test_active_profile_roundtrip(tmp_path):
    (tmp_path / "profiles" / "nida").mkdir(parents=True)
    assert profiles.get_active(tmp_path) is None
    profiles.set_active(tmp_path, "nida")
    assert profiles.get_active(tmp_path) == "nida"


def test_set_active_rejects_unknown(tmp_path):
    with pytest.raises(ValueError):
        profiles.set_active(tmp_path, "ghost")


def test_resolve_precedence_flag_beats_env(tmp_path, monkeypatch):
    for p in ("nida", "adwait"):
        (tmp_path / "profiles" / p).mkdir(parents=True)
    monkeypatch.setenv("APPLYPILOT_PROFILE", "nida")
    assert profiles.resolve(tmp_path, argv=["apply", "--profile", "adwait"]) == "adwait"


def test_resolve_env_beats_active_file(tmp_path, monkeypatch):
    for p in ("nida", "adwait"):
        (tmp_path / "profiles" / p).mkdir(parents=True)
    profiles.set_active(tmp_path, "nida")
    monkeypatch.setenv("APPLYPILOT_PROFILE", "adwait")
    assert profiles.resolve(tmp_path, argv=["apply"]) == "adwait"


def test_resolve_active_file_beats_nothing(tmp_path, monkeypatch):
    for p in ("nida", "adwait"):
        (tmp_path / "profiles" / p).mkdir(parents=True)
    monkeypatch.delenv("APPLYPILOT_PROFILE", raising=False)
    profiles.set_active(tmp_path, "adwait")
    assert profiles.resolve(tmp_path, argv=["apply"]) == "adwait"


def test_resolve_sole_profile_needs_no_config(tmp_path, monkeypatch):
    (tmp_path / "profiles" / "nida").mkdir(parents=True)
    monkeypatch.delenv("APPLYPILOT_PROFILE", raising=False)
    assert profiles.resolve(tmp_path, argv=["apply"]) == "nida"


def test_resolve_ambiguous_raises_listing_choices(tmp_path, monkeypatch):
    for p in ("nida", "adwait"):
        (tmp_path / "profiles" / p).mkdir(parents=True)
    monkeypatch.delenv("APPLYPILOT_PROFILE", raising=False)
    with pytest.raises(profiles.ProfileError) as e:
        profiles.resolve(tmp_path, argv=["apply"])
    assert "adwait" in str(e.value) and "nida" in str(e.value)


def test_resolve_unknown_profile_raises(tmp_path):
    (tmp_path / "profiles" / "nida").mkdir(parents=True)
    with pytest.raises(profiles.ProfileError):
        profiles.resolve(tmp_path, argv=["apply", "--profile", "ghost"])


@pytest.mark.parametrize("argv,expected", [
    (["apply", "--profile", "nida"], "nida"),
    (["apply", "--profile=nida"], "nida"),
    (["--profile", "nida", "apply"], "nida"),
    (["apply"], None),
    (["apply", "--profile"], None),          # dangling flag, no value
])
def test_profile_from_argv(argv, expected):
    assert profiles.profile_from_argv(argv) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PY -m pytest tests/test_profiles.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'applypilot.profiles'`

- [ ] **Step 3: Write the implementation**

```python
# src/applypilot/profiles.py
"""Root + profile resolution.

Imported by __main__.py BEFORE applypilot.config, so this module must import
nothing from applypilot — standard library only.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

ACTIVE_FILE = "active_profile"
PROFILES_DIRNAME = "profiles"
SHARED_DIRNAME = "shared"


class ProfileError(Exception):
    """Profile could not be resolved. The message is shown to the operator."""


def is_valid_id(pid: str) -> bool:
    return bool(pid) and isinstance(pid, str) and bool(_ID_RE.match(pid))


def data_root() -> Path:
    """The data ROOT holding shared/ and profiles/.

    APPLYPILOT_ROOT wins; otherwise fall back to APPLYPILOT_DIR so an existing
    installation keeps its location; otherwise ~/.applypilot.
    """
    for var in ("APPLYPILOT_ROOT", "APPLYPILOT_DIR"):
        val = os.environ.get(var)
        if val:
            return Path(val)
    return Path.home() / ".applypilot"


def is_legacy_layout(root: Path) -> bool:
    """True when `root` is a single-profile data dir (a profile.json sits in it,
    or it has no profiles/ subdir). Legacy dirs bypass profile resolution, which
    is what the existing test suite relies on."""
    root = Path(root)
    if (root / "profile.json").exists():
        return True
    return not (root / PROFILES_DIRNAME).is_dir()


def profiles_dir(root: Path) -> Path:
    return Path(root) / PROFILES_DIRNAME


def shared_dir(root: Path) -> Path:
    return Path(root) / SHARED_DIRNAME


def profile_dir(root: Path, pid: str) -> Path:
    if not is_valid_id(pid):
        raise ValueError(f"invalid profile id: {pid!r}")
    return profiles_dir(root) / pid


def list_profiles(root: Path) -> list[str]:
    d = profiles_dir(root)
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and is_valid_id(p.name))


def get_active(root: Path) -> str | None:
    p = Path(root) / ACTIVE_FILE
    try:
        pid = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return pid if is_valid_id(pid) and pid in list_profiles(root) else None


def set_active(root: Path, pid: str) -> None:
    if pid not in list_profiles(root):
        raise ValueError(f"unknown profile: {pid!r}")
    p = Path(root) / ACTIVE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(pid, encoding="utf-8")


def profile_from_argv(argv: list[str]) -> str | None:
    """Minimal scan for --profile. Tolerates `--profile X` and `--profile=X`.
    Deliberately does not validate — resolve() reports the error with context."""
    for i, a in enumerate(argv):
        if a == "--profile":
            return argv[i + 1] if i + 1 < len(argv) else None
        if a.startswith("--profile="):
            return a.split("=", 1)[1] or None
    return None


def resolve(root: Path, argv: list[str] | None = None) -> str:
    """Resolve the bound profile: flag > env > active file > sole profile."""
    argv = list(argv if argv is not None else [])
    available = list_profiles(root)

    requested = profile_from_argv(argv) or os.environ.get("APPLYPILOT_PROFILE") or None
    if requested:
        if requested not in available:
            raise ProfileError(
                f"unknown profile {requested!r}. Available: {', '.join(available) or '(none)'}"
            )
        return requested

    active = get_active(root)
    if active:
        return active
    if len(available) == 1:
        return available[0]
    if not available:
        raise ProfileError(
            f"no profiles found under {profiles_dir(root)}. "
            "Run `applypilot profile migrate` or `applypilot profile add <id>`."
        )
    raise ProfileError(
        "multiple profiles and no active one selected: "
        f"{', '.join(available)}. Use --profile <id> or `applypilot profile use <id>`."
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PY -m pytest tests/test_profiles.py -q`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git reset
git add src/applypilot/profiles.py tests/test_profiles.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "profiles: root + profile resolution (flag > env > active file > sole profile)"
```

---

## Task 2: Pre-import binding hook in `__main__.py`

`config.py` freezes its path constants at import time, so the profile must be bound
**before** `applypilot.cli` is imported. This is the entire mechanism that keeps the other
19 `load_profile()` call sites untouched.

**Files:**
- Modify: `src/applypilot/__main__.py` (currently 3 lines)
- Test: `tests/test_profile_binding.py`

- [ ] **Step 1: Write the failing test**

These run a real subprocess, because the property under test is import ordering.

```python
# tests/test_profile_binding.py
import json
import os
import subprocess
import sys
import textwrap

SNIPPET = textwrap.dedent("""
    import json, sys
    import applypilot.__main__ as m
    m.bind_profile(sys.argv[1:])
    from applypilot import config
    print(json.dumps({"app_dir": str(config.APP_DIR), "db": str(config.DB_PATH)}))
""")


def _run(tmp_path, argv, env_extra):
    env = dict(os.environ)
    env.pop("APPLYPILOT_PROFILE", None)
    env["APPLYPILOT_ROOT"] = str(tmp_path)
    env.pop("APPLYPILOT_DIR", None)
    env.update(env_extra)
    script = tmp_path / "snip.py"
    script.write_text(SNIPPET, encoding="utf-8")
    out = subprocess.run([sys.executable, str(script), *argv], env=env,
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_flag_binds_app_dir_to_profile(tmp_path):
    for p in ("nida", "adwait"):
        (tmp_path / "profiles" / p).mkdir(parents=True)
    got = _run(tmp_path, ["--profile", "adwait"], {})
    assert got["app_dir"] == str(tmp_path / "profiles" / "adwait")
    assert got["db"] == str(tmp_path / "profiles" / "adwait" / "applypilot.db")


def test_env_binds_app_dir(tmp_path):
    for p in ("nida", "adwait"):
        (tmp_path / "profiles" / p).mkdir(parents=True)
    got = _run(tmp_path, [], {"APPLYPILOT_PROFILE": "nida"})
    assert got["app_dir"] == str(tmp_path / "profiles" / "nida")


def test_legacy_dir_is_used_directly(tmp_path):
    """An explicit APPLYPILOT_DIR containing profile.json bypasses resolution.
    This is what the existing suite relies on."""
    (tmp_path / "profile.json").write_text("{}", encoding="utf-8")
    got = _run(tmp_path, [], {"APPLYPILOT_DIR": str(tmp_path)})
    assert got["app_dir"] == str(tmp_path)


def test_binding_is_noop_when_already_bound(tmp_path):
    """bind_profile must not override an APPLYPILOT_DIR the caller set on purpose
    (the UI spawns subprocesses this way)."""
    (tmp_path / "profiles" / "nida").mkdir(parents=True)
    explicit = tmp_path / "profiles" / "nida"
    got = _run(tmp_path, [], {"APPLYPILOT_DIR": str(explicit)})
    assert got["app_dir"] == str(explicit)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PY -m pytest tests/test_profile_binding.py -q`
Expected: FAIL — `AttributeError: module 'applypilot.__main__' has no attribute 'bind_profile'`

- [ ] **Step 3: Write the implementation**

```python
# src/applypilot/__main__.py
"""Enable `python -m applypilot`.

The bound profile MUST be resolved before applypilot.cli is imported, because
applypilot.config computes its path constants at import time.
"""

import os
import sys


def bind_profile(argv: list[str] | None = None) -> None:
    """Point APPLYPILOT_DIR at the resolved profile directory.

    No-op when APPLYPILOT_DIR is already set (the UI and tests set it on purpose)
    or when the root is a legacy single-profile layout.
    """
    from applypilot import profiles

    if os.environ.get("APPLYPILOT_DIR"):
        return

    root = profiles.data_root()
    if profiles.is_legacy_layout(root):
        os.environ["APPLYPILOT_DIR"] = str(root)
        return

    pid = profiles.resolve(root, argv if argv is not None else sys.argv[1:])
    os.environ["APPLYPILOT_DIR"] = str(profiles.profile_dir(root, pid))
    os.environ["APPLYPILOT_PROFILE"] = pid


def main() -> None:
    from applypilot import profiles
    try:
        bind_profile(sys.argv[1:])
    except profiles.ProfileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    from applypilot.cli import app
    app()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PY -m pytest tests/test_profile_binding.py -q`
Expected: PASS

- [ ] **Step 5: Confirm the console-script entrypoint also binds**

`pyproject.toml` maps the `applypilot` command to an entrypoint. Read it:

Run: `grep -A3 "\[project.scripts\]" pyproject.toml`

If it points at `applypilot.cli:app`, repoint it to `applypilot.__main__:main` so the
installed command binds the profile too. If it already points at `__main__:main`, no change.
Record which it was in the commit message.

- [ ] **Step 6: Run the full suite — legacy mode must be untouched**

Run: `PY -m pytest -q`
Expected: `963 passed, 1 skipped`

- [ ] **Step 7: Commit**

```bash
git reset
git add src/applypilot/__main__.py tests/test_profile_binding.py pyproject.toml
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "profiles: bind APPLYPILOT_DIR before importing cli so config path constants resolve per-profile"
```

---

## Task 3: `config.py` — shared paths

**Files:**
- Modify: `src/applypilot/config.py:8-33`
- Test: `tests/test_profiles.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_profiles.py
def test_config_exposes_shared_paths(tmp_path, monkeypatch):
    """SHARED_DIR/ATLAS_DB_PATH derive from the ROOT, not from APP_DIR."""
    import importlib

    monkeypatch.setenv("APPLYPILOT_ROOT", str(tmp_path))
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path / "profiles" / "nida"))
    from applypilot import config as _c
    config = importlib.reload(_c)
    try:
        assert config.ROOT == tmp_path
        assert config.SHARED_DIR == tmp_path / "shared"
        assert config.ATLAS_DB_PATH == tmp_path / "shared" / "atlas.db"
        assert config.APP_DIR == tmp_path / "profiles" / "nida"
    finally:
        monkeypatch.undo()
        importlib.reload(_c)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PY -m pytest tests/test_profiles.py::test_config_exposes_shared_paths -q`
Expected: FAIL — `AttributeError: module 'applypilot.config' has no attribute 'ROOT'`

- [ ] **Step 3: Write the implementation**

In `src/applypilot/config.py`, immediately after the existing `APP_DIR` line (line 9), add:

```python
# Data ROOT — holds shared/ and profiles/. APP_DIR is the BOUND PROFILE's dir
# (see applypilot.profiles + __main__.bind_profile); ROOT is where the two
# profiles-agnostic things live. In a legacy single-profile layout they are the
# same directory.
from applypilot.profiles import data_root as _data_root  # noqa: E402

ROOT = _data_root()
SHARED_DIR = ROOT / "shared"
ATLAS_DB_PATH = SHARED_DIR / "atlas.db"
```

Then extend `ensure_dirs()` (line 91) to create `SHARED_DIR`:

```python
def ensure_dirs():
    """Create all required directories."""
    for d in [APP_DIR, TAILORED_DIR, COVER_LETTER_DIR, LOG_DIR, CHROME_WORKER_DIR,
              APPLY_WORKER_DIR, SHARED_DIR]:
        d.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PY -m pytest tests/test_profiles.py -q`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `PY -m pytest -q`
Expected: `963 passed, 1 skipped` (plus the new tests)

- [ ] **Step 6: Commit**

```bash
git reset
git add src/applypilot/config.py tests/test_profiles.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "config: ROOT/SHARED_DIR/ATLAS_DB_PATH derived from the data root"
```

---

## Task 4: Split the four atlas tables into an attached shared database

**Read first:** `src/applypilot/database.py:20-90` (`get_connection`, `init_db`) and the DDL at
lines 197 (`boards`), 225 (`source_runs`), 248 (`mapping_cache`), 264 (`submit_endpoints`).

**The mechanism, already verified empirically:** SQLite resolves an unqualified table name
across attached databases. Opening the profile DB as `main` and attaching the shared DB as
`atlas` means `FROM boards` resolves to `atlas.boards` and `FROM jobs` to `main.jobs`, with
**no change to any SQL statement or function signature**. Every atlas module already takes
`conn` as its first parameter.

**Do not** thread a second connection through `run_tick`/`poll_board`. That was considered
and rejected in the spec.

**Files:**
- Modify: `src/applypilot/database.py`
- Test: `tests/test_atlas_split.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_atlas_split.py
import sqlite3

from applypilot import database

ATLAS_TABLES = {"boards", "source_runs", "mapping_cache", "submit_endpoints"}
PROFILE_TABLES = {"jobs", "submission_ledger", "engine_control"}


def _tables(conn, schema):
    return {r[0] for r in conn.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table'")}


def test_atlas_tables_live_in_attached_db(tmp_path):
    db = tmp_path / "p.db"
    atlas = tmp_path / "shared" / "atlas.db"
    conn = database.init_db(db, atlas_path=atlas)
    assert ATLAS_TABLES <= _tables(conn, "atlas")
    assert PROFILE_TABLES <= _tables(conn, "main")
    assert not (ATLAS_TABLES & _tables(conn, "main"))


def test_unqualified_names_resolve_across_databases(tmp_path):
    """The whole point: existing SQL keeps working unchanged."""
    conn = database.init_db(tmp_path / "p.db", atlas_path=tmp_path / "atlas.db")
    conn.execute("INSERT INTO boards (ats, token) VALUES ('greenhouse','twilio')")
    conn.commit()
    assert conn.execute("SELECT token FROM boards").fetchone()[0] == "twilio"
    conn.execute("UPDATE boards SET ring=1 WHERE token='twilio'")
    conn.commit()
    assert conn.execute("SELECT ring FROM boards").fetchone()[0] == 1


def test_atlas_is_shared_between_two_profiles(tmp_path):
    atlas = tmp_path / "atlas.db"
    a = database.init_db(tmp_path / "a.db", atlas_path=atlas)
    b = database.init_db(tmp_path / "b.db", atlas_path=atlas)
    a.execute("INSERT INTO boards (ats, token) VALUES ('lever','acme')")
    a.commit()
    assert b.execute("SELECT COUNT(*) FROM boards WHERE token='acme'").fetchone()[0] == 1


def test_jobs_are_NOT_shared_between_two_profiles(tmp_path):
    """The load-bearing isolation property."""
    atlas = tmp_path / "atlas.db"
    a = database.init_db(tmp_path / "a.db", atlas_path=atlas)
    b = database.init_db(tmp_path / "b.db", atlas_path=atlas)
    a.execute("INSERT INTO jobs (url, title) VALUES ('http://x','Designer')")
    a.commit()
    assert b.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_legacy_single_db_creates_all_tables_in_main(tmp_path):
    """No atlas_path => today's behavior exactly. Keeps the existing suite green."""
    conn = database.init_db(tmp_path / "legacy.db")
    assert (ATLAS_TABLES | PROFILE_TABLES) <= _tables(conn, "main")


def test_in_memory_still_works():
    conn = database.init_db(":memory:")
    assert (ATLAS_TABLES | PROFILE_TABLES) <= _tables(conn, "main")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PY -m pytest tests/test_atlas_split.py -q`
Expected: FAIL — `init_db() got an unexpected keyword argument 'atlas_path'`

- [ ] **Step 3: Implement**

In `database.py`, add the module-level constant and helper:

```python
ATLAS_TABLES = ("boards", "source_runs", "mapping_cache", "submit_endpoints")


def _attach_atlas(conn, atlas_path) -> bool:
    """Attach the shared atlas DB as schema `atlas`. Returns True when attached.

    Unqualified table names resolve across attached databases, so callers need
    no changes. Returns False for in-memory/legacy use, where all tables live in
    main exactly as before.
    """
    if atlas_path is None:
        return False
    from pathlib import Path
    p = Path(atlas_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn.execute("ATTACH DATABASE ? AS atlas", (str(p),))
    conn.execute("PRAGMA atlas.journal_mode=WAL")
    return True
```

Give `get_connection` and `init_db` an `atlas_path` parameter defaulting to `None`. When
`init_db` is called with no explicit `db_path` (i.e. the real configured database), default
`atlas_path` to `config.ATLAS_DB_PATH` — but **only** when the layout is not legacy:

```python
def _default_atlas_path():
    """The configured shared atlas, or None in a legacy single-profile layout
    (where every table stays in main, as today)."""
    from applypilot import config, profiles
    if profiles.is_legacy_layout(config.ROOT):
        return None
    return config.ATLAS_DB_PATH
```

In `init_db`, attach **before** running the DDL, and prefix the four atlas `CREATE TABLE`
statements with the schema when attached. Concretely, compute a prefix once:

```python
    attached = _attach_atlas(conn, atlas_path)
    ns = "atlas." if attached else ""
```

then change those four statements (lines 197, 225, 248, 264) from
`CREATE TABLE IF NOT EXISTS boards (` to `CREATE TABLE IF NOT EXISTS {ns}boards (`, making
each an f-string. Do the same for any `CREATE INDEX` on those four tables — check
`ensure_indexes` (line 417) and prefix indexes that target atlas tables.

Leave `jobs`, `submission_ledger` and `engine_control` untouched.

- [ ] **Step 4: Run test to verify it passes**

Run: `PY -m pytest tests/test_atlas_split.py -q`
Expected: PASS

- [ ] **Step 5: Confirm the atlas tick still works and stays idempotent**

The spec accepts that cross-database transactions are not atomic under WAL, on the grounds
that the tick is idempotent and resumable. Verify that assumption holds.

Run: `PY -m pytest tests/ -q -k "atlas or tick or board or mapping or poller"`
Expected: PASS, with no change in behavior.

- [ ] **Step 6: Run the full suite**

Run: `PY -m pytest -q`
Expected: `963 passed, 1 skipped` plus the new tests

- [ ] **Step 7: Commit**

```bash
git reset
git add src/applypilot/database.py tests/test_atlas_split.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "db: shared atlas via ATTACH — boards/source_runs/mapping_cache/submit_endpoints move to a shared DB with no SQL changes"
```

---

## Task 5: `profile_id` safety assertion

**Read first:** `src/applypilot/apply/launcher.py:2382` (`_safety_prologue`) — note how it
returns a refusal decision, and mirror that shape exactly rather than raising.

The guard catches a `profile.json` copied between directories, which is the realistic path
to a wrong-name submission.

**Files:**
- Modify: `src/applypilot/apply/launcher.py` (`_safety_prologue`)
- Test: `tests/test_profile_isolation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_profile_isolation.py
import pytest

from applypilot import identity_guard


def test_matching_profile_id_passes(tmp_path):
    d = tmp_path / "profiles" / "nida"
    d.mkdir(parents=True)
    identity_guard.assert_profile_identity({"profile_id": "nida"}, d)   # no raise


def test_mismatched_profile_id_refuses(tmp_path):
    d = tmp_path / "profiles" / "nida"
    d.mkdir(parents=True)
    with pytest.raises(identity_guard.ProfileIdentityError) as e:
        identity_guard.assert_profile_identity({"profile_id": "adwait"}, d)
    assert "adwait" in str(e.value) and "nida" in str(e.value)


def test_missing_profile_id_refuses(tmp_path):
    """Fail-safe: absence is a refusal, never a pass."""
    d = tmp_path / "profiles" / "nida"
    d.mkdir(parents=True)
    with pytest.raises(identity_guard.ProfileIdentityError):
        identity_guard.assert_profile_identity({}, d)


def test_legacy_dir_without_profiles_parent_is_exempt(tmp_path):
    """A legacy single-profile dir has no profile_id and must keep working."""
    identity_guard.assert_profile_identity({}, tmp_path)   # no raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PY -m pytest tests/test_profile_isolation.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'applypilot.identity_guard'`

- [ ] **Step 3: Implement**

```python
# src/applypilot/identity_guard.py
"""Refuse to act on a profile whose declared identity disagrees with the
directory it was loaded from — the realistic path to a wrong-name submission."""
from __future__ import annotations

from pathlib import Path


class ProfileIdentityError(Exception):
    """profile.json's profile_id does not match its directory."""


def assert_profile_identity(profile: dict, app_dir: Path) -> None:
    app_dir = Path(app_dir)
    # Legacy single-profile layouts are not under a profiles/ parent and carry
    # no profile_id; they are exempt.
    if app_dir.parent.name != "profiles":
        return
    declared = (profile or {}).get("profile_id")
    expected = app_dir.name
    if declared != expected:
        raise ProfileIdentityError(
            f"profile.json declares profile_id={declared!r} but was loaded from "
            f"{app_dir} (expected {expected!r}). Refusing to act under an "
            "ambiguous identity."
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PY -m pytest tests/test_profile_isolation.py -q`
Expected: PASS

- [ ] **Step 5: Wire it into the safety prologue**

In `_safety_prologue` (`launcher.py:2382`), after the profile is loaded and **before** any
INTENT is recorded, call `assert_profile_identity(profile, config.APP_DIR)`. Convert a
`ProfileIdentityError` into the same refusal decision shape the prologue already returns for
other refusals — do not let it raise into the worker loop. Read the surrounding code and
match the existing return convention exactly.

Add a test asserting a mismatched profile produces a refusal (not an exception) through the
prologue, following the existing prologue-test patterns in the suite.

- [ ] **Step 6: Run the full suite**

Run: `PY -m pytest -q`
Expected: all green

- [ ] **Step 7: Commit**

```bash
git reset
git add src/applypilot/identity_guard.py src/applypilot/apply/launcher.py tests/test_profile_isolation.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "safety: refuse to apply when profile.json's profile_id disagrees with its directory"
```

---

## Task 6: Registry moves to `shared/ui_runs`, records tagged with profile

One batch runs at a time **globally**, so the registry cannot live under a per-profile
directory. Records gain a `profile` field so the Runs history shows whose batch each was.

**Read first:** `src/applypilot/webui/registry.py` in full (137 lines) — note `_dir()` reads
`config.APP_DIR` at call time.

**Files:**
- Modify: `src/applypilot/webui/registry.py`
- Modify: `tests/test_ui_registry.py`

- [ ] **Step 1: Update the existing tests and add coverage**

Change every `monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)` in
`tests/test_ui_registry.py` to patch `SHARED_DIR` instead:

```python
    monkeypatch.setattr("applypilot.config.SHARED_DIR", tmp_path)
```

and change the `open_batch(...)` calls to pass a profile, e.g.
`reg.open_batch(kind="live_apply", dry_run=False, args=[], pid=os.getpid(), profile="nida")`.

Add:

```python
def test_record_carries_profile(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.SHARED_DIR", tmp_path)
    rec = reg.open_batch(kind="live_apply", dry_run=False, args=[],
                         pid=os.getpid(), profile="adwait")
    assert rec["profile"] == "adwait"
    assert reg.get_batch(rec["id"])["profile"] == "adwait"


def test_single_active_batch_is_global_across_profiles(tmp_path, monkeypatch):
    """Two profiles must not both hold an active batch."""
    monkeypatch.setattr("applypilot.config.SHARED_DIR", tmp_path)
    reg.open_batch(kind="live_apply", dry_run=False, args=[], pid=os.getpid(), profile="nida")
    assert reg.active_batch()["profile"] == "nida"
    reg.open_batch(kind="live_apply", dry_run=True, args=[], pid=os.getpid(), profile="adwait")
    assert reg.active_batch()["profile"] == "adwait"   # one slot, globally
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PY -m pytest tests/test_ui_registry.py -q`
Expected: FAIL — `open_batch() got an unexpected keyword argument 'profile'`

- [ ] **Step 3: Implement**

In `registry.py`, change `_dir()` to use the shared dir:

```python
def _dir():
    from applypilot import config
    d = config.SHARED_DIR / "ui_runs"
    d.mkdir(parents=True, exist_ok=True)
    return d
```

and give `open_batch` a required keyword-only `profile: str`, storing it in the record:

```python
def open_batch(*, kind: str, dry_run: bool, args: list[str], pid: int,
               profile: str) -> dict:
    rec = {
        "id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6],
        "kind": kind, "dry_run": bool(dry_run), "args": list(args), "pid": int(pid),
        "profile": profile,
        "started_at": _now(), "finished_at": None, "returncode": None, "outcome": None,
    }
    d = _dir()
    _atomic_write(d / f"{rec['id']}.json", rec)
    (d / "current.pid").write_text(rec["id"], encoding="utf-8")
    return rec
```

Everything else is unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PY -m pytest tests/test_ui_registry.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git reset
git add src/applypilot/webui/registry.py tests/test_ui_registry.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "webui: batch registry moves to shared/ui_runs and tags each record with its profile"
```

---

## Task 7: Shared settings (global spend cap + autopilot)

Per the spec: money is one wallet and the runner is global, so the spend cap and autopilot
config are shared; the apply cap and batch defaults stay per profile in the existing
`webui/settings.py`.

**Read first:** `src/applypilot/webui/settings.py` in full (83 lines) and mirror its
structure — deep merge, atomic write, fail-safe load.

**Files:**
- Create: `src/applypilot/webui/shared_settings.py`
- Test: `tests/test_shared_settings.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shared_settings.py
from applypilot.webui import shared_settings as ss


def test_defaults_are_safe(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.SHARED_DIR", tmp_path)
    s = ss.load_shared_settings()
    assert s["autopilot_enabled"] is False
    assert s["autopilot_profiles"] == []
    assert s["spend_cap_usd_per_day"] == 5.0


def test_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.SHARED_DIR", tmp_path)
    ss.update_shared_settings({"spend_cap_usd_per_day": 9.0,
                               "autopilot_profiles": ["nida", "adwait"]})
    s = ss.load_shared_settings()
    assert s["spend_cap_usd_per_day"] == 9.0
    assert s["autopilot_profiles"] == ["nida", "adwait"]
    assert s["autopilot_enabled"] is False


def test_corrupt_file_resolves_to_safe_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.SHARED_DIR", tmp_path)
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    s = ss.load_shared_settings()
    assert s["autopilot_enabled"] is False


def test_rejects_nonpositive_spend_cap(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr("applypilot.config.SHARED_DIR", tmp_path)
    with pytest.raises(ValueError):
        ss.update_shared_settings({"spend_cap_usd_per_day": 0})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PY -m pytest tests/test_shared_settings.py -q`
Expected: FAIL — `ModuleNotFoundError: ...shared_settings`

- [ ] **Step 3: Implement**

```python
# src/applypilot/webui/shared_settings.py
"""Global operator settings: one wallet, one runner. Per-profile apply caps and
batch defaults live in webui/settings.py instead. A missing or corrupt file
resolves to the SAFE side (autopilot OFF)."""
from __future__ import annotations

import json
import os
import tempfile

DEFAULT_SHARED_SETTINGS: dict = {
    "autopilot_enabled": False,       # global: one runner, one switch
    "autopilot_profiles": [],         # ordered; >1 alternates batches
    "spend_cap_usd_per_day": 5.0,     # global: one wallet
}


def _path():
    from applypilot import config
    return config.SHARED_DIR / "settings.json"


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_shared_settings() -> dict:
    p = _path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        if not isinstance(raw, dict):
            raw = {}
    except Exception:                 # noqa: BLE001 — fail-safe
        raw = {}
    return _deep_merge(DEFAULT_SHARED_SETTINGS, raw)


def _validate(s: dict) -> None:
    if float(s["spend_cap_usd_per_day"]) <= 0:
        raise ValueError("spend_cap_usd_per_day must be > 0")
    if not isinstance(s["autopilot_profiles"], list):
        raise ValueError("autopilot_profiles must be a list")


def save_shared_settings(s: dict) -> dict:
    merged = _deep_merge(DEFAULT_SHARED_SETTINGS, s or {})
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


def update_shared_settings(patch: dict) -> dict:
    return save_shared_settings(_deep_merge(load_shared_settings(), patch or {}))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PY -m pytest tests/test_shared_settings.py -q`
Expected: PASS

- [ ] **Step 5: Remove the now-duplicated spend cap from the per-profile settings**

In `src/applypilot/webui/settings.py`, delete `"spend_cap_usd_per_day": 5.0,` from
`DEFAULT_SETTINGS` and its check from `_validate`, since it is now global. Update
`tests/test_ui_settings.py` accordingly. Keep `max_live_applies_per_day` per profile.

- [ ] **Step 6: Run the full suite**

Run: `PY -m pytest -q`
Expected: all green

- [ ] **Step 7: Commit**

```bash
git reset
git add src/applypilot/webui/shared_settings.py src/applypilot/webui/settings.py tests/test_shared_settings.py tests/test_ui_settings.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "webui: global shared settings (one wallet, one runner); per-profile settings keep the apply cap"
```

---

## Task 8: CLI — `--profile` option and `profile` sub-commands

**Read first:** `src/applypilot/cli.py:1-60` (the Typer app + callback, if any), and the
`ui` command at line 930.

**Files:**
- Modify: `src/applypilot/cli.py`
- Test: `tests/test_profile_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_profile_cli.py
import json

from typer.testing import CliRunner

from applypilot.cli import app

runner = CliRunner()


def _mk(root, pid):
    d = root / "profiles" / pid
    d.mkdir(parents=True)
    (d / "profile.json").write_text(
        json.dumps({"profile_id": pid, "personal": {"name": pid.title()}}),
        encoding="utf-8")
    return d


def test_profile_list_shows_ids_and_active(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ROOT", str(tmp_path))
    _mk(tmp_path, "nida"); _mk(tmp_path, "adwait")
    (tmp_path / "active_profile").write_text("nida", encoding="utf-8")
    res = runner.invoke(app, ["profile", "list"])
    assert res.exit_code == 0
    assert "nida" in res.stdout and "adwait" in res.stdout


def test_profile_use_sets_active(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ROOT", str(tmp_path))
    _mk(tmp_path, "nida"); _mk(tmp_path, "adwait")
    res = runner.invoke(app, ["profile", "use", "adwait"])
    assert res.exit_code == 0
    assert (tmp_path / "active_profile").read_text(encoding="utf-8").strip() == "adwait"


def test_profile_use_rejects_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ROOT", str(tmp_path))
    _mk(tmp_path, "nida")
    res = runner.invoke(app, ["profile", "use", "ghost"])
    assert res.exit_code != 0


def test_profile_add_scaffolds_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ROOT", str(tmp_path))
    res = runner.invoke(app, ["profile", "add", "adwait", "--no-wizard"])
    assert res.exit_code == 0
    assert (tmp_path / "profiles" / "adwait").is_dir()


def test_profile_add_rejects_invalid_id(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ROOT", str(tmp_path))
    res = runner.invoke(app, ["profile", "add", "Bad Id"])
    assert res.exit_code != 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PY -m pytest tests/test_profile_cli.py -q`
Expected: FAIL — no such command `profile`

- [ ] **Step 3: Implement**

Add this to `cli.py`, then register it with `app.add_typer(profile_app, name="profile")`:

```python
profile_app = typer.Typer(help="Manage the people ApplyPilot applies for.")


def _root():
    from applypilot import profiles
    return profiles.data_root()


def _display_name(pdir) -> str:
    """Best-effort display name; an unreadable profile.json must never raise."""
    import json
    try:
        data = json.loads((pdir / "profile.json").read_text(encoding="utf-8"))
        return (data.get("personal") or {}).get("name") or ""
    except Exception:      # noqa: BLE001
        return ""


@profile_app.command("list")
def profile_list() -> None:
    from applypilot import profiles
    root = _root()
    ids = profiles.list_profiles(root)
    if not ids:
        typer.echo(f"No profiles under {profiles.profiles_dir(root)}.")
        typer.echo("Run `applypilot profile migrate --yes` or `applypilot profile add <id>`.")
        return
    active = profiles.get_active(root)
    for pid in ids:
        mark = "*" if pid == active else " "
        typer.echo(f" {mark} {pid:<16} {_display_name(profiles.profile_dir(root, pid))}")


@profile_app.command("show")
def profile_show(pid: str = typer.Argument(None)) -> None:
    from applypilot import profiles
    root = _root()
    if pid is None:
        try:
            pid = profiles.resolve(root, [])
        except profiles.ProfileError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2)
    if pid not in profiles.list_profiles(root):
        typer.echo(f"error: unknown profile {pid!r}", err=True)
        raise typer.Exit(2)
    d = profiles.profile_dir(root, pid)
    typer.echo(f"id:   {pid}")
    typer.echo(f"name: {_display_name(d)}")
    typer.echo(f"dir:  {d}")


@profile_app.command("add")
def profile_add(pid: str,
                wizard: bool = typer.Option(True, "--wizard/--no-wizard")) -> None:
    import json
    from applypilot import profiles
    root = _root()
    if not profiles.is_valid_id(pid):
        typer.echo(f"error: invalid profile id {pid!r} "
                   "(lowercase letters, digits, dash, underscore; max 64)", err=True)
        raise typer.Exit(2)
    d = profiles.profiles_dir(root) / pid
    if d.exists():
        typer.echo(f"error: {d} already exists", err=True)
        raise typer.Exit(2)
    (d / "logs").mkdir(parents=True)
    (d / "profile.json").write_text(json.dumps({"profile_id": pid}, indent=2),
                                    encoding="utf-8")
    typer.echo(f"created {d}")
    if len(profiles.list_profiles(root)) == 1:
        profiles.set_active(root, pid)
        typer.echo(f"active profile set to {pid}")
    if wizard:
        typer.echo(f"Now run:  applypilot --profile {pid} init")


@profile_app.command("use")
def profile_use(pid: str) -> None:
    from applypilot import profiles
    from applypilot.webui import registry
    root = _root()
    try:
        active_batch = registry.active_batch()
    except Exception:      # noqa: BLE001 — no registry yet is not an error
        active_batch = None
    if active_batch and active_batch.get("finished_at") is None:
        typer.echo(f"error: a batch is running for profile "
                   f"{active_batch.get('profile')!r}; stop it before switching.", err=True)
        raise typer.Exit(2)
    try:
        profiles.set_active(root, pid)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2)
    typer.echo(f"active profile set to {pid}")
```

Add a global `--profile` option to the existing Typer callback so `applypilot --profile X ...`
is accepted and appears in `--help`. It is consumed by `bind_profile` before Typer runs, so
the callback accepts and ignores it:

```python
@app.callback()
def _main(profile: str = typer.Option(None, "--profile",
                                      help="Which person to act for.")) -> None:
    # Consumed by __main__.bind_profile before this module was imported.
    pass
```

Finally, fix `cli.py:958` so the `ui` command passes the resolved paths explicitly rather
than calling `create_app()` bare:

```python
    uvicorn.run(create_app(db_path=config.DB_PATH, app_dir=config.APP_DIR),
                host=host, port=port)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PY -m pytest tests/test_profile_cli.py -q`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `PY -m pytest -q`
Expected: all green

- [ ] **Step 6: Commit**

```bash
git reset
git add src/applypilot/cli.py tests/test_profile_cli.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "cli: --profile option, profile list/show/add/use, ui passes resolved paths"
```

---

## Task 9: Migration

**Files:**
- Create: `src/applypilot/migrate_profiles.py`
- Modify: `src/applypilot/cli.py` (register `profile migrate`)
- Test: `tests/test_profile_migrate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_profile_migrate.py
import json
import sqlite3
from pathlib import Path

from applypilot import migrate_profiles


def _legacy_root(tmp_path):
    root = tmp_path / "data"
    (root / "logs").mkdir(parents=True)
    (root / "profile.json").write_text(json.dumps({"personal": {"name": "Nida Shah"}}),
                                       encoding="utf-8")
    (root / "resume.pdf").write_bytes(b"%PDF-1.4 fake")
    (root / "searches.yaml").write_text("queries: []\n", encoding="utf-8")
    (root / "logs" / "review.jsonl").write_text('{"x":1}\n', encoding="utf-8")
    db = root / "applypilot.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('http://a','Designer')")
    conn.execute("CREATE TABLE boards (ats TEXT, token TEXT, ring INT)")
    conn.execute("INSERT INTO boards VALUES ('greenhouse','twilio',0)")
    conn.execute("CREATE TABLE source_runs (id INTEGER PRIMARY KEY, source TEXT)")
    conn.execute("CREATE TABLE mapping_cache (ats TEXT, field_fp TEXT, binding TEXT)")
    conn.execute("CREATE TABLE submit_endpoints (ats TEXT, company TEXT)")
    conn.commit(); conn.close()
    return root


def test_migrate_moves_personal_files_into_profile(tmp_path):
    root = _legacy_root(tmp_path)
    migrate_profiles.migrate(root, profile_id="nida")
    p = root / "profiles" / "nida"
    assert (p / "profile.json").exists()
    assert (p / "resume.pdf").exists()
    assert (p / "searches.yaml").exists()
    assert (p / "logs" / "review.jsonl").exists()
    assert (p / "applypilot.db").exists()
    assert not (root / "profile.json").exists()


def test_migrate_stamps_profile_id(tmp_path):
    root = _legacy_root(tmp_path)
    migrate_profiles.migrate(root, profile_id="nida")
    data = json.loads((root / "profiles" / "nida" / "profile.json").read_text(encoding="utf-8"))
    assert data["profile_id"] == "nida"
    assert data["personal"]["name"] == "Nida Shah"     # preserved


def test_migrate_copies_atlas_tables_and_drops_them_from_profile_db(tmp_path):
    root = _legacy_root(tmp_path)
    migrate_profiles.migrate(root, profile_id="nida")
    atlas = sqlite3.connect(root / "shared" / "atlas.db")
    assert atlas.execute("SELECT token FROM boards").fetchone()[0] == "twilio"
    prof = sqlite3.connect(root / "profiles" / "nida" / "applypilot.db")
    names = {r[0] for r in prof.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "jobs" in names
    assert not ({"boards", "source_runs", "mapping_cache", "submit_endpoints"} & names)


def test_migrate_sets_active_profile(tmp_path):
    root = _legacy_root(tmp_path)
    migrate_profiles.migrate(root, profile_id="nida")
    assert (root / "active_profile").read_text(encoding="utf-8").strip() == "nida"


def test_migrate_is_idempotent(tmp_path):
    root = _legacy_root(tmp_path)
    migrate_profiles.migrate(root, profile_id="nida")
    result = migrate_profiles.migrate(root, profile_id="nida")
    assert result["already_migrated"] is True


def test_migrate_takes_a_backup_first(tmp_path):
    root = _legacy_root(tmp_path)
    result = migrate_profiles.migrate(root, profile_id="nida")
    backup = Path(result["backup"])
    assert backup.is_dir()
    assert (backup / "profile.json").exists()       # the pre-migration state
    assert (backup / "applypilot.db").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PY -m pytest tests/test_profile_migrate.py -q`
Expected: FAIL — `ModuleNotFoundError: ...migrate_profiles`

- [ ] **Step 3: Implement**

```python
# src/applypilot/migrate_profiles.py
"""One-shot, idempotent migration from the single-profile layout to profiles/.

Takes a full backup first. On failure it does NOT attempt a partial rollback —
the backup is the recovery path, and the error names it.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

ATLAS_TABLES = ("boards", "source_runs", "mapping_cache", "submit_endpoints")

# Never moved into the profile dir.
_ROOT_ONLY = {"profiles", "shared", "active_profile"}


def _extract_atlas(db_path: Path, atlas_path: Path) -> dict:
    """Copy the atlas tables into atlas_path, then drop them from db_path."""
    atlas_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    counts: dict[str, int] = {}
    try:
        conn.execute("ATTACH DATABASE ? AS atlas", (str(atlas_path),))
        for t in ATLAS_TABLES:
            row = conn.execute(
                "SELECT sql FROM main.sqlite_master WHERE type='table' AND name=?",
                (t,)).fetchone()
            if not row or not row[0]:
                continue
            # Recreate the table verbatim in the atlas schema.
            ddl = row[0].replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1)
            ddl = ddl.replace(t, f"atlas.{t}", 1)
            conn.execute(ddl)
            conn.execute(f"INSERT INTO atlas.{t} SELECT * FROM main.{t}")
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM atlas.{t}").fetchone()[0]
            conn.execute(f"DROP TABLE main.{t}")
        conn.commit()
    finally:
        conn.close()
    return counts


def migrate(root: Path, *, profile_id: str = "nida") -> dict:
    root = Path(root)
    pdir = root / "profiles" / profile_id

    if (pdir / "profile.json").exists():
        return {"already_migrated": True, "profile_id": profile_id,
                "profile_dir": str(pdir), "backup": None, "atlas_counts": {}}

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = root.parent / f"{root.name}_backup_{stamp}"
    shutil.copytree(root, backup)

    try:
        pdir.mkdir(parents=True, exist_ok=True)
        (root / "shared").mkdir(parents=True, exist_ok=True)

        for entry in list(root.iterdir()):
            if entry.name in _ROOT_ONLY or entry == backup:
                continue
            shutil.move(str(entry), str(pdir / entry.name))

        atlas_counts = {}
        moved_db = pdir / "applypilot.db"
        if moved_db.exists():
            atlas_counts = _extract_atlas(moved_db, root / "shared" / "atlas.db")

        pj = pdir / "profile.json"
        if pj.exists():
            data = json.loads(pj.read_text(encoding="utf-8"))
            data["profile_id"] = profile_id
            pj.write_text(json.dumps(data, indent=2), encoding="utf-8")

        (root / "active_profile").write_text(profile_id, encoding="utf-8")
    except Exception as exc:                      # noqa: BLE001
        raise RuntimeError(
            f"migration failed ({exc}). Your data is intact at {backup} — "
            "restore from there."
        ) from exc

    return {"already_migrated": False, "profile_id": profile_id,
            "profile_dir": str(pdir), "backup": str(backup),
            "atlas_counts": atlas_counts}
```

Note on the DDL rewrite: `ddl.replace(t, f"atlas.{t}", 1)` replaces the **first** occurrence
of the table name, which in `CREATE TABLE <name> (...)` is the name itself. Verify this on
each of the four real tables during Step 4 — if any DDL contains the table name earlier
(inside a comment, say), qualify it explicitly instead.

- [ ] **Step 4: Run test to verify it passes**

Run: `PY -m pytest tests/test_profile_migrate.py -q`
Expected: PASS

- [ ] **Step 5: Register the CLI command**

Add `profile migrate` to the `profile` sub-app, calling `migrate(profiles.data_root())`.
Print the backup path and the per-table counts. Require an explicit `--yes` flag, or prompt
for confirmation, since it moves real data.

- [ ] **Step 6: Run the full suite**

Run: `PY -m pytest -q`
Expected: all green

- [ ] **Step 7: Commit**

```bash
git reset
git add src/applypilot/migrate_profiles.py src/applypilot/cli.py tests/test_profile_migrate.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "migrate: one-shot idempotent move to the profiles/ layout with atlas extraction"
```

---

## Task 10: End-to-end isolation tests

These encode the load-bearing invariant. They are the tests that matter most in this plan.

**Files:**
- Modify: `tests/test_profile_isolation.py` (append)

- [ ] **Step 1: Write the tests**

```python
# append to tests/test_profile_isolation.py
import json
import os
import subprocess
import sys

from applypilot import database


def _mk_profile(root, pid):
    d = root / "profiles" / pid
    (d / "logs").mkdir(parents=True)
    (d / "profile.json").write_text(
        json.dumps({"profile_id": pid, "personal": {"name": pid.title()}}),
        encoding="utf-8")
    return d


def test_two_profiles_have_independent_queues(tmp_path):
    a = _mk_profile(tmp_path, "nida")
    b = _mk_profile(tmp_path, "adwait")
    atlas = tmp_path / "shared" / "atlas.db"
    ca = database.init_db(a / "applypilot.db", atlas_path=atlas)
    cb = database.init_db(b / "applypilot.db", atlas_path=atlas)
    ca.execute("INSERT INTO jobs (url, title) VALUES ('http://x','Product Designer')")
    ca.commit()
    assert cb.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_two_profiles_share_learned_ats_mappings(tmp_path):
    a = _mk_profile(tmp_path, "nida")
    b = _mk_profile(tmp_path, "adwait")
    atlas = tmp_path / "shared" / "atlas.db"
    ca = database.init_db(a / "applypilot.db", atlas_path=atlas)
    cb = database.init_db(b / "applypilot.db", atlas_path=atlas)
    ca.execute("INSERT INTO boards (ats, token) VALUES ('greenhouse','twilio')")
    ca.commit()
    assert cb.execute("SELECT COUNT(*) FROM boards WHERE token='twilio'").fetchone()[0] == 1


def test_bound_process_writes_only_inside_its_own_profile(tmp_path):
    """The filesystem-level guarantee: a process bound to `nida` leaves no trace
    under `adwait`."""
    _mk_profile(tmp_path, "nida")
    b = _mk_profile(tmp_path, "adwait")
    before = sorted(p.name for p in b.rglob("*"))

    snippet = (
        "import applypilot.__main__ as m, sys; m.bind_profile(sys.argv[1:]);"
        "from applypilot import config; config.ensure_dirs();"
        "from applypilot import database; database.init_db();"
        "print(config.APP_DIR)"
    )
    env = dict(os.environ)
    env["APPLYPILOT_ROOT"] = str(tmp_path)
    env.pop("APPLYPILOT_DIR", None)
    env.pop("APPLYPILOT_PROFILE", None)
    out = subprocess.run([sys.executable, "-c", snippet, "--profile", "nida"],
                         env=env, capture_output=True, text=True, check=True)
    assert str(tmp_path / "profiles" / "nida") in out.stdout

    after = sorted(p.name for p in b.rglob("*"))
    assert before == after, "a process bound to nida modified adwait's directory"
```

- [ ] **Step 2: Run tests**

Run: `PY -m pytest tests/test_profile_isolation.py -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git reset
git add tests/test_profile_isolation.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "test: end-to-end profile isolation and atlas sharing"
```

---

## Task 11: Verification

- [ ] **Step 1: Full suite**

Run: `PY -m pytest -q`
Expected: `963 passed, 1 skipped` plus every test added by this plan, zero failures.

- [ ] **Step 2: Lint**

Run: `PY -m ruff check src/ tests/`
Expected: clean.

- [ ] **Step 3: Legacy passthrough is byte-identical**

Prove an existing single-profile installation is unaffected. With `APPLYPILOT_DIR` set to a
legacy dir containing `profile.json`, confirm `config.APP_DIR` equals it, `init_db` puts all
seven tables in `main`, and nothing is attached.

- [ ] **Step 4: Posture greps**

Run each; all must return no matches:
- `grep -rn "profiles/" src/applypilot/apply/` — the apply path must not build profile paths itself
- `grep -rn "ATTACH" src/applypilot/ --include=*.py | grep -v database.py | grep -v migrate_profiles.py`

- [ ] **Step 5: Dry-run smoke on the real data, no live apply**

After running `applypilot profile migrate --yes` against `E:\applypilot-data`:

```
applypilot profile list
applypilot --profile nida health
```

Expected: the profile lists with Nida's name and the queue counts match what they were
before migration. **Do not run any live apply.**

- [ ] **Step 6: Update docs**

Add a "Profiles" section to `docs/OPERATOR_CHEATSHEET.md` (the commands, the layout, and the
one-at-a-time runner rule) and append a Phase entry to `docs/ralph-iterations.md`. Update
`CLAUDE.md` where it describes the single data dir.

- [ ] **Step 7: Commit**

```bash
git reset
git add docs/ CLAUDE.md
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "docs: profiles — layout, commands, and the global one-batch rule"
```

---

## Explicitly deferred

**To Phase 4B (the operator console).** The spec's "Web UI implications" section is only
partly covered here — Task 8 fixes `cli.py:958` so `ui` passes the resolved paths, but these
belong with the console tasks that build the frontend:

- the profile switcher in the UI
- `webui/static/index.html:115`, which hardcodes "real submissions go out under **Nida's**
  name" and must become profile-aware
- composing `--profile <id>` into the batch subprocess arguments at launch
- the cap controller reading the per-profile apply cap and the global spend cap

Phase 4B Tasks 3, 5, 6, 7 and 8 should be re-read against this plan's outcome before they
are implemented, since they now build on a profile-aware foundation.

**To the operator.** Creating the second profile needs the operator's resume and personal
details. After Task 9 lands, run `applypilot profile add adwait` and populate it from the
resume the operator supplies. Its `searches.yaml` targets software/data-engineering roles
rather than Nida's product-design ones, so it is authored fresh rather than copied.
