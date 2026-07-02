"""Phase 2 done-when test: recorder transforms a stream-json sequence into a
valid skill YAML that the Tier 1 replay engine can subsequently consume.

We simulate the stream-json events Claude Code would have emitted for a
typical Greenhouse apply: fills, an upload, a final submit click. Then
commit() and verify the resulting Skill's structure.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from applypilot.apply.recorder import SkillRecorder, _find_profile_path
from applypilot.apply.skill_schema import load_skill


@pytest.fixture
def profile():
    return {
        "personal": {
            "first_name": "Nida",
            "last_name": "Shah",
            "email": "nida@example.com",
            "phone": "5551234567",
            "city": "San Jose",
        },
        "responses": {
            "why_join": "Excited to work on craft-led design.",
        },
        "work_authorization": {
            "legally_authorized_to_work": "Yes",
        },
    }


@pytest.fixture
def resume_path(tmp_path):
    p = tmp_path / "resume.pdf"
    p.write_bytes(b"%PDF-1.4")
    return str(p)


def test_recorder_basic_capture_commit_yaml(tmp_path, profile, resume_path):
    """A typical apply trace produces a skill YAML the replay engine can load."""
    out = tmp_path / "figma.yaml"
    r = SkillRecorder(
        company="figma", ats="greenhouse",
        apply_url="https://boards.greenhouse.io/figma/jobs/123",
        profile=profile, resume_pdf_path=resume_path,
    )

    # Simulate what Sonnet/Haiku would have emitted, in order
    r.observe_tool_use("browser_navigate", {"url": "https://boards.greenhouse.io/figma/jobs/123"})
    r.observe_tool_use("browser_snapshot", {})
    r.observe_tool_use("browser_fill_form", {"fields": [
        {"name": "first_name", "selector": "#first_name", "value": "Nida"},
        {"name": "last_name",  "selector": "#last_name",  "value": "Shah"},
        {"name": "email",      "selector": "#email",      "value": "nida@example.com"},
        {"name": "phone",      "selector": "#phone",      "value": "5551234567"},
    ]})
    r.observe_tool_use("browser_file_upload", {"selector": "#resume", "paths": [resume_path]})
    r.observe_tool_use("browser_type", {"selector": "#why_join", "value": "Excited to work on craft-led design."})
    r.observe_tool_use("browser_select_option", {"selector": "#work_auth", "value": "Yes"})
    r.observe_tool_use("browser_click", {"selector": "#submit"})

    skill = r.commit(out)

    # Persisted YAML loads back cleanly
    loaded = load_skill(out)
    assert loaded.company == "figma"
    assert loaded.ats == "greenhouse"

    # Required selectors include the things we filled
    assert "#first_name" in loaded.required_selectors
    assert "#email" in loaded.required_selectors

    # Submit got tagged at the end
    assert loaded.actions[-1].kind == "submit"
    assert loaded.actions[-1].selector == "#submit"

    # Value sources resolved to profile paths
    by_sel = {a.selector: a for a in loaded.actions}
    assert by_sel["#first_name"].value_source == "profile.personal.first_name"
    assert by_sel["#first_name"].extra["element_spec"]["elem_id"] == "first_name"
    assert by_sel["#first_name"].extra["element_spec"]["label"] == "first name"
    assert by_sel["#email"].value_source == "profile.personal.email"
    assert by_sel["#why_join"].value_source == "profile.responses.why_join"
    # Profile match wins over literal: fallback when the value IS in the profile.
    # work_authorization.legally_authorized_to_work == "Yes" → that profile path
    # is preferred, since it makes the recipe parametric on the user.
    assert by_sel["#work_auth"].value_source == "profile.work_authorization.legally_authorized_to_work"

    # Resume upload uses file: prefix and matches the resume path we passed
    upload = by_sel["#resume"]
    assert upload.kind == "upload"
    assert upload.value_source.startswith("file:")
    assert resume_path.replace("\\", "/") in upload.value_source.replace("\\", "/")

    # form_layout_hash is populated
    assert loaded.form_layout_hash.startswith("sha256:")


def test_recorder_derives_first_last_from_full_name(tmp_path, resume_path):
    out = tmp_path / "derived-name.yaml"
    profile = {
        "personal": {
            "full_name": "Nida Shah",
            "email": "nida@example.com",
        }
    }
    r = SkillRecorder("derived", "greenhouse", "https://example.com", profile, resume_path)
    r.observe_tool_use("browser_fill_form", {"fields": [
        {"name": "first_name", "selector": "#first_name", "value": "Nida"},
        {"name": "last_name", "selector": "#last_name", "value": "Shah"},
        {"name": "email", "selector": "#email", "value": "nida@example.com"},
    ]})
    r.observe_tool_use("browser_click", {"selector": "#submit"})

    skill = r.commit(out)

    by_sel = {a.selector: a for a in skill.actions}
    assert by_sel["#first_name"].value_source == "profile.personal.first_name"
    assert by_sel["#last_name"].value_source == "profile.personal.last_name"
    assert by_sel["#email"].value_source == "profile.personal.email"
    assert not skill.unresolved_fields


def test_recorder_literal_fallback_for_canonical_answers(tmp_path, resume_path):
    """When 'Yes' / 'No' / 'Decline' doesn't appear anywhere in profile,
    the recorder falls back to a literal: value_source."""
    out = tmp_path / "lit.yaml"
    minimal_profile = {"personal": {"first_name": "X"}}  # no Yes/No anywhere
    r = SkillRecorder("lit", "greenhouse", "https://x", minimal_profile, resume_path)
    r.observe_tool_use("browser_select_option", {"selector": "#work_auth", "value": "Yes"})
    r.observe_tool_use("browser_click", {"selector": "#submit"})
    skill = r.commit(out)
    by_sel = {a.selector: a for a in skill.actions}
    assert by_sel["#work_auth"].value_source == "literal:Yes"


def test_recorder_unresolved_free_text_becomes_unresolved_field(tmp_path, profile, resume_path):
    """A value that doesn't match anywhere in profile → unresolved_fields entry."""
    out = tmp_path / "unknown.yaml"
    r = SkillRecorder(
        company="unknown", ats="greenhouse",
        apply_url="https://example.com/apply",
        profile=profile, resume_pdf_path=resume_path,
    )
    r.observe_tool_use("browser_type", {"selector": "#first_name", "value": "Nida"})
    r.observe_tool_use("browser_type", {
        "selector": "#essay_question",
        "value": "A long free-text answer about my proudest design project that has nothing to do with profile fields.",
    })
    r.observe_tool_use("browser_click", {"selector": "#submit"})

    skill = r.commit(out)

    # Filled-from-profile: kept as an action
    selectors = {a.selector for a in skill.actions}
    assert "#first_name" in selectors

    # Free-text: pulled into unresolved_fields, not an action
    assert any(u.selector == "#essay_question" for u in skill.unresolved_fields)
    assert all(a.selector != "#essay_question" for a in skill.actions)


