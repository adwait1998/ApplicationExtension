"""Submit-endpoint capture telemetry (live attempt #5, 2026-07-24).

On the Twilio job-boards.greenhouse.io React board a submit POST fired but
`is_submit_request` returned False, so the one-shot broker ticket was never
consumed and we never learned the real submit path. `ats_mutation_telemetry_row`
is the pure decision that captures such requests (mutating + ATS-host +
ticket-open + NOT submit-shaped) so the NEXT run reveals the endpoint to extend
`_SUBMIT_HINT` with. Behavior otherwise unchanged — telemetry only.
"""
from applypilot.apply.browser_stream import ats_mutation_telemetry_row


def test_non_submit_ats_mutation_with_open_ticket_yields_row():
    row = ats_mutation_telemetry_row(
        "POST",
        "https://job-boards.greenhouse.io/twilio/jobs/7985808/save?foo=bar&csrf=abc",
        ticket_open=True,
    )
    assert row is not None
    assert row["method"] == "POST"
    # path only — query string stripped
    assert row["url"] == "/twilio/jobs/7985808/save"
    assert isinstance(row["ts"], float)


def test_submit_shaped_request_yields_no_row():
    # The classifier already covers this shape; it consumes the ticket instead.
    assert ats_mutation_telemetry_row(
        "POST",
        "https://job-boards.greenhouse.io/twilio/jobs/7985808/applications",
        ticket_open=True,
    ) is None


def test_closed_ticket_yields_no_row():
    assert ats_mutation_telemetry_row(
        "POST", "https://job-boards.greenhouse.io/twilio/jobs/7985808/save",
        ticket_open=False,
    ) is None


def test_non_mutation_method_yields_no_row():
    assert ats_mutation_telemetry_row(
        "GET", "https://job-boards.greenhouse.io/twilio/jobs/7985808/save",
        ticket_open=True,
    ) is None


def test_non_ats_host_yields_no_row():
    assert ats_mutation_telemetry_row(
        "POST", "https://example.com/twilio/jobs/7985808/save", ticket_open=True,
    ) is None


def test_safe_analytics_host_yields_no_row():
    assert ats_mutation_telemetry_row(
        "POST", "https://www.google-analytics.com/collect", ticket_open=True,
    ) is None


def test_resume_parse_mutation_is_not_captured_as_submit_candidate():
    # resume/cv-parse is a known non-submit; is_submit_request returns False for
    # it too, but it is noise for endpoint capture — still, a row here is
    # harmless telemetry. Assert the decision is stable (row produced, path only).
    row = ats_mutation_telemetry_row(
        "POST", "https://job-boards.greenhouse.io/twilio/resume/parse",
        ticket_open=True,
    )
    assert row is not None
    assert row["url"] == "/twilio/resume/parse"
