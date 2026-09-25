"""Common screening questions: answered only from profile.screening.*,
never guessed, never handed to the answer bank or a draft."""
import pytest

from applypilot.extension import resolve, screening
from applypilot.extension.schema import FieldDescriptor, FillResult, SkipResult

SET = {"screening": {"criminal_conviction": "No", "background_check_consent": "Yes",
                     "drug_test_consent": "Yes", "non_compete": "No",
                     "willing_to_travel": "Yes", "how_heard": "LinkedIn"},
       "work_authorization": {"legally_authorized_to_work": False, "require_sponsorship": True}}
UNSET = {"work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": False}}


def _resolve(label, profile):
    return resolve.resolve_field(FieldDescriptor(id="f", label=label), profile)


@pytest.mark.parametrize("q", [
    "Have you ever been convicted of a felony?",
    "Have you ever been convicted of a crime? (Yes/No)",
    "* Have you been convicted of a felony or misdemeanor in the last 7 years?",
    "Criminal History: Have you ever been convicted of a crime?",
    "Are you a registered sex offender?",
])
def test_never_convicted_answers_plain_conviction_questions_no(q):
    r = _resolve(q, SET)
    assert isinstance(r, FillResult) and r.value == "No" and r.source == "screening"


@pytest.mark.parametrize("q", [
    "Have you ever been arrested?",
    "Do you have any pending criminal charges?",
    "Have you ever been convicted of a crime, including traffic violations?",
    "I certify that I have never been convicted of a felony.",
    "If yes, please explain the conviction",
    "Have you never been convicted of a felony?",
])
def test_conviction_setting_does_not_answer_what_it_does_not_entail(q):
    r = _resolve(q, SET)
    assert isinstance(r, SkipResult) and r.source == "screening", (q, r)


def test_a_yes_criminal_setting_is_never_auto_answered():
    prof = {"screening": {"criminal_conviction": "Yes"}}
    r = _resolve("Have you ever been convicted of a felony?", prof)
    assert isinstance(r, SkipResult)


@pytest.mark.parametrize("q", [
    "Have you ever been convicted of a felony?",
    "Are you willing to undergo a background check?",
    "Are you willing to take a drug test?",
    "Are you bound by a non-compete agreement?",
    "Are you willing to travel up to 25%?",
])
def test_unset_attestations_stay_with_the_human_and_never_reach_bank_or_draft(q, monkeypatch):
    # If anything below screening were consulted, this would blow up.
    def boom(*a, **k):
        raise AssertionError("lower tier consulted for a screening attestation")
    monkeypatch.setattr(resolve.answers, "match", boom)
    r = _resolve(q, UNSET)
    assert isinstance(r, SkipResult) and r.source == "screening"
    assert "Settings" in r.reason


def test_background_check_authorization_is_not_answered_as_work_authorization():
    # "authorize" trips the work-auth canary; this profile is NOT authorized
    # to work, so the canary would have answered "No".
    r = _resolve("Do you authorize us to conduct a background check?", SET)
    assert isinstance(r, FillResult) and r.value == "Yes" and r.source == "screening"


def test_work_authorization_questions_still_go_to_the_canary():
    r = _resolve("Are you legally authorized to work in the US?", SET)
    assert r.source == "canary" and r.value == "No"


def test_consent_vs_outcome():
    assert isinstance(_resolve("Do you consent to a background check, including criminal history?", SET),
                      FillResult)
    assert isinstance(_resolve("Can you pass a background check?", SET), SkipResult)


def test_travel_over_half_the_time_is_left_for_the_human():
    assert _resolve("Are you willing to travel up to 25%?", SET).value == "Yes"
    assert isinstance(_resolve("Are you willing to travel 75% of the time?", SET), SkipResult)


def test_how_heard_uses_setting_else_falls_through():
    assert _resolve("How did you hear about us?", SET).value == "LinkedIn"
    fd = FieldDescriptor(id="f", label="How did you hear about this job?")
    assert screening.match(fd, UNSET) is None


@pytest.mark.parametrize("q", ["Why do you want to work here?", "Years of experience with Python",
                                "Describe your background", "Travel Industry Experience"])
def test_unrelated_questions_are_not_screening(q):
    assert screening.family_of(q) is None
