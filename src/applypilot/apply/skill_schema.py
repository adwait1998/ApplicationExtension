"""Skill YAML schema for the Tier 1 replay engine.

A "skill" is a recorded recipe for filling one company's job application form.
It captures the selectors, the action sequence, and the value-source mapping
(which profile field provides each value). Replay is deterministic Playwright;
no LLM involved.

Schema is intentionally permissive: hand-edited skills work, recorder output
works, future fields can be added without breaking older skills.

See docs/superpowers/specs/2026-05-14-skill-playbook-design.md section 1
for the full design.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# Supported action kinds. Stable identifiers — do NOT rename casually; existing
# skill files reference these by name.
ACTION_KINDS = {
    "fill",            # text input via locator.fill()
    "fill_textarea",   # textarea — same mechanism, separate kind for clarity
    "select_native",   # <select> element via locator.select_option()
    "select_react",    # React-select (toggle → type → enter pattern)
    "select_combobox", # Greenhouse-style combobox (delegates to existing prefill._select_combobox_by_label)
    "upload",          # file input via set_input_files()
    "click",           # generic click (apply-button, next-page, etc.)
    "check",           # checkbox.check()
    "uncheck",         # checkbox.uncheck()
    "wait_for",        # wait for selector visible (between steps)
    "submit",          # click the submit button — separated from "click" so the
                       # replay engine can defer it (e.g., let Tier 2 finish first).
}


# Value-source strings supported by resolve_value(). Either a JSONPath-like
# string into the profile dict, a file: prefix, or a literal: prefix.
#
#   profile.personal.email           → profile["personal"]["email"]
#   profile.responses.why_figma      → profile["responses"]["why_figma"]
#   file:E:\applypilot-data\resume.pdf → absolute file path (no expansion)
#   literal:Decline to self-identify  → exact string
#
# The "literal:" prefix is for values the recorder couldn't resolve to a
# profile path (e.g., a one-off answer). Hand-edit-friendly.


@dataclass
class Action:
    """One step in a skill's replay sequence."""
    kind: str
    selector: str | None = None
    value_source: str | None = None         # how to resolve the value at replay
    fallback_selectors: list[str] = field(default_factory=list)
    # Action-kind-specific extras (kept open so we can add new kinds without
    # touching this dataclass)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in ACTION_KINDS:
            raise SkillValidationError(
                f"Unknown action kind: {self.kind!r}. Supported: {sorted(ACTION_KINDS)}"
            )


@dataclass
class UnresolvedField:
    """A field the recorder saw but couldn't resolve to a profile value.

    Tier 2 LLM patch is invoked at replay time for any of these.
    """
    selector: str
    label: str = ""
    type: str = "text"    # text | long_text | select | checkbox
    hint: str = ""        # optional hint to the LLM (e.g., "answer briefly")


@dataclass
class SuccessSignals:
    """How the verifier knows submission succeeded for this skill.

    Layered on top of the global verifier patterns in launcher._verify_submission_success.
    """
    url_pattern: str | None = None             # regex on window.location after submit
    page_text_contains_any: list[str] = field(default_factory=list)


@dataclass
class Skill:
    """One company's recorded form-fill recipe."""
    version: int
    company: str
    ats: str                                   # greenhouse | lever | ashby | workday | custom
    recorded_at: str                           # ISO 8601
    recorded_from_url: str
    form_layout_hash: str                      # sha256:... — drift detection
    required_selectors: list[str]              # MUST be present on the page
    actions: list[Action]
    unresolved_fields: list[UnresolvedField] = field(default_factory=list)
    success_signals: SuccessSignals = field(default_factory=SuccessSignals)


