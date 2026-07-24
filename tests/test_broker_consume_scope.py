"""Ticket-consume scope regression (live bug 2026-07-24).

The one-shot SubmitBroker ticket must be consumed only on the ACTUAL application
submit request, not on the earlier prefill resume-upload POST. On a real live
Greenhouse apply the prefill upload POST to the Greenhouse host burned the ticket
~90s before the agent's submit, so the real submit was refused
(submit_refused_no_broker_ticket). `is_submit_request` is the pure request-time
classifier that scopes the consume to submit-shaped ATS requests only.
"""
from applypilot.apply.browser_stream import is_submit_request


# --- Regression from the live failure: prefill uploads / resume-parse are NOT submits ---

def test_prefill_attachment_upload_is_not_submit():
    # The exact request shape that burned the ticket live.
    assert not is_submit_request(
        "POST", "https://job-boards.greenhouse.io/twilio/attachments/upload"
    )


def test_resume_parse_is_not_submit():
    assert not is_submit_request(
        "POST", "https://job-boards.greenhouse.io/twilio/resume/parse"
    )


# --- The actual application submit MUST classify ---

def test_applications_post_is_submit():
    assert is_submit_request(
        "POST", "https://job-boards.greenhouse.io/twilio/jobs/7985808/applications"
    )


def test_apply_post_is_submit():
    assert is_submit_request(
        "POST", "https://job-boards.greenhouse.io/twilio/jobs/7985808/apply"
    )


# --- Method + host guards ---

def test_get_to_submit_shaped_url_is_not_submit():
    assert not is_submit_request(
        "GET", "https://job-boards.greenhouse.io/twilio/jobs/7985808/applications"
    )


def test_non_ats_host_is_not_submit():
    assert not is_submit_request(
        "POST", "https://example.com/twilio/jobs/7985808/applications"
    )
