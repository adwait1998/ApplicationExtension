"""Tests for applypilot.loop.state."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from applypilot.loop import state as state_mod


# ---------------------------------------------------------------------------
# default_state
# ---------------------------------------------------------------------------

def test_default_state_has_required_fields():
    s = state_mod.default_state()
    assert s["schema_version"] == 1
    assert s["streak"] == 0
    assert s["iteration"] == 0
    assert s["attempts"] == []
    assert s["signature_counts"] == {}
    assert s["cooldown_remaining"] == 0
    assert s["last_patch"] is None
    assert s["patch_budgets"] == {}
    assert s["class_blocklist"] == []
    assert s["rollback_log"] == []
    assert s["last_processed_review_ts"] is None
    assert isinstance(s["session_id"], str) and len(s["session_id"]) == 36
    assert isinstance(s["pid"], int) and s["pid"] > 0
    assert isinstance(s["started_at"], str) and s["started_at"].endswith("Z")


# ---------------------------------------------------------------------------
# save_atomic + load_or_init
# ---------------------------------------------------------------------------

def test_save_atomic_and_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "state_path", lambda: tmp_path / "loop-state.json")
    s = state_mod.default_state()
    s["streak"] = 3
    state_mod.save_atomic(s)
    loaded = state_mod.load_or_init()
    assert loaded["streak"] == 3
    assert loaded["session_id"] == s["session_id"]


def test_load_or_init_creates_default_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "state_path", lambda: tmp_path / "loop-state.json")
    loaded = state_mod.load_or_init()
    assert loaded["streak"] == 0
    assert loaded["schema_version"] == 1
    assert (tmp_path / "loop-state.json").exists()


def test_save_atomic_overwrite_keeps_latest(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "state_path", lambda: tmp_path / "loop-state.json")
    s = state_mod.default_state()
    state_mod.save_atomic(s)
    s2 = dict(s)
    s2["streak"] = 9
    state_mod.save_atomic(s2)
    loaded = state_mod.load_or_init()
    assert loaded["streak"] == 9


def test_save_atomic_leaves_no_temp_files_on_success(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "state_path", lambda: tmp_path / "loop-state.json")
    state_mod.save_atomic(state_mod.default_state())
    leftovers = list(tmp_path.glob(".loop-state-*.json.tmp"))
    assert leftovers == []


# ---------------------------------------------------------------------------
# session lock
# ---------------------------------------------------------------------------

def test_is_pid_alive_current_process():
    assert state_mod.is_pid_alive(os.getpid()) is True


def test_is_pid_alive_zero_and_negative():
    assert state_mod.is_pid_alive(0) is False
    assert state_mod.is_pid_alive(-1) is False


def test_is_pid_alive_likely_dead_pid():
    # Very high PID extremely unlikely to be in use.
    assert state_mod.is_pid_alive(2_147_483_640) is False


def test_verify_lock_passes_for_own_session(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "state_path", lambda: tmp_path / "loop-state.json")
    s = state_mod.load_or_init()
    assert state_mod.verify_lock(s) == "owned"


def test_verify_lock_detects_foreign_living_session(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "state_path", lambda: tmp_path / "loop-state.json")
    foreign_pid = 999_999  # not our pid
    monkeypatch.setattr(state_mod, "is_pid_alive",
                        lambda pid: pid == foreign_pid)
    s = state_mod.default_state()
    s["session_id"] = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    s["pid"] = foreign_pid
    state_mod.save_atomic(s)
    assert state_mod.verify_lock(s) == "conflict"


def test_verify_lock_takes_over_dead_foreign_session(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "state_path", lambda: tmp_path / "loop-state.json")
    s = state_mod.default_state()
    s["session_id"] = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    s["pid"] = 2_147_483_640  # dead pid
    state_mod.save_atomic(s)
    assert state_mod.verify_lock(s) == "stale"


# ---------------------------------------------------------------------------
# derived computations
# ---------------------------------------------------------------------------

def _make_attempt(status: str) -> dict:
    return {
        "url": f"https://example.com/{status}",
        "status": status,
        "failure_class": None if status == "applied" else "transient_timeout",
        "signature": None if status == "applied" else "sig:x",
        "ts": "2026-05-22T00:00:00.000000Z",
    }


def test_rolling_pass_rate_empty():
    assert state_mod.rolling_pass_rate([]) == 0.0


def test_rolling_pass_rate_all_applied():
    attempts = [_make_attempt("applied")] * 5
    assert state_mod.rolling_pass_rate(attempts) == 1.0


def test_rolling_pass_rate_mixed():
    attempts = [
        _make_attempt("applied"),
        _make_attempt("needs_review:timeout"),
        _make_attempt("applied"),
        _make_attempt("applied"),
    ]
    assert state_mod.rolling_pass_rate(attempts) == 0.75


def test_roll_attempts_trims_to_window():
    attempts = [_make_attempt("applied") for _ in range(15)]
    trimmed = state_mod.roll_attempts(attempts)
    assert len(trimmed) == state_mod.ATTEMPT_WINDOW
    assert trimmed[0] is attempts[5]
    assert trimmed[-1] is attempts[-1]


def test_roll_attempts_returns_copy_when_under_window():
    attempts = [_make_attempt("applied") for _ in range(3)]
    trimmed = state_mod.roll_attempts(attempts)
    assert trimmed == attempts
    assert trimmed is not attempts  # defensive copy


def test_streak_from_attempts_counts_trailing_applied():
    attempts = [
        _make_attempt("applied"),
        _make_attempt("failed"),
        _make_attempt("applied"),
        _make_attempt("applied"),
        _make_attempt("applied"),
    ]
    assert state_mod.streak_from_attempts(attempts) == 3


def test_streak_from_attempts_zero_when_last_failed():
    attempts = [_make_attempt("applied"), _make_attempt("needs_review:timeout")]
    assert state_mod.streak_from_attempts(attempts) == 0


def test_streak_from_attempts_empty():
    assert state_mod.streak_from_attempts([]) == 0
