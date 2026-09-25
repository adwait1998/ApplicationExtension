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


# ---- probe #4 (20 live postings) ------------------------------------------

P4 = {
    "personal": {"full_name": "Jordan Quill Testperson", "preferred_name": "Jordan", "city": "Seattle",
                 "province_state": "Washington", "country": "United States"},
    "experience": {"current_company": "Example Labs", "current_job_title": "Senior Product Designer",
                   "years_of_experience_total": "6"},
    "skills_boundary": {"tools": ["Figma", "Sketch"]},
    "education": [{"school": "University of Washington", "degree": "Bachelor of Design",
                   "field": "Interaction Design", "start": "09/2015", "end": "06/2019"}],
    "screening": {"non_compete": "No"},
}


def _v(fd):
    r = resolve.resolve_field(fd, P4)
    return r.value if isinstance(r, FillResult) else None


@pytest.mark.parametrize("fd,want", [
    (F(id="a", label="First and Last Name", type="text"), "Jordan Quill Testperson"),
    (F(id="b", label="Current/Most Recent Company Name", type="text"), "Example Labs"),
    (F(id="c", label="Current/Most Recent Job Title", type="text"), "Senior Product Designer"),
    (F(id="d", label="Do you have a preference in Industry (Consumer Business Services, Financial Services, "
                     "Healthcare, Media, Retail, Telecom, Tech skills)", tag="textarea"), None),
    (F(id="e", label="Please list any software tools you have used or been trained on.", type="text"),
     "Figma, Sketch"),
    (F(id="f", label="What is your location?", tag="select",
       options=["Select...", "Afghanistan", "United Kingdom", "United States"]), "United States"),
    (F(id="g", label="Where are you located?", tag="select", options=["Remote - US", "Remote - Canada"]), None),
    (F(id="h", label="How many years of relevant professional experience do you have?", type="radio",
       options=["0-2 years", "3-5 years", "6-9 years", "10+ years"]), "6-9 years"),
    (F(id="i", label="How many years of experience do you have?", type="radio", options=["1-3", "3-6", "6+"]), None),
    (F(id="j", label="Which university are you currently attending or did you last attend?", tag="select",
       options=["Select...", "University of Washington", "Washington State University"]), "University of Washington"),
    (F(id="k", label="Discipline*", type="text"), "Interaction Design"),
    (F(id="l", label="Are you currently subject to any agreement with a former employer/third party (such as a "
                     "non-compete or non-solicitation agreement)? If yes, please explain.", type="text"), "No"),
    (F(id="m", label="What is your military status?*", type="text"), "Decline to self-identify"),
])
def test_probe4_labels(fd, want):
    assert _v(fd) == want


def test_greenhouse_education_run_fills_split_dates():
    fields = [F(id="s", label="School*"), F(id="d", label="Degree*"), F(id="di", label="Discipline*"),
              F(id="sm", label="Start date month"), F(id="sy", label="Start date year", type="number"),
              F(id="em", label="End date month*"), F(id="ey", label="End date year*", type="number"),
              F(id="x", label="LinkedIn Profile"), F(id="y", label="Start date")]
    plan = resolve.resolve_fields(fields, P4)
    got = {r.id: r.value for r in plan.fills}
    assert got["sm"] == "September" and got["sy"] == "2015" and got["em"] == "June" and got["ey"] == "2019"
    assert "y" not in got   # the run ended at LinkedIn; a later bare "Start date" is not education


def test_city_typeahead_gets_city_and_state_for_disambiguation():
    fd = F(id="c", label="Location (City)*", type="text", widget="combobox")
    assert _v(fd) == "Seattle, Washington"
    assert _v(F(id="t", label="Location (City)*", type="text")) == "Seattle"   # a plain text box: city only
