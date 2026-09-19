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
