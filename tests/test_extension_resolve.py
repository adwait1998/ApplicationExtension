"""Tests for the resolution ladder (applypilot.extension.resolve).

All $0, no network, no filesystem, no real profile.json — profile is
always an inline dict passed directly to resolve_fields()/resolve_field().
"""
from __future__ import annotations

import pytest

from applypilot.extension import answers, resolve
from applypilot.extension.schema import FieldDescriptor, FillResult, SkipResult


@pytest.fixture(autouse=True)
def _clean_answers_env(monkeypatch):
    # Tier 5 (answer bank) now defaults ON in production (see answers.py --
    # it is model-free, so there's no reason to hide it behind an opt-in).
    # Forced OFF here as this file's baseline so the tier-0..4 ladder tests
    # (secret guard, canary, deterministic, laya, structured) stay exactly
    # as before: independent of the real repo-root answer_bank.json's live
    # content. Tests that exist specifically to exercise tiers 5/6 opt back
    # in with their own monkeypatch.setenv(...), which overrides this.
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "0")
    monkeypatch.delenv("APPLYPILOT_DRAFTS", raising=False)
    monkeypatch.delenv("APPLYPILOT_MAX_DRAFTS", raising=False)
    yield

PROFILE = {
    "personal": {
        "full_name": "Nida Shah",
        "email": "nida@example.com",
        "phone": "555-000-1111",
        "linkedin_url": "https://linkedin.com/in/nidashah",
        "password": "hunter2",
    },
    "work_authorization": {
        "legally_authorized_to_work": True,
        "require_sponsorship": False,
    },
    "compensation": {
        "salary_expectation": "140000",
        "salary_currency": "USD",
    },
    "eeo_voluntary": {},
}


def _field(**kwargs) -> FieldDescriptor:
    base = dict(id="f0", selector="#x", tag="input", type="text", name="", autocomplete="", label="", placeholder="")
    base.update(kwargs)
    return FieldDescriptor(**base)


# ---------------------------------------------------------------------------
# tier 0: secret guard — the one the spec calls out explicitly
# ---------------------------------------------------------------------------


def test_password_field_gets_nothing():
    result = resolve.resolve_field(_field(label="Password", type="password"), PROFILE)
    assert isinstance(result, SkipResult)
    assert result.auto_fill is False
    # not just unresolved — flagged as a secret, and never carries a value
    assert not hasattr(result, "value")


def test_password_field_by_name_alone_also_guarded():
    result = resolve.resolve_field(_field(name="password"), PROFILE)
    assert isinstance(result, SkipResult)
    assert result.source == "secret_guard"


def test_secret_guard_runs_even_if_a_tier_would_have_produced_a_value():
    # belt-and-suspenders: is_secret_path() independently blocks any
    # FillResult whose profile_key happens to be the secret path, even if
    # the field-level regex guard were bypassed.
    assert resolve.is_secret_path("personal.password") is True
    assert resolve.is_secret_path("personal.email") is False


# ---------------------------------------------------------------------------
# tier 1: canary — never falls through
# ---------------------------------------------------------------------------


def test_work_authorization_question_resolved_by_canary():
    result = resolve.resolve_field(
        _field(label="Are you legally authorized to work in the United States?"), PROFILE
    )
    assert isinstance(result, FillResult)
    assert result.value == "Yes"
    assert result.source == "canary"


def test_sponsorship_question_resolved_by_canary():
    result = resolve.resolve_field(
        _field(label="Will you now or in the future require sponsorship to work in the United States?"),
        PROFILE,
    )
    assert isinstance(result, FillResult)
    assert result.value == "No"
    assert result.source == "canary"


def test_canary_with_no_resolvable_answer_skips_and_does_not_fall_through():
    # citizenship has no profile path at all -> canary.resolve_canary returns
    # None; the field must NOT then be handed to the deterministic tier.
    result = resolve.resolve_field(_field(label="Are you a U.S. citizen?"), PROFILE)
    assert isinstance(result, SkipResult)
    assert result.source == "canary"
    assert "answer this yourself" in result.reason


def test_salary_canary_fills_from_profile():
    result = resolve.resolve_field(_field(label="Desired salary"), PROFILE)
    assert isinstance(result, FillResult)
    assert result.value == "140000 USD"
    assert result.source == "canary"


def test_salary_canary_skips_when_not_in_profile():
    profile = {"compensation": {}}
    result = resolve.resolve_field(_field(label="Desired salary"), profile)
    assert isinstance(result, SkipResult)
    assert result.source == "canary"


