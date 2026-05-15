"""Iter-13 regression: react-select desync — portal-click reports success
but value never commits; keyboard path must commit it.

This reproduces the exact Chime/Robinhood failure with a synthetic widget
that mimics Greenhouse's react-select:
  - `.select__control` with an inner <input> (the keyboard target)
  - a portal of `.select__option` divs
  - clicking a portal option does NOT update `.select__single-value`
    (the desync — option visually "selected" via aria-live but React's
    controlled value never commits)
  - typing into the inner input + ArrowDown + Enter DOES commit
    (updates `.select__single-value` and clears aria-invalid)

So `_select_combobox_by_label` (portal-click) reports a false success,
`_combobox_committed` correctly returns False for it, and
`_select_combobox_robust` recovers via `_commit_combobox_keyboard`.

No real network / ATS / LLM — Playwright on a data: page only.
"""
from __future__ import annotations

import pytest

from applypilot.apply.prefill import (
    _combobox_committed,
    _commit_combobox_keyboard,
    _select_combobox_by_label,
    _select_combobox_robust,
)


# Synthetic Greenhouse-style react-select. The portal-click handler
# DELIBERATELY does not commit (mirrors the desync). The inner input's
# Enter handler DOES commit.
_DESYNC_FORM = """
<!doctype html><html><body>
<form>
  <label for="rs-input">Gender</label>
  <div class="select__control" id="ctl">
    <div class="select__single-value" id="sv"></div>
    <input id="rs-input" aria-invalid="true" autocomplete="off" value="">
  </div>
  <div id="menu" style="display:none">
    <div class="select__option" data-val="Decline to self-identify">Decline to self-identify</div>
    <div class="select__option" data-val="Male">Male</div>
    <div class="select__option" data-val="Female">Female</div>
  </div>
  <button type="submit" id="sub" disabled>Submit</button>
<script>
  const ctl = document.getElementById('ctl');
  const menu = document.getElementById('menu');
  const sv = document.getElementById('sv');
  const input = document.getElementById('rs-input');
  const sub = document.getElementById('sub');

  function openMenu() { menu.style.display = 'block'; }
  ctl.addEventListener('mousedown', openMenu);
  input.addEventListener('focus', openMenu);

  // DESYNC: clicking a portal option fires an aria-live announce but
  // does NOT update the committed value. This is the Greenhouse bug.
  menu.querySelectorAll('.select__option').forEach(opt => {
    opt.addEventListener('mousedown', e => { /* swallowed; no commit */ });
    opt.addEventListener('click', e => { /* swallowed; no commit */ });
  });

  // KEYBOARD path commits properly: type filters, ArrowDown highlights,
  // Enter commits the first matching option.
  let highlighted = null;
  input.addEventListener('input', () => {
    const q = input.value.toLowerCase();
    const m = Array.from(menu.querySelectorAll('.select__option'))
      .find(o => o.textContent.toLowerCase().includes(q) && q);
    highlighted = m || null;
    openMenu();
  });
  input.addEventListener('keydown', e => {
    // Real react-select calls preventDefault on these so the surrounding
    // <form> doesn't submit/reload on Enter. The synthetic widget must too,
    // otherwise pressing Enter navigates the page and the test is invalid.
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (!highlighted) highlighted = menu.querySelector('.select__option');
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (highlighted) {
        sv.textContent = highlighted.getAttribute('data-val');
        input.value = highlighted.getAttribute('data-val');
        input.setAttribute('aria-invalid', 'false');
        sub.disabled = false;
        menu.style.display = 'none';
      }
    }
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
        p.set_content(_DESYNC_FORM)
        yield p
        browser.close()


def test_portal_click_reports_false_success(page):
    """`_select_combobox_by_label` clicks the option and returns True even
    though nothing committed — this is the false-positive prefill was
    silently trusting (Chime/Robinhood EEO)."""
    clicked = _select_combobox_by_label(page, ("gender",), ("Decline to self-identify",))
    assert clicked is True  # it found + clicked an option
    # ...but it did NOT actually commit:
    assert _combobox_committed(page, ("gender",), ("Decline to self-identify",)) is False
    assert page.locator("#sv").inner_text() == ""
    assert page.locator("#sub").is_disabled()


def test_keyboard_path_commits(page):
    """`_commit_combobox_keyboard` drives the inner input and commits."""
    ok = _commit_combobox_keyboard(page, ("gender",), ("Decline to self-identify",))
    assert ok is True
    assert page.locator("#sv").inner_text() == "Decline to self-identify"
    assert page.locator("#sub").is_enabled()


def test_robust_recovers_from_desync(page):
    """End-to-end: the robust wrapper tries portal-click, detects the
    non-commit, falls back to keyboard, and ends verified-committed."""
    ok = _select_combobox_robust(page, ("gender",), ("Decline to self-identify",))
    assert ok is True
    assert _combobox_committed(page, ("gender",), ("Decline to self-identify",)) is True
    assert page.locator("#sv").inner_text() == "Decline to self-identify"
    assert page.locator("#sub").is_enabled()


def test_robust_returns_false_when_field_absent(page):
    """No matching label → robust returns False (no crash, no false claim)."""
    ok = _select_combobox_robust(page, ("nonexistent field",), ("whatever",))
    assert ok is False
