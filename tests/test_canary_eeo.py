"""EEO canary: decline default, the sex-offender false match, and the
voluntary self-ID families that used to fall through to the answer bank."""
import pytest

from applypilot.apply.canary import _EEO_DECLINE, is_canary, resolve_canary

EMPTY = {}
SET = {"eeo_voluntary": {"gender": "Female", "race_ethnicity": "Asian",
                         "hispanic_latino": "No", "veteran_status": "I am not a protected veteran",
                         "disability_status": "No, I do not have a disability",
                         "sexual_orientation": "Heterosexual", "transgender": "No"},
       "personal": {"pronouns": "she/her"}}


@pytest.mark.parametrize("q", [
    "Are you a registered sex offender?",
    "Have you been convicted of a sex offense?",
    "Have you ever been convicted of a sex offence?",
    "Have you been charged with sex crimes?",
])
def test_sex_as_a_crime_is_never_answered_with_gender(q):
    # These matched the gender marker and were answered "Male"/"Female".
    assert resolve_canary(q, SET) != "Female"
    assert resolve_canary(q, SET) is None


@pytest.mark.parametrize("q", ["Sex", "What is your sex?", "Gender", "Gender identity"])
def test_sex_and_gender_labels_use_profile_gender(q):
    assert resolve_canary(q, SET) == "Female"


@pytest.mark.parametrize("q,key", [
    ("Gender", "gender"),
    ("Are you Hispanic or Latino?", "hispanic_latino"),
    ("Race", "race_ethnicity"),
    ("Veteran Status", "veteran_status"),
    ("Disability Status", "disability_status"),
    ("Sexual orientation", "sexual_orientation"),
    ("Do you identify as transgender?", "transgender"),
])
def test_eeo_uses_profile_value_else_declines(q, key):
    assert resolve_canary(q, SET) == SET["eeo_voluntary"][key]
    # Absent -> the lawful decline, never None ("answer this yourself").
    assert resolve_canary(q, EMPTY) == _EEO_DECLINE


def test_lgbtq_self_id_is_eeo_not_a_draftable_question():
    q = "I identify as a member of the LGBTQ+ community"
    assert is_canary(q)
    assert resolve_canary(q, EMPTY) == _EEO_DECLINE


def test_disability_accommodation_is_not_self_identification():
    q = "Do you require a reasonable accommodation due to a disability?"
    assert is_canary(q)  # still never banked or drafted
    assert resolve_canary(q, SET) is None
    assert resolve_canary(q, EMPTY) is None


def test_pronouns_come_from_profile_or_stay_unresolved():
    assert resolve_canary("Pronouns", SET) == "she/her"
    # Never the decline text, never a guess.
    assert resolve_canary("What are your preferred pronouns?", EMPTY) is None
    assert is_canary("Pronouns")


@pytest.mark.parametrize("q", [
    "Describe a race condition you debugged",
    "Middlesex County",
    "Do you have reliable transportation?",
    "Tell us about a time you embraced change",
])
def test_non_eeo_lookalikes_are_not_canary(q):
    assert not is_canary(q)


NIDA_WA = {"work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": True}}
CITIZEN_WA = {"work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": False}}


@pytest.mark.parametrize("q", [
    "Can you work in the US without restrictions?",
    "Do you have unrestricted work rights in the US?",
    "Are you able to work without an employer visa?",
])
def test_unrestricted_work_is_not_the_same_as_authorized(q):
    # A visa holder IS authorized but NOT unrestricted.
    assert resolve_canary(q, NIDA_WA) == "No"
    assert resolve_canary(q, CITIZEN_WA) == "Yes"


def test_right_to_work_is_authorization():
    assert resolve_canary("Do you have the right to work in the United States?", NIDA_WA) == "Yes"


def test_without_restrictions_outside_work_is_not_a_canary():
    assert not is_canary("Describe a project delivered without restrictions on scope")
