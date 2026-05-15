"""Tier 1 replay engine — deterministic Playwright execution of a skill.

No LLM, no MCP, no Claude Code subprocess. Just Python + Playwright sync API
running through the action sequence from a skill YAML.

Returns a ReplayResult so the launcher can decide:
  - `submitted`        → Tier 1 finished the apply on its own; run verifier
  - `needs_patch`      → Tier 1 filled most fields, but skill.unresolved_fields
                         have to be filled by Tier 2 LLM; Tier 1 has NOT
                         clicked submit yet (so Tier 2 can return control here)
  - `drift_detected`   → required_selectors missing / form_layout_hash mismatch;
                         caller should archive the skill and route to Tier 3
  - `failed`           → unrecoverable replay error (e.g., a fill timed out)

Design intent: this engine is INTENTIONALLY conservative. If anything looks
off (selector not found and no fallback works, value resolution fails,
unexpected DOM state), we stop and return one of the non-`submitted` states.
We do NOT try to be clever — that's what Tier 3 re-recording is for.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from applypilot.apply.skill_schema import (
    Action,
    Skill,
    SkillValidationError,
    UnresolvedField,
    resolve_value,
)

if TYPE_CHECKING:
    # Playwright types are heavy; only imported for type hints.
    from playwright.sync_api import Locator, Page

log = logging.getLogger(__name__)


# Status enum (kept as plain strings for grep-ability + JSON serialization).
STATUS_SUBMITTED = "submitted"
STATUS_NEEDS_PATCH = "needs_patch"
STATUS_DRIFT_DETECTED = "drift_detected"
STATUS_FAILED = "failed"


@dataclass
class ReplayResult:
    status: str
    duration_ms: int
    actions_run: int = 0
    unresolved: list[UnresolvedField] = field(default_factory=list)
    error: str | None = None
    # For observability — which action index we stopped on (for "failed" or "needs_patch")
    stopped_at_index: int | None = None


def replay_skill(
    skill: Skill,
    page: "Page",
    profile: dict,
    *,
    dry_run: bool = False,
    interaction_timeout_ms: int = 4000,
) -> ReplayResult:
    """Execute the skill's actions sequentially against the loaded page.

    Args:
        skill: the loaded Skill (already parsed by `skill_schema.load_skill`).
        page: a Playwright Page already navigated to the apply URL.
        profile: user profile dict (from config.load_profile()).
        dry_run: if True, run all actions EXCEPT `submit`. Caller is in test mode.
        interaction_timeout_ms: per-action timeout for Playwright interactions.

    Returns: ReplayResult describing what happened.
    """
    started = time.monotonic()
    actions_run = 0

    # Drift check #1: required_selectors must be present.
    missing = _missing_required(page, skill.required_selectors, interaction_timeout_ms)
    if missing:
        return ReplayResult(
            status=STATUS_DRIFT_DETECTED,
            duration_ms=_elapsed_ms(started),
            error=f"required_selectors missing: {missing[:3]}",
        )

    # Drift check #2: form_layout_hash consistency. We re-hash the recorded
    # required_selectors and compare to the recorded hash. This catches the
    # "manual mutation" case from spec validation criterion #4 — if a user
    # (or a corrupted write) edited form_layout_hash without re-deriving it
    # from the selector list, the file is structurally inconsistent and
    # we should archive + re-record rather than trust the recipe.
    # (skill_runner.verify_skill_integrity also runs this check up-front,
    #  before launching Chrome — this is the defense in depth.)
    live_hash = _form_layout_hash(skill.required_selectors)
    if skill.form_layout_hash and live_hash != skill.form_layout_hash:
        return ReplayResult(
            status=STATUS_DRIFT_DETECTED,
            duration_ms=_elapsed_ms(started),
            error=f"form_layout_hash mismatch: recorded={skill.form_layout_hash[:12]}... live={live_hash[:12]}...",
        )

    # Execute actions
    submit_deferred = bool(skill.unresolved_fields)
    for idx, action in enumerate(skill.actions):
        try:
            if action.kind == "submit":
                if dry_run:
                    log.info("dry_run: skipping submit action")
                    actions_run += 1
                    continue
                if submit_deferred:
                    # Don't submit yet — Tier 2 still has unresolved fields to fill.
                    # Caller will run Tier 2, then call submit_only() below.
                    return ReplayResult(
                        status=STATUS_NEEDS_PATCH,
                        duration_ms=_elapsed_ms(started),
                        actions_run=actions_run,
                        unresolved=list(skill.unresolved_fields),
                        stopped_at_index=idx,
                    )
                _execute_action(action, page, profile, interaction_timeout_ms)
                actions_run += 1
            else:
                _execute_action(action, page, profile, interaction_timeout_ms)
                actions_run += 1
        except SkillValidationError as e:
            return ReplayResult(
                status=STATUS_FAILED,
                duration_ms=_elapsed_ms(started),
                actions_run=actions_run,
                error=f"value resolution: {e}",
                stopped_at_index=idx,
            )
        except Exception as e:  # broad — any Playwright failure
            return ReplayResult(
                status=STATUS_FAILED,
                duration_ms=_elapsed_ms(started),
                actions_run=actions_run,
                error=f"{type(e).__name__}: {e}",
                stopped_at_index=idx,
            )

    # All actions ran. If there were no unresolved fields AND a submit was in
    # the action list, we're done. If no submit was recorded, that's a recording
    # bug — flag it as drift (caller will re-record).
    has_submit = any(a.kind == "submit" for a in skill.actions)
    if skill.unresolved_fields and not submit_deferred:
        # Shouldn't happen — defensive
        return ReplayResult(
            status=STATUS_NEEDS_PATCH,
            duration_ms=_elapsed_ms(started),
            actions_run=actions_run,
            unresolved=list(skill.unresolved_fields),
        )
    if not has_submit:
        return ReplayResult(
            status=STATUS_DRIFT_DETECTED,
            duration_ms=_elapsed_ms(started),
            actions_run=actions_run,
            error="skill has no 'submit' action (recording incomplete?)",
        )
    return ReplayResult(
        status=STATUS_SUBMITTED,
        duration_ms=_elapsed_ms(started),
        actions_run=actions_run,
    )


def submit_only(skill: Skill, page: "Page", interaction_timeout_ms: int = 4000) -> ReplayResult:
    """Run only the trailing `submit` action(s) of a skill.

    Used by the launcher after Tier 2 patch completes — Tier 1 paused at
    `submit_deferred`, Tier 2 filled the unresolved fields, now Tier 1 wraps up.
    """
    started = time.monotonic()
    submit_actions = [a for a in skill.actions if a.kind == "submit"]
    if not submit_actions:
        return ReplayResult(
            status=STATUS_FAILED,
            duration_ms=_elapsed_ms(started),
            error="no submit action in skill",
        )
    try:
        for a in submit_actions:
            _execute_action(a, page, profile={}, timeout_ms=interaction_timeout_ms)
        return ReplayResult(
            status=STATUS_SUBMITTED,
            duration_ms=_elapsed_ms(started),
            actions_run=len(submit_actions),
        )
    except Exception as e:
        return ReplayResult(
            status=STATUS_FAILED,
            duration_ms=_elapsed_ms(started),
            error=f"{type(e).__name__}: {e}",
        )


# ---------------------------------------------------------------------------
# Action execution
# ---------------------------------------------------------------------------

def _execute_action(action: Action, page: "Page", profile: dict, timeout_ms: int) -> None:
    """Dispatch one action to its handler. Raises on failure."""
    handler = _ACTION_HANDLERS.get(action.kind)
    if handler is None:
        raise SkillValidationError(f"No handler for action kind: {action.kind}")
    handler(action, page, profile, timeout_ms)


def _action_fill(action: Action, page: "Page", profile: dict, timeout_ms: int) -> None:
    value = resolve_value(action.value_source, profile)
    if value is None:
        raise SkillValidationError(f"fill action has no value_source: {action.selector}")
    loc = _resolve_locator(page, action)
    loc.fill(value, timeout=timeout_ms)


def _action_fill_textarea(action: Action, page: "Page", profile: dict, timeout_ms: int) -> None:
    # Same code path as fill — separate kind only for clarity / future divergence.
    _action_fill(action, page, profile, timeout_ms)


def _action_select_native(action: Action, page: "Page", profile: dict, timeout_ms: int) -> None:
    value = resolve_value(action.value_source, profile)
    if value is None:
        raise SkillValidationError(f"select_native has no value_source: {action.selector}")
    loc = _resolve_locator(page, action)
    loc.select_option(value, timeout=timeout_ms)


def _action_select_react(action: Action, page: "Page", profile: dict, timeout_ms: int) -> None:
    """Click toggle → type → enter pattern for react-select dropdowns."""
    toggle_sel = action.extra.get("toggle_selector") or action.selector
    if toggle_sel is None:
        raise SkillValidationError("select_react requires toggle_selector or selector")
    type_value = action.extra.get("type_value")
    if type_value is None:
        type_value = resolve_value(action.value_source, profile)
    if type_value is None:
        raise SkillValidationError("select_react needs type_value or value_source")
    page.locator(toggle_sel).first.click(timeout=timeout_ms)
    page.keyboard.type(type_value, delay=20)
    page.keyboard.press("Enter")


def _action_select_combobox(action: Action, page: "Page", profile: dict, timeout_ms: int) -> None:
    """Greenhouse-style combobox. Delegates to the proven helper in prefill.py."""
    from applypilot.apply import prefill
    value = resolve_value(action.value_source, profile)
    if value is None:
        raise SkillValidationError(f"select_combobox has no value_source: {action.selector}")
    # prefill._select_combobox_by_label expects a label needle and a value.
    label = action.extra.get("label", "")
    if not label:
        raise SkillValidationError("select_combobox requires extra.label (text near the combobox)")
    ok = prefill._select_combobox_by_label(page, label, value)
    if not ok:
        raise RuntimeError(f"select_combobox failed for label={label!r}, value={value!r}")


def _action_upload(action: Action, page: "Page", profile: dict, timeout_ms: int) -> None:
    value = resolve_value(action.value_source, profile)
    if value is None:
        raise SkillValidationError(f"upload has no value_source: {action.selector}")
    loc = _resolve_locator(page, action)
    loc.set_input_files(value, timeout=timeout_ms)


def _action_click(action: Action, page: "Page", profile: dict, timeout_ms: int) -> None:
    loc = _resolve_locator(page, action)
    loc.click(timeout=timeout_ms)


def _action_check(action: Action, page: "Page", profile: dict, timeout_ms: int) -> None:
    loc = _resolve_locator(page, action)
    loc.check(timeout=timeout_ms)


def _action_uncheck(action: Action, page: "Page", profile: dict, timeout_ms: int) -> None:
    loc = _resolve_locator(page, action)
    loc.uncheck(timeout=timeout_ms)


def _action_wait_for(action: Action, page: "Page", profile: dict, timeout_ms: int) -> None:
    loc = _resolve_locator(page, action)
    loc.wait_for(state="visible", timeout=timeout_ms)


def _action_submit(action: Action, page: "Page", profile: dict, timeout_ms: int) -> None:
    loc = _resolve_locator(page, action)
    loc.click(timeout=timeout_ms)


_ACTION_HANDLERS = {
    "fill": _action_fill,
    "fill_textarea": _action_fill_textarea,
    "select_native": _action_select_native,
    "select_react": _action_select_react,
    "select_combobox": _action_select_combobox,
    "upload": _action_upload,
    "click": _action_click,
    "check": _action_check,
    "uncheck": _action_uncheck,
    "wait_for": _action_wait_for,
    "submit": _action_submit,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_locator(page: "Page", action: Action) -> "Locator":
    """Find a working locator from action.selector + action.fallback_selectors.

    Returns the first locator that exists (count() > 0). Raises if none work.
    """
    if action.selector is None:
        raise SkillValidationError(f"action {action.kind} has no selector")
    candidates = [action.selector, *action.fallback_selectors]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    raise RuntimeError(f"no selector matched: {candidates!r}")


def _missing_required(page: "Page", required: list[str], timeout_ms: int) -> list[str]:
    """Return the subset of required_selectors NOT present on the page."""
    missing = []
    for sel in required:
        try:
            if page.locator(sel).first.count() <= 0:
                missing.append(sel)
        except Exception:
            missing.append(sel)
    return missing


def form_layout_hash(selectors: list[str]) -> str:
    """Stable hash over the sorted selector list. Public for the recorder."""
    return _form_layout_hash(selectors)


def _form_layout_hash(selectors: list[str]) -> str:
    canonical = "\n".join(sorted(selectors))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _elapsed_ms(start_monotonic: float) -> int:
    return int((time.monotonic() - start_monotonic) * 1000)
