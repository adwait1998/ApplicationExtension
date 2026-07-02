from __future__ import annotations

from pathlib import Path

import pytest

from applypilot.apply.browser_stream import collect_browser_observation
from applypilot.apply.launcher import (
    _allowed_tools_arg,
    _disallowed_tools_arg,
    _make_mcp_config,
)
from applypilot.apply.stream_executor import (
    execute_stream_actions_on_page,
    observation_for_claude,
)


_FORM = """
<!doctype html>
<html>
  <head><title>Stream Apply</title></head>
  <body>
    <form onsubmit="event.preventDefault(); document.body.dataset.submitted='yes'; document.body.innerHTML='<p>Thank you for applying</p>';">
      <label for="first">First name *</label>
      <input id="first" name="first_name" required>

      <label for="email">Email *</label>
      <input id="email" name="email" type="email" required>

      <label for="level">Level *</label>
      <select id="level" name="level" required>
        <option value="">Select...</option>
        <option value="senior">Senior</option>
      </select>

      <label for="remote">Open to remote</label>
      <input id="remote" name="remote" type="checkbox">

      <label for="resume">Resume/CV</label>
      <input id="resume" name="resume" type="file">

      <label for="country">Country *</label>
      <input id="country" role="combobox" aria-label="Country" required>

      <button id="next" type="button" onclick="document.body.dataset.next='yes'">Continue</button>
      <button id="apply" type="button" onclick="document.body.dataset.apply='yes'">Apply for this role</button>
      <button id="submit" type="submit">Submit application</button>
    </form>
  </body>
</html>
"""


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        p = browser.new_context().new_page()
        p.set_content(_FORM)
        yield p
        browser.close()


def _control_id(page, label: str) -> str:
    obs = collect_browser_observation(page)
    for control in [*obs.controls, *obs.submit_buttons]:
        if label.lower() in control.label.lower():
            return control.control_id
    raise AssertionError(f"control not found: {label}")


def test_stream_executor_fills_by_control_id_and_selector(page):
    first_id = _control_id(page, "First name")

    result = execute_stream_actions_on_page(
        page,
        [
            {"action": "fill", "control_id": first_id, "value": "Nida"},
            {"action": "fill", "selector": "#email", "value": "nida@example.com"},
        ],
    )

    assert result["ok"] is True
    assert page.locator("#first").input_value() == "Nida"
    assert page.locator("#email").input_value() == "nida@example.com"
    values = {c["selector"]: c["value"] for c in result["observation"]["controls"]}
    assert values["#first"] == "Nida"


