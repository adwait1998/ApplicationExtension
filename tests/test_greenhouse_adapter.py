"""Reliability-v2 Phase C done-when: the Greenhouse adapter fills 100% of
the standard fields + submits, with ZERO LLM, on a synthetic Greenhouse-
like form — AND still does so after ids/classes churn (proving the
self-healing locator integration). A custom required question must land
in `unresolved` (Tier-2), never guessed by the adapter.

$0 — synthetic Playwright DOM only.
"""
from __future__ import annotations

import pytest

from applypilot.apply.adapters.greenhouse import fill_greenhouse, AdapterResult


_FORM = """
<!doctype html><html><body>
<form id="application_form">
  <label for="fn">First name</label>
  <input id="fn" name="first_name" type="text" required class="gh-in a1">

  <label for="ln">Last name</label>
  <input id="ln" name="last_name" type="text" required class="gh-in b2">

  <label for="em">Email</label>
  <input id="em" name="email" type="email" required class="gh-in c3">

  <label for="ph">Phone</label>
  <input id="ph" name="phone" type="tel" class="gh-in d4">

  <label for="loc">Current location (City)</label>
  <input id="loc" name="location" type="text" class="gh-in e5">

  <label for="li">LinkedIn Profile</label>
  <input id="li" name="linkedin" type="text" class="gh-in f6">

  <label for="rz">Resume/CV</label>
  <input id="rz" name="resume" type="file" required class="gh-file g7">

  <label for="wa-i">Are you legally authorized to work in the US?</label>
  <div class="select__control" id="wa-c" tabindex="0">
    <span class="select__single-value" id="wa-v">Select...</span>
    <input class="select__input" id="wa-i" autocomplete="off">
  </div>
  <div class="select__menu" id="wa-m" style="display:none">
    <div class="select__option">Yes, I am authorized to work in the US</div>
    <div class="select__option">No, I require sponsorship</div>
  </div>

  <label for="g-i">Gender</label>
  <div class="select__control" id="g-c" tabindex="0">
    <span class="select__single-value" id="g-v">Select...</span>
    <input class="select__input" id="g-i" autocomplete="off">
  </div>
  <div class="select__menu" id="g-m" style="display:none">
    <div class="select__option">Male</div>
    <div class="select__option">Female</div>
    <div class="select__option">Decline to self-identify</div>
  </div>

  <label for="cust">Describe a product you shipped that you're proud of</label>
  <textarea id="cust" name="why_8801" required class="gh-ta z9"></textarea>

  <button id="sub" type="button">Submit application</button>
  <div id="done" style="display:none">Your application has been received</div>
<script>
  // Minimal react-select-ish widget: click control → menu; type filters;
  // ArrowDown highlights; Enter commits (updates single-value text).
  function wire(cId, iId, mId, vId){
    const c=document.getElementById(cId), i=document.getElementById(iId),
          m=document.getElementById(mId), v=document.getElementById(vId);
    let hi=null;
    const open=()=>{m.style.display='block';};
    c.addEventListener('mousedown',open); c.addEventListener('click',open);
    i.addEventListener('focus',open);
    i.addEventListener('input',()=>{
      const q=i.value.toLowerCase();
      const opts=[...m.querySelectorAll('.select__option')];
      hi=opts.find(o=>q&&o.textContent.toLowerCase().includes(q))||null;
      open();
    });
    i.addEventListener('keydown',e=>{
      if(e.key==='ArrowDown'){e.preventDefault();
        if(!hi) hi=m.querySelector('.select__option');}
      else if(e.key==='Enter'){e.preventDefault();
        if(hi){ v.textContent=hi.textContent; i.value=hi.textContent;
          m.style.display='none'; }}
    });
  }
  wire('wa-c','wa-i','wa-m','wa-v');
  wire('g-c','g-i','g-m','g-v');
  document.getElementById('sub').addEventListener('click',()=>{
    document.getElementById('done').style.display='block';});
</script>
</form></body></html>
"""

_PROFILE = {
    "personal": {"full_name": "Nida Shah", "email": "nida@example.com",
                 "phone": "4081234567", "city": "San Jose",
                 "linkedin_url": "https://linkedin.com/in/nidashah",
                 "portfolio_url": "https://nidashah.framer.website"},
    "work_authorization": {"legally_authorized_to_work": True,
                           "require_sponsorship": True},
    "eeo_voluntary": {"gender": "Decline to self-identify",
                      "race_ethnicity": "Decline to self-identify",
                      "veteran_status": "Decline to self-identify",
                      "disability_status": "Decline to self-identify"},
}


@pytest.fixture
def resume(tmp_path):
    p = tmp_path / "resume.pdf"
    p.write_bytes(b"%PDF-1.4 synthetic")
    return str(p)


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_context().new_page()
        yield p
        b.close()


def _churn(html: str) -> str:
    """Simulate the ATS regenerating field ids/classes between sessions.

    Churn ONLY what a real ATS regenerates and what the adapter keys off
    (text/file input ids + classes, breaking the label for=/id link →
    forces self-healing). We deliberately do NOT rename the react-select
    control ids (wa-c/g-c) because the page's own bundled JS references
    them — a real ATS ships matching JS, so scrambling the page's own
    script hooks would be an unrealistic test artifact, not a healing test.
    """
    for o, n in {
        'id="fn"': 'id="fn-9a3f21"', 'id="em"': 'id="em-7c2b"',
        'id="rz"': 'id="rz-44de"',
        'class="gh-in a1"': 'class="x-101"', 'class="gh-in c3"': 'class="x-303"',
        'class="gh-file g7"': 'class="x-707"', 'class="gh-in d4"': 'class="x-404"',
    }.items():
        html = html.replace(o, n)
    return html


