"""Phase 4 done-when test: launcher integration + feature flag + DB columns.

Done-when criterion (from the spec + ralph config):
> Existing `applypilot apply` flow with flag OFF works unchanged;
> flag ON routes through the new code path.

This file exercises that dispatcher contract without launching a real Chrome
or spawning Claude Code. The skill_runner.dispatch_apply function exposes
explicit injection seams (run_job_fn, skill_flow_fn, resolve_skill_fn,
flag_fn) so we can drive every branch deterministically.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from applypilot import config
from applypilot.apply import skill_runner
from applypilot.apply.skill_runner import (
    DRIFT_FALL_THROUGH,
    FEATURE_FLAG_ENV,
    archive_stale_skill,
    dispatch_apply,
    is_skill_flow_enabled,
    normalize_company_key,
    resolve_skill,
    skill_path,
)
from applypilot.apply.replay import form_layout_hash
from applypilot.apply.skill_schema import (
    Action,
    Skill,
    SkillValidationError,
    SuccessSignals,
    save_skill,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("site,expected", [
    ("figma (greenhouse)", "figma"),
    ("Figma (Greenhouse)", "figma"),
    ("stripe", "stripe"),
    ("Robinhood (Greenhous)", "robinhood"),
    ("airbnb careers", "airbnb_careers"),
    ("", ""),
    (None, ""),
    ("vanta-security", "vanta_security"),
])
def test_normalize_company_key(site, expected):
    assert normalize_company_key(site) == expected


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv(FEATURE_FLAG_ENV, raising=False)
    assert is_skill_flow_enabled() is False


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("True", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("anything-else", False),
])
def test_flag_parses(monkeypatch, value, expected):
    monkeypatch.setenv(FEATURE_FLAG_ENV, value)
    assert is_skill_flow_enabled() is expected


# ---------------------------------------------------------------------------
# Skill resolution
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_skills_dir(tmp_path, monkeypatch):
    """Point APP_DIR (and therefore skills/) at a clean temp dir for each test."""
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    (tmp_path / "skills").mkdir()
    return tmp_path / "skills"


def _make_skill(company: str) -> Skill:
    required = ["input#first_name"]
    return Skill(
        version=1,
        company=company,
        ats="greenhouse",
        recorded_at="2026-05-14T20:30:00Z",
        recorded_from_url=f"https://boards.greenhouse.io/{company}/jobs/1",
        form_layout_hash=form_layout_hash(required),
        required_selectors=required,
        actions=[
            Action(kind="fill", selector="input#first_name", value_source="profile.personal.first_name"),
            Action(kind="submit", selector="button[type='submit']"),
        ],
        success_signals=SuccessSignals(page_text_contains_any=["thank you"]),
    )


def test_resolve_skill_missing_returns_none(isolated_skills_dir):
    assert resolve_skill("figma") is None


def test_resolve_skill_present(isolated_skills_dir):
    save_skill(_make_skill("figma"), isolated_skills_dir / "figma.yaml")
    s = resolve_skill("figma")
    assert s is not None
    assert s.company == "figma"


def test_resolve_skill_empty_company_returns_none(isolated_skills_dir):
    assert resolve_skill("") is None


def test_resolve_skill_malformed_returns_none(isolated_skills_dir):
    (isolated_skills_dir / "broken.yaml").write_text("not: a: valid yaml ::: oops")
    # Malformed file MUST NOT crash dispatcher — falls through to record mode.
    assert resolve_skill("broken") is None


def test_archive_stale_skill_moves_file(isolated_skills_dir):
    p = isolated_skills_dir / "figma.yaml"
    save_skill(_make_skill("figma"), p)
    assert p.exists()
    moved = archive_stale_skill("figma")
    assert moved is not None
    assert moved.exists()
    assert moved.parent.name == "_archive"
    assert not p.exists()


def test_archive_stale_skill_returns_none_when_absent(isolated_skills_dir):
    assert archive_stale_skill("never_recorded") is None


# ---------------------------------------------------------------------------
# Dispatcher — the done-when criterion
# ---------------------------------------------------------------------------

class _RunJobSpy:
    """Mimic launcher.run_job's return-type and record what it was called with."""
    def __init__(self, status: str = "applied", duration_ms: int = 1234,
                 prefill: dict | None = None):
        self.status = status
        self.duration_ms = duration_ms
        self.prefill = prefill or {"ats": "greenhouse", "fields_filled": ["first_name"],
                                    "error": None, "duration_ms": 100}
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.status, self.duration_ms, self.prefill


class _SkillFlowSpy:
    def __init__(self, status: str = "applied", duration_ms: int = 5678,
                 prefill: dict | None = None):
        self.status = status
        self.duration_ms = duration_ms
        self.prefill = prefill or {"via_skill": "figma.yaml",
                                    "replay_duration_ms": 200,
                                    "patch_duration_ms": 0}
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.status, self.duration_ms, self.prefill


