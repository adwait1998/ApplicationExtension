import pytest

from applypilot.apply.v2 import drivers
from applypilot.apply.v2 import ir
from applypilot.apply.v2.resolver import PlannedField


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_context().new_page()
        yield p
        b.close()


_TEXT = """<!doctype html><body><form>
  <label for="fn">First name</label>
  <input id="fn" name="first_name" type="text" required>
  <label for="rz">Resume/CV</label>
  <input id="rz" name="resume" type="file">
</form></body>"""

# textarea with an explicit driver-side char_limit (front-end never sets it —
# maxlength is not in the observation — so this exercises the driver clamp path).
_TA = """<!doctype html><body><form>
  <label for="cl">Cover letter</label>
  <textarea id="cl" name="cover"></textarea>
</form></body>"""

# react-select-ish widget (same wiring as test_greenhouse_adapter._FORM)
_RS = """<!doctype html><body><form>
  <label for="wa-i">Are you legally authorized to work?</label>
  <div class="select__control" id="wa-c" tabindex="0">
    <span class="select__single-value" id="wa-v">Select...</span>
    <input class="select__input" id="wa-i" role="combobox" autocomplete="off">
  </div>
  <div class="select__menu" id="wa-m" style="display:none">
    <div class="select__option">Yes, I am authorized to work in the US</div>
    <div class="select__option">No, I require sponsorship</div>
  </div>
<script>
  const c=wa_c=document.getElementById('wa-c'),i=document.getElementById('wa-i'),
        m=document.getElementById('wa-m'),v=document.getElementById('wa-v');
  let hi=null; const open=()=>m.style.display='block';
  c.addEventListener('mousedown',open); c.addEventListener('click',open); i.addEventListener('focus',open);
  i.addEventListener('input',()=>{const q=i.value.toLowerCase();
    hi=[...m.querySelectorAll('.select__option')].find(o=>q&&o.textContent.toLowerCase().includes(q))||null; open();});
  i.addEventListener('keydown',e=>{if(e.key==='ArrowDown'){e.preventDefault(); if(!hi) hi=m.querySelector('.select__option');}
    else if(e.key==='Enter'){e.preventDefault(); if(hi){v.textContent=hi.textContent; i.value=hi.textContent; m.style.display='none';}}});
  m.querySelectorAll('.select__option').forEach(o=>o.addEventListener('click',()=>{v.textContent=o.textContent; m.style.display='none';}));
</script></form></body>"""


def _field(sem, kind, label, options=ir.LAZY, fid="fn", char_limit=None):
    return ir.Field(field_id=fid, frame_path=(), label_text=label, question_text=label,
                    semantic_key=sem, widget=ir.Widget(kind=kind), options=options,
                    required=True, char_limit=char_limit)


def test_text_driver_reads_back_committed(page):
    page.set_content(_TEXT)
    f = _field("first_name", "text", "First name", fid="fn")
    pf = PlannedField(f, value="Nida", driver="text")
    res = drivers.commit(page, pf)
    assert res.committed is True
    assert page.locator("#fn").input_value() == "Nida"
    assert res.locator_tier                                # a healing tier is reported


def test_text_driver_reports_not_committed_on_readback_mismatch(page, monkeypatch):
    page.set_content(_TEXT)
    f = _field("first_name", "text", "First name", fid="fn")
    pf = PlannedField(f, value="Nida", driver="text")
    # Force the fill to silently no-op so read-back != intended -> committed False.
    monkeypatch.setattr(drivers, "_do_fill", lambda loc, val, **k: None)
    res = drivers.commit(page, pf)
    assert res.committed is False                          # never trusts click/fill success


def test_text_driver_value_none_not_committed(page):
    # KNOWN INPUT CONDITION: resolver may emit value=None for an optional field
    # with no profile data. The driver must return not-committed, never blind-fill
    # the literal "None" (and never crash).
    page.set_content(_TEXT)
    f = _field("first_name", "text", "First name", fid="fn")
    pf = PlannedField(f, value=None, driver="text")
    res = drivers.commit(page, pf)
    assert res.committed is False
    assert page.locator("#fn").input_value() == ""        # nothing was typed


def test_textarea_driver_truncates_to_char_limit(page):
    page.set_content(_TA)
    long = "x" * 600
    f = _field("custom.cover", "textarea", "Cover letter", fid="cl", char_limit=500)
    pf = PlannedField(f, value=long, driver="textarea")
    res = drivers.commit(page, pf)
    assert res.committed is True                           # read-back == the clamped value
    got = page.locator("#cl").input_value()
    assert len(got) == 500                                 # driver-side char_limit clamp fired
    assert got == "x" * 500


