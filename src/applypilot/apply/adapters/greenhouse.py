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
                       + ("yes, i am authorized", "yes, i am legally authorized"))},
        {"key": "sponsorship",
         "labels": ["sponsorship", "require sponsorship", "need sponsorship", "visa"],
         "kind": "combobox",
         "preferred": (_yn(wa.get("require_sponsorship", "")),)},
        # Voluntary self-ID dropdowns (gusto et al. add these beyond the
        # standard EEO set). They ALWAYS have a safe canonical answer
        # (decline) — resolve deterministically so submit='auto' fires and
        # the LLM is never invoked. Specific labels come BEFORE the generic
        # "gender" spec so "...transgender..." isn't hijacked by it.
        {"key": "sexual_orientation", "labels": ["sexual orientation"],
         "kind": "combobox",
         "preferred": (eeo.get("sexual_orientation", ""),) + _DECLINE},
        {"key": "gender_identity",
         "labels": ["gender identity", "do you identify as transgender",
                    "identify as transgender", "transgender"],
         "kind": "combobox",
         "preferred": (eeo.get("gender_identity", ""),) + _DECLINE},
        {"key": "first_generation",
         "labels": ["first-generation professional",
                    "first generation professional",
                    "first-generation college", "first generation college",
                    "first-generation", "first generation"],
         "kind": "combobox",
         "preferred": (eeo.get("first_generation", ""),) + _DECLINE},
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
    if _polarity_conflict(preferred, target):
        log.debug("greenhouse adapter rejected polarity-conflicting option %r for %r", target, preferred)
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


def _polarity_conflict(preferred: tuple[str, ...], target: str) -> bool:
    """Avoid fuzzy matching an affirmative intent to a negative option."""
    first = next((str(p).strip().lower() for p in preferred if str(p).strip()), "")
    low = str(target or "").strip().lower()
    if not first or not low:
        return False
    wants_yes = first == "yes" or first.startswith("yes,") or first.startswith("yes ")
    wants_no = first == "no" or first.startswith("no,") or first.startswith("no ")
    target_negative = bool(
        low == "no"
        or low.startswith("no,")
        or low.startswith("no ")
        or " not " in f" {low} "
        or "n't" in low
    )
    target_affirmative = low == "yes" or low.startswith("yes,") or low.startswith("yes ")
    return (wants_yes and target_negative) or (wants_no and target_affirmative)


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


def _missing_required_labels_on_page(page: "Page") -> list[str]:
    """Required labels whose associated control is still empty or invalid."""
    try:
        return list(page.evaluate(
            """() => {
              const norm = s => (s||'').replace(/\\s+/g,' ').trim();
              const visible = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const cs = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
              };
              const labelFor = el => {
                let t = '';
                if (el.id) {
                  const L=document.querySelector('label[for=\"'+CSS.escape(el.id)+'\"]');
                  if(L) t=L.textContent;
                }
                if (!t) {
                  const W=el.closest('label');
                  if(W) t=W.textContent;
                }
                if (!t) {
                  const c=el.closest('div,fieldset,li');
                  const L=c&&c.querySelector('label,legend');
                  if(L) t=L.textContent;
                }
                return norm(t);
              };
              const hasValue = el => {
                if (!el) return false;
                const tag = (el.tagName || '').toLowerCase();
                const type = (el.type || '').toLowerCase();
                if (type === 'checkbox' || type === 'radio') {
                  if (el.name) return !!document.querySelector(`[name="${CSS.escape(el.name)}"]:checked`);
                  return !!el.checked;
                }
                if (type === 'file') return !!(el.files && el.files.length);
                const aria = (el.getAttribute('aria-invalid') || '').toLowerCase();
                if (aria === 'true') return false;
                if (el.matches(':invalid')) return false;
                const c = el.closest('[class*=select__control],[class*=Select__control],fieldset,li,div');
                const text = norm(c ? c.innerText : '');
                const value = norm(el.value);
                if (el.getAttribute('role') === 'combobox' || /select__input|react-select/i.test(el.className || '')) {
                  return !!value || (!!text && !/^select\\.\\.\\.?$/i.test(text));
                }
                if (tag === 'select') return !!el.value;
                return !!value;
              };
              const out = [];
              const reqd = Array.from(document.querySelectorAll(
                '[required], [aria-required="true"], [class*="required"]'
              )).filter(visible);
              for (const el of reqd) {
                if (hasValue(el)) continue;
                const label = labelFor(el);
                if (label && label.length < 160) out.push(label);
              }
              return [...new Set(out)];
            }"""
        )) or []
    except Exception:
        return []