# ---------------------------------------------------------------------------
# tier 2: deterministic (delegated to matcher, exercised through the ladder)
# ---------------------------------------------------------------------------


def test_deterministic_field_fills_through_the_ladder():
    result = resolve.resolve_field(_field(autocomplete="email", label="Email"), PROFILE)
    assert isinstance(result, FillResult)
    assert result.source == "deterministic"
    assert result.value == "nida@example.com"


# ---------------------------------------------------------------------------
# tier 3: laya absent by default
# ---------------------------------------------------------------------------


def test_laya_absent_by_default():
    assert resolve.get_backend() is None


def test_tiers_available_without_laya():
    assert resolve.tiers_available() == ["canary", "deterministic"]


def test_tiers_available_with_fake_laya_backend():
    class FakeLaya:
        def classify(self, field, candidate_keys):
            return None

    assert resolve.tiers_available(laya=FakeLaya()) == ["canary", "deterministic", "laya"]


def test_laya_fills_when_confidence_above_threshold():
    class FakeLaya:
        def classify(self, field, candidate_keys):
            return ("personal.email", 0.9)

    result = resolve.resolve_field(_field(label="Contact"), PROFILE, laya=FakeLaya())
    assert isinstance(result, FillResult)
    assert result.source == "laya"
    assert result.value == "nida@example.com"
    assert result.confidence == 0.9


def test_laya_skips_when_confidence_below_threshold():
    class FakeLaya:
        def classify(self, field, candidate_keys):
            return ("personal.email", 0.2)

    result = resolve.resolve_field(_field(label="Contact"), PROFILE, laya=FakeLaya())
    assert isinstance(result, SkipResult)


def test_laya_never_offered_a_secret_path():
    class FakeLaya:
        def classify(self, field, candidate_keys):
            assert "personal.password" not in candidate_keys
            return ("personal.password", 0.99)

    result = resolve.resolve_field(_field(label="Contact"), PROFILE, laya=FakeLaya())
    # even if a buggy backend answered with the secret path anyway, the
    # guard at the point of emission must still block it.
    assert isinstance(result, SkipResult)


def test_laya_backend_exception_degrades_to_unresolved_not_a_crash():
    class BrokenLaya:
        def classify(self, field, candidate_keys):
            raise RuntimeError("boom")

    result = resolve.resolve_field(_field(label="Contact"), PROFILE, laya=BrokenLaya())
    assert isinstance(result, SkipResult)
    assert result.source == "unresolved"


# ---------------------------------------------------------------------------
# tier 4: unresolved (free text etc.)
# ---------------------------------------------------------------------------


def test_free_text_question_is_unresolved():
    result = resolve.resolve_field(
        _field(tag="textarea", label="Why do you want to work here?"), PROFILE
    )
    assert isinstance(result, SkipResult)
    assert result.source == "unresolved"
    assert result.auto_fill is False


# ---------------------------------------------------------------------------
# resolve_fields(): batch -> FillPlan, only requested fields are returned
# ---------------------------------------------------------------------------


def test_resolve_fields_batch_builds_fill_plan():
    fields = [
        _field(id="f0", autocomplete="given-name", label="First Name"),
        _field(id="f1", tag="textarea", label="Why do you want to work here?"),
        _field(id="f2", label="Password", type="password"),
    ]
    plan = resolve.resolve_fields(fields, PROFILE)
    filled_ids = {f.id for f in plan.fills}
    skipped_ids = {s.id for s in plan.skipped}
    assert filled_ids == {"f0"}
    assert skipped_ids == {"f1", "f2"}
    assert plan.tiers_available == ["canary", "deterministic"]
    # the plan never contains anything beyond the fields it was given
    assert filled_ids | skipped_ids == {"f0", "f1", "f2"}


# ---------------------------------------------------------------------------
# Laya candidate pre-ranking — the confidence gate is only meaningful while
# the option count stays inside Laya's calibrated range (~10).
# ---------------------------------------------------------------------------

def _fd(**kw):
    from applypilot.extension.schema import FieldDescriptor
    base = dict(id="f1", selector="#x", tag="input", type="text", name="",
                autocomplete="", label="", placeholder="", required=False,
                options=[])
    base.update(kw)
    return FieldDescriptor(**base)


