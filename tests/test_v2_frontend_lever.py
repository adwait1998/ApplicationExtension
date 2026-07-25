"""Lever front-end parser tests (Task 6). Probe-grounded: the synthetic forms
reproduce the REAL jobs.lever.co control shapes (9 live probes, 5 tenants) through
the SAME collect_browser_observation pipeline the parser consumes in production.

Ground truth (docs/superpowers/probes/lever_*.json):
  * dropdowns are NATIVE <select> (opportunityLocationId office select, country
    dropdown, eeo[..] selects, cards[..][field] selects) -> native_select; NO
    react-select / JS combobox on any probe.
  * resume upload is a native <input type=file> (#resume-upload-input).
  * ONE full-name field input[name="name"] ("Full name"), not first/last.
  * checkbox/radio OPTIONS are <label> WRAPPING <input>, so the observation emits
    a label twin (control_type='label') beside every real input (LABEL-BLEED).
  * submit is #btn-submit "SUBMIT APPLICATION".
"""
import pytest

from applypilot.apply.v2 import frontend_lever as fe
from applypilot.apply.v2 import ir
from applypilot.apply.v2 import resolver
from applypilot.apply.browser_stream import collect_browser_observation


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_context().new_page()
        yield p
        b.close()


# Synthetic Lever form mirroring the Task-4 probe (jobs.lever.co).
_LEVER_HTML = """
<form>
  <label for="name">Full name</label>
  <input id="name" name="name" type="text" required />
  <label for="email">Email</label>
  <input id="email" name="email" type="email" required />
  <label for="resume">Resume/CV</label>
  <input id="resume" name="resume" type="file" />
  <label for="loc">Location</label>
  <select id="loc" name="cards[location]"><option>Select</option><option>Remote</option></select>
  <label for="lk">LinkedIn URL</label>
  <input id="lk" name="urls[LinkedIn]" type="text" />
  <label for="q1">Why Lever?</label>
  <textarea id="q1" name="cards[abc][field0]" required></textarea>
  <button type="submit">Submit application</button>
</form>
"""


def test_lever_parses_standard_and_native_select(page):
    page.set_content(_LEVER_HTML)
    obs = collect_browser_observation(page)
    schema = fe.parse_observation(obs, company="acme", url="https://jobs.lever.co/acme/1")
    assert schema.ats == "lever"
    keys = {f.semantic_key for s in schema.steps for f in s.fields}
    assert {"email", "resume"} <= keys
    loc = [f for s in schema.steps for f in s.fields if f.semantic_key == "location"]
    assert loc and loc[0].widget.kind == "native_select"     # Lever native <select>
    assert loc[0].options is ir.LAZY                          # still LAZY at parse (invariant 4)


def test_lever_linkedin_keyed_and_custom_textarea(page):
    page.set_content(_LEVER_HTML)
    obs = collect_browser_observation(page)
    schema = fe.parse_observation(obs, company="acme", url="u")
    keys = {f.semantic_key for s in schema.steps for f in s.fields}
    assert "linkedin" in keys
    cust = [f for s in schema.steps for f in s.fields if (f.semantic_key or "").startswith("custom.")]
    assert cust and cust[0].widget.kind == "textarea"


def test_lever_single_terminal_step(page):
    page.set_content(_LEVER_HTML)
    obs = collect_browser_observation(page)
    schema = fe.parse_observation(obs, company="acme", url="u")
    assert len(schema.steps) == 1 and schema.steps[0].terminal
    assert schema.steps[0].advance_control is not None


# --------------------------------------------------------------------------
# Added coverage grounded in the live probes (not in the plan's minimal set).

