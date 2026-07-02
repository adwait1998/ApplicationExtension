"""Phase 1 done-when test: replay engine passes against a synthetic HTML form.

Launches a Playwright page on a data: URL containing a minimal Greenhouse-like
form, loads a hand-authored skill, runs replay, asserts every field was filled
with the right value from a fake profile.

No real network, no real ATS, no LLM — just proves the engine works.
"""
from __future__ import annotations

import pytest
import yaml

from applypilot.apply.replay import (
    STATUS_DRIFT_DETECTED,
    STATUS_FAILED,
    STATUS_NEEDS_PATCH,
    STATUS_SUBMITTED,
    form_layout_hash,
    replay_skill,
)
from applypilot.apply.skill_schema import (
    Action,
    Skill,
    SkillValidationError,
    SuccessSignals,
    UnresolvedField,
    load_skill,
    resolve_value,
    save_skill,
)


# A tiny self-contained HTML form that mimics Greenhouse fields. We use a
# data: URL so no server is needed.
_SYNTH_FORM = """
<!doctype html>
<html><body>
<form id="application_form">
  <input id="first_name" name="first_name" type="text">
  <input id="last_name"  name="last_name"  type="text">
  <input id="email"      name="email"      type="email">
  <input id="phone"      name="phone"      type="tel">
  <textarea id="why_join" name="why_join"></textarea>
  <input id="resume" name="resume" type="file">
  <select id="work_auth">
    <option value=""></option>
    <option value="Yes">Yes</option>
    <option value="No">No</option>
  </select>
  <button id="submit_btn" type="button">Submit Application</button>
  <div id="post_submit" style="display:none">Thank you for applying.</div>
</form>
<script>
document.getElementById('submit_btn').addEventListener('click', () => {
  document.getElementById('post_submit').style.display = 'block';
});
</script>
</body></html>
"""


@pytest.fixture
def profile():
    return {
        "personal": {
            "first_name": "Nida",
            "last_name": "Shah",
            "email": "nida@example.com",
            "phone": "5551234567",
        },
        "responses": {
            "why_join": "Excited to work on craft-led design.",
        },
    }


@pytest.fixture
def tmp_resume(tmp_path):
    p = tmp_path / "resume.pdf"
    p.write_bytes(b"%PDF-1.4 not a real pdf")
    return str(p)