def test_file_driver_uploads_and_reads_back(page, tmp_path):
    page.set_content(_TEXT)
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    f = _field("resume", "file", "Resume/CV", fid="rz")
    pf = PlannedField(f, value=str(pdf), driver="file")
    res = drivers.commit(page, pf)
    assert res.committed is True                           # filename-chip / input value present


def test_react_select_lazy_enumerates_only_at_commit(page):
    page.set_content(_RS)
    f = _field("work_auth", "react_select", "Are you legally authorized to work?", options=ir.LAZY)
    pf = PlannedField(f, option_intent="yes", driver="react_select")
    assert f.options is ir.LAZY                            # still lazy going in
    res = drivers.commit(page, pf)
    assert res.committed is True
    assert page.locator("#wa-v").inner_text().lower().startswith("yes")


def test_react_select_readback_catches_desync(page):
    # Simulate the Chime/Robinhood desync: option click does NOT update the
    # single-value; the driver must fall back to keyboard AND read-back-verify.
    desync = _RS.replace(
        "o.addEventListener('click',()=>{v.textContent=o.textContent; m.style.display='none';})",
        "o.addEventListener('click',()=>{ m.style.display='none'; })")  # click no-ops the commit
    page.set_content(desync)
    f = _field("work_auth", "react_select", "Are you legally authorized to work?", options=ir.LAZY)
    pf = PlannedField(f, option_intent="yes", driver="react_select")
    res = drivers.commit(page, pf)
    assert res.committed is True                           # keyboard fallback committed it
    assert page.locator("#wa-v").inner_text().lower().startswith("yes")


def test_react_select_no_matching_option_not_committed(page):
    page.set_content(_RS)
    f = _field("work_auth", "react_select", "Are you legally authorized to work?", options=ir.LAZY)
    pf = PlannedField(f, option_intent="maybe someday", driver="react_select")  # no real match
    res = drivers.commit(page, pf)
    assert res.committed is False                          # never blind-types a non-option


# --- ASYNC react-select (GH Location typeahead) — live gap #1 (Task 8) ---------
# The react_select driver now runs the SHARED combobox dance, so an ASYNC remote-
# options combobox (Greenhouse Location: options ~300ms after typing, a DECOY
# first row) commits the CORRECT option with a genuine read-back — the live
# `validation_location_persist` blocker (committed:false ×2 on Twilio). Mirrors
# the fixture in test_location_async_combobox.
_ASYNC_LOC = """<!doctype html><body><form onsubmit="event.preventDefault()">
  <label for="candidate-location">Location (City)</label>
  <div class="select__control" id="ctl">
    <span class="select__single-value" id="chip"></span>
    <input id="candidate-location" role="combobox" aria-invalid="true" autocomplete="off" value="">
    <input type="hidden" id="loc_hidden" name="location" value="">
  </div>
  <div id="menu" role="listbox" style="display:none"></div>
<script>
  const input=document.getElementById('candidate-location'),menu=document.getElementById('menu'),
        chip=document.getElementById('chip'),hidden=document.getElementById('loc_hidden');
  const OPTIONS=["Valencia, Venezuela","San Francisco, California, United States","San Jose, California, United States"];
  let timer=null;
  function populate(){menu.innerHTML='';const q=input.value.trim().toLowerCase();
    OPTIONS.filter(t=>!q||t.toLowerCase().includes(q)).forEach(text=>{
      const o=document.createElement('div');o.setAttribute('role','option');o.className='select__option';
      o.textContent=text;o.addEventListener('mousedown',e=>e.preventDefault());
      o.addEventListener('click',()=>{chip.textContent=text;hidden.value=text;input.value='';
        input.setAttribute('aria-invalid','false');menu.style.display='none';});
      menu.appendChild(o);});
    menu.style.display='block';}
  input.addEventListener('input',()=>{if(timer)clearTimeout(timer);
    menu.style.display='none';menu.innerHTML='';timer=setTimeout(populate,300);});
  input.addEventListener('blur',()=>{if(!hidden.value)input.value='';});
</script></form></body>"""


def test_react_select_async_location_commits_correct_option(page):
    # GH location arrives as a react_select with value from profile.personal.city.
    page.set_content(_ASYNC_LOC)
    f = _field("location", "react_select", "Location (City)", fid="candidate-location")
    pf = PlannedField(f, value="San Francisco, California",
                      option_intent="san francisco, california", driver="react_select")
    res = drivers.commit(page, pf)
    assert res.committed is True                           # async options awaited, real pick
    assert page.locator("#loc_hidden").input_value() == "San Francisco, California, United States"
    assert "venezuela" not in page.locator("#chip").inner_text().lower()  # never the decoy


