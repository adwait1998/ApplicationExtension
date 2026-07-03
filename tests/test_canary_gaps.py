from applypilot.apply.canary import is_canary, resolve_canary

PROFILE = {"work_authorization": {"require_sponsorship": True, "legally_authorized_to_work": False}}

import pytest

from applypilot.apply.adapters.greenhouse import _LEGAL_ATTESTATION_RE


@pytest.mark.parametrize("q", [
    "Will you require the company to commence an immigration case?",
    "Are you subject to ITAR / export-control restrictions?",
    "What is your immigration status?",
    "Are you subject to export control restrictions (ITAR/EAR)?",
    "Are you a US person as defined under export control regulations?",
])
def test_legal_attestation_phrasings_are_canary(q):
    assert is_canary(q) is True


@pytest.mark.parametrize("q", [
    "Will you require the company to commence an immigration case?",
    "Are you subject to ITAR / export-control restrictions?",
    "What is your immigration status?",
])
def test_immigration_and_export_park_not_guessed(q):
    # no profile field answers these -> resolver returns None -> field stays unresolved
    assert resolve_canary(q, PROFILE) is None


def test_ordinary_question_still_not_canary():
    assert is_canary("Describe a project you are proud of.") is False
    assert is_canary("Why do you want to work here?") is False


def test_legal_attestation_denylist_matches_expected():
    assert _LEGAL_ATTESTATION_RE.search("Have you ever been convicted of a felony?")
    assert _LEGAL_ATTESTATION_RE.search("Are you authorized to work in the US?")
    assert not _LEGAL_ATTESTATION_RE.search("What is your favorite design tool?")
