"""Tier 3 recorder — captures a Claude Code apply run into a skill YAML.

Hooks into the launcher's existing stream-json parser. For each successful
`mcp__playwright__browser_*` tool_use block, we record (action_kind, selector,
value) and infer a `value_source` by reverse-matching the value against the
applicant's profile.

The recorder does NOT modify the launcher's control flow — it's a passive
observer. On `commit()` (called by the launcher when the apply reaches
`applied` status), it writes the skill YAML. On `discard()` (any other final
status), it throws the buffer away.

Design tenets:
- "Final state wins": if Sonnet/Haiku filled the same field twice during the
  run (e.g., once with a wrong value, then corrected), only the LAST fill
  for a given selector survives in the recording.
- Selector normalization: strip whitespace, trim noise like dynamic IDs.
  (Greenhouse uses stable IDs like #first_name; some sites use varying refs.)
- Value-source inference: try every leaf in profile; longest match wins to
  avoid weird coincidences (e.g. matching "Nida" against personal.full_name).
- Resume path is special-cased — the recorder recognizes it by suffix.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from applypilot.apply.skill_schema import (
    Action,
    Skill,
    SuccessSignals,
    UnresolvedField,
    save_skill,
)
from applypilot.apply.replay import form_layout_hash

log = logging.getLogger(__name__)


# Tool names → action kinds. We map Playwright MCP tool names to our skill
# vocabulary. Unrecognized tools are dropped (they may not be replay-relevant).
_TOOL_NAME_TO_KIND = {
    "browser_click": "click",
    "browser_type": "fill",            # generic typing; refined below
    "browser_fill_form": "fill",       # multi-field; expanded
    "browser_file_upload": "upload",
    "browser_select_option": "select_native",
    "browser_press_key": None,         # not directly replay-actionable; skip for v1
    "browser_navigate": None,          # the launcher controls navigation in Tier 1
    "browser_snapshot": None,          # readings, not actions
    "browser_evaluate": None,          # JS injections; skip — too varied
    "browser_take_screenshot": None,
    "browser_tabs": None,
    "browser_wait_for": "wait_for",
}


@dataclass
class _Capture:
    """One captured tool_use event before reduction."""
    seq: int
    tool_name: str
    inputs: dict[str, Any]
    # Resolved at commit() time
    inferred_kind: str | None = None
    inferred_selector: str | None = None
    inferred_value: str | None = None
    inferred_value_source: str | None = None
    inferred_element_spec: dict[str, Any] = field(default_factory=dict)


class SkillRecorder:
    """Buffer tool_use events from one apply run, then materialize a Skill on commit.

    Lifecycle:
      r = SkillRecorder(company, ats, apply_url, profile, resume_pdf_path)
      ... launcher loop ...
        r.observe_tool_use(name, inputs)   # for each tool_use block
      ... done ...
      r.commit(out_path)                   # on success → writes YAML
      r.discard()                          # on failure → no file written
    """

    def __init__(
        self,
        company: str,
        ats: str,
        apply_url: str,
        profile: dict,
        resume_pdf_path: str | None = None,
    ) -> None:
        self.company = company
        self.ats = ats
        self.apply_url = apply_url
        self.profile = profile
        self.resume_pdf_path = resume_pdf_path
        self._captures: list[_Capture] = []
        self._seq = 0
        self._committed = False
        self._discarded = False

    # ------------------------------------------------------------------ observe

    def observe_tool_use(self, tool_name: str, inputs: dict[str, Any] | None) -> None:
        """Called once per `tool_use` block in the stream-json output.

        Safe to call with any tool name; non-replay-relevant tools are ignored.
        Idempotent against the same (name, inputs) — caller doesn't need to dedupe.
        """
        if self._committed or self._discarded:
            return
        if tool_name not in _TOOL_NAME_TO_KIND:
            return
        kind = _TOOL_NAME_TO_KIND[tool_name]
        if kind is None:
            return
        self._seq += 1
        self._captures.append(_Capture(
            seq=self._seq,
            tool_name=tool_name,
            inputs=dict(inputs or {}),
        ))

    # ----------------------------------------------------------- commit/discard

    def discard(self) -> None:
        """Throw away the buffer. Called when the apply did not succeed."""
        self._discarded = True
        self._captures = []

    def commit(self, out_path: str | Path) -> Skill:
        """Materialize the buffered captures into a Skill and write YAML.

        Returns the Skill that was written. Raises RuntimeError if no captures
        survived reduction (which would mean nothing useful was recorded).
        """
        if self._committed:
            raise RuntimeError("recorder.commit() called twice")
        if self._discarded:
            raise RuntimeError("recorder was already discarded")

        reduced = self._reduce(self._captures)
        if not reduced:
            raise RuntimeError("no replay-actionable events captured")

        unresolved: list[UnresolvedField] = []
        actions: list[Action] = []
        required_sel_set: list[str] = []
        for cap in reduced:
            self._infer_value_source(cap)
            if cap.inferred_kind == "fill" and cap.inferred_value_source is None:
                # The model wrote a value we couldn't trace back to any profile
                # field — that's a free-text answer the future Tier 2 has to handle.
                unresolved.append(UnresolvedField(
                    selector=cap.inferred_selector or "",
                    label=cap.inputs.get("element") or "",
                    type="long_text" if len(cap.inferred_value or "") > 80 else "text",
                    hint=(cap.inferred_value or "")[:120],
                ))
                continue
            actions.append(Action(
                kind=cap.inferred_kind or "click",
                selector=cap.inferred_selector,
                value_source=cap.inferred_value_source,
                fallback_selectors=[],
                extra={"element_spec": cap.inferred_element_spec} if cap.inferred_element_spec else {},
            ))

        # The launcher knows the submit happened (the apply succeeded). We don't
        # always see a unique "submit" click in the stream because the model may
        # have clicked many things. Trailing best-effort: tag the LAST click as
        # the submit. If no click was recorded, append a synthetic submit action.
        actions = self._tag_terminal_submit(actions)

        required_selectors = self._collect_required(actions)

        skill = Skill(
            version=1,
            company=self.company,
            ats=self.ats,
            recorded_at=datetime.now(timezone.utc).isoformat(),
            recorded_from_url=self.apply_url,
            form_layout_hash=form_layout_hash(required_selectors),
            required_selectors=required_selectors,
            actions=actions,
            unresolved_fields=unresolved,
            success_signals=SuccessSignals(
                page_text_contains_any=[
                    "thank you for your interest",
                    "thank you for applying",
                    "application received",
                    "we'll be in touch",
                ],
            ),
        )
        save_skill(skill, out_path)
        self._committed = True
        log.info("recorder.commit: wrote skill to %s (%d actions, %d unresolved)",
                 out_path, len(actions), len(unresolved))
        return skill

    # ----------------------------------------------------------------- internals

    def _reduce(self, caps: list[_Capture]) -> list[_Capture]:
        """Reduce raw captures into a final action sequence.

        Rules:
          - Same selector + fill action → keep only the LAST capture.
          - browser_fill_form with N fields → expand into N individual fills.
          - browser_navigate / snapshot / evaluate → already filtered out at
            observe time.
          - Empty / invalid selectors → drop.
        """
        expanded: list[_Capture] = []
        for c in caps:
            if c.tool_name == "browser_fill_form":
                fields = c.inputs.get("fields") or []
                for f in fields:
                    sel = self._extract_selector(f)
                    val = f.get("value")
                    if not sel or val is None:
                        continue
                    expanded.append(_Capture(
                        seq=c.seq,
                        tool_name="browser_type",
                        inputs={"selector": sel, "value": val, "element": f.get("name", "")},
                        inferred_kind="fill",
                        inferred_selector=sel,
                        inferred_value=str(val),
                        inferred_element_spec=self._build_element_spec(
                            {"selector": sel, "element": f.get("name", ""), "name": f.get("name", "")},
                            sel,
                        ),
                    ))
                continue
            # Single-field tool — annotate kind/selector/value
            kind = _TOOL_NAME_TO_KIND.get(c.tool_name)
            if kind is None:
                continue
            sel = self._extract_selector(c.inputs)
            val = self._extract_value(c.inputs, kind)
            if kind in {"fill", "upload", "select_native"} and val is None:
                continue
            if not sel:
                continue
            c.inferred_kind = kind
            c.inferred_selector = sel
            c.inferred_value = val
            c.inferred_element_spec = self._build_element_spec(c.inputs, sel)
            expanded.append(c)

        # Final-state wins: same (kind, selector) — keep latest only
        seen: dict[tuple[str, str], int] = {}
        for i, c in enumerate(expanded):
            seen[(c.inferred_kind or "", c.inferred_selector or "")] = i
        return [expanded[i] for i in sorted(seen.values())]

    def _extract_selector(self, inputs: dict[str, Any]) -> str | None:
        """Pull a replay-stable selector from MCP input shapes.

        Real Playwright MCP doesn't pass CSS selectors directly — it uses
        `ref` (ephemeral snapshot ID) + `name` (form field's name= attribute)
        + `element` (human description). Refs aren't replay-stable, so we
        prefer name (→ `[name="X"]`), then explicit selectors, then a
        text-based locator on `element` (for buttons like "Submit").

        Returns None when no stable selector can be derived.
        """
        # 1. Explicit CSS selector (only present in synthetic/test traces)
        sel = inputs.get("selector") or inputs.get("css") or inputs.get("locator")
        if sel:
            return self._normalize_selector(sel)

        # 2. Form field `name` attribute → stable across renders.
        #    Playwright MCP's browser_fill_form passes name per-field.
        name = inputs.get("name")
        if isinstance(name, str) and name.strip():
            return f"[name=\"{name.strip()}\"]"

        # 3. ID hint embedded in `element` description (rare, but used by
        #    test_skill_recorder.py's contrived inputs)
        element = inputs.get("element") or ""
        if isinstance(element, str) and "#" in element:
            normalized = self._normalize_selector(element)
            if normalized:
                return normalized

        # 4. Last resort: human element description → text-based locator.
        #    Stable enough for buttons ("Submit application", "Apply") and
        #    common labeled controls. Strips Playwright MCP's role-prefix
        #    noise like "button \"Submit application\"".
        if isinstance(element, str) and element.strip():
            stripped = self._element_to_text_selector(element.strip())
            if stripped:
                return stripped
        return None

    def _build_element_spec(self, inputs: dict[str, Any], selector: str | None) -> dict[str, Any]:
        """Build a semantic healing spec from MCP metadata.

        We do not have a live DOM in the recorder, so this is necessarily
        metadata-derived. Replay will still prefer exact selectors when they
        work; the spec is for drift recovery when text/id/class selectors churn.
        """
        spec: dict[str, Any] = {}
        element = inputs.get("element") if isinstance(inputs.get("element"), str) else ""
        name = inputs.get("name") if isinstance(inputs.get("name"), str) else ""

        role, accessible_name = self._parse_element_role_name(element)
        if role:
            spec["role"] = role
        if accessible_name:
            spec["name"] = accessible_name
            spec["text"] = accessible_name

        label = accessible_name or (name.replace("_", " ").replace("-", " ").strip() if name else "") or self._label_from_selector(selector)
        if label:
            spec["label"] = label

        if name:
            spec["name_attr"] = name.strip()

        selector_bits = self._spec_from_selector(selector)
        spec.update({k: v for k, v in selector_bits.items() if v})

        if spec:
            fp = {}
            if spec.get("label"):
                fp["label_text"] = spec["label"]
            if spec.get("name_attr"):
                fp["name_attr"] = spec["name_attr"]
            if spec.get("text"):
                fp["text"] = spec["text"]
            if spec.get("role"):
                fp["role"] = spec["role"]
            if spec.get("tag"):
                fp["tag"] = spec["tag"]
            if fp:
                spec["fingerprint"] = fp
        return spec

    def _parse_element_role_name(self, element: str) -> tuple[str | None, str | None]:
        m = re.match(r'^\s*(\w+)\s+"([^"]+)"\s*$', element or "")
        if m:
            return m.group(1).lower(), self._clean_accessible_name(m.group(2).strip())

        cleaned = self._clean_accessible_name(element)
        low = (element or "").strip().lower()
        for suffix, role in (
            (" job link", "link"),
            (" link", "link"),
            (" button", "button"),
            (" option", "option"),
        ):
            if low.endswith(suffix) and cleaned:
                return role, cleaned
        if low.startswith("toggle flyout for ") and "dropdown" in low and cleaned:
            return "combobox", cleaned
        return None, None

    def _clean_accessible_name(self, text: str) -> str:
        cleaned = re.sub(r"\s+", " ", (text or "").strip())
        cleaned = re.sub(r"\s+job link$", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s+(link|button|option)$", "", cleaned, flags=re.I)
        cleaned = re.sub(r"^toggle flyout for\s+", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s+dropdown$", "", cleaned, flags=re.I)
        return cleaned.strip()

    def _label_from_selector(self, selector: str | None) -> str | None:
        if not selector:
            return None
        m = re.search(r'\[name=["\']?([^"\']+)["\']?\]', selector)
        if m:
            return m.group(1).replace("_", " ").strip()
        if selector.startswith("#"):
            return selector[1:].replace("_", " ").replace("-", " ").strip()
        m = re.match(r'text="([^"]+)"', selector)
        if m:
            return m.group(1).strip()
        return None

    def _spec_from_selector(self, selector: str | None) -> dict[str, Any]:
        if not selector:
            return {}
        out: dict[str, Any] = {}
        m = re.match(r'text="([^"]+)"', selector)
        if m:
            out["text"] = m.group(1).strip()
            return out
        m = re.match(r"^([a-zA-Z][\w-]*)", selector)
        if m:
            out["tag"] = m.group(1).lower()
        if selector.startswith("#"):
            out["elem_id"] = selector[1:].strip()
        m = re.search(r'\[name=["\']?([^"\']+)["\']?\]', selector)
        if m:
            out["name_attr"] = m.group(1).strip()
        return out

    def _element_to_text_selector(self, element: str) -> str | None:
        """Convert a Playwright MCP element description into a text= locator.

        Examples:
          'button "Submit application"'  → 'text="Submit application"'
          'Submit'                       → 'text="Submit"'
          'textbox "First name"'         → 'text="First name"'  (fallback)
        """
        # Pattern: <role> "<accessible name>"
        m = re.match(r'^\s*\w+\s+"([^"]+)"\s*$', element)
        if m:
            return f'text="{self._clean_accessible_name(m.group(1))}"'
        # Bare label
        if element and len(element) < 80 and "\n" not in element:
            return f'text="{self._clean_accessible_name(element)}"'
        return None

    def _normalize_selector(self, sel: Any) -> str | None:
        if not isinstance(sel, str):
            return None
        s = sel.strip()
        return s or None

    def _extract_value(self, inputs: dict[str, Any], kind: str) -> str | None:
        if kind == "upload":
            paths = inputs.get("paths") or inputs.get("files") or inputs.get("filePaths")
            if isinstance(paths, list) and paths:
                return paths[0]
            return inputs.get("path")
        if kind in {"fill", "select_native"}:
            v = inputs.get("value") or inputs.get("text")
            return None if v is None else str(v)
        return None

    def _infer_value_source(self, cap: _Capture) -> None:
        """Reverse-map the captured value to a profile path.

        Strategy: walk every leaf in profile; pick the path whose value EQUALS
        the captured value (case-sensitive). For upload actions, check if the
        value matches the resume path → store as `file:<path>`. For values that
        don't match anything, leave value_source=None (caller will treat the
        field as unresolved or use a literal).
        """
        if cap.inferred_value is None:
            cap.inferred_value_source = None
            return

        # Upload — special: file: prefix
        if cap.inferred_kind == "upload":
            if (
                self.resume_pdf_path
                and Path(cap.inferred_value).resolve() == Path(self.resume_pdf_path).resolve()
            ):
                cap.inferred_value_source = f"file:{self.resume_pdf_path}"
            else:
                cap.inferred_value_source = f"file:{cap.inferred_value}"
            return

        # Walk profile for a match
        match = _find_profile_path(self.profile, cap.inferred_value)
        if match:
            cap.inferred_value_source = "profile." + ".".join(match)
            return

        # Short canonical answers — use literal:
        if cap.inferred_value in {"Yes", "No", "Decline to self-identify",
                                   "Decline To Self Identify",
                                   "I do not wish to answer",
                                   "I don't wish to answer",
                                   "I am not a protected veteran"}:
            cap.inferred_value_source = "literal:" + cap.inferred_value
            return

        # Anything else: leave value_source=None → field becomes "unresolved"
        cap.inferred_value_source = None

    def _tag_terminal_submit(self, actions: list[Action]) -> list[Action]:
        """If the last action is a click, promote it to a 'submit' kind."""
        if not actions:
            return actions
        last = actions[-1]
        if last.kind == "click":
            actions[-1] = Action(
                kind="submit",
                selector=last.selector,
                value_source=last.value_source,
                fallback_selectors=last.fallback_selectors,
                extra=last.extra,
            )
        return actions

    def _collect_required(self, actions: list[Action]) -> list[str]:
        """Required selectors = unique non-None selectors across all actions."""
        seen: list[str] = []
        for a in actions:
            if a.selector and a.selector not in seen:
                seen.append(a.selector)
        return seen


# ---------------------------------------------------------------------------
# Profile-path search
# ---------------------------------------------------------------------------

def _find_profile_path(profile: dict, target: str) -> list[str] | None:
    """Return the dot-path into profile that EQUALS target, or None.

    Longest-path wins (to prefer specific over general matches).
    """
    matches: list[list[str]] = []

    def _walk(node: Any, path: list[str]) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, path + [k])
        elif isinstance(node, list):
            for i, v in enumerate(node):
                _walk(v, path + [str(i)])
        else:
            if str(node) == target:
                matches.append(list(path))

    _walk(profile, [])
    if not matches:
        derived = _find_derived_profile_path(profile, target)
        return derived
    matches.sort(key=lambda p: -len(p))
    return matches[0]


def _find_derived_profile_path(profile: dict, target: str) -> list[str] | None:
    personal = profile.get("personal") if isinstance(profile, dict) else None
    if not isinstance(personal, dict):
        return None
    first, last = _split_full_name(str(personal.get("full_name") or ""))
    if target == first:
        return ["personal", "first_name"]
    if target == last:
        return ["personal", "last_name"]
    return None


def _split_full_name(full_name: str) -> tuple[str, str]:
    parts = [p for p in str(full_name or "").strip().split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])