def test_react_select_async_location_unfilled_when_no_match(page):
    # An intent that matches no rendered async option must report UNFILLED, never
    # a fake commit (require_option=True — React drops free text on blur).
    page.set_content(_ASYNC_LOC)
    f = _field("location", "react_select", "Location (City)", fid="candidate-location")
    pf = PlannedField(f, value="Atlantis", option_intent="atlantis", driver="react_select")
    res = drivers.commit(page, pf)
    assert res.committed is False
    assert page.locator("#loc_hidden").input_value() == ""


# --- radio_group / checkbox / date drivers (close the Task 6 registry gap) -----

# A labeled radio group with 3 labeled options. Association is MIXED on purpose:
# wa-yes uses label[for], wa-no is a WRAPPING label, wa-na uses label[for]. The
# non-first correct answer (wa-no) proves the driver never blind-clicks option 0.
_RADIO = """<!doctype html><body><form>
  <fieldset id="wa-group">
    <legend>Are you authorized to work in the US?</legend>
    <label for="wa-yes">Yes, authorized</label>
    <input type="radio" id="wa-yes" name="work_auth" value="yes">
    <label>No, I need sponsorship
      <input type="radio" id="wa-no" name="work_auth" value="no">
    </label>
    <label for="wa-na">Prefer not to say</label>
    <input type="radio" id="wa-na" name="work_auth" value="na">
  </fieldset>
</form></body>"""

# Regression (review-critical): prefix/substring-sharing option groups where the
# WRONG shorter option appears FIRST. Exact-match priority must beat a naive
# first-substring find — else intent 'none' commits 'No', and the long
# 'No, I require sponsorship…' intent commits the bare 'No'.
_RADIO_PREFIX = """<!doctype html><body><form>
  <fieldset id="spon-group">
    <legend>Sponsorship</legend>
    <label for="sp-no">No</label>
    <input type="radio" id="sp-no" name="spon" value="no">
    <label for="sp-none">None</label>
    <input type="radio" id="sp-none" name="spon" value="none">
    <label for="sp-full">No, I require sponsorship now or in the future</label>
    <input type="radio" id="sp-full" name="spon" value="full">
  </fieldset>
</form></body>"""

# Prefix-sharing 'Yes' / 'Yes, with conditions' — exact 'Yes' must not commit the
# longer prefixed option that appears later.
_RADIO_YES = """<!doctype html><body><form>
  <fieldset id="cond-group">
    <legend>Conditions</legend>
    <label for="y-yes">Yes</label>
    <input type="radio" id="y-yes" name="cond" value="yes">
    <label for="y-cond">Yes, with conditions</label>
    <input type="radio" id="y-cond" name="cond" value="cond">
  </fieldset>
</form></body>"""

# A single labeled consent checkbox (unchecked / pre-checked variants).
_CHECK = """<!doctype html><body><form>
  <label for="consent">I agree to the terms and conditions</label>
  <input type="checkbox" id="consent" name="consent">
</form></body>"""
_CHECK_ON = _CHECK.replace('name="consent">', 'name="consent" checked>')

# A native date input AND a plain text date input.
_DATE = """<!doctype html><body><form>
  <label for="start">Available start date</label>
  <input id="start" name="start_date" type="date">
  <label for="startx">Start date (free text)</label>
  <input id="startx" name="start_text" type="text">
</form></body>"""


def test_radio_group_checks_matching_option_only(page):
    page.set_content(_RADIO)
    f = _field("work_auth", "radio_group", "Are you authorized to work in the US?", fid="wa-group")
    # Intent is a case-insensitive substring of the WRAPPING-label option (not the
    # first radio) — proves substring match + wrapping-label association.
    pf = PlannedField(f, option_intent="I need sponsorship", driver="radio_group")
    res = drivers.commit(page, pf)
    assert res.committed is True
    assert page.locator("#wa-no").is_checked() is True     # the matched radio
    assert page.locator("#wa-yes").is_checked() is False    # siblings untouched
    assert page.locator("#wa-na").is_checked() is False
    assert res.locator_tier


def test_radio_group_no_match_checks_nothing(page):
    page.set_content(_RADIO)
    f = _field("work_auth", "radio_group", "Are you authorized to work in the US?", fid="wa-group")
    pf = PlannedField(f, option_intent="retired abroad", driver="radio_group")  # matches no label
    res = drivers.commit(page, pf)
    assert res.committed is False                          # invariant 6: never click a wrong option
    assert page.locator("#wa-yes").is_checked() is False
    assert page.locator("#wa-no").is_checked() is False
    assert page.locator("#wa-na").is_checked() is False


