import pytest

from applypilot.apply.v2 import ir
from applypilot.apply.v2 import executor as ex
from applypilot.apply.v2.resolver import PlannedField, FillPlan


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_context().new_page()
        yield p
        b.close()


# Form where uploading the resume injects the REST of the fields (server-side
# parse re-render). If the executor fills before the chip/settle, #fn won't exist.
_RERENDER = """<!doctype html><body><form id="f">
  <label for="rz">Resume/CV</label>
  <input id="rz" name="resume" type="file" required>
  <div id="rest"></div>
<script>
  document.getElementById('rz').addEventListener('change',()=>{
    setTimeout(()=>{ document.getElementById('rest').innerHTML =
      '<label for="fn">First name</label><input id="fn" type="text" required>' +
      '<span id="chip">resume.pdf</span>'; }, 30);   // async re-render
  });
</script></form></body>"""


def _f(sem, kind, label, fid, options=ir.LAZY, required=True):
    return ir.Field(field_id=fid, frame_path=(), label_text=label, question_text=label,
                    semantic_key=sem, widget=ir.Widget(kind=kind), options=options, required=required)


def test_resume_fills_first_then_settles_before_rest(page, tmp_path):
    page.set_content(_RERENDER)
    pdf = tmp_path / "resume.pdf"; pdf.write_bytes(b"%PDF-1.4 x")
    resume = PlannedField(_f("resume", "file", "Resume/CV", "rz"), value=str(pdf), driver="file")
    first = PlannedField(_f("first_name", "text", "First name", "fn"), value="Nida", driver="text")
    plan = FillPlan(planned=[first, resume])          # note: NOT resume-first in the list
    report = ex.execute(page, ir.FormSchema("greenhouse", "acme", "u",
                                            [ir.Step(0, [resume.field, first.field], terminal=True)]),
                        plan)
    # executor MUST reorder resume-first + wait for settle so #fn exists when filled
    assert page.locator("#fn").input_value() == "Nida"
    assert report.committed_keys and "resume" in report.committed_keys


def test_no_fixed_sleeps_uses_settle_gate(page, tmp_path, monkeypatch):
    import time as _t
    calls = {"sleep": 0}
    monkeypatch.setattr(ex, "time", type("T", (), {"sleep": lambda s: calls.__setitem__("sleep", calls["sleep"] + 1),
                                                    "monotonic": _t.monotonic})())
    page.set_content(_RERENDER)
    pdf = tmp_path / "r.pdf"; pdf.write_bytes(b"%PDF-1.4 x")
    plan = FillPlan(planned=[PlannedField(_f("resume", "file", "Resume/CV", "rz"), value=str(pdf), driver="file")])
    ex.execute(page, ir.FormSchema("greenhouse", "acme", "u",
                                   [ir.Step(0, [plan.planned[0].field], terminal=True)]), plan)
    assert calls["sleep"] == 0                         # zero fixed sleeps (invariant 9)


def test_required_completeness_interlock_blocks_submit_when_missing(page):
    page.set_content('<form id="f"><label for="fn">First name</label>'
                     '<input id="fn" type="text" required></form>')
    f = _f("first_name", "text", "First name", "fn")
    # a plan that DID NOT fill it (park) -> required sweep must report incomplete
    plan = FillPlan(planned=[PlannedField(f, park=True, driver="text")])
    report = ex.execute(page, ir.FormSchema("greenhouse", "acme", "u",
                                            [ir.Step(0, [f], terminal=True)]), plan)
    assert report.ready_to_submit is False
    assert report.missing_required                      # names the empty required field


def test_committed_false_on_required_marks_unresolved(page):
    page.set_content('<form><label for="fn">First name</label>'
                     '<input id="fn" type="text" required></form>')
    f = _f("first_name", "text", "First name", "fn")
    plan = FillPlan(planned=[PlannedField(f, value="Nida", driver="text")])
    # driver reports committed True here (value read-back matches) -> resolved
    report = ex.execute(page, ir.FormSchema("greenhouse", "acme", "u",
                                            [ir.Step(0, [f], terminal=True)]), plan)
    assert report.ready_to_submit is True
    assert "first_name" in report.committed_keys
    assert report.unresolved == []