@pytest.fixture
def page():
    """Live Playwright page on a data: URL holding the synthetic form."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        p = ctx.new_page()
        p.set_content(_SYNTH_FORM)
        yield p
        browser.close()


def _synth_skill(form_hash: str, tmp_resume_path: str, with_unresolved: bool = False) -> Skill:
    required = [
        "#first_name", "#last_name", "#email", "#phone",
        "#why_join", "#resume", "#work_auth", "#submit_btn",
    ]
    actions = [
        Action(kind="fill",          selector="#first_name", value_source="profile.personal.first_name"),
        Action(kind="fill",          selector="#last_name",  value_source="profile.personal.last_name"),
        Action(kind="fill",          selector="#email",      value_source="profile.personal.email"),
        Action(kind="fill",          selector="#phone",      value_source="profile.personal.phone"),
        Action(kind="fill_textarea", selector="#why_join",   value_source="profile.responses.why_join"),
        Action(kind="upload",        selector="#resume",     value_source=f"file:{tmp_resume_path}"),
        Action(kind="select_native", selector="#work_auth",  value_source="literal:Yes"),
        Action(kind="submit",        selector="#submit_btn"),
    ]
    unresolved = [
        UnresolvedField(selector="#why_join", label="why join", type="long_text"),
    ] if with_unresolved else []
    return Skill(
        version=1, company="synth", ats="greenhouse",
        recorded_at="2026-05-14T20:00:00Z",
        recorded_from_url="data:text/html,synthetic",
        form_layout_hash=form_hash,
        required_selectors=required,
        actions=actions,
        unresolved_fields=unresolved,
        success_signals=SuccessSignals(page_text_contains_any=["thank you for applying"]),
    )


def test_replay_happy_path(page, profile, tmp_resume):
    """All fields fill correctly, submit fires, status = submitted."""
    required = ["#first_name", "#last_name", "#email", "#phone",
                "#why_join", "#resume", "#work_auth", "#submit_btn"]
    skill = _synth_skill(form_hash=form_layout_hash(required), tmp_resume_path=tmp_resume)
    result = replay_skill(skill, page, profile)

    assert result.status == STATUS_SUBMITTED, f"got {result.status} / error={result.error}"
    assert result.actions_run == 8

    # Verify each field was actually filled
    assert page.locator("#first_name").input_value() == "Nida"
    assert page.locator("#last_name").input_value() == "Shah"
    assert page.locator("#email").input_value() == "nida@example.com"
    assert page.locator("#phone").input_value() == "5551234567"
    assert page.locator("#why_join").input_value() == "Excited to work on craft-led design."
    assert page.locator("#work_auth").input_value() == "Yes"
    # The submit button was clicked, revealing the success div
    assert page.locator("#post_submit").is_visible()


def test_replay_dry_run_skips_submit(page, profile, tmp_resume):
    required = ["#first_name", "#last_name", "#email", "#phone",
                "#why_join", "#resume", "#work_auth", "#submit_btn"]
    skill = _synth_skill(form_hash=form_layout_hash(required), tmp_resume_path=tmp_resume)
    result = replay_skill(skill, page, profile, dry_run=True)
    assert result.status == STATUS_SUBMITTED  # treated as success — all non-submit actions ran
    # Submit didn't fire
    assert not page.locator("#post_submit").is_visible()


def test_replay_drift_detected_on_missing_selector(page, profile, tmp_resume):
    """If required_selectors include something not on the page, return drift_detected."""
    required = ["#first_name", "#NONEXISTENT_FIELD"]
    skill = _synth_skill(form_hash=form_layout_hash(required), tmp_resume_path=tmp_resume)
    skill.required_selectors = required
    result = replay_skill(skill, page, profile)
    assert result.status == STATUS_DRIFT_DETECTED
    assert "NONEXISTENT" in (result.error or "")


def test_replay_drift_on_hash_mismatch(page, profile, tmp_resume):
    """If form_layout_hash is set but doesn't match live, return drift_detected."""
    required = ["#first_name", "#last_name", "#email", "#phone",
                "#why_join", "#resume", "#work_auth", "#submit_btn"]
    skill = _synth_skill(form_hash="sha256:thiswrong", tmp_resume_path=tmp_resume)
    result = replay_skill(skill, page, profile)
    assert result.status == STATUS_DRIFT_DETECTED
    assert "form_layout_hash" in (result.error or "")


def test_replay_heals_action_selectors_before_declaring_drift(page, profile):
    """A recorded selector can churn if its semantic element spec still resolves."""
    page.set_content("""
    <!doctype html><html><body>
      <label for="first_name_99">First name</label>
      <input id="first_name_99" name="first_name" type="text">
      <button id="submit_99" type="button">Submit application</button>
      <div id="post_submit" style="display:none">Thank you for applying.</div>
      <script>
      document.getElementById('submit_99').addEventListener('click', () => {
        document.getElementById('post_submit').style.display = 'block';
      });
      </script>
    </body></html>
    """)
    required = ["#first_name_old", 'text="Submit application"']
    skill = Skill(
        version=1,
        company="synth",
        ats="custom",
        recorded_at="",
        recorded_from_url="",
        form_layout_hash=form_layout_hash(required),
        required_selectors=required,
        actions=[
            Action(
                kind="fill",
                selector="#first_name_old",
                value_source="profile.personal.first_name",
                extra={"element_spec": {"role": "textbox", "label": "First name", "name": "First name", "tag": "input"}},
            ),
            Action(
                kind="submit",
                selector='text="Submit application"',
                extra={"element_spec": {"role": "button", "name": "Submit application", "text": "Submit application", "tag": "button"}},
            ),
        ],
    )

    result = replay_skill(skill, page, profile)

    assert result.status == STATUS_SUBMITTED, result.error
    assert page.locator("#first_name_99").input_value() == "Nida"
    assert page.locator("#post_submit").is_visible()


