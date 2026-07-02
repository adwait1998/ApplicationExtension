from __future__ import annotations

import time

import pytest

from applypilot.apply.browser_stream import (
    BrowserObservation,
    BrowserStateStream,
    collect_browser_observation,
    summarize_observation,
)


_FORM = """
<!doctype html>
<html>
  <head><title>Apply</title></head>
  <body>
    <form>
      <label for="first">First name *</label>
      <input id="first" name="first_name" required>

      <label for="email">Email *</label>
      <input id="email" name="email" type="email" required>

      <label for="portfolio">Portfolio</label>
      <input id="portfolio" name="portfolio">

      <label for="why">Why us? *</label>
      <textarea id="why" name="why" required></textarea>

      <label for="level">Level *</label>
      <select id="level" name="level" required>
        <option value="">Select...</option>
        <option>Senior</option>
      </select>

      <input id="resume" name="resume" type="file" aria-label="Resume/CV">
      <div role="alert" class="field-error">Email is required</div>
      <button id="submit" type="submit" disabled>Submit application</button>
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


def test_collect_observation_detects_form_state(page):
    obs = collect_browser_observation(page)

    assert obs.url.startswith("about:")
    assert obs.title == "Apply"
    assert obs.ready_state in {"complete", "interactive"}
    assert {c.label for c in obs.controls} >= {"First name *", "Email *", "Why us? *", "Level *"}
    assert "First name *" in obs.required_missing
    assert "Email *" in obs.required_missing
    assert "Why us? *" in obs.required_missing
    assert "Email is required" in obs.validation_errors
    assert obs.submit_buttons[0].label == "Submit application"
    assert obs.submit_buttons[0].disabled is True
    assert obs.submit_enabled is False


def test_collect_observation_updates_after_fills_and_navigation(page):
    page.fill("#first", "Nida")
    page.fill("#email", "nida@example.com")
    page.fill("#why", "The role matches my automation and product experience.")
    page.select_option("#level", label="Senior")
    page.evaluate("document.querySelector('#submit').disabled = false")
    page.evaluate("document.querySelector('[role=alert]').remove()")

    obs = collect_browser_observation(page)

    assert obs.required_missing == []
    assert obs.validation_errors == []
    assert obs.submit_enabled is True
    values = {c.selector: c.value for c in obs.controls}
    assert values["#first"] == "Nida"
    assert values["#level"] == "Senior"

    page.set_content("<html><head><title>Done</title></head><body><p>Thank you for applying</p></body></html>")
    obs2 = collect_browser_observation(page)
    assert obs2.title == "Done"
    assert obs2.controls == []


def test_collect_observation_includes_iframe_controls(page):
    page.set_content(
        """
        <html><body>
          <iframe srcdoc='<label for="x">Screening *</label><input id="x" required><button>Continue</button>'></iframe>
        </body></html>
        """
    )
    page.frame_locator("iframe").locator("#x").wait_for()

    obs = collect_browser_observation(page)

    assert any(c.label == "Screening *" for c in obs.controls)
    assert "Screening *" in obs.required_missing
    assert any(b.label == "Continue" for b in obs.submit_buttons)


def test_collect_observation_labels_radio_choices_with_question(page):
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

    labels = {c.label for c in obs.controls}
    assert any("legally authorized" in label and "Yes" in label for label in labels)
    selectors = {c.selector for c in obs.controls}
    assert 'input[name="auth"][value="yes"]' in selectors
    assert 'input[name="auth"][value="no"]' in selectors


def test_collect_observation_radio_group_missing_clears_when_one_option_checked(page):
    page.set_content(
        """
        <html><body>
          <form>
            <fieldset>
              <legend>Are you legally authorized to work? *</legend>
              <label><input type="radio" name="auth" value="yes" required> Yes</label>
              <label><input type="radio" name="auth" value="no" required> No</label>
            </fieldset>
          </form>
        </body></html>
        """
    )

    obs = collect_browser_observation(page)
    assert obs.required_missing == ["Are you legally authorized to work? *"]
    assert all(control.invalid for control in obs.controls if control.role == "radio")

    page.check('input[name="auth"][value="yes"]')
    obs = collect_browser_observation(page)

    assert obs.required_missing == []
    radios = [control for control in obs.controls if control.role == "radio"]
    assert radios
    assert all(not control.invalid for control in radios)


def test_collect_observation_detects_label_proxy_checkbox(page):
    page.set_content(
        """
        <html><body>
          <form>
            <label class="terms">
              <input id="terms" type="checkbox" required style="opacity:0; width:0; height:0">
              <span class="box"></span>
              I agree with the terms and conditions *
            </label>
            <button>Next</button>
          </form>
        </body></html>
        """
    )

    obs = collect_browser_observation(page)

    terms = next(c for c in obs.controls if "terms and conditions" in c.label)
    assert terms.role == "checkbox"
    assert terms.required is True
    assert terms.invalid is True
    assert "terms and conditions" in obs.required_missing[0]

    page.locator("label.terms").click()
    obs = collect_browser_observation(page)

    terms = next(c for c in obs.controls if "terms and conditions" in c.label)
    assert terms.value == "checked"
    assert obs.required_missing == []


def test_collect_observation_reads_react_select_ancestor_value(page):
    page.set_content(
        """
        <html><body>
          <label for="country">Country *</label>
          <div class="select__control">
            <div class="select__value-container">
              <div class="select__single-value">United States</div>
              <input id="country" class="select__input" role="combobox" required value="">
            </div>
          </div>
        </body></html>
        """
    )

    obs = collect_browser_observation(page)

    country = next(c for c in obs.controls if c.selector == "#country")
    assert country.value == "United States"
    assert country.invalid is False
    assert obs.required_missing == []


def test_observation_summary_is_compact(page):
    obs = collect_browser_observation(page)
    summary = summarize_observation(obs, max_controls=2)

    assert "Required missing:" in summary
    assert "Submit application [disabled]" in summary
    assert summary.count("- ") == 2


def test_stream_publish_debounces_identical_observations():
    stream = BrowserStateStream(9222, debounce_s=60)
    obs = BrowserObservation(url="https://example.com", ready_state="complete", observed_at=time.time())

    stream._publish(obs)
    stream._publish(obs)

    assert stream.update_count == 1

    changed = BrowserObservation(url="https://example.com/next", ready_state="complete", observed_at=time.time())
    stream._publish(changed)
    assert stream.update_count == 2


def test_collect_observation_performance(page):
    started = time.monotonic()
    collect_browser_observation(page)
    elapsed_ms = (time.monotonic() - started) * 1000
    assert elapsed_ms < 300