def test_laya_is_never_offered_more_options_than_it_can_calibrate():
    from applypilot.extension import matcher
    seen = {}

    class Spy:
        def classify(self, field, candidate_keys):
            seen["n"] = len(candidate_keys)
            return None

    profile = {"personal": {"email": "a@b.com"}}
    resolve.resolve_field(_fd(label="Some Unmatched Question"), profile, laya=Spy())
    # +1 for the "none" option a backend adds; stay at or under 10 total.
    assert seen["n"] <= matcher.LAYA_MAX_CANDIDATES
    assert seen["n"] + 1 <= 10


def test_ranking_puts_the_obvious_key_first():
    from applypilot.extension import matcher
    assert matcher.rank_candidates(_fd(label="Current Employer"))[0] == \
        "experience.current_company"
    assert matcher.rank_candidates(_fd(label="Mobile Number"))[0] == "personal.phone"
    assert matcher.rank_candidates(_fd(label="Zip"))[0] == "personal.postal_code"
    assert matcher.rank_candidates(_fd(label="LinkedIn Profile"))[0] == \
        "personal.linkedin_url"


def test_ranking_is_stable_and_never_empty_for_an_unknown_field():
    from applypilot.extension import matcher
    got = matcher.rank_candidates(_fd(label="Favourite colour"))
    assert 0 < len(got) <= matcher.LAYA_MAX_CANDIDATES
    assert got == matcher.rank_candidates(_fd(label="Favourite colour"))


def test_secret_paths_are_not_rankable_candidates():
    from applypilot.extension import matcher
    for key in matcher.rank_candidates(_fd(label="Password"), limit=99):
        assert key != "personal.password"


# ---------------------------------------------------------------------------
# tier 3: structured, exercised through the ladder — reproducing the real
# Workday "My Experience" step that filled 0 of 10 fields because the
# profile had no work_history in it. Two work_history entries here.
# ---------------------------------------------------------------------------

WORKDAY_PROFILE = {
    "personal": {"full_name": "Nida Shah", "email": "nida@example.com"},
    "work_history": [
        {
            "title": "Senior Product Designer",
            "company": "Acme",
            "location": "Seattle, WA",
            "start": "03/2022",
            "end": "",
            "current": True,
            "description": "Led design for the core product.",
        },
        {
            "title": "Product Designer",
            "company": "Globex",
            "location": "Portland, OR",
            "start": "06/2018",
            "end": "02/2022",
            "current": False,
            "description": "Owned onboarding flows.",
        },
    ],
}


def _workday_fields(section: str, section_index: int) -> list[FieldDescriptor]:
    common = dict(section=section, section_index=section_index)
    return [
        _field(id="title", label="Job Title", **common),
        _field(id="company", label="Company", **common),
        _field(id="location", label="Location", **common),
        _field(id="current", label="I currently work here", type="checkbox", **common),
        _field(id="from", label="From", **common),
        _field(id="to", label="To", **common),
        _field(id="description", label="Role Description", tag="textarea", **common),
    ]


def test_workday_work_experience_1_fills_from_first_position():
    plan = resolve.resolve_fields(_workday_fields("Work Experience 1", 1), WORKDAY_PROFILE)
    fills = {f.id: f for f in plan.fills}
    assert plan.skipped == [] or all(s.id not in fills for s in plan.skipped)
    assert fills["title"].value == "Senior Product Designer"
    assert fills["company"].value == "Acme"
    assert fills["location"].value == "Seattle, WA"
    assert fills["current"].value == "true"
    assert fills["from"].value == "03/2022"
    assert fills["to"].value == ""  # current position -> end date left blank
    assert fills["description"].value == "Led design for the core product."
    for f in fills.values():
        assert f.source == "structured"
        assert f.auto_fill is True


def test_workday_work_experience_2_fills_from_second_position():
    plan = resolve.resolve_fields(_workday_fields("Work Experience 2", 2), WORKDAY_PROFILE)
    fills = {f.id: f for f in plan.fills}
    assert fills["title"].value == "Product Designer"
    assert fills["company"].value == "Globex"
    assert fills["location"].value == "Portland, OR"
    assert fills["current"].value == "false"
    assert fills["from"].value == "06/2018"
    assert fills["to"].value == "02/2022"
    assert fills["description"].value == "Owned onboarding flows."


def test_workday_work_experience_3_all_skip_never_wraps_to_position_1():
    # Only two positions in the profile — a 3rd section must skip every
    # field, never wrap back to Senior Product Designer / Acme.
    plan = resolve.resolve_fields(_workday_fields("Work Experience 3", 3), WORKDAY_PROFILE)
    assert plan.fills == []
    assert len(plan.skipped) == 7
    for s in plan.skipped:
        assert s.source == "structured"
        assert s.auto_fill is False
        assert "3rd" in s.reason