def _answer_context(profile: dict) -> str:
    """Compact applicant context for the answer-cache on novel free-text."""
    per = profile.get("personal", {}) or {}
    exp = profile.get("experience", {}) or {}
    bits = []
    if per.get("full_name"):
        bits.append(f"Name: {per['full_name']}")
    if exp.get("current_title"):
        bits.append(f"Role: {exp['current_title']}")
    if exp.get("years_of_experience_total"):
        bits.append(f"Experience: {exp['years_of_experience_total']} years")
    return " | ".join(bits)


def _form_scope(page: "Page"):
    """The Greenhouse form is often embedded in a child iframe (vanity
    careers sites like careers.roblox.com). Return the Page or the child
    Frame that actually contains the application form so locators resolve.
    Frame has the same locator/get_by_role/evaluate API as Page, so the
    rest of the adapter is scope-agnostic."""
    try:
        frames = list(page.frames)
    except Exception:
        return page
    for fr in frames:
        try:
            if fr.locator(
                '#first_name, input[name*="first" i], '
                'input[autocomplete="given-name"], '
                '[class*="select__control"]'
            ).count() > 0:
                return fr
        except Exception:
            continue
    return page


def _post_submit_verdict(button_gone: bool, errors_visible: bool,
                         button_enabled: bool, deadline_hit: bool) -> tuple[bool | None, str | None]:
    """Pure decision core for post-click submit confirmation.

    Returns (verdict, error): verdict True = submitted, False = not
    submitted, None = keep polling. Click success != submission —
    Greenhouse keeps the form on screen with field errors when anything
    is invalid; previously that still reported submitted=True, the LLM
    was skipped, and the job landed in needs_review:unverified_submission
    with zero chance of recovery.
    """
    if button_gone:
        return True, None
    if errors_visible:
        return False, "submit_rejected: validation errors visible after click"
    if deadline_hit:
        if button_enabled:
            return False, "submit_unconfirmed: form still interactive after click"
        # Button present but disabled at deadline → most likely mid-flight
        # submission; treat as submitted (verifier still runs downstream).
        return True, None
    return None, None


_VALIDATION_ERROR_SELECTOR = (
    ".field-error, .error-message, [role='alert'], "
    ".helper-text--error, [class*='error'][class*='field']"
)


def submit_greenhouse(scope, *, interaction_ms: int = 4000) -> tuple[bool, str | None]:
    """Deterministically click the Greenhouse submit button via a healing
    locator, then CONFIRM the submission took (form gone / no validation
    errors). Returns (submitted, error)."""
    btn_spec = ElementSpec(role="button", name="Submit application",
                           label="Submit application", text="Submit application",
                           fingerprint={"tag": "button",
                                        "label_text": "submit application"})
    try:
        bloc, _ = heal(scope, btn_spec, timeout_ms=1200)
        if bloc is None:
            return False, "submit button not found"
        bloc.click(timeout=interaction_ms)
    except Exception as e:
        return False, f"submit failed: {type(e).__name__}: {e}"

    # Post-click confirmation loop (~6s budget).
    deadline = time.time() + max(6.0, interaction_ms / 1000)
    while True:
        deadline_hit = time.time() >= deadline
        try:
            button_gone = not bloc.is_visible()
        except Exception:
            # Detached element / navigated page → the form is gone.
            return True, None
        errors_visible = False
        button_enabled = False
        try:
            err = scope.locator(_VALIDATION_ERROR_SELECTOR).first
            errors_visible = err.is_visible()
        except Exception:
            pass
        try:
            button_enabled = bloc.is_enabled()
        except Exception:
            return True, None
        verdict, error = _post_submit_verdict(
            button_gone, errors_visible, button_enabled, deadline_hit)
        if verdict is not None:
            return verdict, error
        time.sleep(0.25)


