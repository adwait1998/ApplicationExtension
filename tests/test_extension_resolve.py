"""Tests for the resolution ladder (applypilot.extension.resolve).

All $0, no network, no filesystem, no real profile.json — profile is
always an inline dict passed directly to resolve_fields()/resolve_field().
"""
from __future__ import annotations

from applypilot.extension import resolve
from applypilot.extension.schema import FieldDescriptor, FillResult, SkipResult

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
