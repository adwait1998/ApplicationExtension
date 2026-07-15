import pytest

from applypilot.apply.v2 import ir
from applypilot.apply.v2 import frontend_greenhouse as fe
from applypilot.apply.browser_stream import collect_browser_observation


# Reuse the adapter-test synthetic form shape (label+control pairs, a react-
# select-ish combobox, a required custom textarea, a submit button). Plus a
# native <select> EEO pair (Gender identity / Gender) to exercise the two
# subtlest classifier branches: native-select-first ordering (a native <select>
# reports control_type="select" AND role="combobox", so it must NOT fall through
# to the react_select role check) and EEO specific-before-generic key ordering
# ("Gender identity" must not be hijacked by the generic "gender" synonym).
_FORM = """
<!doctype html><html><body>
<form id="application_form">
  <label for="fn">First name</label>
  <input id="fn" name="first_name" type="text" required class="gh-in a1">
  <label for="em">Email</label>
  <input id="em" name="email" type="email" required class="gh-in c3">
  <label for="loc">Current location (City)</label>
  <input id="loc" name="location" type="text" class="gh-in e5">
  <label for="rz">Resume/CV</label>
  <input id="rz" name="resume" type="file" required class="gh-file g7">
  <label for="wa-i">Are you legally authorized to work in the US?</label>
  <div class="select__control" id="wa-c" tabindex="0">
    <span class="select__single-value">Select...</span>
    <input class="select__input" id="wa-i" role="combobox" autocomplete="off" aria-required="true">
  </div>
  <label for="cust">Describe a product you shipped</label>
  <textarea id="cust" name="why_8801" required maxlength="500" class="gh-ta z9"></textarea>
  <label for="gi">Gender identity</label>
  <select id="gi" name="gender_identity" class="gh-sel eeo1">
    <option value="">Select...</option>
    <option value="woman">Woman</option>
    <option value="man">Man</option>
    <option value="nonbinary">Non-binary</option>
  </select>
  <label for="gn">Gender</label>
  <select id="gn" name="gender" class="gh-sel eeo2">
    <option value="">Select...</option>
    <option value="female">Female</option>
    <option value="male">Male</option>
  </select>
  <button id="sub" type="button">Submit application</button>
</form></body></html>
"""


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_context().new_page()
        yield p
        b.close()


def _schema(page):
    page.set_content(_FORM)
    obs = collect_browser_observation(page)
    return fe.parse_observation(obs, company="acme", url="https://boards.greenhouse.io/acme/jobs/1")


def test_assigns_semantic_keys_from_promoted_taxonomy(page):
    schema = _schema(page)
    by_key = {f.semantic_key: f for s in schema.steps for f in s.fields}
    assert "first_name" in by_key and "email" in by_key
    assert "location" in by_key and "resume" in by_key
    assert "work_auth" in by_key                      # promoted work_authorization synonyms


def test_custom_question_gets_custom_key_not_none(page):
    schema = _schema(page)
    cust = [f for s in schema.steps for f in s.fields
            if f.semantic_key and f.semantic_key.startswith("custom.")]
    assert len(cust) == 1
    assert "product" in cust[0].question_text.lower()
    # front-end can't read maxlength from the observation; the oracle length-clamps
    # text answers (§6.5) and the textarea driver clamps only when char_limit is
    # explicitly set.
    assert cust[0].char_limit is None


def test_widget_kinds_classified(page):
    schema = _schema(page)
    kinds = {f.semantic_key: f.widget.kind for s in schema.steps for f in s.fields}
    assert kinds["first_name"] == "text"
    assert kinds["email"] == "text"
    assert kinds["resume"] == "file"
    assert kinds["work_auth"] == "react_select"       # select__control -> react_select
    cust_kind = [f.widget.kind for s in schema.steps for f in s.fields
                 if f.semantic_key.startswith("custom.")][0]
    assert cust_kind == "textarea"


def test_options_are_lazy_never_enumerated_at_parse(page):
    schema = _schema(page)
    for s in schema.steps:
        for f in s.fields:
            if f.widget.kind in ("react_select", "native_select"):
                assert f.options is ir.LAZY          # invariant 4: never opened at parse


def test_required_flags_and_locator_spec_seeded(page):
    schema = _schema(page)
    by_key = {f.semantic_key: f for s in schema.steps for f in s.fields}
    assert by_key["first_name"].required is True
    assert by_key["location"].required is False
    # locator_spec is a dict usable to build a healing.ElementSpec at fill time
    ls = by_key["first_name"].locator_spec
    assert ls.get("label") and (ls.get("elem_id") or ls.get("name_attr"))


def test_terminal_step_and_advance_control(page):
    schema = _schema(page)
    assert len(schema.steps) == 1
    assert schema.steps[0].terminal is True           # single-step GH form
    assert schema.steps[0].advance_control is not None  # the Submit button locator_spec


def test_canary_keys_flagged_on_schema(page):
    schema = _schema(page)
    wa = [f for s in schema.steps for f in s.fields if f.semantic_key == "work_auth"][0]
    assert ir.is_canary_key(wa.semantic_key) is True  # resolver will not oracle it


def test_native_select_classified_not_react_select(page):
    # A native <select> reports control_type="select" AND role="combobox"; the
    # native-select-first ordering in _widget_kind MUST win over the react_select
    # role check, else the executor would drive it as a react-select combobox.
    schema = _schema(page)
    kinds = {f.semantic_key: f.widget.kind for s in schema.steps for f in s.fields}
    assert kinds["eeo.gender"] == "native_select"
    assert kinds["eeo.gender_identity"] == "native_select"
    # invariant 4 still holds for native selects (options never enumerated at parse)
    by_key = {f.semantic_key: f for s in schema.steps for f in s.fields}
    assert by_key["eeo.gender"].options is ir.LAZY
    assert by_key["eeo.gender_identity"].options is ir.LAZY


def test_eeo_specific_key_beats_generic_gender(page):
    # "Gender identity" must resolve to the specific eeo.gender_identity key and
    # not be hijacked by the generic "gender" synonym (most-specific-first order).
    # If the ordering were reversed both labels would collapse onto eeo.gender and
    # eeo.gender_identity would be absent.
    schema = _schema(page)
    by_key = {f.semantic_key: f for s in schema.steps for f in s.fields}
    assert "eeo.gender_identity" in by_key
    assert "eeo.gender" in by_key
    assert by_key["eeo.gender_identity"].label_text.lower() == "gender identity"
    assert by_key["eeo.gender"].label_text.lower() == "gender"
