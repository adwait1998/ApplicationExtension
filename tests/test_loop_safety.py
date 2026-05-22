"""Tests for applypilot.loop.safety carveout enforcement."""
from __future__ import annotations

import pytest

from applypilot.loop import safety


# ---------------------------------------------------------------------------
# is_immutable_path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "src/applypilot/loop/state.py",
    "src/applypilot/loop/signature_extractor.py",
    "src/applypilot/loop/safety.py",
    "src/applypilot/loop/runner.py",
    "src/applypilot/loop/driver_prompt.md",
    "src/applypilot/loop/__init__.py",
    "src/applypilot/loop/subdir/anything.py",
    "tests/test_scoring.py",
    "tests/subdir/whatever.py",
    "profile.json",
    "resume.pdf",
    "resume.txt",
    ".env",
    ".git/config",
    "CLAUDE.md",
    "CONTEXT.md",
    "docs/superpowers/specs/2026-05-22-autonomous-apply-reliability-loop-design.md",
])
def test_immutable_paths_are_blocked(path):
    assert safety.is_immutable_path(path) is True


@pytest.mark.parametrize("path", [
    "src/applypilot/apply/prefill.py",
    "src/applypilot/apply/prompt.py",
    "src/applypilot/apply/adapters/greenhouse.py",
    "src/applypilot/scoring/scorer.py",
    "src/applypilot/discovery/ats_boards.py",
    "src/applypilot/config/sites.yaml",
    "src/applypilot/llm.py",
    "src/applypilot/cli.py",
    "src/applypilot/reporting.py",
])
def test_mutable_paths_are_allowed(path):
    assert safety.is_immutable_path(path) is False


def test_path_separator_agnostic():
    assert safety.is_immutable_path("src\\applypilot\\loop\\state.py") is True
    assert safety.is_immutable_path("src\\applypilot\\apply\\prefill.py") is False


def test_leading_dot_slash_is_stripped():
    assert safety.is_immutable_path("./src/applypilot/loop/state.py") is True
    assert safety.is_immutable_path("./profile.json") is True


# ---------------------------------------------------------------------------
# touches_immutable_functions
# ---------------------------------------------------------------------------

def test_touches_immutable_functions_detects_target(tmp_path):
    src = tmp_path / "launcher.py"
    src.write_text(
        "def other():\n    return 1\n\n"
        "def _verify_submission_success(page):\n    return True  # tampered\n",
        encoding="utf-8",
    )
    assert safety.touches_immutable_functions(src, {"_verify_submission_success"}) is True


def test_touches_immutable_functions_allows_unrelated_changes(tmp_path):
    src = tmp_path / "launcher.py"
    src.write_text(
        "def other():\n    return 1\n\n"
        "def _verify_submission_success(page):\n    return True\n",
        encoding="utf-8",
    )
    # Caller asks about a different function name; the verifier is present
    # but not in the immutable set provided here.
    assert safety.touches_immutable_functions(src, {"some_other_function"}) is False


def test_touches_immutable_functions_handles_missing_file(tmp_path):
    src = tmp_path / "does_not_exist.py"
    assert safety.touches_immutable_functions(src, {"anything"}) is False


def test_touches_immutable_functions_treats_syntax_errors_as_suspicious(tmp_path):
    src = tmp_path / "bad.py"
    src.write_text("def broken(\n  # syntax error\n", encoding="utf-8")
    # Conservative: cannot parse, return True.
    assert safety.touches_immutable_functions(src, {"any"}) is True


def test_touches_immutable_functions_uses_default_set(tmp_path):
    src = tmp_path / "launcher.py"
    src.write_text(
        "async def _classify_failure(err):\n    return 'transient'\n",
        encoding="utf-8",
    )
    # No explicit immutable set → uses safety.IMMUTABLE_FUNCTIONS default.
    assert safety.touches_immutable_functions(src) is True


# ---------------------------------------------------------------------------
# brick_detected
# ---------------------------------------------------------------------------

def test_brick_detected_fast_nonzero_exit():
    assert safety.brick_detected(returncode=1, elapsed_s=2.5) is True


def test_brick_detected_slow_nonzero_exit_is_not_brick():
    assert safety.brick_detected(returncode=1, elapsed_s=120.0) is False


def test_brick_detected_zero_exit_is_never_brick():
    assert safety.brick_detected(returncode=0, elapsed_s=2.5) is False
    assert safety.brick_detected(returncode=0, elapsed_s=600.0) is False


def test_brick_detected_exactly_at_threshold():
    assert safety.brick_detected(returncode=1, elapsed_s=10.0) is True
    assert safety.brick_detected(returncode=1, elapsed_s=10.001) is False
