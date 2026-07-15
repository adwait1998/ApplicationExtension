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


# The two tests below cover the load-bearing _frame_path branch (embedded ATS
# forms in vanity-domain iframes). The single-form tests above only ever exercise
# top-frame controls, so nothing there distinguishes "gate on frame DEPTH" from
# the plausible-but-wrong "gate on frame_url truthiness" simplification.


def test_embedded_frame_field_stamps_stripped_path_top_stays_empty(page):
    # Render a top-frame control PLUS an iframe-embedded control served at a real
    # URL carrying a per-job ?token= query, then assert:
    #   - the top-frame field -> frame_path == ()  (its frame_url is a truthy
    #     'about:blank', so a gate on frame_url TRUTHINESS would wrongly stamp it;
    #     only a gate on frame DEPTH (frame_index) leaves it empty)
    #   - the embedded field -> frame_path == the query-STRIPPED origin+path, so
    #     recurring custom questions share a question_fp across jobs on a board
    #     (question_fp joins frame_path; an unstripped per-job token would break it)
    embed_url = "http://embed.greenhouse.test/forms/acme?token=perjob-123"
    iframe_html = (
        "<!doctype html><html><body>"
        "<label for='ph'>Phone</label>"
        "<input id='ph' name='phone' type='tel'>"
        "</body></html>"
    )
    top = (
        "<!doctype html><html><body>"
        "<form id='top'><label for='fn'>First name</label>"
        "<input id='fn' name='first_name' type='text' required></form>"
        f"<iframe id='emb' src='{embed_url}'></iframe>"
        "</body></html>"
    )
    page.context.route(
        "**/embed.greenhouse.test/**",
        lambda route: route.fulfill(status=200, content_type="text/html", body=iframe_html),
    )
    page.set_content(top)
    # Deterministic wait: the embedded input must be attached before we observe.
    page.frame_locator("#emb").locator("#ph").wait_for(state="attached", timeout=5000)
    obs = collect_browser_observation(page)
    schema = fe.parse_observation(obs, company="acme",
                                  url="https://boards.greenhouse.io/acme/jobs/1")
    by_label = {f.label_text.lower(): f for s in schema.steps for f in s.fields}
    # Guard: both controls present (a broken iframe load would drop "phone" and
    # let the top-frame assertion pass vacuously).
    assert "first name" in by_label and "phone" in by_label
    # Guard: the observation really placed the embedded control in a child frame.
    assert [c for c in obs.controls if c.label == "Phone"][0].frame_index > 0

    assert by_label["first name"].frame_path == ()   # top document, not stamped
    assert by_label["phone"].frame_path == ("http://embed.greenhouse.test/forms/acme",)


def test_frame_path_gates_on_depth_and_strips_query_and_fragment():
    # Direct unit coverage of the pure _frame_path branches (no browser).
    from applypilot.apply.browser_stream import ControlObservation
    # frame_index == 0 with a truthy per-job page URL -> top document, NOT stamped.
    top = ControlObservation(
        frame_index=0, frame_url="https://boards.greenhouse.io/acme/jobs/1?t=abc")
    assert fe._frame_path(top) == ()
    # Embedded frame: strip BOTH ?query and #fragment (the per-job token).
    emb = ControlObservation(
        frame_index=2, frame_url="https://apply.example.com/embed/form?token=xyz#sec")
    assert fe._frame_path(emb) == ("https://apply.example.com/embed/form",)
    # Embedded but no frame_url -> nothing to stamp.
    assert fe._frame_path(ControlObservation(frame_index=1, frame_url="")) == ()