@pytest.fixture
def job():
    return {
        "url": "https://boards.greenhouse.io/figma/jobs/1",
        "application_url": "https://boards.greenhouse.io/figma/jobs/1",
        "site": "figma (greenhouse)",
        "title": "Senior Product Designer",
    }


def test_dispatch_flag_off_is_passthrough(job):
    """Flag OFF: dispatcher must call run_job ONCE, with no skill machinery,
    no recorder, and the exact kwargs the caller passed in."""
    run_job = _RunJobSpy(status="applied")
    skill_flow = _SkillFlowSpy()

    status, dur, prefill = dispatch_apply(
        job=job, port=9222, worker_id=0,
        model="haiku", dry_run=False, verify_threshold=0.75,
        run_job_fn=run_job,
        run_job_kwargs={"model": "haiku", "dry_run": False, "retry_count": 0},
        skill_flow_fn=skill_flow,
        flag_fn=lambda: False,
        resolve_skill_fn=lambda c: pytest.fail("must not resolve when flag OFF"),
    )
    assert status == "applied"
    assert dur == 1234
    assert len(run_job.calls) == 1
    assert len(skill_flow.calls) == 0
    # Recorder must NOT be injected in the OFF path
    assert "recorder" not in run_job.calls[0]
    # No skill telemetry in prefill
    assert "skill_used" not in prefill
    assert "recorded_new_skill" not in prefill


def test_dispatch_flag_on_no_skill_attaches_recorder(job, isolated_skills_dir,
                                                     monkeypatch):
    """Flag ON + no saved skill: dispatcher calls run_job WITH a recorder so
    a skill gets written on success."""
    run_job = _RunJobSpy(status="applied")
    monkeypatch.setattr(config, "load_profile", lambda: {"personal": {"first_name": "Nida"}})
    monkeypatch.setattr(config, "RESUME_PDF_PATH", isolated_skills_dir.parent / "resume.pdf")

    status, dur, prefill = dispatch_apply(
        job=job, port=9222, worker_id=0,
        model="haiku", dry_run=False, verify_threshold=0.75,
        run_job_fn=run_job,
        run_job_kwargs={"model": "haiku", "dry_run": False, "retry_count": 0,
                        "profile": {"personal": {"first_name": "Nida"}}},
        skill_flow_fn=_SkillFlowSpy(),  # MUST not be invoked
        flag_fn=lambda: True,
    )
    assert status == "applied"
    assert len(run_job.calls) == 1
    recorder = run_job.calls[0].get("recorder")
    assert recorder is not None, "recorder must be passed when flag is ON and no skill exists"
    # Recorder should record the job's normalized company key
    assert recorder.company == "figma"
    assert recorder.ats == "greenhouse"


def test_dispatch_flag_on_with_skill_routes_skill_flow(job, isolated_skills_dir):
    """Flag ON + saved skill exists: dispatcher calls skill flow, NOT run_job."""
    save_skill(_make_skill("figma"), isolated_skills_dir / "figma.yaml")
    run_job = _RunJobSpy()
    skill_flow = _SkillFlowSpy(status="applied")

    status, dur, prefill = dispatch_apply(
        job=job, port=9222, worker_id=0,
        model="haiku", dry_run=False, verify_threshold=0.75,
        run_job_fn=run_job,
        run_job_kwargs={"model": "haiku", "dry_run": False, "retry_count": 0},
        skill_flow_fn=skill_flow,
        flag_fn=lambda: True,
    )
    assert status == "applied"
    assert dur == 5678
    assert len(run_job.calls) == 0, "run_job must NOT run when a skill exists"
    assert len(skill_flow.calls) == 1
    assert skill_flow.calls[0]["skill"].company == "figma"
    # The dispatcher tags prefill with skill_used so the launcher can persist it
    assert prefill.get("skill_used") == "figma.yaml"


def test_dispatch_drift_archives_and_falls_through_to_record(job, isolated_skills_dir):
    """Skill drift: archive the stale file, run run_job WITH a recorder."""
    save_skill(_make_skill("figma"), isolated_skills_dir / "figma.yaml")
    run_job = _RunJobSpy(status="applied")
    skill_flow = _SkillFlowSpy(status=DRIFT_FALL_THROUGH, duration_ms=900,
                                prefill=None)

    status, dur, prefill = dispatch_apply(
        job=job, port=9222, worker_id=0,
        model="haiku", dry_run=False, verify_threshold=0.75,
        run_job_fn=run_job,
        run_job_kwargs={"model": "haiku", "dry_run": False, "retry_count": 0,
                        "profile": {"personal": {"first_name": "Nida"}}},
        skill_flow_fn=skill_flow,
        flag_fn=lambda: True,
    )
    # Tried skill flow once, then ran run_job WITH a recorder
    assert len(skill_flow.calls) == 1
    assert len(run_job.calls) == 1
    assert run_job.calls[0].get("recorder") is not None
    # Stale skill must have been archived
    assert not (isolated_skills_dir / "figma.yaml").exists()
    arch = list((isolated_skills_dir / "_archive").glob("figma.*.yaml"))
    assert len(arch) == 1
    # Result and duration come from the FALLBACK run_job, not the drift signal
    assert status == "applied"


