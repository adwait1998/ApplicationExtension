"""Phase 5 done-when test: drift detection + archive + reroute to Tier 3.

Spec validation criterion #4:
> Drift simulation (manually edit `form_layout_hash` to a wrong value in a
> skill) routes to Tier 3 cleanly without crashing.

Two layers of coverage:

1. Unit-level: `verify_skill_integrity` returns the right verdict on clean
   vs. mutated skills, and the dispatcher consults it before launching the
   skill flow.

2. End-to-end with REAL Playwright + a synthetic HTML form: write a skill,
   mutate its hash, hand the YAML to the dispatcher, watch it archive the
   file and fall through to the recorder path (the dispatcher's "record
   mode" branch) — all without a fake skill_flow injection.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from applypilot import config
from applypilot.apply import skill_runner
from applypilot.apply.replay import (
    STATUS_DRIFT_DETECTED,
    STATUS_SUBMITTED,
    form_layout_hash,
    replay_skill,
)
from applypilot.apply.skill_runner import (
    archive_stale_skill,
    dispatch_apply,
    skill_path,
    verify_skill_integrity,
)
from applypilot.apply.skill_schema import (
    Action,
    Skill,
    SuccessSignals,
    load_skill,
    save_skill,
)


# ---------------------------------------------------------------------------
# Synthetic form + skill (mirrors test_skill_replay.py for compatibility)
# ---------------------------------------------------------------------------

_SYNTH_FORM = """
<!doctype html>
<html><body>
<form id="application_form">
  <input id="first_name" name="first_name" type="text">
  <input id="email"      name="email"      type="email">
  <button id="submit_btn" type="button">Submit</button>
  <div id="post_submit" style="display:none">Thank you for applying.</div>
</form>
<script>
document.getElementById('submit_btn').addEventListener('click', () => {
  document.getElementById('post_submit').style.display = 'block';
});
</script>
</body></html>
"""


def _clean_skill() -> Skill:
    required = ["#first_name", "#email", "#submit_btn"]
    return Skill(
        version=1, company="synth", ats="greenhouse",
        recorded_at="2026-05-14T20:00:00Z",
        recorded_from_url="data:text/html,synth",
        form_layout_hash=form_layout_hash(required),
        required_selectors=required,
        actions=[
            Action(kind="fill", selector="#first_name", value_source="profile.personal.first_name"),
            Action(kind="fill", selector="#email",      value_source="profile.personal.email"),
            Action(kind="submit", selector="#submit_btn"),
        ],
        success_signals=SuccessSignals(page_text_contains_any=["thank you"]),
    )


@pytest.fixture
def isolated_app_dir(tmp_path, monkeypatch):
    """Point APP_DIR (and therefore skills/) at a clean per-test dir."""
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    (tmp_path / "skills").mkdir()
    return tmp_path


@pytest.fixture
def profile():
    return {"personal": {"first_name": "Nida", "email": "nida@example.com"}}


# ---------------------------------------------------------------------------
# Unit: verify_skill_integrity
# ---------------------------------------------------------------------------

def test_integrity_ok_on_clean_skill():
    ok, reason = verify_skill_integrity(_clean_skill())
    assert ok is True
    assert reason is None


def test_integrity_fails_on_manual_hash_mutation():
    s = _clean_skill()
    s.form_layout_hash = "sha256:wrongvalue123"
    ok, reason = verify_skill_integrity(s)
    assert ok is False
    assert "form_layout_hash mismatch" in (reason or "")


def test_integrity_ok_when_hash_unset():
    """Hand-edited skills may omit the hash entirely — that's allowed."""
    s = _clean_skill()
    s.form_layout_hash = ""
    ok, reason = verify_skill_integrity(s)
    assert ok is True


def test_integrity_detects_when_selectors_added_without_rehash():
    """If a user adds a required selector but forgets to update the hash,
    integrity must fail — that's exactly the drift signal we care about."""
    s = _clean_skill()
    s.required_selectors = list(s.required_selectors) + ["#new_field"]
    # Hash was computed for the OLD selector list — now stale
    ok, reason = verify_skill_integrity(s)
    assert ok is False


# ---------------------------------------------------------------------------
# Dispatcher behavior on mutated skills (no Chrome needed)
# ---------------------------------------------------------------------------

