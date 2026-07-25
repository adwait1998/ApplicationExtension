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
    # Ashby's single "Full Name" box now keys full_name via the shared word-boundary
    # name matcher (Task 7 addendum); email/resume/linkedin resolve from their labels.
    assert {"full_name", "email", "resume", "linkedin"} <= keys


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


# --------------------------------------------------------------------------
# Task 7 addendum: full_name matcher precedence for the Ashby #_systemfield_name
# single-name box. "First and Last Name" must NOT substring-match last_name (which
# on a live form would fill ONLY the surname into a Full Name box); it must key
# full_name so the resolver binds the WHOLE personal.full_name.

_ASHBY_FIRST_AND_LAST_HTML = """
<form>
  <label for="_systemfield_name">First and Last Name</label>
  <input id="_systemfield_name" type="text" required />
  <label for="_systemfield_email">Email</label>
  <input id="_systemfield_email" type="email" required />
  <button type="submit">Submit Application</button>
</form>
"""


def test_ashby_first_and_last_name_is_full_name_not_last_name(page):
    # The exact addendum bug: "First and Last Name" contains the 'last name'
    # substring. Precedence + word-boundary must resolve it to full_name.
    page.set_content(_ASHBY_FIRST_AND_LAST_HTML)
    obs = collect_browser_observation(page)
    schema = fe.parse_observation(obs, company="acme", url="https://jobs.ashbyhq.com/acme/app")
    by_key = {f.semantic_key: f for s in schema.steps for f in s.fields}
    assert "full_name" in by_key
    assert by_key["full_name"].widget.kind == "text"
    keys = set(by_key)
    assert "last_name" not in keys and "first_name" not in keys   # NOT split/hijacked


def test_ashby_full_name_resolves_whole_name(page):
    # The single Full Name box binds the WHOLE personal.full_name verbatim (Nida
    # Shah), never a split token.
    from applypilot.apply.v2 import resolver
    page.set_content(_ASHBY_FIRST_AND_LAST_HTML)
    obs = collect_browser_observation(page)
    schema = fe.parse_observation(obs, company="acme", url="https://jobs.ashbyhq.com/acme/app")
    profile = {"personal": {"full_name": "Nida Shah", "email": "nida@example.com"}}
    plan = resolver.resolve(schema, profile)
    pf = [p for p in plan.planned if p.field.semantic_key == "full_name"]
    assert pf and pf[0].value == "Nida Shah"
    assert pf[0].binding == "profile.personal.full_name"


def test_ashby_bare_name_input_single_field_heuristic(page):
    # A bare "Name" label on the lone #_systemfield_name text box (no separate
    # first/last inputs) is promoted to full_name by the single-name-input heuristic.
    html = """
    <form>
      <label for="_systemfield_name">Name</label>
      <input id="_systemfield_name" type="text" required />
      <button type="submit">Submit Application</button>
    </form>
    """
    page.set_content(html)
    obs = collect_browser_observation(page)
    schema = fe.parse_observation(obs, company="acme", url="https://jobs.ashbyhq.com/acme/app")
    keys = {f.semantic_key for s in schema.steps for f in s.fields}
    assert "full_name" in keys