def _assert_full_fill(res: AdapterResult):
    # Every standard field present on this synthetic form must be filled.
    for k in ("first_name", "last_name", "email", "phone", "location",
              "linkedin", "resume", "work_authorization", "gender"):
        assert k in res.fields_filled, f"{k} not filled (got {res.fields_filled})"
    assert res.used_llm is False
    assert res.submitted is True
    # The custom required question must be deferred to Tier-2, not guessed.
    assert any("proud of" in u["label"].lower() for u in res.unresolved), res.unresolved


def test_adapter_fills_pristine_form(page, resume):
    page.set_content(_FORM)
    res = fill_greenhouse(page, _PROFILE, resume, submit=True)
    _assert_full_fill(res)
    assert page.locator("#fn").input_value() == "Nida"
    assert page.locator("#em").input_value() == "nida@example.com"
    assert page.locator("#wa-v").inner_text().startswith("Yes")
    assert "Decline" in page.locator("#g-v").inner_text()
    assert page.locator("#done").is_visible()      # submit fired


def test_adapter_self_heals_after_id_class_churn(page, resume):
    """Phase C's whole point: ids/classes regenerate between sessions, the
    adapter still fills 100% via semantic/self-healing locators."""
    page.set_content(_churn(_FORM))
    res = fill_greenhouse(page, _PROFILE, resume, submit=True)
    _assert_full_fill(res)
    assert page.locator("#done").is_visible()


def test_adapter_never_guesses_custom_question(page, resume):
    page.set_content(_FORM)
    res = fill_greenhouse(page, _PROFILE, resume, submit=False)
    # The custom textarea must be UNTOUCHED (left for Tier-2 LLM).
    assert page.locator("#cust").input_value() == ""
    assert any("proud of" in u["label"].lower() for u in res.unresolved)
    assert res.used_llm is False


def test_adapter_cdp_pass_is_fail_open_on_dead_port():
    """The launcher wiring (_greenhouse_adapter_pass) MUST never raise — a
    dead/uncontactable Chrome → None so run_job falls back to prefill+LLM.
    This is the guarantee that makes the live wiring safe."""
    from applypilot.apply.launcher import _greenhouse_adapter_pass
    out = _greenhouse_adapter_pass(59999, {"personal": {}}, "/nonexistent/r.pdf")
    assert out is None


# ---- iter-9: frame-embedded form, answer-cache wiring, auto-submit ----

_IFRAME_WRAP = """<!doctype html><html><body>
  <h1>Careers at Roblox</h1>
  <iframe id="gh" srcdoc='__INNER__'></iframe>
</body></html>"""


def _embed(form_html: str) -> str:
    # srcdoc attribute uses single quotes in the wrapper → escape inner.
    return _IFRAME_WRAP.replace("__INNER__", form_html.replace("'", "&apos;"))


def test_adapter_drives_iframe_embedded_form(page, resume):
    """roblox-class: the GH form lives in a child iframe. _form_scope must
    target that frame so the adapter actually drives it (this batch's
    'roblox fell to skill_record' gap)."""
    page.set_content(_embed(_FORM))
    page.wait_for_timeout(200)
    res = fill_greenhouse(page, _PROFILE, resume, submit=True)
    for k in ("first_name", "email", "resume", "work_authorization", "gender"):
        assert k in res.fields_filled, f"{k} not filled in iframe (got {res.fields_filled})"
    assert res.used_llm is False


def test_answer_cache_resolves_custom_question(page, resume):
    """With an answer-cache, the custom required Q is filled (cache hit,
    $0) and removed from unresolved — so the form can fully resolve."""
    from applypilot.apply.answer_cache import AnswerCache
    q = "Describe a product you shipped that you're proud of"
    calls = []
    ac = AnswerCache(_PROFILE)
    ac.answer(q, llm_fn=lambda question, ctx: (calls.append(1), "Shipped a design system at Intuit.")[1])
    assert len(calls) == 1  # warmed once
    page.set_content(_FORM)
    res = fill_greenhouse(page, _PROFILE, resume, submit=False, answer_cache=ac)
    assert not any("proud of" in u["label"].lower() for u in res.unresolved), res.unresolved
    assert page.locator("#cust").input_value().startswith("Shipped a design system")
    assert len(calls) == 1  # cache hit on the fill — no extra LLM
    assert res.used_llm is False


def test_auto_submit_only_when_fully_resolved(page, resume):
    from applypilot.apply.answer_cache import AnswerCache
    # (a) custom Q UNRESOLVED + submit='auto' → must NOT submit (LLM's job)
    page.set_content(_FORM)
    r1 = fill_greenhouse(page, _PROFILE, resume, submit="auto")
    assert r1.submitted is False
    assert any("proud of" in u["label"].lower() for u in r1.unresolved)
    assert not page.locator("#done").is_visible()
    # (b) custom Q resolved via cache + submit='auto' → submits
    q = "Describe a product you shipped that you're proud of"
    ac = AnswerCache(_PROFILE)
    ac.answer(q, llm_fn=lambda question, ctx: "A payments flow at Brex.")
    page.set_content(_FORM)
    r2 = fill_greenhouse(page, _PROFILE, resume, submit="auto", answer_cache=ac)
    assert r2.unresolved == []
    assert r2.submitted is True
    assert page.locator("#done").is_visible()