def test_dispatch_short_circuits_on_integrity_failure(isolated_app_dir):
    """Manual-mutation drift: dispatcher must NOT call skill_flow, must
    archive the file, AND must fall through to run_job with a recorder."""
    skill = _clean_skill()
    skill.form_layout_hash = "sha256:tampered"  # mutate BEFORE save
    skills_d = isolated_app_dir / "skills"
    save_skill(skill, skills_d / "figma.yaml")

    skill_flow_calls = []

    def fake_skill_flow(**kw):
        skill_flow_calls.append(kw)
        return "applied", 100, {}

    run_job_calls = []

    def fake_run_job(**kw):
        run_job_calls.append(kw)
        return "applied", 200, {"ats": "greenhouse"}

    status, _, prefill = dispatch_apply(
        job={"url": "u", "site": "figma (greenhouse)",
             "application_url": "https://boards.greenhouse.io/figma/jobs/1"},
        port=9222, worker_id=0,
        model="haiku", dry_run=False, verify_threshold=0.75,
        run_job_fn=fake_run_job,
        run_job_kwargs={"model": "haiku", "dry_run": False, "retry_count": 0,
                        "profile": {"personal": {}}},
        skill_flow_fn=fake_skill_flow,
        flag_fn=lambda: True,
    )

    # The skill_flow MUST NOT have been invoked (integrity gate caught it
    # before we paid the Chrome+CDP cost)
    assert skill_flow_calls == [], (
        "skill_flow was invoked on a tampered skill — integrity check is not gating"
    )
    # run_job was invoked with a recorder (record-mode fallback)
    assert len(run_job_calls) == 1
    assert run_job_calls[0].get("recorder") is not None
    # Stale file archived
    assert not (skills_d / "figma.yaml").exists()
    archived = list((skills_d / "_archive").glob("figma.*.yaml"))
    assert len(archived) == 1
    # And the apply still completed cleanly via record-mode fallback
    assert status == "applied"


def test_dispatch_does_not_crash_on_repeated_mutation(isolated_app_dir):
    """Calling dispatch twice on a repeatedly-tampered skill must not raise.

    First call archives; second call sees no skill and routes straight to
    record mode."""
    skill = _clean_skill()
    skill.form_layout_hash = "sha256:tampered"
    skills_d = isolated_app_dir / "skills"
    save_skill(skill, skills_d / "figma.yaml")

    def fake_run_job(**kw):
        return "needs_review:timeout", 100, {"ats": "greenhouse"}

    job = {"url": "u", "site": "figma (greenhouse)",
           "application_url": "https://boards.greenhouse.io/figma/jobs/1"}

    # Pass 1 — archives the bad file
    dispatch_apply(
        job=job, port=9222, worker_id=0, model="haiku",
        dry_run=False, verify_threshold=0.75,
        run_job_fn=fake_run_job,
        run_job_kwargs={"model": "haiku", "dry_run": False, "retry_count": 0,
                        "profile": {"personal": {}}},
        flag_fn=lambda: True,
    )

    # Pass 2 — no skill, just goes to record mode
    status, _, _ = dispatch_apply(
        job=job, port=9222, worker_id=0, model="haiku",
        dry_run=False, verify_threshold=0.75,
        run_job_fn=fake_run_job,
        run_job_kwargs={"model": "haiku", "dry_run": False, "retry_count": 0,
                        "profile": {"personal": {}}},
        flag_fn=lambda: True,
    )
    assert status == "needs_review:timeout"


# ---------------------------------------------------------------------------
# End-to-end with REAL Playwright: drift detected at replay layer too
# ---------------------------------------------------------------------------

@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        p = ctx.new_page()
        p.set_content(_SYNTH_FORM)
        yield p
        browser.close()


def test_replay_drift_on_mutated_hash_against_live_page(page, profile):
    """Defense in depth: even if a tampered skill slips past the dispatcher,
    the replay engine's own hash check catches it."""
    skill = _clean_skill()
    skill.form_layout_hash = "sha256:NOT_THE_REAL_HASH"

    result = replay_skill(skill, page, profile)
    assert result.status == STATUS_DRIFT_DETECTED
    assert "form_layout_hash" in (result.error or "")
    # Form was NOT submitted
    assert not page.locator("#post_submit").is_visible()


def test_replay_clean_skill_succeeds_against_same_page(page, profile):
    """Sanity check: the same form + non-mutated skill DOES succeed,
    so the drift test above is actually comparing apples to apples."""
    skill = _clean_skill()
    result = replay_skill(skill, page, profile)
    assert result.status == STATUS_SUBMITTED, f"expected submitted, got {result.status} / {result.error}"
    assert page.locator("#post_submit").is_visible()


# ---------------------------------------------------------------------------
# Archive housekeeping
# ---------------------------------------------------------------------------

def test_archive_uses_utc_timestamp(isolated_app_dir):
    """Two archives of the same company shouldn't collide — UTC-second-grained
    suffix is the discriminator."""
    skills_d = isolated_app_dir / "skills"

    save_skill(_clean_skill(), skills_d / "figma.yaml")
    arch1 = archive_stale_skill("figma")
    assert arch1 is not None
    assert arch1.name.startswith("figma.")
    assert arch1.name.endswith(".yaml")
    # Filename format: figma.<YYYYMMDDTHHMMSSZ>.yaml
    middle = arch1.name[len("figma."):-len(".yaml")]
    assert middle.endswith("Z")
    assert len(middle) == len("YYYYMMDDTHHMMSSZ")