def test_recorder_final_state_wins(tmp_path, profile, resume_path):
    """If the same selector is filled multiple times, last value wins."""
    out = tmp_path / "dedup.yaml"
    r = SkillRecorder("dedup", "greenhouse", "https://example.com", profile, resume_path)
    # Wrong value first, then corrected
    r.observe_tool_use("browser_type", {"selector": "#email", "value": "wrong@example.com"})
    r.observe_tool_use("browser_type", {"selector": "#email", "value": "nida@example.com"})
    r.observe_tool_use("browser_click", {"selector": "#submit"})

    skill = r.commit(out)
    email_actions = [a for a in skill.actions if a.selector == "#email"]
    assert len(email_actions) == 1
    assert email_actions[0].value_source == "profile.personal.email"


def test_recorder_drops_refs_without_recoverable_label(tmp_path, profile, resume_path):
    """A ref with no `element` description AND no `name` → unrecoverable, drop it."""
    out = tmp_path / "refs.yaml"
    r = SkillRecorder("refs", "greenhouse", "https://example.com", profile, resume_path)
    # ref-only click — no element label, no name → no stable selector derivable
    r.observe_tool_use("browser_click", {"ref": "e21"})
    r.observe_tool_use("browser_click", {"selector": "#submit"})

    skill = r.commit(out)
    # Only the proper-selector click survives (and gets tagged submit)
    assert len(skill.actions) == 1
    assert skill.actions[0].selector == "#submit"


