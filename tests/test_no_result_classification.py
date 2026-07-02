"""Iter-5 regression: operator interruption is not an agent failure.

Transcripts for the 2026-05-21 `no_result_line` rows (Adapt, Fanatics) end
with cmd.exe's "Terminate batch job (Y/N)?" — the run was Ctrl+C'd, the
subprocess died before emitting RESULT, and the row was logged as a
removable agent failure. `_classify_no_result` now reports "interrupted"
when the stop event was set.
"""

from __future__ import annotations

from applypilot.apply.launcher import _classify_no_result


def test_stop_requested_classifies_as_interrupted():
    fc, status = _classify_no_result(True, "transient_no_result_line")
    assert fc == "transient_interrupted"
    assert status == "needs_review:interrupted"


def test_stop_requested_overrides_missing_structured_result_too():
    fc, status = _classify_no_result(True, "verification_missing_structured_result")
    assert fc == "transient_interrupted"
    assert status == "needs_review:interrupted"


def test_genuine_no_result_keeps_default_class():
    fc, status = _classify_no_result(False, "transient_no_result_line")
    assert fc == "transient_no_result_line"
    assert status == "needs_review:no_result_line"


def test_genuine_missing_structured_result_keeps_class():
    fc, status = _classify_no_result(False, "verification_missing_structured_result")
    assert fc == "verification_missing_structured_result"
    assert status == "needs_review:no_result_line"
