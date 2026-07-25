"""Ashby front-end tests — assertions keyed to the Task-4 + Task-5 live probes
under docs/superpowers/probes/ashby_*.json (invariant 12).

Ground truth from 10 probes across 6 tenants (linear, notion, openai, ramp,
vanta, sentry): Ashby renders system fields with STABLE ids (#_systemfield_name/
_email/_resume), custom questions as text/textarea with UUID-hash ids, and EVERY
choice field — including EEO gender/race/veteran/disability, work-auth, "how did
you hear" and Yes/No — as native radio/checkbox groups. NO native <select> and
NO react-select combobox appeared in ANY probe, so those widget kinds are
UNSUPPORTED-with-named-terminal ('unknown'), never guessed. The synthetic DOM
below mirrors that observed markup (each radio/checkbox surfaces twice — once as
its <label>, once as the <input> — exactly as browser_stream reports live)."""
import pytest

from applypilot.apply.browser_stream import collect_browser_observation
from applypilot.apply.v2 import frontend_ashby as fe
from applypilot.apply.v2 import ir


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_context().new_page()
        yield p
        b.close()


# Synthetic Ashby form mirroring the live probes (jobs.ashbyhq.com). System
# fields carry the stable _systemfield_ ids; EEO gender is a radio group and
# "how did you hear" a checkbox group (both as observed on notion/openai/vanta);
# a native <select> stands in for a widget kind NEVER observed in any probe.
_ASHBY_HTML = """
<form>
  <label for="_systemfield_name">Full Name</label>
  <input id="_systemfield_name" type="text" required />
  <label for="_systemfield_email">Email</label>
  <input id="_systemfield_email" type="email" required />
  <label for="_systemfield_resume">Resume</label>
  <input id="_systemfield_resume" type="file" required />
  <label for="ln">LinkedIn Profile</label>
  <input id="ln" type="text" />
  <fieldset>
    <legend>Gender</legend>
    <label><input type="radio" name="gender" value="male" />Male</label>
    <label><input type="radio" name="gender" value="female" />Female</label>
    <label><input type="radio" name="gender" value="decline" />Decline to self-identify</label>
  </fieldset>
  <fieldset>
    <legend>How did you hear about us?</legend>
    <label><input type="checkbox" name="hdyh" value="li" />LinkedIn</label>
    <label><input type="checkbox" name="hdyh" value="hn" />Hacker News</label>
  </fieldset>
  <label for="q1">Why do you want to join our team?</label>
  <textarea id="q1" required></textarea>
  <label for="loc">Location</label>
  <select id="loc"><option value="">Select...</option><option value="us">United States</option></select>
  <button type="submit">Submit Application</button>
</form>
"""


def _schema(page):
    page.set_content(_ASHBY_HTML)
    obs = collect_browser_observation(page)
    return fe.parse_observation(obs, company="acme", url="https://jobs.ashbyhq.com/acme/app")


def test_ashby_parses_standard_semantic_keys(page):
    schema = _schema(page)
    assert schema.ats == "ashby"
    keys = {f.semantic_key for s in schema.steps for f in s.fields}
    # Ashby's single "Full Name" field is not a taxonomy key (mirrors the plan's
    # tolerance); email/resume/linkedin resolve from their labels.
    assert {"email", "resume", "linkedin"} <= keys


def test_ashby_eeo_gender_is_radio_group_options_lazy(page):
    schema = _schema(page)
    gender = [f for s in schema.steps for f in s.fields if f.semantic_key == "eeo.gender"]
    assert gender, "EEO gender should be keyed eeo.gender"
    assert all(f.widget.kind == "radio_group" for f in gender)
    assert all(f.options is ir.LAZY for f in gender)  # never enumerated at parse (invariant 4)


def test_ashby_checkbox_identity_bleed_guarded(page):
    # "How did you hear ... - LinkedIn" is a checkbox whose label bleeds the
    # identity token 'linkedin'; the reused bleed guard must fall it back to a
    # custom.* key, and it must classify as a checkbox.
    schema = _schema(page)
    hdyh = [f for s in schema.steps for f in s.fields
            if "how did you hear" in (f.label_text or "").lower()]
    assert hdyh
    assert all(f.widget.kind == "checkbox" for f in hdyh)
    assert all((f.semantic_key or "").startswith("custom.") for f in hdyh)
    assert all(f.semantic_key != "linkedin" for f in hdyh)


def test_ashby_custom_question_keyed_custom(page):
    schema = _schema(page)
    cust = [f for s in schema.steps for f in s.fields
            if (f.semantic_key or "").startswith("custom.") and f.widget.kind == "textarea"]
    assert cust and cust[0].widget.kind == "textarea"


def test_ashby_unobserved_widget_is_unknown_terminal(page):
    # A native <select>/combobox is a widget kind that appears in ZERO of the 10
    # Ashby probes. It must map to the named 'unknown' terminal (WidgetKind.ALL),
    # NOT be silently guessed as text/native_select/react_select.
    schema = _schema(page)
    loc = [f for s in schema.steps for f in s.fields if f.semantic_key == "location"]
    assert loc, "the <select> Location field should still be parsed as a field"
    assert loc[0].widget.kind == "unknown"
    assert loc[0].widget.kind in ir.WidgetKind.ALL


def test_ashby_single_terminal_step_with_submit(page):
    schema = _schema(page)
    assert len(schema.steps) == 1 and schema.steps[0].terminal
    assert schema.steps[0].advance_control is not None
    assert schema.steps[0].advance_control["label"] == "Submit Application"
