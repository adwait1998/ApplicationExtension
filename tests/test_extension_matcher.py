"""Tests for the deterministic tier (applypilot.extension.matcher).

All $0, no network, no filesystem — profile is always an inline dict.
"""
from __future__ import annotations

from applypilot.extension.matcher import match, value_for_key
from applypilot.extension.schema import FieldDescriptor, FillPlan, FillResult, SkipResult

PROFILE = {
    "personal": {
        "full_name": "Nida Shah",
        "email": "nida@example.com",
        "phone": "555-000-1111",
        "linkedin_url": "https://linkedin.com/in/nidashah",
        "portfolio_url": "https://nida.design",
        "city": "Seattle",
        "province_state": "WA",
        "country": "USA",
        "password": "hunter2",
    },
    "experience": {
        "current_company": "Acme",
        "current_job_title": "Product Designer",
    },
}


def _field(**kwargs) -> FieldDescriptor:
    base = dict(id="f0", selector="#x", tag="input", type="text", name="", autocomplete="", label="", placeholder="")
    base.update(kwargs)
    return FieldDescriptor(**base)


# ---------------------------------------------------------------------------
# autocomplete tier
# ---------------------------------------------------------------------------


def test_autocomplete_given_name_splits_full_name():
    result = match(_field(autocomplete="given-name", label="First Name"), PROFILE)
    assert isinstance(result, FillResult)
    assert result.value == "Nida"
    assert result.source == "deterministic"
    assert result.profile_key == "personal.full_name"
    assert result.confidence == 1.0
    assert result.auto_fill is True


def test_autocomplete_family_name_splits_full_name():
    result = match(_field(autocomplete="family-name", label="Last Name"), PROFILE)
    assert isinstance(result, FillResult)
    assert result.value == "Shah"


def test_autocomplete_email():
    result = match(_field(autocomplete="email", label="Email"), PROFILE)
    assert isinstance(result, FillResult)
    assert result.value == "nida@example.com"
    assert result.profile_key == "personal.email"


def test_autocomplete_tel():
    result = match(_field(autocomplete="tel", label="Phone"), PROFILE)
    assert isinstance(result, FillResult)
    assert result.value == "555-000-1111"


def test_autocomplete_multi_token_uses_last_token():
    result = match(_field(autocomplete="shipping given-name", label="First"), PROFILE)
    assert isinstance(result, FillResult)
    assert result.value == "Nida"


def test_autocomplete_recognized_but_profile_missing_value_skips():
    profile = {"personal": {"full_name": "Nida Shah"}}  # no email
    result = match(_field(autocomplete="email", label="Email"), profile)
    assert isinstance(result, SkipResult)
    assert result.source == "deterministic"
    assert "personal.email" in result.reason


# ---------------------------------------------------------------------------
# name/id/label regex fallback (no autocomplete present)
# ---------------------------------------------------------------------------


def test_label_regex_first_name_without_autocomplete():
    result = match(_field(label="First Name"), PROFILE)
    assert isinstance(result, FillResult)
    assert result.value == "Nida"
    assert result.confidence == 0.85


def test_label_regex_last_name_without_autocomplete():
    result = match(_field(label="Last Name"), PROFILE)
    assert isinstance(result, FillResult)
    assert result.value == "Shah"


def test_label_regex_linkedin():
    result = match(_field(label="LinkedIn Profile URL"), PROFILE)
    assert isinstance(result, FillResult)
    assert result.value == "https://linkedin.com/in/nidashah"
    assert result.profile_key == "personal.linkedin_url"


def test_label_regex_portfolio():
    result = match(_field(label="Portfolio / personal site"), PROFILE)
    assert isinstance(result, FillResult)
    assert result.value == "https://nida.design"


def test_name_field_used_when_label_missing():
    result = match(_field(name="email-address"), PROFILE)
    assert isinstance(result, FillResult)
    assert result.value == "nida@example.com"


