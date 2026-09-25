"""Reasons are shown in the panel and exported in fill reports that the user
is told to send to someone else: they must never carry the applicant's
values (reviewer round 3 reproduced EEO/disability values in reasons)."""
import pytest

from applypilot.extension import resolve
from applypilot.extension.schema import FieldDescriptor as F

SENSITIVE = {
    "Yes, I have a disability (or had one in the past)", "I am a protected veteran", "Female",
    "Bisexual", "Asian", "No, I do not have a disability", "Immediately", "120000", "6",
    "Taylor Quill Morgan", "taylor@example.com", "98101",
}
PROFILE = {
    "personal": {"full_name": "Taylor Quill Morgan", "email": "taylor@example.com", "postal_code": "98101",
                 "city": "Seattle", "province_state": "Washington", "country": "United States"},
    "eeo_voluntary": {"gender": "Female", "disability_status": "Yes, I have a disability (or had one in the past)",
                      "veteran_status": "I am a protected veteran", "sexual_orientation": "Bisexual",
                      "race_ethnicity": "Asian"},
    "work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": True, "work_permit_type": "H-1B"},
    "compensation": {"salary_expectation": "120000", "salary_currency": "USD"},
    "availability": {"earliest_start_date": "Immediately"},
    "experience": {"years_of_experience_total": "6"},
    "screening": {"criminal_conviction": "Yes"},
}
FIELDS = [
    F(id="1", label="Veteran", type="checkbox"),
    F(id="2", label="Person with disability", type="checkbox"),
    F(id="3", label="Gender", type="checkbox"),
    F(id="4", label="Earliest start date", type="date"),
    F(id="5", label="How many years of experience do you have?", type="radio", options=["0-2", "3-5", "5-7", "6-9"]),
    F(id="6", label="Have you ever been convicted of a felony?", type="radio", options=["Yes", "No"]),
    F(id="7", label="Are you legally authorized to work in the US?", type="radio",
      options=["Yes, I am a U.S. citizen", "Yes, I need sponsorship", "Yes, I need sponsorship later"]),
    F(id="8", label="Disability Status", tag="select", options=["Decline", "I don't wish to answer"]),
    F(id="9", label="Desired salary", type="text"),
]


@pytest.mark.parametrize("fd", FIELDS, ids=[f.label[:30] for f in FIELDS])
def test_no_reason_carries_a_profile_value(fd):
    r = resolve.resolve_field(fd, PROFILE)
    for value in SENSITIVE:
        assert value.lower() not in (r.reason or "").lower(), (fd.label, r.reason)