def test_replay_needs_patch_when_unresolved(page, profile, tmp_resume):
    """If skill has unresolved_fields, Tier 1 stops at submit and returns needs_patch."""
    required = ["#first_name", "#last_name", "#email", "#phone",
                "#why_join", "#resume", "#work_auth", "#submit_btn"]
    skill = _synth_skill(form_hash=form_layout_hash(required), tmp_resume_path=tmp_resume, with_unresolved=True)
    result = replay_skill(skill, page, profile)
    assert result.status == STATUS_NEEDS_PATCH
    assert len(result.unresolved) == 1
    # Submit did NOT fire
    assert not page.locator("#post_submit").is_visible()


def test_replay_failed_on_bad_value_source(page, profile, tmp_resume):
    """A bad value_source should surface as STATUS_FAILED with the error string."""
    required = ["#first_name"]
    skill = Skill(
        version=1, company="synth", ats="greenhouse",
        recorded_at="", recorded_from_url="",
        form_layout_hash=form_layout_hash(required),
        required_selectors=required,
        actions=[Action(kind="fill", selector="#first_name", value_source="profile.nope.does_not_exist")],
    )
    result = replay_skill(skill, page, profile)
    assert result.status == STATUS_FAILED
    assert "unresolvable" in (result.error or "")


def test_resolve_value_paths():
    p = {"personal": {"email": "a@b.com"}, "responses": {"why": "because"}}
    assert resolve_value("profile.personal.email", p) == "a@b.com"
    assert resolve_value("profile.responses.why", p) == "because"
    assert resolve_value("literal:hello", p) == "hello"
    assert resolve_value("file:E:\\foo.pdf", p) == "E:\\foo.pdf"
    assert resolve_value(None, p) is None
    with pytest.raises(SkillValidationError):
        resolve_value("profile.missing", p)
    with pytest.raises(SkillValidationError):
        resolve_value("unknownprefix:thing", p)


def test_resolve_value_derives_first_last_from_full_name():
    profile = {"personal": {"full_name": "Nida Shah"}}
    assert resolve_value("profile.personal.first_name", profile) == "Nida"
    assert resolve_value("profile.personal.last_name", profile) == "Shah"

    explicit_profile = {
        "personal": {
            "full_name": "Legal Name",
            "first_name": "Preferred",
            "last_name": "Override",
        }
    }
    assert resolve_value("profile.personal.first_name", explicit_profile) == "Preferred"
    assert resolve_value("profile.personal.last_name", explicit_profile) == "Override"


def test_skill_yaml_roundtrip(tmp_path, tmp_resume):
    """Save → load preserves the skill."""
    required = ["#first_name"]
    original = _synth_skill(form_hash=form_layout_hash(required), tmp_resume_path=tmp_resume,
                            with_unresolved=True)
    p = tmp_path / "synth.yaml"
    save_skill(original, p)
    reloaded = load_skill(p)
    assert reloaded.company == original.company
    assert reloaded.ats == original.ats
    assert reloaded.form_layout_hash == original.form_layout_hash
    assert len(reloaded.actions) == len(original.actions)
    assert reloaded.actions[0].kind == original.actions[0].kind
    assert reloaded.actions[0].value_source == original.actions[0].value_source
    assert len(reloaded.unresolved_fields) == 1


def test_skill_yaml_rejects_unknown_action_kind(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({
        "version": 1, "company": "x", "ats": "greenhouse",
        "form_layout_hash": "sha256:x", "required_selectors": [],
        "actions": [{"kind": "frobnicate", "selector": "#x"}],
    }), encoding="utf-8")
    with pytest.raises(SkillValidationError) as excinfo:
        load_skill(bad)
    assert "frobnicate" in str(excinfo.value).lower() or "kind" in str(excinfo.value).lower()