def test_workday_step_with_no_work_history_behaves_exactly_as_before():
    # The actual bug: a profile with NO work_history must still just skip,
    # with the same generic message as before tier 3 existed — never crash,
    # never claim a "structured" skip it has no data to back up.
    profile = {"personal": {"full_name": "Nida Shah"}}
    plan = resolve.resolve_fields(_workday_fields("Work Experience 1", 1), profile)
    assert plan.fills == []
    assert len(plan.skipped) == 7
    for s in plan.skipped:
        assert s.source == "unresolved"
        assert s.auto_fill is False


# ---------------------------------------------------------------------------
# tiers 5/6: answer bank + draft — off by default, wired at the end of the
# ladder. These tests never touch the real repo-root answer_bank.json:
# resolve_field()'s answer_cache= override (built against a tmp bank path)
# covers direct ladder calls, and answers._DEFAULT_BANK_PATH is monkeypatched
# to a tmp path for the resolve_fields()-batch tests that can't take an
# override directly.
# ---------------------------------------------------------------------------


def test_tiers_available_reports_nothing_extra_when_answers_disabled():
    # This file's fixture forces APPLYPILOT_ANSWERS=0 -- the TRUE default
    # (answer bank on) is covered directly in test_extension_answers.py,
    # without going through the real repo-root answer_bank.json this file
    # deliberately never touches.
    assert resolve.tiers_available() == ["canary", "deterministic"]


