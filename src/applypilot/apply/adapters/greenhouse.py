"""Reliability-v2 Phase C: deterministic Greenhouse adapter (zero LLM).

One adapter for the *ATS platform*, not per company — a Greenhouse form is
~identical across all 50+ Greenhouse companies. Fields are described
SEMANTICALLY (label / role) and resolved through the self-healing locator
core (`healing.heal`), so the adapter survives the id/class churn that made
MCP-recorded skills non-replay-grade.

Contract: fill every STANDARD field deterministically; collect any
remaining *required* field the adapter can't map into `unresolved` so the
Tier-2 LLM patch (not the adapter) handles genuine novelty; optionally
submit. `used_llm` is always False — the adapter never calls a model.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from applypilot.apply.healing import ElementSpec, heal
from applypilot.apply.prefill import (
    _match_real_option,
    _split_name,
    _visible_combobox_options,
    _click_option_by_text,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page

log = logging.getLogger(__name__)

_DECLINE = (
    "decline to self-identify", "decline", "i do not wish to answer",
    "i don't wish to answer", "prefer not to answer", "prefer not",
    "i don't wish to disclose",
)


@dataclass
class AdapterResult:
    fields_filled: list[str] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)   # → Tier-2 LLM
    submitted: bool = False
    error: str | None = None
    used_llm: bool = False                                  # invariant: never True


def _profile(p: dict, *path, default: str = "") -> str:
    cur: Any = p
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return "" if cur is None else str(cur)


def _yn(v: str) -> str:
    return "Yes" if str(v).strip().lower() in ("true", "yes", "1") else "No"


def _standard_plan(profile: dict, resume_pdf_path: str) -> list[dict]:
    """The standard Greenhouse field set as semantic specs + resolved value."""
    full = _profile(profile, "personal", "full_name")
    first, last = _split_name(full)
    eeo = profile.get("eeo_voluntary", {}) or {}
    wa = profile.get("work_authorization", {}) or {}
    return [
        {"key": "first_name", "labels": ["first name", "legal first name"],
         "kind": "text", "value": first},
        {"key": "last_name", "labels": ["last name", "legal last name", "surname"],
         "kind": "text", "value": last},
        {"key": "email", "labels": ["email"], "role": "textbox",
         "kind": "text", "value": _profile(profile, "personal", "email")},
        {"key": "phone", "labels": ["phone", "mobile"], "kind": "text",
         "value": _profile(profile, "personal", "phone")},
        {"key": "location", "labels": ["location", "city", "where are you"],
         "kind": "text", "value": _profile(profile, "personal", "city")},
        {"key": "linkedin", "labels": ["linkedin"], "kind": "text",
         "value": _profile(profile, "personal", "linkedin_url")},
        {"key": "portfolio", "labels": ["portfolio", "website", "personal site"],
         "kind": "text", "value": _profile(profile, "personal", "portfolio_url")},
        {"key": "resume", "labels": ["resume", "cv", "resume/cv"],
         "kind": "file", "value": resume_pdf_path},
        {"key": "work_authorization",
         "labels": ["authorized to work", "legally authorized", "work authorization",
                    "eligible to work", "currently authorized"],
         "kind": "combobox",
         "preferred": ((_yn(wa.get("legally_authorized_to_work", "")),)
                       + ("yes, i am authorized", "authorized"))},
        {"key": "sponsorship",
         "labels": ["sponsorship", "require sponsorship", "need sponsorship", "visa"],
         "kind": "combobox",
         "preferred": (_yn(wa.get("require_sponsorship", "")),)},
        {"key": "gender", "labels": ["gender"], "kind": "combobox",
         "preferred": (eeo.get("gender", ""),) + _DECLINE},
        {"key": "race", "labels": ["race", "ethnicity", "race/ethnicity"],
         "kind": "combobox",
         "preferred": (eeo.get("race_ethnicity", ""),) + _DECLINE},
        {"key": "veteran", "labels": ["veteran"], "kind": "combobox",
         "preferred": (eeo.get("veteran_status", ""), "i am not a protected veteran",
                       "not a protected veteran") + _DECLINE},
        {"key": "disability", "labels": ["disability"], "kind": "combobox",
         "preferred": (eeo.get("disability_status", ""),
                       "no, i do not have a disability",
                       "i don't have a disability") + _DECLINE},
    ]


def _heal_any(page: "Page", labels: list[str], role: str | None,
              tag_hint: str, type_hint: str = "", timeout_ms: int = 1200):
    """Try each label synonym until the self-healing locator finds something.
    Returns (locator, tier, matched_label) or (None, '', None)."""
    for lab in labels:
        spec = ElementSpec(
            label=lab, name=lab, role=role,
            fingerprint={"label_text": lab, "tag": tag_hint, "type": type_hint},
        )
        try:
            loc, tier = heal(page, spec, timeout_ms=timeout_ms)
        except Exception:
            loc, tier = None, ""
        if loc is not None:
            try:
                if loc.count() > 0:
                    return loc, tier, lab
            except Exception:
                continue
    return None, "", None


def _combobox_input(page: "Page", control):
    """react-select's typing target is its inner search <input>. heal() may
    return the input, the control <div>, or a label-associated node — resolve
    to the actual input to type into."""
    try:
        tag = (control.evaluate("e => e.tagName") or "").lower()
    except Exception:
        tag = ""
    if tag == "input":
        return control
    for sel in ('input.select__input', '[class*="select__control"] input',
                'input[id*="react-select" i]', '[role="combobox"]', 'input'):
        try:
            inner = control.locator(sel).first
            if inner.count() > 0:
                return inner
        except Exception:
            continue
    # Maybe heal returned a label/sibling — search the nearest field container.
    try:
        cont = control.locator(
            "xpath=ancestor-or-self::*[self::div or self::fieldset or self::li][1]"
        ).first
        for sel in ('input.select__input', '[class*="select__control"] input',
                    '[role="combobox"]', 'input'):
            inner = cont.locator(sel).first
            if inner.count() > 0:
                return inner
    except Exception:
        pass
    return control


def _select_combobox(page: "Page", control, preferred: tuple[str, ...],
                     interaction_ms: int) -> bool:
    """Option-aware react-select commit on an already-located control.

    Open → read REAL options → map intent to a real option → type that
    (guaranteed to filter non-empty) → ArrowDown+Enter → verify. If no
    preferred maps to a real option, return False (caller marks the field
    unresolved → Tier-2). Never blind-types a non-existent value.
    """
    try:
        control.scroll_into_view_if_needed(timeout=interaction_ms)
        control.click(timeout=interaction_ms)   # open the menu
        time.sleep(0.3)
    except Exception:
        return False
    options = _visible_combobox_options(page)
    target = _match_real_option(tuple(p for p in preferred if p), options)
    if target is None:
        return False
    typ = _combobox_input(page, control)        # react-select inner <input>
    try:
        try:
            typ.click(timeout=1500)
            typ.fill("", timeout=800)
        except Exception:
            pass
        typ.type(target[:40], delay=18, timeout=2500)
        time.sleep(0.25)
        typ.press("ArrowDown", timeout=800)
        typ.press("Enter", timeout=800)
        time.sleep(0.25)
        # verify: the rendered single-value now shows the chosen option
        shown = (typ.evaluate(
            "el => { const c = el.closest('[class*=select__control],[class*=Select__control],fieldset,li,div');"
            " return c ? (c.innerText||'') : ''; }"
        ) or "").lower()
        if target.lower()[:12] in shown:
            return True
        if _click_option_by_text(page, target):
            time.sleep(0.2)
            return True
    except Exception as e:
        log.debug("greenhouse adapter combobox commit failed (%r): %s", target, e)
    return False


def _required_labels_on_page(page: "Page") -> list[str]:
    """Labels of required form controls, for the unresolved (Tier-2) sweep."""
    try:
        return list(page.evaluate(
            """() => {
              const norm = s => (s||'').replace(/\\s+/g,' ').trim();
              const out = [];
              const reqd = document.querySelectorAll(
                '[required], [aria-required="true"], [class*="required"]');
              for (const el of reqd) {
                let t = '';
                if (el.id) { const L=document.querySelector('label[for=\"'+CSS.escape(el.id)+'\"]'); if(L) t=L.textContent; }
                if (!t) { const W=el.closest('label'); if(W) t=W.textContent; }
                if (!t) { const c=el.closest('div,fieldset,li'); const L=c&&c.querySelector('label,legend'); if(L) t=L.textContent; }
                t = norm(t);
                if (t && t.length < 160) out.push(t);
              }
              return [...new Set(out)];
            }"""
        )) or []
    except Exception:
        return []


def fill_greenhouse(page: "Page", profile: dict, resume_pdf_path: str, *,
                    submit: bool = True, interaction_ms: int = 4000) -> AdapterResult:
    """Deterministically fill a Greenhouse form. Zero LLM."""
    res = AdapterResult()
    plan = _standard_plan(profile, resume_pdf_path)
    matched_labels: list[str] = []

    for fld in plan:
        value = fld.get("value", "")
        kind = fld["kind"]
        if kind in ("text", "file") and not value:
            continue
        tag_hint = "input"
        type_hint = "file" if kind == "file" else ("email" if fld["key"] == "email" else "")
        loc, tier, _ = _heal_any(page, fld["labels"], fld.get("role"),
                                 tag_hint, type_hint, timeout_ms=1200)
        if loc is None:
            continue
        try:
            if kind == "text":
                loc.fill(str(value), timeout=interaction_ms)
                res.fields_filled.append(fld["key"])
                matched_labels.append(fld["labels"][0])
            elif kind == "file":
                loc.set_input_files(value, timeout=interaction_ms)
                res.fields_filled.append(fld["key"])
                matched_labels.append(fld["labels"][0])
            elif kind == "combobox":
                if _select_combobox(page, loc, tuple(fld.get("preferred", ())),
                                    interaction_ms):
                    res.fields_filled.append(fld["key"])
                    matched_labels.append(fld["labels"][0])
        except Exception as e:
            log.debug("fill failed for %s (tier=%s): %s", fld["key"], tier, e)

    # Anything REQUIRED on the page we didn't map → Tier-2 LLM, not the
    # adapter. The adapter never guesses free-text / unknown controls.
    norm_matched = [m.lower() for m in matched_labels]
    for lbl in _required_labels_on_page(page):
        low = lbl.lower()
        if any(m in low or low in m for m in norm_matched):
            continue
        if any(low in " ".join(f["labels"]) or
               any(n in low for n in f["labels"]) for f in plan):
            continue
        res.unresolved.append({"label": lbl, "type": "unknown"})

    if submit:
        btn_spec = ElementSpec(role="button", name="Submit application",
                               label="Submit application", text="Submit application",
                               fingerprint={"tag": "button",
                                            "label_text": "submit application"})
        try:
            bloc, _ = heal(page, btn_spec, timeout_ms=1200)
            if bloc is not None:
                bloc.click(timeout=interaction_ms)
                res.submitted = True
            else:
                res.error = "submit button not found"
        except Exception as e:
            res.error = f"submit failed: {type(e).__name__}: {e}"

    return res
