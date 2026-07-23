import pytest

from applypilot.apply.v2 import drivers
from applypilot.apply.v2 import ir
from applypilot.apply.v2.resolver import PlannedField, build_element_spec


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
    pdf = tmp_path / "r.pdf"; pdf.write_bytes(b"%PDF-1.4 x")
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
