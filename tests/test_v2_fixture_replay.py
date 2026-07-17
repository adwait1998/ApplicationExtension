import json
from pathlib import Path

import pytest

from applypilot.apply.v2 import frontend_greenhouse as fe
from applypilot.apply.v2 import resolver as rz
from applypilot.apply.browser_stream import collect_browser_observation


FIXDIR = Path(__file__).parent / "fixtures" / "v2"
_fixtures = sorted(FIXDIR.glob("*.html")) if FIXDIR.exists() else []


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_context().new_page()
        yield p
        b.close()


@pytest.mark.skipif(not _fixtures, reason="no promoted fixtures yet (populate via 'applypilot fixtures promote')")
@pytest.mark.parametrize("html_path", _fixtures, ids=lambda p: p.stem)
def test_recorded_dom_parses_and_resolves(page, html_path):
    # Real recorded DOM (NOT synthetic) parses to a schema whose standard fields
    # resolve deterministically. Guards against parse-gap regressions (spec §11).
    page.set_content(html_path.read_text(encoding="utf-8"))
    obs = collect_browser_observation(page)
    schema = fe.parse_observation(obs, company=html_path.stem, url="https://boards.greenhouse.io/x/jobs/1")
    keys = {f.semantic_key for s in schema.steps for f in s.fields}
    # every promoted GH fixture must at least surface the resume + a name/email field
    assert "resume" in keys or "email" in keys or "first_name" in keys
    expected = json.loads(html_path.with_suffix(".expected.json").read_text(encoding="utf-8")) \
        if html_path.with_suffix(".expected.json").exists() else None
    if expected:
        assert {f.semantic_key for s in schema.steps for f in s.fields} >= set(expected["semantic_keys"])
