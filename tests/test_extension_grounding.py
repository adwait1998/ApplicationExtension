"""The draft tier's grounding was only a prompt instruction until these tests:
the model was *told* to stay inside resume_facts and nothing checked that it
had. A fabricated employer under a real person's name is the worst thing this
feature can produce, so the check is narrow (first-person employment claims
only) and deliberately does NOT police company names in general — mentioning
the employer you are applying to is normal and desirable.
"""
import pytest

from applypilot.extension import grounding

PROFILE = {
    "resume_facts": {
        "preserved_companies": ["Acme Corp", "Globex"],
        "preserved_school": "Arizona State University",
        "preserved_projects": ["Atlas Design System"],
    },
    "experience": {"current_company": "Acme Corp"},
    "work_history": [{"company": "Initech Systems"}],
    "education": [{"school": "Mesa Community College"}],
}


@pytest.mark.parametrize("text", [
    "I worked at Acme Corp for three years.",
    "During my time at Globex I led the design system.",
    "My tenure at Globex taught me a lot.",
    "I studied at Arizona State University.",
    "I worked at Initech Systems on infrastructure.",       # from work_history
    "I was a Senior Designer at Acme Corp.",
    "I attended Mesa Community College.",                   # from education
    "I led the Atlas Design System project.",               # a project, not an employer
])
def test_claims_backed_by_the_real_history_are_not_flagged(text):
    assert grounding.find_unsupported_claims(text, PROFILE) == []


@pytest.mark.parametrize("text,expected", [
    ("I worked at Netflix on their recommendations team.", "Netflix"),
    ("My tenure at Stripe taught me a lot.", "Stripe"),
    ("I joined Palantir in 2019.", "Palantir"),
    ("I founded Widgets Unlimited.", "Widgets Unlimited"),
    ("I was employed by Initech Dynamics before that.", "Initech Dynamics"),
])
def test_invented_employers_are_flagged(text, expected):
    assert expected in grounding.find_unsupported_claims(text, PROFILE)
    assert not grounding.is_grounded(text, PROFILE)


@pytest.mark.parametrize("text", [
    "I admire how Tessera Labs approaches design.",
    "I'd love to work at Tessera Labs.",
    "Tessera Labs is doing interesting work in this space.",
])
def test_the_company_being_applied_to_may_be_named_freely(text):
    """The check must not fire on the employer you are applying to — that would
    flag every good answer and get the guard switched off."""
    assert grounding.find_unsupported_claims(text, PROFILE, "Tessera Labs") == []


@pytest.mark.parametrize("text", [
    "I want to bring my experience to your team.",
    "The team I worked with was great.",
    "I am passionate about accessible design.",
    "I have five years of product design experience.",
    "",
])
def test_ordinary_prose_is_not_flagged(text):
    """Noise here is what would discredit the guard, so it must stay quiet on
    text that makes no employment claim at all."""
    assert grounding.find_unsupported_claims(text, PROFILE) == []


def test_legal_suffixes_and_case_do_not_defeat_the_match():
    assert grounding.find_unsupported_claims("I worked at acme corp.", PROFILE) == []
    assert grounding.find_unsupported_claims("I worked at Acme Corporation.", PROFILE) == []
    assert grounding.find_unsupported_claims("I worked at ACME CORP, Inc.", PROFILE) == []


def test_empty_profile_flags_any_employment_claim():
    """Fail safe: with nothing known about the applicant, an employment claim
    cannot be supported and must be treated as unsupported."""
    assert grounding.find_unsupported_claims("I worked at Acme Corp.", {}) == ["Acme Corp"]


def test_known_organisations_gathers_every_source():
    known = grounding.known_organisations(PROFILE, "Tessera Labs")
    for expected in ("acme", "globex", "arizona state university",
                     "initech systems", "mesa community college", "tessera labs"):
        assert any(expected in k for k in known), expected