# ---------------------------------------------------------------------------
# "First and Last Legal Name" and sibling phrasings — the real Workday gap.
# ---------------------------------------------------------------------------


def test_first_and_last_legal_name_matches_full_name():
    result = match(_field(label="First and Last Legal Name"), PROFILE)
    assert isinstance(result, FillResult)
    assert result.value == "Nida Shah"
    assert result.profile_key == "personal.full_name"


def test_legal_name_alone_matches_full_name():
    result = match(_field(label="Legal Name"), PROFILE)
    assert isinstance(result, FillResult)
    assert result.value == "Nida Shah"


def test_full_legal_name_matches_full_name():
    result = match(_field(label="Full Legal Name"), PROFILE)
    assert isinstance(result, FillResult)
    assert result.value == "Nida Shah"


def test_name_first_and_last_parenthetical_matches_full_name():
    result = match(_field(label="Name (First and Last)"), PROFILE)
    assert isinstance(result, FillResult)
    assert result.value == "Nida Shah"


def test_first_name_alone_still_matches_first_name_only():
    # Regression guard: the new "legal name" / "first and last name" patterns
    # must not swallow the existing bare first-name / last-name matches.
    result = match(_field(label="First Name"), PROFILE)
    assert isinstance(result, FillResult)
    assert result.value == "Nida"


def test_last_name_alone_still_matches_last_name_only():
    result = match(_field(label="Last Name"), PROFILE)
    assert isinstance(result, FillResult)
    assert result.value == "Shah"


# ---------------------------------------------------------------------------
# file input / no match / unrelated fields
# ---------------------------------------------------------------------------


def test_file_input_never_auto_filled():
    result = match(_field(tag="input", type="file", label="Resume/CV"), PROFILE)
    assert isinstance(result, SkipResult)
    assert result.auto_fill is False
    assert "attach" in result.reason.lower()


def test_unmatched_field_returns_none_to_continue_ladder():
    result = match(_field(label="Why do you want to work here?", tag="textarea"), PROFILE)
    assert result is None


def test_full_name_missing_from_profile_skips_with_reason():
    result = match(_field(autocomplete="given-name", label="First Name"), {"personal": {}})
    assert isinstance(result, SkipResult)


# ---------------------------------------------------------------------------
# value_for_key helper
# ---------------------------------------------------------------------------


def test_value_for_key_plain_path():
    assert value_for_key("personal.email", PROFILE) == "nida@example.com"


def test_value_for_key_first_last_suffixes():
    assert value_for_key("personal.full_name#first", PROFILE) == "Nida"
    assert value_for_key("personal.full_name#last", PROFILE) == "Shah"


def test_value_for_key_missing_path_returns_none():
    assert value_for_key("personal.does_not_exist", PROFILE) is None


def test_value_for_key_single_word_name_has_no_last():
    profile = {"personal": {"full_name": "Cher"}}
    assert value_for_key("personal.full_name#first", profile) == "Cher"
    assert value_for_key("personal.full_name#last", profile) is None


# ---------------------------------------------------------------------------
# schema shapes (smoke test — dataclasses match spec's JSON exactly)
# ---------------------------------------------------------------------------


def test_schema_shapes_serialize_to_spec_json():
    fill = FillResult(
        id="f0", value="Nida", source="deterministic", profile_key="personal.full_name",
        confidence=1.0, auto_fill=True, reason="autocomplete=given-name",
    )
    skip = SkipResult(id="f7", source="canary", reason="canary:salary not present", auto_fill=False)
    plan = FillPlan(fills=[fill], skipped=[skip], tiers_available=["canary", "deterministic"])
    d = plan.to_dict()
    assert d["fills"][0]["profile_key"] == "personal.full_name"
    assert d["skipped"][0]["source"] == "canary"
    assert "profile_key" not in d["skipped"][0]
    assert d["tiers_available"] == ["canary", "deterministic"]