def test_tiers_available_reports_answer_bank_when_enabled(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")
    tiers = resolve.tiers_available()
    assert "answer_bank" in tiers
    assert "draft" not in tiers


def test_tiers_available_reports_draft_only_with_both_flags(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_DRAFTS", "1")  # APPLYPILOT_ANSWERS unset
    assert "draft" not in resolve.tiers_available()
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")
    assert "draft" in resolve.tiers_available()


def test_ladder_fills_from_the_answer_bank_before_falling_to_unresolved(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")
    bank = tmp_path / "bank.json"
    # Company-NEUTRAL on purpose. This used "Why do you want to work at this
    # company?" — but a real answer to that names the employer, and reusing it
    # across companies is the bug is_company_directed() now blocks.
    bank.write_text('[{"q": "When can you start?", '
                     '"a": "Two weeks after an offer."}]', encoding="utf-8")
    cache = answers.make_cache(PROFILE, bank_path=bank)

    result = resolve.resolve_field(
        _field(tag="textarea", label="What is your earliest available start date?"),
        PROFILE, answer_cache=cache,
    )
    assert isinstance(result, FillResult)
    assert result.source == "answer_bank"
    assert result.value == "Two weeks after an offer."
    assert result.draft is False
    assert result.auto_fill is True


def test_ladder_produces_a_marked_draft_on_a_genuine_miss(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")
    monkeypatch.setenv("APPLYPILOT_DRAFTS", "1")
    monkeypatch.setattr(answers, "_real_llm_fn", lambda q, c: "A drafted, ungrounded-looking answer.")
    bank = tmp_path / "bank.json"
    bank.write_text("[]", encoding="utf-8")
    cache = answers.make_cache(PROFILE, bank_path=bank)

    result = resolve.resolve_field(
        _field(tag="textarea", label="Tell us about a challenge you overcame."),
        PROFILE, answer_cache=cache,
    )
    assert isinstance(result, FillResult)
    assert result.source == "draft"
    assert result.draft is True
    assert result.auto_fill is True
    assert result.value == "A drafted, ungrounded-looking answer."


def test_ladder_never_calls_the_llm_when_drafts_are_off(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")  # drafts NOT enabled

    def _explode(q, c):
        raise AssertionError("drafts are off — the LLM must never run")

    monkeypatch.setattr(answers, "_real_llm_fn", _explode)
    bank = tmp_path / "bank.json"
    bank.write_text("[]", encoding="utf-8")
    cache = answers.make_cache(PROFILE, bank_path=bank)

    result = resolve.resolve_field(
        _field(tag="textarea", label="Tell us about a challenge you overcame."),
        PROFILE, answer_cache=cache,
    )
    assert isinstance(result, SkipResult)
    assert result.source == "unresolved"


def test_canary_never_reaches_answer_bank_or_draft_even_with_both_flags_on(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")
    monkeypatch.setenv("APPLYPILOT_DRAFTS", "1")

    def _explode(q, c):
        raise AssertionError("a canary question must never reach the LLM")

    monkeypatch.setattr(answers, "_real_llm_fn", _explode)
    bank = tmp_path / "bank.json"
    # A tempting, wrong-for-this-profile bank entry — proves the canary
    # tier's early return (tier 1, terminal) is what's blocking this, not
    # merely an empty bank.
    bank.write_text('[{"q": "Will you now or in the future require '
                     'sponsorship to work in the United States?", "a": "Yes"}]',
                     encoding="utf-8")
    cache = answers.make_cache(PROFILE, bank_path=bank)

    result = resolve.resolve_field(
        _field(label="Will you now or in the future require sponsorship to work in the United States?"),
        PROFILE, answer_cache=cache,
    )
    assert result.source == "canary"
    assert result.value == "No"  # PROFILE's real work_authorization answer, not the poisoned bank


def test_secret_field_never_reaches_answer_bank_or_draft(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")
    monkeypatch.setenv("APPLYPILOT_DRAFTS", "1")
    bank = tmp_path / "bank.json"
    bank.write_text("[]", encoding="utf-8")
    cache = answers.make_cache(PROFILE, bank_path=bank)

    result = resolve.resolve_field(_field(label="Password", type="password"), PROFILE, answer_cache=cache)
    assert isinstance(result, SkipResult)
    assert result.source == "secret_guard"


def test_resolve_fields_batch_shares_one_draft_cap_across_the_request(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")
    monkeypatch.setenv("APPLYPILOT_DRAFTS", "1")
    monkeypatch.setenv("APPLYPILOT_MAX_DRAFTS", "2")
    bank = tmp_path / "bank.json"
    bank.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(answers, "_DEFAULT_BANK_PATH", bank)

    calls = []
    monkeypatch.setattr(answers, "_real_llm_fn",
                         lambda q, c: calls.append(q) or f"draft: {q}")

    fields = [
        _field(id="f0", tag="textarea", label="Describe your design process."),
        _field(id="f1", tag="textarea", label="What is your biggest weakness."),
        _field(id="f2", tag="textarea", label="Tell us about a time you failed."),
    ]
    plan = resolve.resolve_fields(fields, PROFILE)

    assert len(calls) == 2  # the cap, not the field count
    drafts = [f for f in plan.fills if f.source == "draft"]
    assert len(drafts) == 2
    for f in drafts:
        assert f.draft is True
    cap_skips = [s for s in plan.skipped if s.source == "draft"]
    assert len(cap_skips) == 1
    assert "cap" in cap_skips[0].reason.lower()
    assert plan.tiers_available == ["canary", "deterministic", "answer_bank", "draft"]


# ---------------------------------------------------------------------------
# "Have you previously worked here?" -- the real Viasat wording, resolved
# through the whole ladder (resolve_fields -> answers.match's deterministic
# employer check), not just the answers.py unit tests.
# ---------------------------------------------------------------------------

VIASAT_QUESTION = (
    "Have you previously been employed by our company or any of "
    "its subsidiaries or affiliates?"
)


def test_ladder_answers_previously_employed_yes_when_company_in_work_history(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")
    profile = {**PROFILE, "work_history": [{"title": "Designer", "company": "Viasat Inc."}]}
    plan = resolve.resolve_fields(
        [_field(id="f0", tag="textarea", label=VIASAT_QUESTION)],
        profile,
        url="https://viasat.wd1.myworkdayjobs.com/en-US/Viasat_Careers/job/123",
    )
    assert plan.fills[0].value == "Yes"
    assert plan.fills[0].source == "answer_bank"


def test_ladder_answers_previously_employed_no_when_company_absent(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")
    profile = {**PROFILE, "work_history": [{"title": "Designer", "company": "Acme Corp"}]}
    plan = resolve.resolve_fields(
        [_field(id="f0", tag="textarea", label=VIASAT_QUESTION)],
        profile,
        url="https://viasat.wd1.myworkdayjobs.com/en-US/Viasat_Careers/job/123",
    )
    assert plan.fills[0].value == "No"
    assert plan.fills[0].source == "answer_bank"


def test_ladder_falls_back_to_seed_default_when_employer_unidentifiable(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")
    plan = resolve.resolve_fields(
        [_field(id="f0", tag="textarea", label=VIASAT_QUESTION)],
        PROFILE,
        url="",  # no page URL, no company named in the question itself
    )
    assert plan.fills[0].value == "No"
    assert "could not be identified" in plan.fills[0].reason
