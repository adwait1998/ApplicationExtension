"""Live-blocker fix (2026-07-24, Twilio): Greenhouse's Location (City) field is
a react-select combobox backed by ASYNC remote options (city autocomplete).
Typing free text does NOT persist — React drops it on blur, then client-side
validation blocks submit (`validation_location_persist`). A naive keyboard flow
(blind ArrowDown+Enter) also mis-selected a WRONG city (a Venezuela row).

These tests exercise the shared async-combobox dance
(`applypilot.apply.combobox.select_async_combobox_option`) and its two call
sites (`prefill._fill_greenhouse_location_async`,
`stream_executor._select_value`) against a synthetic react-select whose options
populate ~300ms after typing, with a DECOY first row ("Valencia, Venezuela") and
the real target ("San Francisco, California, United States"). Selecting sets a
hidden value + a rendered chip; free text clears on blur (React-controlled
reset). No network / ATS / LLM — Playwright on a data: page only.
"""
from __future__ import annotations

import contextlib

import pytest

from applypilot.apply.combobox import (
    _commit_by_keyboard,
    select_async_combobox_option,
)
from applypilot.apply.prefill import _fill_greenhouse_location_async, _fill_text_locator
from applypilot.apply.stream_executor import execute_stream_actions_on_page


# Synthetic async react-select. Typing schedules a 300ms async populate of a
# listbox whose FIRST row is a decoy. Clicking an option commits: sets the
# hidden value + the `.select__single-value` chip and clears the query text
# (mirrors react-select). Blur clears any uncommitted free text.
_ASYNC_LOCATION_FORM = """
<!doctype html><html><body>
<form onsubmit="event.preventDefault()">
  <label for="candidate-location">Location (City) *</label>
  <div class="select__control" id="ctl">
    <span class="select__single-value" id="chip"></span>
    <input id="candidate-location" role="combobox" aria-invalid="true"
           autocomplete="off" value="">
    <input type="hidden" id="loc_hidden" name="location" value="">
  </div>
  <div id="menu" role="listbox" style="display:none"></div>
<script>
  const input = document.getElementById('candidate-location');
  const menu = document.getElementById('menu');
  const chip = document.getElementById('chip');
  const hidden = document.getElementById('loc_hidden');
  // Decoy FIRST — a blind ArrowDown+Enter would wrongly pick this.
  const OPTIONS = [
    "Valencia, Venezuela",
    "San Francisco, California, United States",
    "San Jose, California, United States"
  ];
  let timer = null;
  function populate() {
    menu.innerHTML = '';
    OPTIONS.forEach(text => {
      const o = document.createElement('div');
      o.setAttribute('role', 'option');
      o.className = 'select__option';
      o.textContent = text;
      o.style.padding = '4px';
      o.addEventListener('mousedown', e => e.preventDefault());
      o.addEventListener('click', () => {
        chip.textContent = text;
        hidden.value = text;
        input.value = '';                     // react clears the query on select
        input.setAttribute('aria-invalid', 'false');
        menu.style.display = 'none';
      });
      menu.appendChild(o);
    });
    menu.style.display = 'block';
  }
  input.addEventListener('input', () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(populate, 300);        // async: options ~300ms later
  });
  input.addEventListener('blur', () => {
    if (!hidden.value) input.value = '';       // React-controlled reset
  });
</script>
</form></body></html>
"""


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        p = ctx.new_page()
        p.set_content(_ASYNC_LOCATION_FORM)
        yield p
        browser.close()


TARGET = "San Francisco, California, United States"


# --- shared helper directly ------------------------------------------------

def test_dance_picks_correct_option_not_decoy_first(page):
    """The dance must select San Francisco, NOT the decoy first row."""
    loc = page.locator("#candidate-location").first
    selected = select_async_combobox_option(
        page, loc, "San Francisco", ("San Francisco, California", "San Francisco"),
    )
    assert selected == TARGET
    # Committed to the hidden value + chip — NOT "Valencia, Venezuela".
    assert page.locator("#loc_hidden").input_value() == TARGET
    assert page.locator("#chip").inner_text() == TARGET


