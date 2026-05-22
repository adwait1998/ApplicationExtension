"""Tests for applypilot.loop.runner.apply_one (subprocess wrapper)."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from applypilot.loop import runner


def _fake_completed(returncode: int, stdout: str = "ok", stderr: str = ""):
    """Build a fake subprocess.CompletedProcess result."""
    completed = MagicMock()
    completed.returncode = returncode
    completed.stdout = stdout
    completed.stderr = stderr
    return completed


def test_apply_one_sets_visual_trace_env_when_missing(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_VISUAL_TRACE", raising=False)
    completed = _fake_completed(0, "ok")
    with patch("applypilot.loop.runner.subprocess.run", return_value=completed), \
         patch("applypilot.loop.runner._monotonic_elapsed", return_value=200.0):
        result = runner.apply_one()
    assert os.environ.get("APPLYPILOT_VISUAL_TRACE") == "1"
    assert result["status"] == "completed"
    assert result["brick"] is False
    assert result["returncode"] == 0


def test_apply_one_preserves_visual_trace_if_already_set(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_VISUAL_TRACE", "custom")
    completed = _fake_completed(0, "ok")
    with patch("applypilot.loop.runner.subprocess.run", return_value=completed), \
         patch("applypilot.loop.runner._monotonic_elapsed", return_value=120.0):
        runner.apply_one()
    # setdefault should not overwrite a pre-existing value.
    assert os.environ["APPLYPILOT_VISUAL_TRACE"] == "custom"


def test_apply_one_flags_brick_on_fast_fail(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_VISUAL_TRACE", "1")
    completed = _fake_completed(1, "", "ImportError: cannot import name")
    with patch("applypilot.loop.runner.subprocess.run", return_value=completed), \
         patch("applypilot.loop.runner._monotonic_elapsed", return_value=3.0):
        result = runner.apply_one()
    assert result["brick"] is True
    assert result["status"] == "brick_detected"
    assert result["returncode"] == 1
    assert "ImportError" in result["stderr"]


def test_apply_one_slow_fail_is_not_brick(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_VISUAL_TRACE", "1")
    completed = _fake_completed(2, "", "real failure mid-apply")
    with patch("applypilot.loop.runner.subprocess.run", return_value=completed), \
         patch("applypilot.loop.runner._monotonic_elapsed", return_value=240.0):
        result = runner.apply_one()
    assert result["brick"] is False
    assert result["status"] == "completed"
    assert result["returncode"] == 2


def test_apply_one_extra_args_are_appended(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_VISUAL_TRACE", "1")
    completed = _fake_completed(0, "ok")
    with patch("applypilot.loop.runner.subprocess.run", return_value=completed) as mock_run, \
         patch("applypilot.loop.runner._monotonic_elapsed", return_value=200.0):
        runner.apply_one(extra_args=("--url", "https://example.com/job/1"))
    args_called = mock_run.call_args[0][0]
    assert "--url" in args_called
    assert "https://example.com/job/1" in args_called
    # Default args still present.
    assert "--workers" in args_called
    assert "--limit" in args_called


def test_apply_one_truncates_long_output(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_VISUAL_TRACE", "1")
    long_text = "x" * 10_000
    completed = _fake_completed(0, long_text, long_text)
    with patch("applypilot.loop.runner.subprocess.run", return_value=completed), \
         patch("applypilot.loop.runner._monotonic_elapsed", return_value=200.0):
        result = runner.apply_one()
    assert len(result["stdout"]) == 4000
    assert len(result["stderr"]) == 4000
