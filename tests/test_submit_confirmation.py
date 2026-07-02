"""F3 regression: adapter submit must be CONFIRMED, not click-assumed.

Live-data evidence (review.jsonl 2026-05-20..22): tier=greenhouse_adapter_submit
rows finishing in 13-73s with verification_confidence 0.1-0.25 — the click
"succeeded" while the form stayed on screen (validation errors / wrong page),
the LLM was skipped, and the job died in needs_review with no recovery path.

These are $0 tests against the pure decision core `_post_submit_verdict`.
"""

from __future__ import annotations

import pytest

from applypilot.apply.adapters.greenhouse import _post_submit_verdict


def test_button_gone_means_submitted():
    assert _post_submit_verdict(
        button_gone=True, errors_visible=False, button_enabled=False, deadline_hit=False
    ) == (True, None)


def test_button_gone_wins_even_at_deadline():
    assert _post_submit_verdict(
        button_gone=True, errors_visible=False, button_enabled=False, deadline_hit=True
    ) == (True, None)


def test_validation_errors_mean_rejected():
    verdict, error = _post_submit_verdict(
        button_gone=False, errors_visible=True, button_enabled=True, deadline_hit=False
    )
    assert verdict is False
    assert "validation errors" in error


def test_form_still_interactive_at_deadline_is_not_submitted():
    # The discord/DeepMind case: click landed, nothing happened, form fully
    # interactive. Must NOT claim submitted (lets Tier-2/LLM recover).
    verdict, error = _post_submit_verdict(
        button_gone=False, errors_visible=False, button_enabled=True, deadline_hit=True
    )
    assert verdict is False
    assert "submit_unconfirmed" in error


def test_disabled_button_at_deadline_treated_as_in_flight_submit():
    # Greenhouse disables the button while the POST is in flight; downstream
    # verifier still gets the final say.
    assert _post_submit_verdict(
        button_gone=False, errors_visible=False, button_enabled=False, deadline_hit=True
    ) == (True, None)


def test_keep_polling_before_deadline():
    assert _post_submit_verdict(
        button_gone=False, errors_visible=False, button_enabled=True, deadline_hit=False
    ) == (None, None)


@pytest.mark.parametrize("enabled", [True, False])
def test_errors_beat_enabled_state(enabled):
    verdict, _ = _post_submit_verdict(
        button_gone=False, errors_visible=True, button_enabled=enabled, deadline_hit=True
    )
    assert verdict is False