def test_read_back_reflects_selected_value_not_typed_text(page):
    """Read-back returns the committed SELECTED value; the typed query is
    cleared by the widget (so a text read-back would be empty)."""
    loc = page.locator("#candidate-location").first
    selected = select_async_combobox_option(
        page, loc, "San Francisco", ("San Francisco",),
    )
    assert selected == TARGET
    assert page.locator("#candidate-location").input_value() == ""  # query cleared
    assert "venezuela" not in page.locator("#chip").inner_text().lower()


def test_no_matching_option_reports_unfilled_no_fake_success(page):
    """Options are present but none match -> None (never blind-pick), and
    nothing is committed (no fake success)."""
    loc = page.locator("#candidate-location").first
    selected = select_async_combobox_option(
        page, loc, "Atlantis", ("Atlantis, Oceania", "Atlantis"),
    )
    assert selected is None
    assert page.locator("#loc_hidden").input_value() == ""
    assert page.locator("#chip").inner_text() == ""


# --- prefill call site: _fill_greenhouse_location_async --------------------

def test_prefill_location_helper_commits_correct_city(page):
    selected = _fill_greenhouse_location_async(page, "San Francisco", "California")
    assert selected == TARGET
    assert page.locator("#loc_hidden").input_value() == TARGET


def test_prefill_location_helper_unfilled_when_no_match(page):
    selected = _fill_greenhouse_location_async(page, "Atlantis", "Oceania")
    assert selected is None
    assert page.locator("#loc_hidden").input_value() == ""


# --- executor call site: _select_value via execute_stream_actions_on_page --

def test_executor_select_action_exercises_shared_dance(page):
    """The stream executor's `select` on a remote-options combobox drives the
    same shared dance and lands on the correct option, not the decoy."""
    result = execute_stream_actions_on_page(
        page,
        [{"action": "select", "label": "Location", "value": "San Francisco"}],
    )
    assert result["ok"] is True
    assert page.locator("#loc_hidden").input_value() == TARGET
    assert page.locator("#chip").inner_text() == TARGET


# --- plain (non-combobox) text inputs keep today's behavior ----------------

_PLAIN_FORM = """
<!doctype html><html><body>
  <label for="fn">First name</label>
  <input id="fn" name="first_name" value="">
</body></html>
"""


def test_plain_text_input_prefill_fill_unchanged():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        p = browser.new_context().new_page()
        p.set_content(_PLAIN_FORM)
        # _fill_text_locator is the plain-input path — must behave byte-identically.
        assert _fill_text_locator(p, "#fn", "Nida") is True
        assert p.locator("#fn").input_value() == "Nida"
        browser.close()


def test_plain_text_input_executor_fill_unchanged():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        p = browser.new_context().new_page()
        p.set_content(_PLAIN_FORM)
        result = execute_stream_actions_on_page(
            p, [{"action": "fill", "selector": "#fn", "value": "Nida Shah"}],
        )
        assert result["ok"] is True
        assert p.locator("#fn").input_value() == "Nida Shah"
        browser.close()


# --- keyboard fallback (react-select desync: option click does NOT commit) ---
#
# In this widget the option CLICK handler is deliberately swallowed (mirrors the
# live Greenhouse desync), so `_commit_by_click` always fails and the KEYBOARD
# fallback must take over: type the target -> await the filtered list -> verify
# the first option matches the target -> ArrowDown+Enter commits. The decoy
# ("San Salvador...") shares the "San" prefix so it competes on a short query.


def _desync_form(*, commit_on_click: bool, filtered: bool) -> str:
    return (
        _DESYNC_FORM_TEMPLATE
        .replace("__COMMIT_ON_CLICK__", "true" if commit_on_click else "false")
        .replace("__FILTERED__", "true" if filtered else "false")
    )


