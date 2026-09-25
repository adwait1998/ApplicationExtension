"""Canary answers against a question's real options (reviewer round 2):
bundled facts, decline options that assert status, salary into a number box."""
import pytest

from applypilot.apply.canary import choose_option
from applypilot.extension import resolve
from applypilot.extension.schema import FieldDescriptor as F, FillResult, SkipResult

NIDA = {"work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": True,
                               "work_permit_type": "H-1B"},
        "compensation": {"salary_expectation": "120000", "salary_currency": "USD"}}
CITIZEN = {"work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": False,
                                  "work_permit_type": "US Citizen"}}
UNKNOWN = {"work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": False}}
BUNDLED = ["Yes, I am a U.S. citizen or permanent resident",
           "Yes, I am authorized but will require sponsorship now or in the future", "No"]
Q = "Are you legally authorized to work in the United States?"


def test_visa_holder_never_gets_the_citizen_option():
    r = resolve.resolve_field(F(id="w", label=Q, type="radio", options=BUNDLED), NIDA)
    assert isinstance(r, FillResult) and "require sponsorship" in r.value
    assert "citizen" not in r.value.lower()


def test_citizen_gets_the_citizen_option():
    assert choose_option(Q, BUNDLED, CITIZEN)[0] == BUNDLED[0]


def test_unknown_citizenship_is_left_for_the_applicant():
    r = resolve.resolve_field(F(id="w", label=Q, type="radio", options=BUNDLED), UNKNOWN)
    assert isinstance(r, SkipResult) and r.source == "canary"


@pytest.mark.parametrize("q,opts,prof,want", [
    (Q, ["Yes", "No"], NIDA, "Yes"),
    ("Will you now or in the future require sponsorship for employment visa status?", ["Yes", "No"], NIDA, "Yes"),
    ("Will you now or in the future require sponsorship for employment visa status?", ["Yes", "No"], CITIZEN, "No"),
    ("Will you now or in the future require sponsorship?",
     ["Yes, I will require sponsorship", "No, I do not require sponsorship"], NIDA, "Yes, I will require sponsorship"),
    ("Will you now or in the future require sponsorship?",
     ["Yes, I will require sponsorship", "No, I do not require sponsorship"], CITIZEN,
     "No, I do not require sponsorship"),
])
def test_plain_and_bundled_sponsorship(q, opts, prof, want):
    assert choose_option(q, opts, prof)[0] == want


@pytest.mark.parametrize("label,opts,want", [
    ("Veteran Status", ["I am not a protected veteran",
                        "I identify as one or more of the classifications of protected veteran",
                        "I am a protected veteran, but I choose not to self-identify the classifications to which I belong",
                        "I don't wish to answer"], "I don't wish to answer"),
    ("Do you identify as part of the LGBTQ+ community?", ["Yes, I self-identify as LGBTQ+", "No",
                                                         "I prefer not to say"], "I prefer not to say"),
])
def test_decline_never_picks_an_option_that_asserts_status(label, opts, want):
    r = resolve.resolve_field(F(id="e", label=label, type="radio", options=opts), {})
    assert isinstance(r, FillResult) and r.value == want


def test_two_plain_decline_options_is_ambiguous():
    r = resolve.resolve_field(F(id="g", label="Gender", tag="select",
                                options=["Male", "Female", "Decline to self-identify", "I don't wish to answer"]), {})
    assert isinstance(r, SkipResult)


def test_salary_into_a_number_input_is_the_number():
    r = resolve.resolve_field(F(id="s", label="Desired salary", type="number"), NIDA)
    assert isinstance(r, FillResult) and r.value == "120000"
    r2 = resolve.resolve_field(F(id="s", label="Desired salary", type="text"), NIDA)
    assert r2.value == "120000 USD"


@pytest.mark.parametrize("label", ["Veteran", "Person with disability", "Hispanic or Latine"])
def test_one_checkbox_of_an_eeo_group_is_left_unticked_not_filled(label):
    r = resolve.resolve_field(F(id="c", label=label, type="checkbox"), {})
    assert isinstance(r, SkipResult) and "single checkbox" in r.reason


def test_a_yes_no_canary_still_ticks_a_single_checkbox():
    r = resolve.resolve_field(F(id="c", label="I am legally authorized to work in the United States", type="checkbox"),
                              NIDA)
    assert isinstance(r, FillResult) and r.value == "Yes"
