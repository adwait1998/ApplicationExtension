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


VISA_OPTS = ["Yes, I will require H-1B sponsorship", "Yes, I will require TN visa support",
             "Yes, I am on F-1 OPT and will need sponsorship", "Yes, I will require L-1 transfer",
             "No, I will not require sponsorship"]
SQ = "Will you now or in the future require sponsorship for a visa to remain in your current location?"


def test_the_option_naming_the_applicants_own_visa_is_chosen():
    assert choose_option(SQ, VISA_OPTS, NIDA)[0] == "Yes, I will require H-1B sponsorship"


def test_an_option_naming_a_different_visa_is_never_chosen():
    other = {"work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": True,
                                    "work_permit_type": "O-1"}}
    assert choose_option(SQ, VISA_OPTS[1:4] + VISA_OPTS[4:], other)[0] is None


def test_no_sponsorship_needed_picks_the_no_option():
    assert choose_option(SQ, VISA_OPTS, CITIZEN)[0] == "No, I will not require sponsorship"


def test_generic_yes_options_that_differ_only_in_timing_stay_with_the_applicant():
    assert choose_option(SQ, ["Yes, now", "Yes, in the future", "No"], NIDA)[0] is None


GREEN_CARD = {"work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": False,
                                     "work_permit_type": "Green Card"}}
WQ = "Are you legally authorized to work in the United States?"


@pytest.mark.parametrize("opts", [
    ["Yes, I am a U.S. citizen or permanent resident", "No, I will require sponsorship"],
    ["Yes, I am a U.S. citizen", "No"],
])
def test_visa_holder_never_gets_a_citizenship_option_even_when_it_is_the_only_yes(opts):
    assert choose_option(WQ, opts, NIDA)[0] is None


def test_a_leading_no_does_not_hide_a_citizenship_claim():
    # "No, I am a U.S. citizen" CLAIMS citizenship; a green-card holder is not a citizen.
    opts = ["Yes, I will require sponsorship", "No, I am a U.S. citizen"]
    assert choose_option("Will you now or in the future require sponsorship?", opts, GREEN_CARD)[0] is None


def test_permanent_resident_vs_citizen_are_different_claims():
    either = ["Yes, I am a U.S. citizen or permanent resident", "Yes, I need sponsorship", "No"]
    citizen_only = ["Yes, I am a U.S. citizen", "Yes, I need sponsorship", "No"]
    assert choose_option(WQ, either, GREEN_CARD)[0] == either[0]
    assert choose_option(WQ, citizen_only, GREEN_CARD)[0] is None
    assert choose_option(WQ, citizen_only, CITIZEN)[0] == citizen_only[0]


def test_a_negated_status_is_not_a_claim():
    opts = ["Yes, but I am not a U.S. citizen and will need sponsorship", "No"]
    assert choose_option(WQ, opts, NIDA)[0] == opts[0]