class SkillValidationError(ValueError):
    """Raised when a skill YAML is malformed or references unknown action kinds."""


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_skill(path: str | Path) -> Skill:
    """Load and validate a skill YAML file. Raises SkillValidationError on failure."""
    p = Path(path)
    if not p.exists():
        raise SkillValidationError(f"Skill file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise SkillValidationError(f"YAML parse error in {p}: {e}") from e
    if not isinstance(raw, dict):
        raise SkillValidationError(f"Skill {p} root is not a mapping")
    return _from_dict(raw)


def save_skill(skill: Skill, path: str | Path) -> None:
    """Serialize a Skill to YAML at the given path. Atomic write (tmp + rename)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(_to_yaml(skill), encoding="utf-8")
    tmp.replace(p)


def _from_dict(raw: dict) -> Skill:
    required_top = ("version", "company", "ats", "form_layout_hash", "actions")
    missing = [k for k in required_top if k not in raw]
    if missing:
        raise SkillValidationError(f"Skill missing required keys: {missing}")
    if raw["version"] != 1:
        raise SkillValidationError(f"Unsupported skill version: {raw['version']!r} (expected 1)")

    actions = []
    for i, a in enumerate(raw.get("actions") or []):
        if not isinstance(a, dict):
            raise SkillValidationError(f"actions[{i}] is not a mapping")
        kind = a.get("kind")
        if not kind:
            raise SkillValidationError(f"actions[{i}] missing 'kind'")
        known = {"kind", "selector", "value_source", "fallback_selectors"}
        extra = {k: v for k, v in a.items() if k not in known}
        actions.append(Action(
            kind=kind,
            selector=a.get("selector"),
            value_source=a.get("value_source"),
            fallback_selectors=a.get("fallback_selectors") or [],
            extra=extra,
        ))

    unresolved = []
    for u in raw.get("unresolved_fields") or []:
        if not isinstance(u, dict):
            raise SkillValidationError("unresolved_fields entry is not a mapping")
        unresolved.append(UnresolvedField(
            selector=u["selector"],
            label=u.get("label", ""),
            type=u.get("type", "text"),
            hint=u.get("hint", ""),
        ))

    signals_raw = raw.get("success_signals") or {}
    signals = SuccessSignals(
        url_pattern=signals_raw.get("url_pattern"),
        page_text_contains_any=signals_raw.get("page_text_contains_any") or [],
    )

    return Skill(
        version=raw["version"],
        company=raw["company"],
        ats=raw["ats"],
        recorded_at=raw.get("recorded_at", ""),
        recorded_from_url=raw.get("recorded_from_url", ""),
        form_layout_hash=raw["form_layout_hash"],
        required_selectors=raw.get("required_selectors") or [],
        actions=actions,
        unresolved_fields=unresolved,
        success_signals=signals,
    )


def _to_yaml(skill: Skill) -> str:
    """Serialize a Skill to YAML. Ordered keys for readability."""
    payload: dict[str, Any] = {
        "version": skill.version,
        "company": skill.company,
        "ats": skill.ats,
        "recorded_at": skill.recorded_at,
        "recorded_from_url": skill.recorded_from_url,
        "form_layout_hash": skill.form_layout_hash,
        "required_selectors": list(skill.required_selectors),
        "actions": [_action_to_dict(a) for a in skill.actions],
    }
    if skill.unresolved_fields:
        payload["unresolved_fields"] = [
            {"selector": u.selector, "label": u.label, "type": u.type, "hint": u.hint}
            for u in skill.unresolved_fields
        ]
    if skill.success_signals.url_pattern or skill.success_signals.page_text_contains_any:
        payload["success_signals"] = {
            "url_pattern": skill.success_signals.url_pattern,
            "page_text_contains_any": list(skill.success_signals.page_text_contains_any),
        }
    return yaml.safe_dump(payload, sort_keys=False, default_flow_style=False, allow_unicode=True)


def _action_to_dict(a: Action) -> dict:
    d: dict[str, Any] = {"kind": a.kind}
    if a.selector is not None:
        d["selector"] = a.selector
    if a.value_source is not None:
        d["value_source"] = a.value_source
    if a.fallback_selectors:
        d["fallback_selectors"] = list(a.fallback_selectors)
    for k, v in a.extra.items():
        d[k] = v
    return d


# ---------------------------------------------------------------------------
# Value resolution
# ---------------------------------------------------------------------------

def resolve_value(value_source: str | None, profile: dict) -> str | None:
    """Resolve a `value_source` string against the profile dict.

    Returns the resolved string, or None if value_source is None.
    Raises SkillValidationError on unknown prefix or unresolvable path.
    """
    if value_source is None:
        return None
    if value_source.startswith("literal:"):
        return value_source[len("literal:"):]
    if value_source.startswith("file:"):
        return value_source[len("file:"):]
    if value_source.startswith("profile."):
        # Dot-path into profile dict
        parts = value_source[len("profile."):].split(".")
        cur: Any = profile
        for p in parts:
            if not isinstance(cur, dict) or p not in cur:
                derived = _resolve_derived_profile_value(parts, profile)
                if derived is not None:
                    return derived
                raise SkillValidationError(
                    f"value_source {value_source!r} unresolvable at segment {p!r}"
                )
            cur = cur[p]
        if not isinstance(cur, (str, int, float, bool)):
            raise SkillValidationError(
                f"value_source {value_source!r} resolved to non-scalar: {type(cur).__name__}"
            )
        return str(cur)
    raise SkillValidationError(
        f"Unknown value_source prefix: {value_source!r}. "
        "Use 'profile.', 'literal:', or 'file:'."
    )


def _resolve_derived_profile_value(parts: list[str], profile: dict) -> str | None:
    """Resolve virtual profile paths derived from canonical profile fields."""
    if parts == ["personal", "first_name"]:
        first, _last = _split_full_name(str((profile.get("personal") or {}).get("full_name") or ""))
        return first or None
    if parts == ["personal", "last_name"]:
        _first, last = _split_full_name(str((profile.get("personal") or {}).get("full_name") or ""))
        return last or None
    return None


def _split_full_name(full_name: str) -> tuple[str, str]:
    parts = [p for p in str(full_name or "").strip().split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])
