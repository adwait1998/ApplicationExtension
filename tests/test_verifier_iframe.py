"""Verifier v3 regression: post-submit success rendered INSIDE an embedded
ATS iframe (Greenhouse/Lever embed) must be detected. Before this, the
verifier read only the top window → roblox-style embedded applications
were mis-flagged needs_review at conf 0.2 despite really succeeding.

Synthetic Playwright page only — $0, no CDP, no network.
"""
from __future__ import annotations

import pytest

from applypilot.apply.launcher import _scan_frames_for_success


# Top page = careers landing (NO success text). Child iframe = the
# Greenhouse embed showing the post-submit confirmation.
_TOP_WITH_SUCCESS_IN_IFRAME = """
<!doctype html><html><body>
  <h1>Careers at Roblox</h1>
  <p>Explore open roles. Life at Roblox. Benefits.</p>
  <iframe id="gh" srcdoc='
    <!doctype html><html><body>
      <h2>Your application has been received</h2>
      <p>Thank you for applying. Our team is reviewing your application
         and we will be in touch about next steps.</p>
    </body></html>'></iframe>
</body></html>
"""

_TOP_NO_SUCCESS_ANYWHERE = """
<!doctype html><html><body>
  <h1>Careers at Roblox</h1>
  <iframe id="gh" srcdoc='
    <!doctype html><html><body>
      <form><input id="first_name"><button>Submit application</button></form>
    </body></html>'></iframe>
</body></html>
"""

_TOP_LEVEL_SUCCESS = """
<!doctype html><html><body>
  <h2>Thank you for your interest. We have received your application.</h2>
</body></html>
"""


@pytest.fixture
def browser_page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_context().new_page()
        yield p
        b.close()


def test_success_inside_iframe_is_detected(browser_page):
    """The exact roblox bug: top window has no success text, the embed
    iframe does. Aggregated scan MUST find the confirmation."""
    browser_page.set_content(_TOP_WITH_SUCCESS_IN_IFRAME)
    browser_page.wait_for_timeout(150)  # let srcdoc frame attach
    state = _scan_frames_for_success(browser_page)
    assert state["hits"], f"no confirmation found across frames: {state}"
    joined = " ".join(state["hits"])
    assert "your application has been received" in joined or "thank you for applying" in joined
    # evidence should come from the frame that actually had the hit
    assert "received" in state["evidence"].lower()


def test_top_level_success_still_works(browser_page):
    """Don't regress the normal (non-embedded) case."""
    browser_page.set_content(_TOP_LEVEL_SUCCESS)
    state = _scan_frames_for_success(browser_page)
    assert state["hits"]
    assert any("received" in h or "interest" in h for h in state["hits"])


def test_no_success_anywhere_means_no_hits(browser_page):
    """A still-on-the-form page (no confirmation in any frame) → no hits,
    submit button seen in the iframe (so submit_gone would be False)."""
    browser_page.set_content(_TOP_NO_SUCCESS_ANYWHERE)
    browser_page.wait_for_timeout(150)
    state = _scan_frames_for_success(browser_page)
    assert state["hits"] == []
    assert state["submitVisible"] is True   # detected inside the iframe


def test_detached_or_bad_frame_is_non_fatal(browser_page):
    """Scan must never throw even on a blank page with no frames."""
    browser_page.set_content("<html><body></body></html>")
    state = _scan_frames_for_success(browser_page)
    assert state["hits"] == []
    assert state["submitVisible"] is False
    assert "url" in state