def test_dispatch_records_skill_on_success(job, isolated_skills_dir, monkeypatch):
    """Flag ON + no skill + run_job returns 'applied': new YAML appears on disk."""
    monkeypatch.setattr(config, "load_profile", lambda: {"personal": {"first_name": "Nida"}})

    class _RecordingRunJob:
        def __init__(self):
            self.recorder = None
        def __call__(self, **kwargs):
            # Mimic what run_job does internally: push tool_use events through
            # the recorder, then return success.
            self.recorder = kwargs.get("recorder")
            assert self.recorder is not None
            self.recorder.observe_tool_use("browser_fill_form", {"fields": [
                {"name": "first_name", "selector": "#first_name", "value": "Nida"},
            ]})
            self.recorder.observe_tool_use("browser_click", {"selector": "button[type='submit']"})
            return "applied", 100, {"ats": "greenhouse"}

    run_job = _RecordingRunJob()

    status, dur, prefill = dispatch_apply(
        job=job, port=9222, worker_id=0,
        model="haiku", dry_run=False, verify_threshold=0.75,
        run_job_fn=run_job,
        run_job_kwargs={"model": "haiku", "dry_run": False, "retry_count": 0,
                        "profile": {"personal": {"first_name": "Nida"}}},
        flag_fn=lambda: True,
    )
    assert status == "applied"
    out = isolated_skills_dir / "figma.yaml"
    assert out.exists(), "successful apply with no prior skill must write a new YAML"
    assert prefill.get("skill_used") == "figma.yaml"
    assert prefill.get("recorded_new_skill") is True


def test_dispatch_discards_recorder_on_failure(job, isolated_skills_dir, monkeypatch):
    """Flag ON + no skill + run_job returns non-applied: no YAML gets written."""
    monkeypatch.setattr(config, "load_profile", lambda: {"personal": {}})
    run_job = _RunJobSpy(status="needs_review:timeout",
                         prefill={"ats": "greenhouse"})

    status, dur, prefill = dispatch_apply(
        job=job, port=9222, worker_id=0,
        model="haiku", dry_run=False, verify_threshold=0.75,
        run_job_fn=run_job,
        run_job_kwargs={"model": "haiku", "dry_run": False, "retry_count": 0,
                        "profile": {"personal": {}}},
        flag_fn=lambda: True,
    )
    assert status == "needs_review:timeout"
    assert not (isolated_skills_dir / "figma.yaml").exists(), \
        "failed apply must NOT write a skill"


# ---------------------------------------------------------------------------
# DB schema migration
# ---------------------------------------------------------------------------

def test_database_columns_added(tmp_path, monkeypatch):
    """Phase 4 adds 3 additive columns. Verify ensure_columns picks them up
    on an older schema, and a fresh init_db has them too."""
    import sqlite3
    from applypilot import database

    db_path = tmp_path / "test.db"

    # Simulate an "older" schema missing the Phase 4 columns
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(database, "DB_PATH", db_path)
    # Wipe any cached connections
    if hasattr(database._local, "connections"):
        database._local.connections = {}

    conn = database.init_db(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "skill_used" in cols
    assert "replay_duration_ms" in cols
    assert "patch_duration_ms" in cols


def test_write_job_runtime_metadata_persists_skill_telemetry(tmp_path, monkeypatch):
    """The _write_job_runtime_metadata helper accepts and persists the 3
    skill columns. The launcher's worker_loop relies on this."""
    import sqlite3
    from applypilot import database
    from applypilot.apply import launcher

    db_path = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    if hasattr(database._local, "connections"):
        database._local.connections = {}

    conn = database.init_db(db_path)
    conn.execute("INSERT INTO jobs(url) VALUES (?)", ("https://example.com/a",))
    conn.commit()

    launcher._write_job_runtime_metadata(
        "https://example.com/a",
        skill_used="figma.yaml",
        replay_duration_ms=200,
        patch_duration_ms=150,
    )
    row = conn.execute(
        "SELECT skill_used, replay_duration_ms, patch_duration_ms "
        "FROM jobs WHERE url = ?",
        ("https://example.com/a",),
    ).fetchone()
    assert row[0] == "figma.yaml"
    assert row[1] == 200
    assert row[2] == 150
