import pytest

from applypilot.extension import matcher, resolve
from applypilot.extension.schema import FieldDescriptor, FillResult, SkipResult

PROFILE = {"availability": {"earliest_start_date": "Immediately"},
           "skills_boundary": {"tools": ["React (basic understanding)", "Figma", "react", "SQL [advanced]"]}}


@pytest.mark.parametrize("label", [
    "Earliest start date", "When can you start?", "Desired start date",
    "When would you be available to start?", "Availability to start", "Date available",
])
def test_start_availability_questions_use_profile(label):
    r = resolve.resolve_field(FieldDescriptor(id="s", label=label), PROFILE)
    assert isinstance(r, FillResult) and r.value == "Immediately"
    assert r.profile_key == "availability.earliest_start_date"


@pytest.mark.parametrize("fd", [
    FieldDescriptor(id="w", label="Start Date"),                                   # work history date
    FieldDescriptor(id="w", label="Start date", section="Work Experience 2", section_index=2),
    FieldDescriptor(id="w", label="Desired start date", section="Work Experience 1", section_index=1),
])
def test_history_dates_never_get_availability(fd):
    r = resolve.resolve_field(fd, PROFILE)
    assert not (isinstance(r, FillResult) and r.value == "Immediately")


def test_date_widget_is_not_given_text():
    r = matcher.match(FieldDescriptor(id="d", label="Earliest start date", type="date"), PROFILE)
    assert isinstance(r, SkipResult)
    iso = {"availability": {"earliest_start_date": "2026-10-15"}}
    r2 = matcher.match(FieldDescriptor(id="d", label="Earliest start date", type="date"), iso)
    assert isinstance(r2, FillResult) and r2.value == "2026-10-15"


def test_skill_search_terms_drop_qualifiers_but_text_keeps_them():
    r = matcher.match(FieldDescriptor(id="k", label="Skills"), PROFILE)
    assert isinstance(r, FillResult)
    assert r.values == ["React", "Figma", "SQL"]        # deduped case-insensitively
    assert "React (basic understanding)" in r.value      # plain text stays honest