_DESYNC_FORM_TEMPLATE = """
<!doctype html><html><body>
<form onsubmit="event.preventDefault()">
  <label for="candidate-location">Location (City) *</label>
  <div class="select__control" id="ctl">
    <span class="select__single-value" id="chip"></span>
    <input id="candidate-location" role="combobox" aria-invalid="true"
           autocomplete="off" value="">
    <input type="hidden" id="loc_hidden" name="location" value="">
  </div>
  <div id="menu" role="listbox" style="display:none"></div>
<script>
  const COMMIT_ON_CLICK = __COMMIT_ON_CLICK__;
  const FILTERED = __FILTERED__;
  const input = document.getElementById('candidate-location');
  const menu = document.getElementById('menu');
  const chip = document.getElementById('chip');
  const hidden = document.getElementById('loc_hidden');
  const OPTIONS = [
    "San Salvador, El Salvador",
    "San Francisco, California, United States",
    "San Jose, California, United States"
  ];
  let timer = null, highlighted = null;
  function commit(text) {
    chip.textContent = text; hidden.value = text; input.value = '';
    input.setAttribute('aria-invalid', 'false'); menu.style.display = 'none';
  }
  function populate() {
    menu.innerHTML = ''; highlighted = null;
    const q = FILTERED ? input.value.trim().toLowerCase() : '';
    const list = OPTIONS.filter(t => !q || t.toLowerCase().includes(q));
    list.forEach(text => {
      const o = document.createElement('div');
      o.setAttribute('role', 'option'); o.className = 'select__option';
      o.textContent = text; o.style.padding = '4px';
      o.addEventListener('mousedown', e => e.preventDefault());
      // DESYNC: the click is swallowed unless COMMIT_ON_CLICK.
      o.addEventListener('click', () => { if (COMMIT_ON_CLICK) commit(text); });
      menu.appendChild(o);
    });
    highlighted = menu.querySelector('[role=option]');
    menu.style.display = list.length ? 'block' : 'none';
  }
  input.addEventListener('input', () => {
    if (timer) clearTimeout(timer);
    // A changed query invalidates the old results: react-select clears the
    // menu and re-fetches (no stale options linger from the prior query).
    menu.style.display = 'none'; menu.innerHTML = ''; highlighted = null;
    timer = setTimeout(populate, 300);
  });
  // Keyboard commits properly (react-select's own handlers): ArrowDown
  // highlights the first filtered option, Enter commits it.
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (!highlighted) highlighted = menu.querySelector('[role=option]');
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (highlighted) commit(highlighted.textContent);
    }
  });
  input.addEventListener('blur', () => { if (!hidden.value) input.value = ''; });
</script>
</form></body></html>
"""


@contextlib.contextmanager
def _page_with(html: str):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        p = browser.new_context().new_page()
        p.set_content(html)
        try:
            yield p
        finally:
            browser.close()


def test_keyboard_fallback_recovers_correct_option_when_click_desyncs():
    """End-to-end: option click is swallowed (desync), so the shared dance must
    fall through to the keyboard path and still commit the CORRECT (non-decoy)
    option."""
    with _page_with(_desync_form(commit_on_click=False, filtered=True)) as p:
        loc = p.locator("#candidate-location").first
        selected = select_async_combobox_option(
            p, loc, "San", ("San Francisco, California", "San Francisco"),
        )
        assert selected == TARGET
        assert p.locator("#loc_hidden").input_value() == TARGET
        assert "salvador" not in p.locator("#chip").inner_text().lower()


def test_commit_by_keyboard_selects_correct_option_directly():
    """`_commit_by_keyboard` in isolation: re-filters to the target and commits
    it via ArrowDown+Enter (proving the fallback function itself works)."""
    with _page_with(_desync_form(commit_on_click=False, filtered=True)) as p:
        loc = p.locator("#candidate-location").first
        loc.click()
        committed = _commit_by_keyboard(p, loc, TARGET, timeout_ms=5000, settle_ms=350)
        assert committed == TARGET
        assert p.locator("#loc_hidden").input_value() == TARGET


def test_commit_by_keyboard_refuses_when_first_option_is_not_target():
    """Decoy-mismatch guard: when the widget does NOT filter (the first option
    stays the decoy that does not match the target), the keyboard path must
    REFUSE (no ArrowDown+Enter, nothing committed) rather than blind-pick."""
    with _page_with(_desync_form(commit_on_click=False, filtered=False)) as p:
        loc = p.locator("#candidate-location").first
        loc.click()
        committed = _commit_by_keyboard(p, loc, TARGET, timeout_ms=2500, settle_ms=350)
        assert committed is None
        assert p.locator("#loc_hidden").input_value() == ""
        assert p.locator("#chip").inner_text() == ""
