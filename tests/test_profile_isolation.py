import time

import pytest

from applypilot import identity_guard
from applypilot.apply import launcher


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


def test_prologue_refuses_mismatched_profile_identity(tmp_path, monkeypatch):
    """A profile.json copied from another profile's directory must come back
    through _safety_prologue as a blocked decision, never as a raised
    exception — the decision shape is the contract every caller (run_job and
    the v2 production fn) branches on."""
    app_dir = tmp_path / "profiles" / "nida"
    app_dir.mkdir(parents=True)
    monkeypatch.setattr(launcher.config, "APP_DIR", app_dir)
    monkeypatch.setattr(launcher.config, "load_profile",
                        lambda: {"profile_id": "adwait", "personal": {"city": "San Jose"}})
    monkeypatch.setattr(launcher, "add_event", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "update_state", lambda *a, **k: None)

    job = {"url": "https://boards.greenhouse.io/x/jobs/1",
           "application_url": "https://boards.greenhouse.io/x/jobs/1"}
    dec = launcher._safety_prologue(
        job, worker_id=0, run_started=time.time(), job_meta={},
        identity_id=None, broker=None, dry_run=False)

    assert dec.blocked is True
    assert dec.status == "needs_review:profile_identity_mismatch"
    assert isinstance(dec.duration_ms, int)
    # Refused before any ledger/INTENT work — no dangling ledger row.
    assert dec.ledger is None
    assert dec.broker_file is None


# ---------------------------------------------------------------------------
# End-to-end profile isolation (Task 10). These encode the load-bearing
# invariant: isolation between profiles is a FILESYSTEM property, not a query
# predicate a future refactor could drop.
# ---------------------------------------------------------------------------

import json
import os
import sqlite3
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
    ca.execute("INSERT INTO boards (ats, token, first_seen) "
               "VALUES ('greenhouse','twilio','2026-09-19T00:00:00Z')")
    ca.commit()
    assert cb.execute(
        "SELECT COUNT(*) FROM boards WHERE token='twilio'").fetchone()[0] == 1


def test_submission_ledger_is_never_shared(tmp_path):
    """The ledger is what proves a person already applied somewhere. If it
    leaked across profiles, one person's history could suppress another's
    legitimate application."""
    a = _mk_profile(tmp_path, "nida")
    b = _mk_profile(tmp_path, "adwait")
    atlas = tmp_path / "shared" / "atlas.db"
    ca = database.init_db(a / "applypilot.db", atlas_path=atlas)
    cb = database.init_db(b / "applypilot.db", atlas_path=atlas)
    ca.execute("INSERT INTO submission_ledger "
               "(identity_id, state, created_at, updated_at) "
               "VALUES ('gh:twilio:123','confirmed','t','t')")
    ca.commit()
    assert cb.execute("SELECT COUNT(*) FROM submission_ledger").fetchone()[0] == 0
    # ...and it really is in nida's own file, not the shared one.
    shared = sqlite3.connect(atlas)
    try:
        names = {r[0] for r in shared.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "submission_ledger" not in names
        assert "jobs" not in names
    finally:
        shared.close()


def test_atlas_writes_land_in_the_shared_file_not_the_profile_db(tmp_path):
    a = _mk_profile(tmp_path, "nida")
    atlas = tmp_path / "shared" / "atlas.db"
    ca = database.init_db(a / "applypilot.db", atlas_path=atlas)
    ca.execute("INSERT INTO boards (ats, token, first_seen) "
               "VALUES ('lever','acme','2026-09-19T00:00:00Z')")
    ca.commit()
    prof = sqlite3.connect(a / "applypilot.db")
    try:
        names = {r[0] for r in prof.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "boards" not in names, "atlas table leaked into the profile DB"
    finally:
        prof.close()
    shared = sqlite3.connect(atlas)
    try:
        assert shared.execute(
            "SELECT COUNT(*) FROM boards WHERE token='acme'").fetchone()[0] == 1
    finally:
        shared.close()


def _run_bound(tmp_path, argv, snippet):
    env = dict(os.environ)
    env["APPLYPILOT_ROOT"] = str(tmp_path)
    env.pop("APPLYPILOT_DIR", None)
    env.pop("APPLYPILOT_PROFILE", None)
    return subprocess.run([sys.executable, "-c", snippet, *argv],
                          env=env, capture_output=True, text=True)


def test_bound_process_writes_only_inside_its_own_profile(tmp_path):
    """The filesystem-level guarantee: a process bound to `nida` leaves no
    trace whatsoever under `adwait`."""
    _mk_profile(tmp_path, "nida")
    b = _mk_profile(tmp_path, "adwait")
    before = sorted(p.name for p in b.rglob("*"))

    snippet = (
        "import sys, applypilot.__main__ as m; m.bind_profile(sys.argv[1:]);"
        "from applypilot import config; config.ensure_dirs();"
        "from applypilot import database; database.init_db();"
        "print(config.APP_DIR)"
    )
    out = _run_bound(tmp_path, ["--profile", "nida"], snippet)
    assert out.returncode == 0, out.stderr
    assert str(tmp_path / "profiles" / "nida") in out.stdout

    after = sorted(p.name for p in b.rglob("*"))
    assert before == after, "a process bound to nida modified adwait's directory"


def test_worker_thread_connection_sees_the_shared_atlas(tmp_path):
    """Apply workers build their OWN thread-local connection via
    get_connection() and never call init_db(). If that path didn't attach the
    atlas, every `boards`/`mapping_cache` query inside a worker would fail with
    'no such table' — but only in real multi-profile operation, never in tests
    that pass an explicit db_path. Exercised here in a bound subprocess so the
    real APP_DIR/ROOT resolution is used."""
    _mk_profile(tmp_path, "nida")
    snippet = (
        "import sys, threading; import applypilot.__main__ as m;"
        "m.bind_profile(sys.argv[1:]);"
        "from applypilot import config, database; config.ensure_dirs();"
        "database.init_db();"
        "out = {};\n"
        "def work():\n"
        "    c = database.get_connection()\n"
        "    c.execute(\"INSERT INTO boards (ats, token, first_seen)"
        " VALUES ('greenhouse','from-worker','t')\")\n"
        "    c.commit()\n"
        "    out['n'] = c.execute(\"SELECT COUNT(*) FROM boards"
        " WHERE token='from-worker'\").fetchone()[0]\n"
        "t = threading.Thread(target=work); t.start(); t.join()\n"
        "print('ROWS', out.get('n'))"
    )
    out = _run_bound(tmp_path, ["--profile", "nida"], snippet)
    assert out.returncode == 0, f"worker-thread atlas attach failed:\n{out.stderr}"
    assert "ROWS 1" in out.stdout, out.stdout
    # The worker's write must have gone to the SHARED atlas file.
    shared = sqlite3.connect(tmp_path / "shared" / "atlas.db")
    try:
        assert shared.execute(
            "SELECT COUNT(*) FROM boards WHERE token='from-worker'").fetchone()[0] == 1
    finally:
        shared.close()
