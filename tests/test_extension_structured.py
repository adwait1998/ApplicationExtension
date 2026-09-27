"""Tests for tier 3 (applypilot.extension.structured): work_history /
education matching for repeating ATS sections ("Work Experience 2", ...).

All $0, no network, no filesystem, no real profile.json — profile is
always an inline dict.
"""
from __future__ import annotations

from applypilot.extension.schema import FieldDescriptor, FillResult, SkipResult
from applypilot.extension.structured import match

PROFILE_TWO_JOBS = {
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

PROFILE_ONE_EDU = {
    "education": [
        {
            "school": "University of Washington",
            "degree": "BFA",
            "field": "Design",
            "start": "08/2014",
            "end": "05/2018",
        },
    ],
}


def _field(**kwargs) -> FieldDescriptor:
    base = dict(
        id="f0", selector="#x", tag="input", type="text", name="",
        autocomplete="", label="", placeholder="", required=False, options=[],
        section="", section_index=None,
    )
    base.update(kwargs)
    return FieldDescriptor(**base)


# ---------------------------------------------------------------------------
# section_index -> work_history[index - 1]
# ---------------------------------------------------------------------------


def test_job_title_fills_from_indexed_section_position_1():
    result = match(_field(label="Job Title", section="Work Experience 1", section_index=1), PROFILE_TWO_JOBS)
    assert isinstance(result, FillResult)
    assert result.value == "Senior Product Designer"
    assert result.source == "structured"


def test_job_title_fills_from_indexed_section_position_2():
    result = match(_field(label="Job Title", section="Work Experience 2", section_index=2), PROFILE_TWO_JOBS)
    assert isinstance(result, FillResult)
    assert result.value == "Product Designer"


def test_company_fills_per_indexed_section():
    r1 = match(_field(label="Company", section="Work Experience 1", section_index=1), PROFILE_TWO_JOBS)
    r2 = match(_field(label="Company", section="Work Experience 2", section_index=2), PROFILE_TWO_JOBS)
    assert r1.value == "Acme"
    assert r2.value == "Globex"


def test_location_fills_per_indexed_section():
    r1 = match(_field(label="Location", section="Work Experience 1", section_index=1), PROFILE_TWO_JOBS)
    r2 = match(_field(label="Location", section="Work Experience 2", section_index=2), PROFILE_TWO_JOBS)
    assert r1.value == "Seattle, WA"
    assert r2.value == "Portland, OR"


def test_role_description_fills_per_indexed_section():
    # Normalised to a bullet at FILL time (applypilot.extension.resume_import
    # ._normalize_description, reused) -- PROFILE_TWO_JOBS stores a plain
    # sentence, simulating a profile saved before that normalisation
    # existed on import. See the bullets-at-fill-time section below for
    # more cases, and for proof the stored profile itself is never touched.
    result = match(_field(label="Role Description", section="Work Experience 2", section_index=2), PROFILE_TWO_JOBS)
    assert result.value == "- Owned onboarding flows."


# ---------------------------------------------------------------------------
# index out of range: skip, never wrap, never fall back to position 1
# ---------------------------------------------------------------------------


def test_third_position_out_of_range_skips_job_title():
    result = match(_field(label="Job Title", section="Work Experience 3", section_index=3), PROFILE_TWO_JOBS)
    assert isinstance(result, SkipResult)
    assert result.source == "structured"
    assert result.auto_fill is False
    assert "3rd" in result.reason
    assert "work history" in result.reason


def test_out_of_range_never_wraps_to_position_1():
    result = match(_field(label="Company", section="Work Experience 3", section_index=3), PROFILE_TWO_JOBS)
    assert isinstance(result, SkipResult)
    assert not (isinstance(result, FillResult))


def test_all_ten_fields_out_of_range_are_skipped_not_wrapped():
    labels = [
        "Job Title", "Company", "Location", "I currently work here",
        "From", "To", "Role Description",
    ]
    for label in labels:
        result = match(
            _field(label=label, section="Work Experience 3", section_index=3),
            PROFILE_TWO_JOBS,
        )
        assert isinstance(result, SkipResult), f"{label} should skip, got {result!r}"
        assert result.auto_fill is False


# ---------------------------------------------------------------------------
# no section_index but clearly work-experience-shaped -> most recent (index 0)
# ---------------------------------------------------------------------------


def test_no_section_index_uses_most_recent_position():
    result = match(_field(label="Job Title"), PROFILE_TWO_JOBS)
    assert isinstance(result, FillResult)
    assert result.value == "Senior Product Designer"


def test_no_section_at_all_falls_back_to_kind_hint_for_company():
    result = match(_field(label="Company"), PROFILE_TWO_JOBS)
    assert isinstance(result, FillResult)
    assert result.value == "Acme"


def test_bare_generic_field_with_no_section_and_no_kind_hint_defers():
    # "Location" alone, no section, no other kind evidence -> cannot tell if
    # this is a work_history field at all; must not guess.
    result = match(_field(label="Location"), PROFILE_TWO_JOBS)
    assert result is None


# ---------------------------------------------------------------------------
# current:true -> "I currently work here" true, End date left blank
# ---------------------------------------------------------------------------


def test_currently_work_here_checkbox_true_for_current_position():
    result = match(
        _field(label="I currently work here", type="checkbox", section="Work Experience 1", section_index=1),
        PROFILE_TWO_JOBS,
    )
    assert isinstance(result, FillResult)
    assert result.value == "true"


def test_currently_work_here_checkbox_false_for_past_position():
    result = match(
        _field(label="I currently work here", type="checkbox", section="Work Experience 2", section_index=2),
        PROFILE_TWO_JOBS,
    )
    assert isinstance(result, FillResult)
    assert result.value == "false"


def test_end_date_blank_when_current():
    result = match(_field(label="To", section="Work Experience 1", section_index=1), PROFILE_TWO_JOBS)
    assert isinstance(result, FillResult)
    assert result.value == ""


def test_end_date_filled_when_not_current():
    result = match(_field(label="To", section="Work Experience 2", section_index=2), PROFILE_TWO_JOBS)
    assert isinstance(result, FillResult)
    assert result.value == "02/2022"


def test_start_date_fills_mm_yyyy_by_default():
    result = match(_field(label="From", section="Work Experience 1", section_index=1), PROFILE_TWO_JOBS)
    assert isinstance(result, FillResult)
    assert result.value == "03/2022"


def test_start_date_formats_iso_for_native_date_input():
    result = match(
        _field(label="From", type="date", section="Work Experience 1", section_index=1),
        PROFILE_TWO_JOBS,
    )
    assert isinstance(result, FillResult)
    assert result.value == "2022-03-01"


def test_start_date_uses_placeholder_mm_yyyy_verbatim():
    result = match(
        _field(label="From", placeholder="MM/YYYY", section="Work Experience 1", section_index=1),
        PROFILE_TWO_JOBS,
    )
    assert isinstance(result, FillResult)
    assert result.value == "03/2022"


# ---------------------------------------------------------------------------
# no work_history at all -> defer (None), exactly like before this tier existed
# ---------------------------------------------------------------------------


def test_no_work_history_in_profile_defers_instead_of_crashing():
    result = match(_field(label="Job Title", section="Work Experience 1", section_index=1), {})
    assert result is None


def test_empty_work_history_list_defers():
    result = match(_field(label="Job Title", section="Work Experience 1", section_index=1), {"work_history": []})
    assert result is None


# ---------------------------------------------------------------------------
# section text names neither kind -> defer
# ---------------------------------------------------------------------------


def test_unrelated_section_heading_defers():
    result = match(_field(label="Job Title", section="References 1", section_index=1), PROFILE_TWO_JOBS)
    assert result is None


# ---------------------------------------------------------------------------
# education mirrors work_history
# ---------------------------------------------------------------------------


def test_education_school_fills_from_section():
    result = match(_field(label="School", section="Education 1", section_index=1), PROFILE_ONE_EDU)
    assert isinstance(result, FillResult)
    assert result.value == "University of Washington"


def test_education_degree_and_field_fill():
    r_degree = match(_field(label="Degree", section="Education 1", section_index=1), PROFILE_ONE_EDU)
    r_field = match(_field(label="Field of Study", section="Education 1", section_index=1), PROFILE_ONE_EDU)
    assert r_degree.value == "BFA"
    assert r_field.value == "Design"


def test_education_out_of_range_skips_not_wraps():
    result = match(_field(label="School", section="Education 2", section_index=2), PROFILE_ONE_EDU)
    assert isinstance(result, SkipResult)
    assert "education" in result.reason
    assert "2nd" in result.reason


def test_no_education_in_profile_defers():
    result = match(_field(label="School", section="Education 1", section_index=1), {})
    assert result is None


# ---------------------------------------------------------------------------
# missing value at a valid index still skips cleanly (never fabricates)
# ---------------------------------------------------------------------------


def test_valid_index_but_field_blank_in_profile_skips_with_reason():
    profile = {"work_history": [{"title": "Designer", "company": "", "start": "01/2020"}]}
    result = match(_field(label="Company", section="Work Experience 1", section_index=1), profile)
    assert isinstance(result, SkipResult)
    assert result.auto_fill is False
    assert "not set" in result.reason


# ---------------------------------------------------------------------------
# GPA: keyed by section_index exactly like school/degree/field, filled only
# from the profile, never converted between scales.
# ---------------------------------------------------------------------------

PROFILE_ONE_EDU_WITH_GPA = {
    "education": [
        {
            "school": "University of Washington",
            "degree": "BFA",
            "field": "Design",
            "start": "08/2014",
            "end": "05/2018",
            "gpa": "3.8/4.0",
        },
    ],
}


def test_gpa_fills_from_indexed_education_section():
    result = match(_field(label="GPA", section="Education 1", section_index=1), PROFILE_ONE_EDU_WITH_GPA)
    assert isinstance(result, FillResult)
    assert result.value == "3.8/4.0"
    assert result.source == "structured"
    assert result.profile_key == "education[0].gpa"


def test_cumulative_gpa_label_also_recognized():
    result = match(_field(label="Cumulative GPA", section="Education 1", section_index=1), PROFILE_ONE_EDU_WITH_GPA)
    assert isinstance(result, FillResult)
    assert result.value == "3.8/4.0"


def test_grade_point_average_label_also_recognized():
    result = match(
        _field(label="Grade Point Average", section="Education 1", section_index=1), PROFILE_ONE_EDU_WITH_GPA
    )
    assert isinstance(result, FillResult)
    assert result.value == "3.8/4.0"


def test_gpa_matching_scale_fills_normally():
    result = match(
        _field(label="GPA (out of 4.0)", section="Education 1", section_index=1), PROFILE_ONE_EDU_WITH_GPA
    )
    assert isinstance(result, FillResult)
    assert result.value == "3.8/4.0"


def test_gpa_out_of_a_different_scale_skips_instead_of_converting():
    # The form explicitly wants a 10-point scale; the stored value is on a
    # 4.0 scale -- must not be converted, just skipped with a clear reason.
    result = match(
        _field(label="GPA out of 10", section="Education 1", section_index=1), PROFILE_ONE_EDU_WITH_GPA
    )
    assert isinstance(result, SkipResult)
    assert result.source == "structured"
    assert result.auto_fill is False
    assert "scale" in result.reason.lower()
    assert "10" in result.reason and "4" in result.reason


def test_gpa_default_scale_assumed_when_stored_value_has_none():
    profile = {"education": [{"school": "State University", "degree": "B.S.", "gpa": "3.8"}]}
    result = match(_field(label="GPA out of 4.0", section="Education 1", section_index=1), profile)
    assert isinstance(result, FillResult)
    assert result.value == "3.8"


def test_gpa_not_set_on_matched_entry_skips_cleanly():
    profile = {"education": [{"school": "State University", "degree": "B.S.", "gpa": ""}]}
    result = match(_field(label="GPA", section="Education 1", section_index=1), profile)
    assert isinstance(result, SkipResult)
    assert result.auto_fill is False
    assert "not set" in result.reason


def test_gpa_question_terminal_skip_when_no_education_data_at_all():
    """The non-negotiable: GPA must only ever come from the profile, never
    the answer bank or a draft. Unlike every other slot (which defers with
    None so a lower tier can try), a recognised GPA question stays a
    terminal structured skip even with zero education data, so it can never
    reach a lower tier."""
    result = match(_field(label="Cumulative GPA", section="Education 1", section_index=1), {})
    assert isinstance(result, SkipResult)
    assert result.source == "structured"
    assert result.auto_fill is False


def test_bare_gpa_field_with_no_section_establishes_education_kind():
    # Like school/degree/field, a lone "GPA" field with no section at all
    # still recognises the field as education-shaped.
    result = match(_field(label="GPA"), PROFILE_ONE_EDU_WITH_GPA)
    assert isinstance(result, FillResult)
    assert result.value == "3.8/4.0"


# ---------------------------------------------------------------------------
# Work-history description bullets at FILL time, not just at import time --
# reuses resume_import._normalize_description rather than a second
# implementation, and never mutates the profile dict it was given.
# ---------------------------------------------------------------------------


def test_description_paragraph_normalized_to_bullets_at_fill_time():
    profile = {
        "work_history": [{
            "title": "Designer", "company": "Acme", "location": "", "start": "", "end": "",
            "current": False,
            "description": "Led the redesign of onboarding. Reduced drop-off by 30%.",
        }],
    }
    result = match(_field(label="Role Description", section="Work Experience 1", section_index=1), profile)
    assert isinstance(result, FillResult)
    assert result.value == "- Led the redesign of onboarding.\n- Reduced drop-off by 30%."


def test_description_already_bulleted_is_unaffected():
    profile = {
        "work_history": [{
            "title": "Designer", "company": "Acme", "location": "", "start": "", "end": "",
            "current": False,
            "description": "- Shipped the redesign\n- Ran user research",
        }],
    }
    result = match(_field(label="Role Description", section="Work Experience 1", section_index=1), profile)
    assert result.value == "- Shipped the redesign\n- Ran user research"


def test_description_normalization_never_mutates_the_stored_profile():
    profile = {
        "work_history": [{
            "title": "Designer", "company": "Acme", "location": "", "start": "", "end": "",
            "current": False,
            "description": "Led the redesign of onboarding. Reduced drop-off by 30%.",
        }],
    }
    before = profile["work_history"][0]["description"]
    match(_field(label="Role Description", section="Work Experience 1", section_index=1), profile)
    after = profile["work_history"][0]["description"]
    assert after == before == "Led the redesign of onboarding. Reduced drop-off by 30%."


def test_bare_work_history_heading_recognized_as_work_kind():
    """Paylocity (recruiting.paylocity.com, 2026-09-26) headings its repeating block "Work
    History" / "Work History 1" — the literal phrase _WORK_SECTION_RE used to lack ("work
    experience"/"employment (history)"/"job history" were covered, but not "work history"
    itself), so a field correctly section-tagged "Work History 1" by the scanner still fell
    through _kind_for as neither kind and was never placed into the right job entry."""
    result = match(_field(label="Job Title", section="Work History 1", section_index=1), PROFILE_TWO_JOBS)
    assert isinstance(result, FillResult)
    assert result.value == "Senior Product Designer"


def test_bare_work_history_heading_second_entry():
    result = match(_field(label="Company Name", section="Work History 2", section_index=2), PROFILE_TWO_JOBS)
    assert isinstance(result, FillResult)
    assert result.value == "Globex"
