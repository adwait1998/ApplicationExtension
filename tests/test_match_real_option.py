"""User-reported bug fix: combobox must select from REAL options, never
blind-type a value that isn't in the dropdown (which spun ~3.5s/value then
fell back to the LLM). _match_real_option is pure — tested at $0.
"""
from __future__ import annotations

import pytest

from applypilot.apply.prefill import _match_real_option


def test_exact_match():
    assert _match_real_option(("Yes",), ["Yes", "No"]) == "Yes"


def test_preferred_is_substring_of_company_phrased_option():
    # The exact bug: we want "Yes"; the employer's option is verbose.
    opts = ["Yes, I am authorized to work in the US",
            "No, I require sponsorship"]
    assert _match_real_option(("Yes",), opts) == "Yes, I am authorized to work in the US"


def test_decline_variants_token_overlap():
    opts = ["I prefer not to answer", "Male", "Female", "Non-binary"]
    # our canonical "Decline to self-identify" / "prefer not to answer"
    got = _match_real_option(
        ("Decline to self-identify", "prefer not to answer"), opts
    )
    assert got == "I prefer not to answer"


def test_option_is_substring_of_preferred():
    opts = ["No"]
    assert _match_real_option(("No, I have not",), opts) == "No"


def test_no_match_returns_none_fail_fast():
    # Nothing reasonably matches → None → caller fails fast (no spin).
    opts = ["Banana", "Helicopter", "Tuesday"]
    assert _match_real_option(("Yes", "Authorized"), opts) is None


def test_empty_options_returns_none():
    assert _match_real_option(("Yes",), []) is None
    assert _match_real_option(("Yes",), [""]) is None


def test_priority_exact_beats_substring():
    opts = ["Yes, with conditions", "Yes"]
    # exact "Yes" must win over the verbose substring match
    assert _match_real_option(("Yes",), opts) == "Yes"


def test_first_preferred_wins():
    opts = ["Decline to self-identify", "Male"]
    assert _match_real_option(("Male", "Decline to self-identify"), opts) == "Male"
