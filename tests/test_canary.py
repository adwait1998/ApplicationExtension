from applypilot.apply.canary import is_canary, resolve_canary

PROFILE = {
    "work_authorization": {"legally_authorized_to_work": False, "require_sponsorship": True,
                           "work_permit_type": "F-1 OPT"},
    "compensation": {"salary_expectation": "150000", "salary_currency": "USD"},
    "eeo_voluntary": {"gender": "Female", "race_ethnicity": "Decline to self-identify",
                      "veteran_status": "I am not a veteran", "disability_status": "No"},
    "personal": {"address": "1 Main St", "city": "San Jose", "province_state": "CA",
                 "country": "USA", "postal_code": "95110", "password": "SECRET"},
    "availability": {"earliest_start_date": "2 weeks"},
}


def test_sponsorship_is_canary():
    assert is_canary("Will you now or in the future require visa sponsorship?")
    assert is_canary("Are you legally authorized to work in the US?")
    assert is_canary("What is your expected salary?")
    assert is_canary("What is your gender?")
    assert is_canary("What is your date of birth?")


def test_non_canary():
    assert not is_canary("Describe a product you shipped that you're proud of")
    assert not is_canary("Why do you want to work here?")


def test_resolve_sponsorship_polarity_positive():
    # require_sponsorship=True -> "require sponsorship?" answers Yes
    assert resolve_canary("Will you require sponsorship?", PROFILE) in ("Yes", "yes")


def test_resolve_sponsorship_polarity_negated():
    # "work WITHOUT sponsorship?" with require_sponsorship=True -> No
    assert resolve_canary("Can you work without sponsorship?", PROFILE) in ("No", "no")


def test_resolve_workauth():
    # legally_authorized_to_work=False -> "are you authorized?" -> No
    assert resolve_canary("Are you legally authorized to work in the United States?", PROFILE) in ("No", "no")


def test_resolve_salary():
    assert "150000" in (resolve_canary("What is your expected salary?", PROFILE) or "")


def test_resolve_eeo_fields_distinct():
    assert resolve_canary("What is your gender?", PROFILE) == "Female"
    assert resolve_canary("What is your veteran status?", PROFILE) == "I am not a veteran"
    assert resolve_canary("Do you have a disability?", PROFILE) == "No"


def test_resolve_dob_is_none():
    # profile has NO date-of-birth field -> never guess
    assert resolve_canary("What is your date of birth?", PROFILE) is None


def test_resolve_never_leaks_password():
    for q in ("What is your address?", "What is your password?"):
        ans = resolve_canary(q, PROFILE) or ""
        assert "SECRET" not in ans


def test_ambiguous_polarity_returns_none():
    # if we can't confidently determine polarity, refuse (park, don't guess)
    assert resolve_canary("Sponsorship?", PROFILE) is None


def test_citizenship_refused():
    # profile has no citizenship field -> never answer citizenship questions
    assert resolve_canary("Are you a US citizen?", PROFILE) is None


def test_string_booleans_normalized():
    p = {"work_authorization": {"legally_authorized_to_work": "Yes", "require_sponsorship": "No"}}
    assert resolve_canary("Will you require sponsorship?", p) in ("No", "no")       # string "No" != truthy
    assert resolve_canary("Are you legally authorized to work in the US?", p) in ("Yes", "yes")


def test_unparseable_flag_stays_unresolved():
    p = {"work_authorization": {"require_sponsorship": "maybe"}}
    assert resolve_canary("Will you require sponsorship?", p) is None
    assert resolve_canary("Do you require sponsorship?", {}) is None  # missing flag -> park, never guess


# ---------------------------------------------------------------------------
# "address" is only a postal-address canary when it really means one. The bare
# alternative used to match "Email Address", so an email input resolved as the
# ADDRESS canary and was filled with a street address — caught end-to-end by
# the Copilot extension on a realistic ATS form.
# ---------------------------------------------------------------------------

import pytest

from applypilot.apply import canary as _canary


@pytest.mark.parametrize("label", [
    "Street Address", "Home Address", "Mailing Address",
    "Address Line 1", "Address", "Zip", "ZIP Code", "Postal Code",
])
def test_real_postal_address_labels_are_still_canaries(label):
    assert _canary.is_canary(label) is True


@pytest.mark.parametrize("label", [
    "Email Address", "E-mail Address", "Web address", "URL address", "IP address",
])
def test_address_shaped_but_non_postal_labels_are_not_canaries(label):
    assert _canary.is_canary(label) is False


def test_email_address_does_not_resolve_to_the_postal_address():
    profile = {
        "personal": {
            "email": "a@b.test", "address": "1 Test Street",
            "city": "Seattle", "province_state": "WA",
            "postal_code": "98101", "country": "United States",
        }
    }
    assert _canary.resolve_canary("Email Address", profile) is None
    assert "Test Street" in (_canary.resolve_canary("Street Address", profile) or "")