def fill_greenhouse(page: "Page", profile: dict, resume_pdf_path: str, *,
                    submit: bool = True, interaction_ms: int = 4000,
                    answer_cache=None) -> AdapterResult:
    """Deterministically fill a Greenhouse form. Zero Claude-Code LLM.

    Operates on the form's actual frame (handles iframe-embedded vanity
    careers sites). If `answer_cache` is provided, unresolved free-text
    screening questions are answered from it (profile-seeded / cached →
    $0; genuine novelty → the cheap provider-flexible llm.py client, NOT
    a Claude Code subprocess) and removed from `unresolved`."""
    res = AdapterResult()
    page = _form_scope(page)        # <-- scope to the GH form frame
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

    # Resolve remaining free-text screening questions via the semantic
    # answer-cache (Phase D). Seed/cache hits cost $0; genuine novelty
    # uses the cheap provider-flexible llm.py client — NOT a Claude Code
    # subprocess. Resolved questions are filled and dropped from
    # `unresolved`; the adapter never blind-guesses (cache decides).
    if answer_cache is not None and res.unresolved:
        from applypilot.apply.canary import is_canary, resolve_canary
        ctx = _answer_context(profile)
        still: list[dict] = []
        for u in res.unresolved:
            lab = u["label"]
            # Canary-first: work-auth / sponsorship / citizenship / EEO / etc.
            # are answered ONLY by the deterministic resolver — never fuzzy-
            # served or LLM-guessed. An unresolvable canary MUST stay in
            # `unresolved` so the submit="auto" interlock blocks a live submit
            # with a guessed legal attestation.
            if is_canary(lab):
                det = resolve_canary(lab, profile)
                if det:
                    loc = None
                    for tag in ("textarea", "input"):
                        loc, _, _ = _heal_any(page, [lab], None, tag, "", timeout_ms=900)
                        if loc is not None:
                            break
                    if loc is not None:
                        try:
                            loc.fill(det, timeout=interaction_ms)
                            res.fields_filled.append(f"canary:{lab[:24]}")
                        except Exception:
                            still.append(u)
                    else:
                        still.append(u)
                else:
                    still.append(u)  # unresolvable canary stays UNRESOLVED
                continue
            loc = None
            for tag in ("textarea", "input"):
                loc, _, _ = _heal_any(page, [lab], None, tag, "", timeout_ms=900)
                if loc is not None:
                    break
            if loc is None:
                still.append(u)
                continue
            try:
                ans = answer_cache.answer(lab, context=ctx).answer
                if ans:
                    loc.fill(ans, timeout=interaction_ms)
                    res.fields_filled.append(f"answered:{lab[:24]}")
                else:
                    still.append(u)
            except Exception:
                still.append(u)
        res.unresolved = still

    # Final safety gate for submit="auto": known standard controls can fail to
    # commit, especially react-select comboboxes. Do not submit while the live
    # form still reports required controls as empty/invalid.
    for lbl in _missing_required_labels_on_page(page):
        low = lbl.lower()
        if "resume" in low or low == "cv" or "resume/cv" in low:
            continue
        if any((u.get("label") or "").lower() == low for u in res.unresolved):
            continue
        res.unresolved.append({"label": lbl, "type": "unknown"})

    # submit: True = always; False = never (LLM submits); "auto" = submit
    # deterministically ONLY when the form is fully satisfied (no
    # unresolved custom questions left, and we actually filled fields) —
    # this is the path that eliminates the LLM submit (no timeout, ~$0).
    do_submit = (submit is True) or (
        submit == "auto" and not res.unresolved and bool(res.fields_filled))
    if do_submit:
        res.submitted, res.error = submit_greenhouse(
            page, interaction_ms=interaction_ms)

    return res