def test_stream_executor_selects_checks_uploads_and_clicks(page, tmp_path: Path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")

    result = execute_stream_actions_on_page(
        page,
        [
            {"action": "select", "label": "Level", "value": "Senior"},
            {"action": "check", "label": "Open to remote"},
            {"action": "upload", "label": "Resume", "value": str(resume)},
            {"action": "click", "label": "Continue"},
        ],
    )

    assert result["ok"] is True
    assert page.locator("#level").input_value() == "senior"
    assert page.locator("#remote").is_checked() is True
    assert page.locator("#resume").evaluate("el => el.files[0].name") == "resume.pdf"
    assert page.evaluate("document.body.dataset.next") == "yes"


def test_stream_executor_selects_combobox_with_keyboard(page):
    result = execute_stream_actions_on_page(
        page,
        [{"action": "select", "label": "Country", "value": "United States"}],
    )

    assert result["ok"] is True
    assert page.locator("#country").input_value() == "United States"


def test_stream_executor_selects_portal_combobox_option(page):
    page.set_content("""
    <!doctype html>
    <label for="auth">Are you authorized to work?</label>
    <input id="auth" role="combobox" aria-label="Are you authorized to work?" required
           oninput="document.querySelector('#yes-option').hidden=false">
    <div id="yes-option" role="option" hidden
         onclick="document.querySelector('#auth').value='Yes'; this.hidden=true">Yes</div>
    """)

    result = execute_stream_actions_on_page(
        page,
        [{"action": "select", "label": "authorized to work", "value": "Yes"}],
    )

    assert result["ok"] is True
    assert page.locator("#auth").input_value() == "Yes"


def test_stream_executor_selects_generic_div_combobox_by_bbox(page):
    page.set_content("""
    <!doctype html>
    <p>Country / Region *</p>
    <div role="combobox" tabindex="0"
         onclick="document.querySelector('#menu').hidden=false">Select...</div>
    <div id="menu" hidden>
      <div role="option"
           onclick="document.querySelector('[role=combobox]').textContent='United States'; this.parentElement.hidden=true">United States</div>
    </div>
    """)

    result = execute_stream_actions_on_page(
        page,
        [{"action": "select", "label": "Country / Region", "value": "United States"}],
    )

    assert result["ok"] is True
    assert page.locator('[role="combobox"]').inner_text() == "United States"


def test_stream_executor_type_and_press_actions(page):
    result = execute_stream_actions_on_page(
        page,
        [
            {"action": "click", "label": "Country"},
            {"action": "type", "label": "Country", "value": "United"},
            {"action": "press", "key": "Tab"},
        ],
    )

    assert result["ok"] is True
    assert page.locator("#country").input_value() == "United"


def test_stream_executor_click_apply_link_without_submit_guard(page):
    result = execute_stream_actions_on_page(
        page,
        [{"action": "click", "label": "Apply for this role"}],
    )

    assert result["ok"] is True
    assert page.evaluate("document.body.dataset.apply") == "yes"


def test_stream_executor_checks_radio_choice_by_question_label(page):
    page.set_content(
        """
        <html><body>
          <form>
            <div class="question">
              <p>Are you legally authorized to work in the country where this role is based? *</p>
              <label><input type="radio" name="auth" value="yes" required> Yes</label>
              <label><input type="radio" name="auth" value="no" required> No</label>
            </div>
          </form>
        </body></html>
        """
    )
    obs = collect_browser_observation(page)
    auth_yes = next(
        control for control in obs.controls
        if "legally authorized" in control.label and "Yes" in control.label
    )

    result = execute_stream_actions_on_page(
        page,
        [{"action": "check", "control_id": auth_yes.control_id}],
    )

    assert result["ok"] is True
    assert page.locator('input[name="auth"][value="yes"]').is_checked() is True


def test_stream_executor_checks_label_proxy_checkbox(page):
    page.set_content(
        """
        <!doctype html>
        <label class="terms">
          <input id="terms" type="checkbox" required style="opacity:0; width:0; height:0">
          <span class="box"></span>
          I agree with the terms and conditions *
        </label>
        """
    )
    obs = collect_browser_observation(page)
    terms = next(c for c in obs.controls if "terms and conditions" in c.label)

    result = execute_stream_actions_on_page(
        page,
        [{"action": "check", "control_id": terms.control_id}],
    )

    assert result["ok"] is True
    assert page.locator("#terms").is_checked() is True


def test_stream_executor_guards_submit_until_required_fields_complete(page):
    submit_id = _control_id(page, "Submit application")

    blocked = execute_stream_actions_on_page(
        page,
        [{"action": "submit", "control_id": submit_id}],
        allow_submit=True,
    )

    assert blocked["ok"] is False
    assert "submit_guard_required_missing" in blocked["results"][0]["message"]

    allowed = execute_stream_actions_on_page(
        page,
        [
            {"action": "fill", "label": "First name", "value": "Nida"},
            {"action": "fill", "label": "Email", "value": "nida@example.com"},
            {"action": "select", "label": "Level", "value": "Senior"},
            {"action": "select", "label": "Country", "value": "United States"},
            {"action": "submit", "selector": "#submit"},
        ],
        allow_submit=True,
    )

    assert allowed["ok"] is True
    assert "Thank you for applying" in allowed["observation"]["page_text_sample"]


def test_stream_executor_refuses_submit_without_allow_submit(page):
    result = execute_stream_actions_on_page(
        page,
        [{"action": "submit", "selector": "#submit"}],
        allow_submit=False,
    )

    assert result["ok"] is False
    assert result["results"][0]["message"] == "submit_refused_allow_submit_false"


def test_stream_mcp_is_wired_into_claude_config():
    cfg = _make_mcp_config(9333)

    server = cfg["mcpServers"]["applypilot_stream"]
    assert server["command"]
    assert "applypilot.apply.stream_mcp_server" in server["args"]
    assert "9333" in server["args"]
    allowed = _allowed_tools_arg()
    assert "mcp__applypilot_stream__stream_latest" in allowed
    assert "mcp__applypilot_stream__stream_execute" in allowed
    assert "mcp__playwright__browser_snapshot" in allowed


def test_stream_observation_for_claude_includes_signature(page):
    obs = collect_browser_observation(page)
    payload = observation_for_claude(obs)

    assert payload["signature"]
    assert isinstance(payload["signature"], str)


def test_stream_strict_allowed_tools_can_omit_snapshot():
    allowed = _allowed_tools_arg(allow_snapshot=False, allow_raw_browser=False, allow_navigate=False)
    disallowed = _disallowed_tools_arg(allow_snapshot=False, allow_raw_browser=False, allow_navigate=False)

    assert "mcp__applypilot_stream__stream_latest" in allowed
    assert "mcp__applypilot_stream__stream_execute" in allowed
    assert "mcp__playwright__browser_navigate" not in allowed
    assert "mcp__playwright__browser_snapshot" not in allowed
    assert "mcp__playwright__browser_take_screenshot" not in allowed
    assert "mcp__playwright__browser_evaluate" not in allowed
    assert "mcp__playwright__browser_type" not in allowed
    assert "mcp__playwright__browser_navigate" in disallowed
    assert "mcp__playwright__browser_snapshot" in disallowed
    assert "mcp__playwright__browser_take_screenshot" in disallowed
    assert "mcp__playwright__browser_evaluate" in disallowed
    assert "mcp__playwright__browser_type" in disallowed


def test_stream_retry_allowed_tools_restore_snapshot():
    allowed = _allowed_tools_arg(allow_snapshot=True, allow_raw_browser=True)
    disallowed = _disallowed_tools_arg(allow_snapshot=True, allow_raw_browser=True)

    assert "mcp__playwright__browser_snapshot" in allowed
    assert "mcp__playwright__browser_take_screenshot" in allowed
    assert "mcp__playwright__browser_evaluate" in allowed
    assert "mcp__playwright__browser_type" in allowed
    assert "mcp__playwright__browser_navigate" in allowed
    assert "mcp__playwright__browser_snapshot" not in disallowed
    assert "mcp__playwright__browser_take_screenshot" not in disallowed
    assert "mcp__playwright__browser_evaluate" not in disallowed
    assert "mcp__playwright__browser_type" not in disallowed
