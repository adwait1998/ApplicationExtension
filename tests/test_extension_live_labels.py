"""Labels copied verbatim from live postings (Greenhouse job-boards, embedded
Greenhouse, Ashby, Lever) probed on 2026-09-24 with a synthetic applicant.
Each case was a wrong fill on the real page before its fix."""
import datetime

import pytest

from applypilot.apply.canary import resolve_canary
from applypilot.extension import answers, resolve
from applypilot.extension.schema import FieldDescriptor as F, FillResult

PROFILE = {
    "personal": {"full_name": "Jordan Quill Testperson", "preferred_name": "Jordan",
                 "address": "100 Example Ave", "city": "Seattle", "province_state": "Washington",
                 "postal_code": "98101", "country": "United States"},
    "experience": {"current_company": "Example Labs"},
    "work_history": [{"company": "Example Labs", "title": "Senior Product Designer"}],
}


def _value(label, **kw):
    r = resolve.resolve_field(F(id="x", label=label, **kw), PROFILE)
    return r.value if isinstance(r, FillResult) else None


@pytest.mark.parametrize("label,want", [
    ("Home Address Line 1*", "100 Example Ave"),
    ("Home Address City*", "Seattle"),
    ("Home Address State*", "Washington"),
    ("Home Address Zip Code*", "98101"),
    ("Home Address Country*", "United States"),
    ("Home Address CEP (Brazil Only)", None),
    ("Home Address Line 2", None),
])
def test_sofi_home_address_components(label, want):
    assert resolve_canary(label, PROFILE) == want


@pytest.mark.parametrize("label,want", [
    ("What is your notice period to your current employer?*", None),
    ("How may we pronounce your name?", None),
    ("Preferred Name (if different from legal name)", "Jordan"),
    ("Where are you currently employed or where were you last employed?*", "Example Labs"),
    ("Current location", "Seattle, Washington"),
    ("Before seeing this job posting, how familiar were you with Faire as a company?*", None),
])
def test_label_meaning_not_keyword(label, want):
    assert _value(label, type="text") == want


def test_location_select_is_matched_by_city():
    assert _value("What is your location?", tag="select", options=["Seattle, WA", "Portland, OR"]) == "Seattle"


def test_location_inside_a_work_history_block_is_not_the_applicants():
    r = resolve.resolve_field(F(id="l", label="Location", section="Work Experience 1", section_index=1),
                              PROFILE)
    assert not (isinstance(r, FillResult) and r.value in ("Seattle", "Seattle, Washington"))


def test_todays_date_in_the_requested_format():
    want = datetime.date.today().strftime("%m/%d/%y")
    assert _value("Today's Date of Application (MM/DD/YY Format)*", type="text") == want


@pytest.mark.parametrize("label,company", [
    ("Have you worked at or been a consultant for SoFi or any of its affiliates?", "SoFi"),
    ("Are you currently employed with or have been employed by SoFi or any of its subsidiaries?", "SoFi"),
    ("Are you currently a SoFi or SoFi Tech Solutions (former Galileo) employee?", "SoFi"),
    ("Have you previously been employed by Flexport or any of its subsidiaries?", "Flexport"),
])
def test_previously_employed_phrasings_are_checked_against_history(label, company):
    assert answers._company_from_question(label) == company
    r = resolve.resolve_field(F(id="p", label=label, type="text"), PROFILE)
    assert isinstance(r, FillResult) and r.value == "No" and not r.draft
    assert r.source == "answer_bank"


def test_current_employee_of_the_company_is_yes():
    r = resolve.resolve_field(F(id="p", label="Are you a current Example Labs employee?", type="text"), PROFILE)
    assert isinstance(r, FillResult) and r.value == "Yes"
