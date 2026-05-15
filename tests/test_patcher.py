"""Phase 3 done-when test: patch flow.

Tests the patch prompt builder and the run_patch dispatcher using a mocked
spawn function — no real Claude Code subprocess, no real Chrome. Validates:
  - prompt contains the scoped instructions + the unresolved fields
  - run_patch correctly classifies PATCHED / FAILED:patch_form_drift / etc.
  - empty unresolved list short-circuits to PATCHED (no-op)
  - timeout path returns failed:patch_timeout
"""
from __future__ import annotations

import pytest

from applypilot.apply.patcher import (
    PATCH_FAILED_DRIFT,
    PATCH_FAILED_NO_RESULT,
    PATCH_FAILED_TIMEOUT,
    PATCH_FAILED_UNINTERPRETABLE,
    PATCH_PATCHED,
    _SpawnTimeout,
    _classify_output,
    run_patch,
)
from applypilot.apply.prompt_patch import build_patch_prompt
from applypilot.apply.skill_schema import UnresolvedField


@pytest.fixture
def job():
    return {
        "title": "Senior Product Designer",
        "site": "figma (greenhouse)",
        "fit_score": 8,
    }


@pytest.fixture
def profile():
    return {
        "personal": {
            "full_name": "Nida Shah",
            "email": "nidashah1409@gmail.com",
            "city": "San Jose",
        },
        "experience": {
            "current_title": "Product Designer",
            "years_of_experience_total": "5",
            "education_level": "Master's",
        },
        "resume_facts": {"preserved_companies": ["Intuit", "TalkShopLive"]},
        "skills_boundary": {"tools": ["Figma", "Sketch"]},
    }


@pytest.fixture
def unresolved():
    return [
        UnresolvedField(selector="#essay_8501", label="Why this role?", type="long_text"),
        UnresolvedField(selector="#proudest", label="Project you're proud of", type="long_text"),
    ]


# ---------------------------------------------------------------- prompt builder

def test_patch_prompt_contains_scoped_hard_rules(job, profile, unresolved):
    prompt = build_patch_prompt(
        job=job, apply_url="https://boards.greenhouse.io/figma/jobs/123",
        unresolved=unresolved, profile=profile,
    )
    # The prompt forbids navigate & submit — that's the safety contract
    assert "Do NOT call browser_navigate" in prompt
    assert "Do NOT click Submit" in prompt
    # Mentions both unresolved selectors
    assert "#essay_8501" in prompt
    assert "#proudest" in prompt
    # Uses RESULT:PATCHED not APPLIED
    assert "RESULT:PATCHED" in prompt
    assert "RESULT:APPLIED" not in prompt


def test_patch_prompt_zero_fields_raises(job, profile):
    with pytest.raises(ValueError, match="at least one"):
        build_patch_prompt(
            job=job, apply_url="https://x", unresolved=[], profile=profile,
        )


def test_patch_prompt_includes_compact_profile_summary(job, profile, unresolved):
    prompt = build_patch_prompt(
        job=job, apply_url="https://x", unresolved=unresolved, profile=profile,
    )
    # Profile summary appears
    assert "Nida Shah" in prompt
    assert "Product Designer" in prompt
    # Should be MUCH smaller than the full apply prompt's profile dump
    assert len(prompt) < 5000


# -------------------------------------------------------------- run_patch logic

def _mock_spawn_returning(rc: int, stdout: str):
    """Build a _spawn_fn replacement that returns canned (rc, stdout)."""
    def fn(*, argv, stdin_text, timeout_s, workdir):
        return rc, stdout
    return fn


def _mock_spawn_timeout(partial: str = ""):
    def fn(*, argv, stdin_text, timeout_s, workdir):
        raise _SpawnTimeout(partial)
    return fn


def test_run_patch_zero_unresolved_returns_patched_noop(job, profile):
    res = run_patch(
        job=job, apply_url="https://x", unresolved=[],
        profile=profile, dry_run=False,
        _spawn_fn=_mock_spawn_returning(0, ""),  # would error if called
    )
    assert res.status == PATCH_PATCHED
    assert "no unresolved" in res.output


def test_run_patch_dry_run_short_circuits(job, profile, unresolved):
    res = run_patch(
        job=job, apply_url="https://x", unresolved=unresolved,
        profile=profile, dry_run=True,
        _spawn_fn=_mock_spawn_returning(0, "should not be called"),
    )
    assert res.status == PATCH_PATCHED
    assert "dry_run" in res.output


def test_run_patch_parses_result_patched(job, profile, unresolved):
    stdout = (
        '{"type":"assistant","message":{"content":['
        '{"type":"text","text":"All fields filled."}]}}\n'
        '{"type":"assistant","message":{"content":['
        '{"type":"text","text":"RESULT:PATCHED"}]}}\n'
    )
    res = run_patch(
        job=job, apply_url="https://x", unresolved=unresolved,
        profile=profile, dry_run=False,
        _spawn_fn=_mock_spawn_returning(0, stdout),
    )
    assert res.status == PATCH_PATCHED


def test_run_patch_parses_drift_failure(job, profile, unresolved):
    stdout = (
        '{"type":"assistant","message":{"content":['
        '{"type":"text","text":"RESULT:FAILED:patch_form_drift"}]}}\n'
    )
    res = run_patch(
        job=job, apply_url="https://x", unresolved=unresolved,
        profile=profile, dry_run=False,
        _spawn_fn=_mock_spawn_returning(0, stdout),
    )
    assert res.status == PATCH_FAILED_DRIFT


def test_run_patch_handles_timeout(job, profile, unresolved):
    res = run_patch(
        job=job, apply_url="https://x", unresolved=unresolved,
        profile=profile, dry_run=False,
        _spawn_fn=_mock_spawn_timeout("partial output before kill"),
    )
    assert res.status == PATCH_FAILED_TIMEOUT
    assert res.output == "partial output before kill"


def test_run_patch_no_result_line(job, profile, unresolved):
    stdout = (
        '{"type":"assistant","message":{"content":['
        '{"type":"text","text":"I am thinking but not emitting a result."}]}}\n'
    )
    res = run_patch(
        job=job, apply_url="https://x", unresolved=unresolved,
        profile=profile, dry_run=False,
        _spawn_fn=_mock_spawn_returning(0, stdout),
    )
    assert res.status == PATCH_FAILED_NO_RESULT


def test_classify_output_strips_markdown_bold():
    """Same markdown-resilience as the main launcher's result extractor."""
    stdout = (
        '{"type":"assistant","message":{"content":['
        '{"type":"text","text":"**RESULT:PATCHED**"}]}}\n'
    )
    status, err = _classify_output(stdout, 0)
    assert status == PATCH_PATCHED
    assert err is None


def test_classify_output_uninterpretable():
    stdout = (
        '{"type":"assistant","message":{"content":['
        '{"type":"text","text":"RESULT:FAILED:patch_uninterpretable"}]}}\n'
    )
    status, err = _classify_output(stdout, 0)
    assert status == PATCH_FAILED_UNINTERPRETABLE