def test_recorder_extracts_text_locator_from_element_description(tmp_path, profile, resume_path):
    """Phase 6 fix: Playwright MCP clicks pass `element` (human description) +
    `ref` (ephemeral). The recorder must extract a text-based locator from
    the element description so the click is replayable across page loads."""
    out = tmp_path / "buttons.yaml"
    r = SkillRecorder("buttons", "greenhouse", "https://example.com", profile, resume_path)
    # Playwright MCP's canonical element format: <role> "<accessible name>"
    r.observe_tool_use("browser_click", {"ref": "e10", "element": 'button "Submit application"'})

    skill = r.commit(out)
    assert len(skill.actions) == 1
    # Promoted to submit (only/last action)
    assert skill.actions[0].kind == "submit"
    assert skill.actions[0].selector == 'text="Submit application"'
    assert skill.actions[0].extra["element_spec"]["role"] == "button"
    assert skill.actions[0].extra["element_spec"]["name"] == "Submit application"


def test_recorder_cleans_mcp_narration_suffixes(tmp_path, profile, resume_path):
    """MCP element strings often append role nouns that are not visible text."""
    out = tmp_path / "suffixes.yaml"
    r = SkillRecorder("buttons", "custom", "https://example.com", profile, resume_path)
    r.observe_tool_use("browser_click", {
        "ref": "e10",
        "element": "Senior Product Designer, CoreUX - Weights & Biases job link",
    })

    skill = r.commit(out)

    assert skill.actions[0].selector == 'text="Senior Product Designer, CoreUX - Weights & Biases"'
    assert skill.actions[0].extra["element_spec"]["role"] == "link"
    assert skill.actions[0].extra["element_spec"]["name"] == "Senior Product Designer, CoreUX - Weights & Biases"


def test_recorder_extracts_name_attribute_from_fill_form(tmp_path, profile, resume_path):
    """Phase 6 fix: real Playwright MCP browser_fill_form passes each field
    with a `name` (= the HTML name attribute) and a `ref` (ephemeral). The
    recorder must produce a `[name="X"]` selector — replay-stable across runs."""
    out = tmp_path / "form.yaml"
    r = SkillRecorder("form", "greenhouse", "https://example.com", profile, resume_path)
    r.observe_tool_use("browser_fill_form", {"fields": [
        {"name": "first_name", "ref": "e21", "value": "Nida"},
        {"name": "email",      "ref": "e22", "value": "nida@example.com"},
    ]})
    r.observe_tool_use("browser_click", {"ref": "e99", "element": 'button "Submit"'})

    skill = r.commit(out)
    selectors = [a.selector for a in skill.actions]
    assert '[name="first_name"]' in selectors
    assert '[name="email"]' in selectors
    by_sel = {a.selector: a for a in skill.actions}
    assert by_sel['[name="email"]'].extra["element_spec"]["name_attr"] == "email"
    # Submit button text-locator captured
    assert any(s == 'text="Submit"' for s in selectors)


def test_recorder_discard_does_not_write_file(tmp_path, profile, resume_path):
    out = tmp_path / "ditched.yaml"
    r = SkillRecorder("ditched", "greenhouse", "https://example.com", profile, resume_path)
    r.observe_tool_use("browser_type", {"selector": "#x", "value": "v"})
    r.discard()
    assert not out.exists()
    with pytest.raises(RuntimeError):
        r.commit(out)


def test_recorder_commit_twice_errors(tmp_path, profile, resume_path):
    out = tmp_path / "once.yaml"
    r = SkillRecorder("once", "greenhouse", "https://example.com", profile, resume_path)
    r.observe_tool_use("browser_type", {"selector": "#a", "value": profile["personal"]["first_name"]})
    r.observe_tool_use("browser_click", {"selector": "#submit"})
    r.commit(out)
    with pytest.raises(RuntimeError, match="twice"):
        r.commit(out)


def test_find_profile_path_longest_match():
    """When multiple paths match, longest wins (more specific)."""
    profile = {
        "personal": {"first_name": "Nida"},
        "alias": "Nida",  # shorter path also matches
    }
    path = _find_profile_path(profile, "Nida")
    assert path == ["personal", "first_name"]  # longer one


def test_recorder_empty_buffer_raises_on_commit(tmp_path, profile, resume_path):
    out = tmp_path / "empty.yaml"
    r = SkillRecorder("empty", "greenhouse", "https://example.com", profile, resume_path)
    r.observe_tool_use("browser_snapshot", {})  # ignored
    r.observe_tool_use("browser_navigate", {"url": "x"})  # ignored
    with pytest.raises(RuntimeError, match="no replay-actionable"):
        r.commit(out)
