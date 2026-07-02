from __future__ import annotations

import pytest

from applypilot.apply.prefill import _click_segmented_button_by_label


ASHBY_SEGMENTED_HTML = """
<html><body>
  <section class="field">
    <p>Are you permanently authorized to work in the United States without visa sponsorship?</p>
    <button type="button">Yes</button>
    <button type="button">No</button>
  </section>
  <section class="field">
    <p>Will you require sponsorship for authorization to work in the United States?</p>
    <button type="button">Yes</button>
    <button type="button">No</button>
  </section>
  <script>
    document.querySelectorAll('.field').forEach(field => {
      field.querySelectorAll('button').forEach(button => {
        button.addEventListener('click', () => {
          field.querySelectorAll('button').forEach(b => {
            b.classList.remove('active');
            b.setAttribute('aria-pressed', 'false');
          });
          button.classList.add('active');
          button.setAttribute('aria-pressed', 'true');
        });
      });
    });
  </script>
</body></html>
"""


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        yield page
        browser.close()


def test_ashby_segmented_buttons_are_scoped_by_question(page):
    page.set_content(ASHBY_SEGMENTED_HTML)

    assert _click_segmented_button_by_label(
        page,
        ("permanently authorized", "without sponsorship"),
        ("no",),
    )
    assert _click_segmented_button_by_label(
        page,
        ("require sponsorship", "authorization to work"),
        ("yes",),
    )

    states = page.evaluate(
        """() => Array.from(document.querySelectorAll('.field')).map(field => ({
          question: field.querySelector('p').innerText,
          active: field.querySelector('button.active')?.innerText || null
        }))"""
    )

    assert states[0]["active"] == "No"
    assert states[1]["active"] == "Yes"