def test_lever_full_name_keyed_and_resolves_whole_name(page):
    # Lever uses ONE input[name="name"] "Full name". It must key 'full_name' and
    # the resolver must bind the WHOLE personal.full_name (first_name/last_name
    # would split off a single token -> a truncated application name).
    page.set_content(_LEVER_HTML)
    obs = collect_browser_observation(page)
    schema = fe.parse_observation(obs, company="acme", url="u")
    by_key = {f.semantic_key: f for s in schema.steps for f in s.fields}
    assert "full_name" in by_key
    assert by_key["full_name"].widget.kind == "text"
    profile = {"personal": {"full_name": "Nida Shah", "email": "nida@example.com"}}
    plan = resolver.resolve(schema, profile)
    pf = [p for p in plan.planned if p.field.semantic_key == "full_name"]
    assert pf and pf[0].value == "Nida Shah"                  # whole name, NOT "Nida"
    assert pf[0].binding == "profile.personal.full_name"


def test_lever_label_bleed_twins_dropped_no_phantom_text(page):
    # Every checkbox/radio OPTION is a <label> WRAPPING its <input>; the observer
    # emits a control_type='label' twin beside the real input. Those twins must be
    # DROPPED -- keeping them spawns a phantom text field per option.
    html = """
    <form>
      <fieldset>
        <legend>Pronouns</legend>
        <label><input type="checkbox" name="pronouns" value="He/him"> He/him</label>
        <label><input type="checkbox" name="pronouns" value="She/her"> She/her</label>
        <label><input type="checkbox" name="pronouns" value="They/them"> They/them</label>
      </fieldset>
      <fieldset>
        <legend>Eligible?</legend>
        <label><input type="radio" name="cards[x][field0]" value="Yes"> Yes</label>
        <label><input type="radio" name="cards[x][field0]" value="No"> No</label>
      </fieldset>
      <button type="submit">Submit application</button>
    </form>
    """
    page.set_content(html)
    obs = collect_browser_observation(page)
    # Sanity: the observation really contains the label-bleed twins (else this
    # test would pass vacuously without exercising the drop).
    assert any((c.control_type or "").lower() == "label" for c in obs.controls)
    schema = fe.parse_observation(obs, company="acme", url="u")
    fields = [f for s in schema.steps for f in s.fields]
    # No field may originate from a bare <label> element (the dropped twin).
    assert not any((f.locator_spec.get("selector") or "").strip().lower() == "label"
                   for f in fields)
    # No phantom text field: every option field is a checkbox/radio widget.
    kinds = {f.widget.kind for f in fields}
    assert "text" not in kinds
    assert kinds <= {"checkbox", "radio_group"}
    # 3 checkbox options + 2 radio options == 5 real option fields, no doubling.
    assert sum(f.widget.kind == "checkbox" for f in fields) == 3
    assert sum(f.widget.kind == "radio_group" for f in fields) == 2


def test_lever_office_location_native_select(page):
    # The confirmed ground truth: the location dropdown is a NATIVE
    # <select name="opportunityLocationId"> -> native_select (never react_select),
    # options LAZY at parse.
    html = """
    <form>
      <select name="opportunityLocationId"><option>Select</option><option>Remote</option></select>
      <button type="submit">Submit application</button>
    </form>
    """
    page.set_content(html)
    obs = collect_browser_observation(page)
    schema = fe.parse_observation(obs, company="acme", url="u")
    sel = [f for s in schema.steps for f in s.fields
           if (f.locator_spec.get("name_attr") or "") == "opportunityLocationId"]
    assert sel and sel[0].widget.kind == "native_select"
    assert sel[0].options is ir.LAZY


def test_lever_closed_posting_empty_observation_is_terminal_not_crash(page):
    # One probe was a closed/404 posting with 0 controls. The parser must not
    # crash and must yield a single terminal step (downstream names the terminal:
    # no fields + no advance_control -> v2_incomplete_required, never a raise).
    page.set_content("<html><body><h1>This posting is closed.</h1></body></html>")
    obs = collect_browser_observation(page)
    schema = fe.parse_observation(obs, company="acme", url="https://jobs.lever.co/acme/dead/apply")
    assert schema.ats == "lever"
    assert len(schema.steps) == 1 and schema.steps[0].terminal
    assert schema.steps[0].fields == []
    assert schema.steps[0].advance_control is None