def test_radio_group_exact_beats_prefix_shorter_first(page):
    # 'none' shares a substring with the FIRST option 'No'; the exact 'None' comes
    # later. Exact-match priority must win so the RIGHT radio is checked.
    page.set_content(_RADIO_PREFIX)
    f = _field("sponsorship", "radio_group", "Sponsorship", fid="spon-group")
    pf = PlannedField(f, option_intent="none", driver="radio_group")
    res = drivers.commit(page, pf)
    assert res.committed is True
    assert page.locator("#sp-none").is_checked() is True    # exact 'None', not first 'No'
    assert page.locator("#sp-no").is_checked() is False
    assert page.locator("#sp-full").is_checked() is False


def test_radio_group_full_intent_not_captured_by_bare_prefix(page):
    # The full sponsorship phrase must commit its exact option, not the bare 'No'
    # that appears first and is a substring of the intent.
    page.set_content(_RADIO_PREFIX)
    f = _field("sponsorship", "radio_group", "Sponsorship", fid="spon-group")
    pf = PlannedField(f, option_intent="No, I require sponsorship now or in the future",
                      driver="radio_group")
    res = drivers.commit(page, pf)
    assert res.committed is True
    assert page.locator("#sp-full").is_checked() is True
    assert page.locator("#sp-no").is_checked() is False
    assert page.locator("#sp-none").is_checked() is False


def test_radio_group_exact_yes_not_prefixed_variant(page):
    page.set_content(_RADIO_YES)
    f = _field("conditions", "radio_group", "Conditions", fid="cond-group")
    pf = PlannedField(f, option_intent="Yes", driver="radio_group")
    res = drivers.commit(page, pf)
    assert res.committed is True
    assert page.locator("#y-yes").is_checked() is True      # exact 'Yes'
    assert page.locator("#y-cond").is_checked() is False    # not 'Yes, with conditions'


def test_radio_group_no_intent_not_committed(page):
    page.set_content(_RADIO)
    f = _field("work_auth", "radio_group", "Are you authorized to work in the US?", fid="wa-group")
    pf = PlannedField(f, option_intent=None, driver="radio_group")
    res = drivers.commit(page, pf)
    assert res.committed is False
    assert "intent" in (res.error or "")                   # error indicates missing intent


def test_checkbox_yes_intent_checks(page):
    page.set_content(_CHECK)
    f = _field("custom.consent", "checkbox", "I agree to the terms and conditions", fid="consent")
    pf = PlannedField(f, option_intent="agree", driver="checkbox")
    res = drivers.commit(page, pf)
    assert res.committed is True
    assert page.locator("#consent").is_checked() is True


def test_checkbox_no_intent_unchecks_prechecked(page):
    page.set_content(_CHECK_ON)
    assert page.locator("#consent").is_checked() is True   # starts checked
    f = _field("custom.consent", "checkbox", "I agree to the terms and conditions", fid="consent")
    pf = PlannedField(f, option_intent="decline", driver="checkbox")
    res = drivers.commit(page, pf)
    assert res.committed is True
    assert page.locator("#consent").is_checked() is False


def test_checkbox_ambiguous_intent_leaves_state(page):
    page.set_content(_CHECK)
    f = _field("custom.consent", "checkbox", "I agree to the terms and conditions", fid="consent")
    pf = PlannedField(f, option_intent="maybe", driver="checkbox")  # neither yes-ish nor no-ish
    res = drivers.commit(page, pf)
    assert res.committed is False                          # invariant 6: never guess
    assert page.locator("#consent").is_checked() is False  # state unchanged


def test_date_native_iso_value_commits(page):
    page.set_content(_DATE)
    f = _field("custom.start_date", "date", "Available start date", fid="start")
    pf = PlannedField(f, value="2026-07-23", driver="date")
    res = drivers.commit(page, pf)
    assert res.committed is True
    assert page.locator("#start").input_value() == "2026-07-23"


def test_date_native_normalizes_non_iso(page):
    page.set_content(_DATE)
    f = _field("custom.start_date", "date", "Available start date", fid="start")
    pf = PlannedField(f, value="07/23/2026", driver="date")   # US M/D/Y -> ISO for type=date
    res = drivers.commit(page, pf)
    assert res.committed is True
    assert page.locator("#start").input_value() == "2026-07-23"


def test_date_text_input_filled_verbatim(page):
    page.set_content(_DATE)
    f = _field("custom.start_date", "date", "Start date (free text)", fid="startx")
    pf = PlannedField(f, value="07/23/2026", driver="date")   # plain text -> no normalization
    res = drivers.commit(page, pf)
    assert res.committed is True
    assert page.locator("#startx").input_value() == "07/23/2026"
