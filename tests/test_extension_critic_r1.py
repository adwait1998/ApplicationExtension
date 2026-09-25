"""Critic round-1 findings, each pinned: consent statements are never
filled, prose never lands in a choice field, and label rules only fill
short-answer boxes that actually ask for that value."""
import json

import pytest

from applypilot.extension import answers, resolve
from applypilot.extension.schema import FieldDescriptor as F, FillResult, SkipResult

PROFILE = {
    "personal": {"full_name": "Taylor Morgan", "email": "t@example.com", "city": "Tempe",
                 "province_state": "Arizona", "country": "United States",
                 "linkedin_url": "https://linkedin.com/in/t"},
    "work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": False},
    "screening": {"background_check_consent": "Yes"},
}


@pytest.mark.parametrize("label", [
    "I certify that the information above is true and complete, and I authorize its verification.",
    "I authorize Acme to contact my references.",
    "I agree to the Terms and Conditions",
    "By checking this box, I consent to receive SMS updates",
    "Electronic Signature (type your full name)",
    "I acknowledge that I have read the privacy notice",
    "Acknowledgement",
])
@pytest.mark.parametrize("ftype", ["checkbox", "text"])
def test_statements_the_applicant_signs_are_never_filled(label, ftype):
    r = resolve.resolve_field(F(id="x", label=label, type=ftype), PROFILE)
    assert isinstance(r, SkipResult) and r.source == "attestation"


def test_work_auth_and_screening_questions_still_answer():
    assert resolve.resolve_field(F(id="w", label="Are you legally authorized to work in the US?"),
                                 PROFILE).value == "Yes"
    assert resolve.resolve_field(F(id="b", label="Do you authorize us to conduct a background check?"),
                                 PROFILE).value == "Yes"


@pytest.mark.parametrize("fd", [
    F(id="a", label="Please state why you want to join", tag="textarea"),
    F(id="a2", label="Please state why you want to join", type="text"),
    F(id="h", label="How did you hear about us? (LinkedIn, Indeed, Referral)", type="text"),
    F(id="r", label="Do you have a LinkedIn profile?", type="radio", options=["Yes", "No"]),
    F(id="c", label="Would you like to receive emails about future openings?", type="checkbox"),
])
def test_label_rules_never_put_profile_values_into_questions(fd):
    r = resolve.resolve_field(fd, PROFILE)
    assert not isinstance(r, FillResult), (fd.label, getattr(r, "value", None))


@pytest.mark.parametrize("fd,want", [
    (F(id="s", label="State", type="text"), "Arizona"),
    (F(id="s2", label="State/Province", tag="select", options=["Arizona", "California"]), "Arizona"),
    (F(id="l", label="Please provide your LinkedIn profile URL", type="text"), "https://linkedin.com/in/t"),
    (F(id="e", label="Email", type="email"), "t@example.com"),
])
def test_label_rules_still_fill_real_short_answers(fd, want):
    assert resolve.resolve_field(fd, PROFILE).value == want


class _AlwaysCity:
    """A Laya stand-in that confidently says every field is the city."""
    def classify(self, field, candidates):
        return ("personal.city", 0.99)


@pytest.mark.parametrize("fd", [
    F(id="t", label="Tell us about a time you disagreed with a PM", tag="textarea"),
    F(id="r", label="Pick one", type="radio", options=["A", "B"]),
])
def test_laya_is_gated_like_the_label_rules(fd):
    r = resolve.resolve_field(fd, PROFILE, laya=_AlwaysCity())
    assert not (isinstance(r, FillResult) and r.source == "laya")


@pytest.mark.parametrize("answer,fd,ok", [
    ("Yes", F(id="1", type="radio", options=["Yes", "No"]), True),
    ("Yes, within the US", F(id="2", type="radio", options=["Yes", "No"]), True),
    ("Yes", F(id="3", type="radio", options=["Yes, I am authorized", "No"]), True),
    ("I know relocation can be hard, but I am open to it", F(id="4", type="radio", options=["Yes", "No"]), False),
    ("I know Figma deeply and use it daily", F(id="5", tag="select", options=["Yes", "No"]), False),
    ("Not sure", F(id="6", type="radio", options=["Yes", "No"]), False),
    ("Yes", F(id="7", type="checkbox"), True),
    ("Remote-first teams with a strong design culture", F(id="8", widget="wd-dropdown"), False),
    ("LinkedIn", F(id="9", widget="wd-dropdown"), True),
])
def test_fits_choice(answer, fd, ok):
    assert answers.fits_choice(answer, fd) is ok


def test_bank_prose_never_reaches_a_choice_field(tmp_path):
    bank = tmp_path / "answer_bank.json"
    bank.write_text(json.dumps([{"q": "Are you open to relocation?",
                                 "a": "I know relocation can be hard, but I am open to it"}]), encoding="utf-8")
    cache = answers.make_cache({}, bank)
    radio = F(id="r", label="Are you open to relocation?", type="radio", options=["Yes", "No"])
    text = F(id="t", label="Are you open to relocation?", type="text")
    assert not isinstance(resolve.resolve_field(radio, {}, answer_cache=cache), FillResult)
    r = resolve.resolve_field(text, {}, answer_cache=cache)
    assert isinstance(r, FillResult) and r.value.startswith("I know relocation")


def test_drafts_never_generated_for_choice_fields(monkeypatch):
    calls = []
    monkeypatch.setattr(answers, "drafts_enabled", lambda app_dir=None: True)
    radio = F(id="r", label="Describe your favourite design tool", type="radio", options=["Figma", "Sketch"])
    answers.match(radio, {}, llm_fn=lambda q, c: calls.append(q) or "Figma is great")
    assert calls == []
    text = F(id="t", label="Describe your favourite design tool", tag="textarea")
    answers.match(text, {}, llm_fn=lambda q, c: calls.append(q) or "Figma is great")
    assert calls == ["Describe your favourite design tool"]  # the control: drafts do run for text


def test_unidentified_employer_answer_is_marked_for_review():
    r = answers.previously_employed_check(
        F(id="p", label="Have you previously been employed by this company?"), {}, url="")
    assert isinstance(r, FillResult) and r.value == "No" and r.draft is True
