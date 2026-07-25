"""Structured browser-state observation for the apply flow.

The stream is intentionally DOM/accessibility first. Screenshots are not part
of the normal control loop; callers can attach a screenshot path when a
separate artifact capture is useful.
"""
from __future__ import annotations

import json
import logging
import re as _re
import threading
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)


_ATS_HOST_RE = _re.compile(r"(greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com)$", _re.I)
_MUTATION_METHODS = {"POST", "PUT", "PATCH"}
# Mutating requests to these hosts are always safe (page assets/analytics/CDN).
# Kept deliberately small; anything else is subject to the dry-run fail-closed rule.
_SAFE_MUTATION_HOSTS = _re.compile(
    r"(google-analytics\.com|googletagmanager\.com|doubleclick\.net|"
    r"segment\.(io|com)|sentry\.io|datadoghq\.com|cloudflareinsights\.com|"
    r"fullstory\.com|hotjar\.com|fonts\.googleapis\.com|gstatic\.com)$", _re.I)
# Endpoints that are NOT the application submit even though they POST to an ATS
# host (resume/cv parse, analytics, validation, tracking, logging). Defined here
# (the safety kernel) so both the ticket-consume gate below and v2.verify's
# passive success signal classify "submit" from one source of truth.
_NOT_SUBMIT = _re.compile(r"/(resume|cv)/?parse|/validate|analytics|/collect|/track|/log", _re.I)
# The application-submit path hint (Greenhouse et al.).
_SUBMIT_HINT = _re.compile(r"/applications?\b|/apply\b|/submit\b", _re.I)


def _request_host(url: str) -> str:
    """Netloc lowercased with any port stripped."""
    host = urlparse(url or "").netloc.lower()
    return host.split(":", 1)[0]


def should_block_request(method: str, url: str, *, ticket_open: bool, dry_run: bool) -> bool:
    """Pure route decision.
    LIVE: block mutating requests to known ATS hosts unless a submit ticket is open;
          leave everything else alone (minimal interference).
    DRY-RUN: fail closed - block EVERY mutating request except an explicit safe-host
          allowlist, so a vanity/embedded/unknown ATS submit cannot POST. GET/HEAD/OPTIONS allowed."""
    if (method or "").upper() not in _MUTATION_METHODS:
        return False
    host = _request_host(url)
    if _SAFE_MUTATION_HOSTS.search(host):
        return False
    if dry_run:
        return True   # fail closed: any non-safe mutation is blocked in dry-run
    if not _ATS_HOST_RE.search(host):
        return False
    return not ticket_open


def is_submit_request(method: str, url: str) -> bool:
    """Pure request-time classifier for THE application submit.

    True only for a mutating request to a known ATS host whose path looks like
    the application submit (POST .../applications, /apply, /submit) and is NOT a
    resume/cv-parse, analytics, validation, or tracking XHR. Reads method+url
    only (no status) so the CDP guard can decide at request time.

    Scopes the one-shot broker-ticket consume to the real submit: a real form
    POSTs prefill uploads (e.g. /attachments/upload) to the same ATS host before
    submitting, and those must NOT burn the ticket. Also the request-time half of
    v2.verify.is_submit_post, which layers the 2xx/3xx success check on top."""
    if (method or "").upper() not in _MUTATION_METHODS:
        return False
    host = _request_host(url)
    if not _ATS_HOST_RE.search(host):
        return False
    if _NOT_SUBMIT.search(url or ""):
        return False
    return bool(_SUBMIT_HINT.search(url or ""))


def ats_mutation_telemetry_row(method: str, url: str, *, ticket_open: bool) -> dict[str, Any] | None:
    """Pure decision for passive submit-endpoint capture (live attempt #5).

    On 2026-07-24 a submit POST fired but `is_submit_request` returned False, so
    the one-shot broker ticket was never consumed — the real Greenhouse submit
    path is not covered by `_SUBMIT_HINT`. This returns a `{method, url, ts}`
    row (url = PATH ONLY, no query) for a mutating, non-safe request to a known
    ATS host that slips through while a submit ticket is OPEN yet is NOT
    submit-shaped — i.e. a candidate for the real submit endpoint we're missing.
    Returns None for everything else. Telemetry only; drives no behavior."""
    if not ticket_open:
        return None
    if (method or "").upper() not in _MUTATION_METHODS:
        return None
    host = _request_host(url)
    if _SAFE_MUTATION_HOSTS.search(host):
        return None
    if not _ATS_HOST_RE.search(host):
        return None
    if is_submit_request(method, url):
        return None
    return {
        "method": (method or "").upper(),
        "url": urlparse(url or "").path,
        "ts": time.time(),
    }


@dataclass
class ControlObservation:
    control_id: str = ""
    label: str = ""
    role: str = ""
    control_type: str = ""
    selector: str = ""
    value: str = ""
    required: bool = False
    disabled: bool = False
    invalid: bool = False
    visible: bool = True
    frame_index: int = 0
    frame_url: str = ""
    bbox: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0


@dataclass
class BrowserObservation:
    url: str = ""
    title: str = ""
    ready_state: str = ""
    observed_at: float = 0.0
    controls: list[ControlObservation] = field(default_factory=list)
    required_missing: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    submit_buttons: list[ControlObservation] = field(default_factory=list)
    tabs: list[dict[str, Any]] = field(default_factory=list)
    page_text_sample: str = ""
    screenshot_path: str | None = None
    error: str | None = None

    @property
    def submit_enabled(self) -> bool:
        return any(not b.disabled for b in self.submit_buttons)

    @property
    def resume_present(self) -> bool:
        for c in self.controls:
            if c.control_type == "file" and c.value:
                return True
        joined = "\n".join([self.page_text_sample] + [c.value for c in self.controls] + self.validation_errors)
        return ".pdf" in joined.lower()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_OBSERVE_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const lower = s => norm(s).toLowerCase();
  const allRoots = () => {
    const roots = [document];
    for (let i = 0; i < roots.length; i += 1) {
      const root = roots[i];
      for (const el of Array.from(root.querySelectorAll ? root.querySelectorAll('*') : [])) {
        if (el.shadowRoot) roots.push(el.shadowRoot);
      }
    }
    return roots;
  };
  const allElements = selector => allRoots().flatMap(root => Array.from(root.querySelectorAll(selector)));
  const rootQuery = (el, selector) => {
    const root = el && el.getRootNode ? el.getRootNode() : document;
    return (root && root.querySelector ? root : document).querySelector(selector);
  };
  const visible = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const choiceInput = el => {
    if (!el) return null;
    const tag = lower(el.tagName);
    const typ = lower(el.getAttribute('type'));
    if (tag === 'input' && /^(checkbox|radio)$/.test(typ)) return el;
    if (el.querySelector) {
      return el.querySelector('input[type="checkbox"],input[type="radio"]');
    }
    return null;
  };
  const cssEscape = s => {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/(["\\\]\[\.#:>~+()\s])/g, '\\$1');
  };
  const nearLabel = el => {
    if (!el) return '';
    const aria = el.getAttribute('aria-label');
    if (aria) return norm(aria);
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const root = el.getRootNode && el.getRootNode();
      const txt = labelledBy.split(/\s+/).map(id => (root?.getElementById?.(id) || document.getElementById(id))?.textContent || '').join(' ');
      if (norm(txt)) return norm(txt);
    }
    if (el.id) {
      const L = rootQuery(el, 'label[for="' + cssEscape(el.id) + '"]');
      if (L) return norm(L.textContent);
    }
    const W = el.closest('label');
    if (W) return norm(W.textContent);
    const box = el.closest('div,fieldset,li,p,section') || el.parentElement;
    if (box) {
      const labels = box.querySelectorAll('label,legend');
      let best = '';
      for (const L of labels) {
        if (L.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) {
          best = norm(L.textContent);
        }
      }
      if (best) return best;
      if (labels.length) return norm(labels[0].textContent);
    }
    let p = el.previousElementSibling;
    for (let i = 0; i < 3 && p; i++, p = p.previousElementSibling) {
      if (/^(label|legend|span|div|p)$/i.test(p.tagName) && norm(p.textContent)) {
        return norm(p.textContent);
      }
    }
    return '';
  };
  const choiceText = el => {
    const input = choiceInput(el);
    const role = lower(el.getAttribute('role'));
    if (!input && role !== 'checkbox' && !el.hasAttribute('aria-checked')) return '';
    if (lower(el.tagName) === 'label' && norm(el.textContent)) return norm(el.textContent);
    const choiceEl = input || el;
    if (choiceEl.id) {
      const L = rootQuery(choiceEl, 'label[for="' + cssEscape(choiceEl.id) + '"]');
      if (L && norm(L.textContent)) return norm(L.textContent);
    }
    const wrapped = choiceEl.closest('label');
    if (wrapped && norm(wrapped.textContent)) return norm(wrapped.textContent);
    let n = choiceEl.nextElementSibling;
    for (let i = 0; i < 3 && n; i += 1, n = n.nextElementSibling) {
      const txt = norm(n.innerText || n.textContent);
      if (txt) return txt;
    }
    return norm(choiceEl.getAttribute('value') || el.textContent || '');
  };
  const questionText = el => {
    const input = choiceInput(el);
    const role = lower(el.getAttribute('role'));
    if (!input && role !== 'checkbox' && !el.hasAttribute('aria-checked')) return '';
    const anchor = input || el;
    const option = choiceText(anchor);
    const fieldset = anchor.closest('fieldset,[role="radiogroup"],[role="group"]');
    const legend = fieldset?.querySelector('legend,[data-test*="label" i],[class*="label" i]');
    if (legend && norm(legend.textContent)) return norm(legend.textContent);
    let cur = anchor.parentElement;
    for (let depth = 0; depth < 6 && cur && cur !== document.body; depth += 1, cur = cur.parentElement) {
      const txt = norm(cur.innerText || cur.textContent);
      if (!txt || txt === option) continue;
      const before = txt.split(option)[0];
      const lines = before.split(/[\r\n]+/).map(norm).filter(Boolean);
      const candidate = lines.length ? lines[lines.length - 1] : before;
      if (candidate && candidate.length > 8 && !/^(yes|no|true|false)$/i.test(candidate)) return norm(candidate);
    }
    return '';
  };
  const choiceLabel = el => {
    const input = choiceInput(el);
    const role = lower(el.getAttribute('role'));
    if (!input && role !== 'checkbox' && !el.hasAttribute('aria-checked')) return '';
    const question = questionText(el);
    const option = choiceText(el);
    if (question && option && !question.toLowerCase().includes(option.toLowerCase())) {
      return norm(question + ' - ' + option);
    }
    return norm(question || option);
  };
  const implicitRole = el => {
    const tag = lower(el.tagName);
    const typ = lower(el.getAttribute('type'));
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const input = choiceInput(el);
    if (input) {
      const inputType = lower(input.getAttribute('type'));
      if (inputType === 'checkbox') return 'checkbox';
      if (inputType === 'radio') return 'radio';
    }
    if (tag === 'button' || (tag === 'input' && /^(button|submit|reset)$/.test(typ))) return 'button';
    if (tag === 'a') return 'link';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input' && /^(|text|email|tel|url|search|password|number)$/.test(typ)) return 'textbox';
    if (tag === 'input' && typ === 'checkbox') return 'checkbox';
    if (tag === 'input' && typ === 'radio') return 'radio';
    return '';
  };
  const valueOf = el => {
    const tag = lower(el.tagName);
    const typ = lower(el.getAttribute('type'));
    const input = choiceInput(el);
    if (typ === 'file') return el.files && el.files.length ? Array.from(el.files).map(f => f.name).join(', ') : '';
    if (tag === 'select') return el.selectedOptions?.[0]?.textContent || el.value || '';
    if (typ === 'checkbox' || typ === 'radio') return el.checked ? 'checked' : '';
    if (input) return input.checked ? 'checked' : '';
    if (el.getAttribute('role') === 'checkbox' || el.hasAttribute('aria-checked')) {
      return el.getAttribute('aria-checked') === 'true' ? 'checked' : '';
    }
    if ('value' in el && el.value && !/select__input|Select__input/i.test(String(el.className || ''))) return String(el.value);
    if (el.getAttribute('role') === 'combobox' || /select__input|Select__input|combobox/i.test(String(el.className || ''))) {
      let cur = el;
      for (let depth = 0; depth < 8 && cur && cur !== document.body; depth += 1, cur = cur.parentElement) {
        const selected = cur.querySelector?.(
          '[class*="singleValue"], [class*="single-value" i], [class*="multiValue"], [class*="multi-value" i], [class*="selected" i], [aria-selected="true"]'
        );
        if (selected && selected !== el && norm(selected.textContent)) return norm(selected.textContent);
      }
      if ('value' in el && el.value) return String(el.value);
    }
    return norm(el.textContent);
  };
  const selectorFor = el => {
    const tag = lower(el.tagName) || '*';
    const typ = lower(el.getAttribute('type'));
    if (el.id) return '#' + cssEscape(el.id);
    const name = el.getAttribute('name');
    if (name && /^(checkbox|radio)$/.test(typ) && el.getAttribute('value')) {
      return tag + '[name="' + name.replace(/"/g, '\\"') + '"][value="' + String(el.getAttribute('value')).replace(/"/g, '\\"') + '"]';
    }
    if (name) return tag + '[name="' + name.replace(/"/g, '\\"') + '"]';
    const aria = el.getAttribute('aria-label');
    if (aria) return tag + '[aria-label="' + aria.replace(/"/g, '\\"') + '"]';
    if (tag === 'label') {
      const input = choiceInput(el);
      if (input && input.id && el.getAttribute('for') === input.id) return 'label[for="' + cssEscape(input.id) + '"]';
      const cls = String(el.className || '').split(/\s+/).filter(Boolean)[0];
      if (cls) return 'label.' + cssEscape(cls);
    }
    return tag;
  };
  const boxOf = el => {
    const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height)};
  };
  const isRequired = el => {
    const label = nearLabel(el);
    const input = choiceInput(el);
    return !!(el.required || input?.required || el.getAttribute('aria-required') === 'true' || input?.getAttribute('aria-required') === 'true' || /\*/.test(label));
  };
  const confidenceFor = el => {
    let score = 0.2;
    if (nearLabel(el)) score += 0.35;
    if (el.id) score += 0.15;
    if (el.getAttribute('name')) score += 0.15;
    if (el.getAttribute('aria-label') || el.getAttribute('aria-labelledby')) score += 0.1;
    if (visible(el)) score += 0.05;
    return Math.min(1, score);
  };
  const controlEls = allElements(
    'input:not([type=hidden]), textarea, select, [role="combobox"], [contenteditable="true"], [role="checkbox"], [aria-checked], oj-checkboxset, oj-option, label'
  ).filter(el => {
    if (!visible(el)) return false;
    if (lower(el.tagName) === 'label') return !!choiceInput(el);
    return true;
  });
  const radioGroupEls = el => {
    const name = el.getAttribute('name');
    if (name) {
      const root = el.getRootNode && el.getRootNode();
      const scope = root && root.querySelectorAll ? root : document;
      return Array.from(scope.querySelectorAll('input[type="radio"][name="' + name.replace(/"/g, '\\"') + '"]')).filter(visible);
    }
    const group = el.closest('fieldset,[role="radiogroup"],[role="group"],div,section');
    return group ? Array.from(group.querySelectorAll('input[type="radio"]')).filter(visible) : [el];
  };
  const radioGroupKey = el => {
    const name = el.getAttribute('name');
    if (name) return 'name:' + name;
    const group = el.closest('fieldset,[role="radiogroup"],[role="group"],div,section');
    return 'group:' + (questionText(el) || nearLabel(el) || Array.from(controlEls).indexOf(el)) + ':' + Array.from(allElements('fieldset,[role="radiogroup"],[role="group"],div,section')).indexOf(group);
  };
  const radioSatisfied = el => radioGroupEls(el).some(r => r.checked);
  const controlMissing = (el, required, value) => {
    const tag = lower(el.tagName);
    const typ = lower(el.getAttribute('type'));
    const input = choiceInput(el);
    if (!required || el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
    if (tag === 'input' && typ === 'radio') return !radioSatisfied(el);
    if (input && lower(input.getAttribute('type')) === 'radio') return !radioSatisfied(input);
    if ((tag === 'input' && typ === 'checkbox') || (input && lower(input.getAttribute('type')) === 'checkbox') || lower(el.getAttribute('role')) === 'checkbox' || el.hasAttribute('aria-checked')) {
      return norm(value) !== 'checked';
    }
    const val = norm(value);
    return !val || /^select\.?\s*$/i.test(val);
  };
  const controls = controlEls.map(el => {
    const tag = lower(el.tagName);
    const typ = lower(el.getAttribute('type')) || (tag === 'textarea' ? 'textarea' : tag);
    const label = choiceLabel(el) || nearLabel(el);
    const required = isRequired(el);
    const value = valueOf(el);
    const missing = controlMissing(el, required, value);
    return {
      label,
      role: implicitRole(el),
      control_type: typ,
      selector: selectorFor(el),
      value,
      required,
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      invalid: el.getAttribute('aria-invalid') === 'true' || missing,
      visible: true,
      bbox: boxOf(el),
      confidence: confidenceFor(el),
    };
  });
  const submitButtons = allElements('button, input[type=submit], [role="button"]')
    .filter(visible)
    .filter(el => /submit application|submit my application|submit|apply|continue|next/i.test(el.innerText || el.value || el.getAttribute('aria-label') || ''))
    .map(el => ({
      label: norm(el.innerText || el.value || el.getAttribute('aria-label') || ''),
      role: implicitRole(el),
      control_type: lower(el.getAttribute('type')) || lower(el.tagName),
      selector: selectorFor(el),
      value: norm(el.innerText || el.value || ''),
      required: false,
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      invalid: false,
      visible: true,
      bbox: boxOf(el),
      confidence: confidenceFor(el),
    }));
  const validationErrors = allElements(
    '[aria-invalid="true"], .error, .errors, .field-error, [data-testid*="error" i], [role="alert"]'
  )
    .filter(visible)
    .map(el => norm(el.innerText || el.textContent))
    .filter(Boolean);
  const requiredMissing = [];
  const seenRadioGroups = new Set();
  for (let i = 0; i < controlEls.length; i += 1) {
    const el = controlEls[i];
    const c = controls[i];
    if (!c.required || c.disabled) continue;
    const typ = lower(el.getAttribute('type'));
    const input = choiceInput(el);
    const isRadio = (lower(el.tagName) === 'input' && typ === 'radio') || (input && lower(input.getAttribute('type')) === 'radio');
    if (isRadio) {
      const radioEl = input || el;
      const key = radioGroupKey(radioEl);
      if (seenRadioGroups.has(key)) continue;
      seenRadioGroups.add(key);
      if (!radioSatisfied(radioEl)) {
        requiredMissing.push(questionText(radioEl) || c.label || c.selector || 'required radio group');
      }
      continue;
    }
    if (controlMissing(el, c.required, c.value)) {
      requiredMissing.push(c.label || c.selector || 'required field');
    }
  }
  return {
    url: window.location.href,
    title: document.title || '',
    ready_state: document.readyState || '',
    page_text_sample: norm(allRoots().map(root => root.body ? root.body.innerText || root.body.textContent || '' : root.textContent || '').join(' ')).slice(0, 4000),
    controls,
    required_missing: [...new Set(requiredMissing)],
    validation_errors: [...new Set(validationErrors)],
    submit_buttons: submitButtons,
  };
}
"""


def collect_browser_observation(page, *, tabs: list[dict[str, Any]] | None = None) -> BrowserObservation:
    """Collect a one-shot structured observation from a Playwright Page.

    The top frame establishes URL/title. Child frames are scanned separately
    and merged so embedded ATS forms are visible to callers.
    """
    frames = []
    try:
        frames = list(page.frames)
    except Exception:
        frames = []

    observation = BrowserObservation(
        url=getattr(page, "url", "") or "",
        observed_at=time.time(),
        tabs=tabs or [],
    )
    seen_controls: set[tuple[str, str, str]] = set()
    seen_buttons: set[tuple[str, str, str]] = set()

    for idx, frame in enumerate(frames[:12]):
        try:
            raw = frame.evaluate(_OBSERVE_JS)
        except Exception as e:
            if idx == 0 and not observation.error:
                observation.error = f"{type(e).__name__}: {e}"
            continue
        if not isinstance(raw, dict):
            continue
        if idx == 0:
            observation.url = raw.get("url") or observation.url
            observation.title = raw.get("title") or ""
            observation.ready_state = raw.get("ready_state") or ""
        page_text = raw.get("page_text_sample") or ""
        if page_text:
            combined = f"{observation.page_text_sample}\n{page_text}".strip()
            observation.page_text_sample = combined[:8000]

        frame_url = ""
        try:
            frame_url = frame.url or ""
        except Exception:
            frame_url = ""

        for ordinal, item in enumerate(raw.get("controls") or []):
            control = _control_from_raw(item, frame_url, idx, ordinal, "control")
            key = (control.frame_url, control.selector, control.label)
            if key in seen_controls:
                continue
            seen_controls.add(key)
            observation.controls.append(control)

        for ordinal, item in enumerate(raw.get("submit_buttons") or []):
            button = _control_from_raw(item, frame_url, idx, ordinal, "button")
            key = (button.frame_url, button.selector, button.label)
            if key in seen_buttons:
                continue
            seen_buttons.add(key)
            observation.submit_buttons.append(button)

        for label in raw.get("required_missing") or []:
            if label and label not in observation.required_missing:
                observation.required_missing.append(str(label))
        for err in raw.get("validation_errors") or []:
            if err and err not in observation.validation_errors:
                observation.validation_errors.append(str(err))

    return observation


def _control_from_raw(
    raw: dict[str, Any],
    frame_url: str,
    frame_index: int = 0,
    ordinal: int = 0,
    kind: str = "control",
) -> ControlObservation:
    selector = str(raw.get("selector") or "")
    label = str(raw.get("label") or "")
    control_type = str(raw.get("control_type") or "")
    control_id = _control_id(frame_index, ordinal, kind, selector, label, control_type)
    return ControlObservation(
        control_id=control_id,
        label=label,
        role=str(raw.get("role") or ""),
        control_type=control_type,
        selector=selector,
        value=str(raw.get("value") or ""),
        required=bool(raw.get("required")),
        disabled=bool(raw.get("disabled")),
        invalid=bool(raw.get("invalid")),
        visible=bool(raw.get("visible", True)),
        frame_index=frame_index,
        frame_url=frame_url,
        bbox=dict(raw.get("bbox") or {}),
        confidence=float(raw.get("confidence") or 0.0),
    )


def _control_id(
    frame_index: int,
    ordinal: int,
    kind: str,
    selector: str,
    label: str,
    control_type: str,
) -> str:
    """Stable-enough observation handle for one page state.

    It deliberately stays human-readable for Claude. The executor always
    re-validates the handle against the current observation before acting.
    """
    basis = "|".join([str(frame_index), kind, selector, label, control_type]).lower()
    import hashlib

    digest = hashlib.sha1(basis.encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"f{frame_index}:{kind}:{ordinal}:{digest}"


def summarize_observation(obs: BrowserObservation | None, *, max_controls: int = 14) -> str:
    """Return a compact, prompt-safe summary of the latest observation."""
    if obs is None:
        return ""
    lines = [
        f"URL: {obs.url}",
        f"Title: {obs.title or '(untitled)'}",
        f"Ready state: {obs.ready_state or 'unknown'}",
        f"Required missing: {', '.join(obs.required_missing) if obs.required_missing else 'none detected'}",
        f"Validation errors: {', '.join(obs.validation_errors[:5]) if obs.validation_errors else 'none detected'}",
        f"Submit buttons: {', '.join(_button_summary(b) for b in obs.submit_buttons[:5]) if obs.submit_buttons else 'none detected'}",
    ]
    visible_controls = [c for c in obs.controls if c.visible]
    if visible_controls:
        lines.append("Visible controls:")
        for c in visible_controls[:max_controls]:
            label = c.label or c.selector or c.role or c.control_type
            val = _shorten(c.value, 60) if c.value else "empty"
            req = "required" if c.required else "optional"
            state = "disabled" if c.disabled else ("invalid" if c.invalid else "ok")
            lines.append(f"- {c.control_id} | {label}: {val} ({req}, {state})")
    return "\n".join(lines)


def _button_summary(button: ControlObservation) -> str:
    state = "disabled" if button.disabled else "enabled"
    return f"{button.label or button.selector} [{state}]"


def _shorten(value: str, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "..."


class BrowserStateStream:
    """Background structured observer for one worker browser.

    It owns its own Playwright/CDP connection on a daemon thread. Consumers call
    `latest()` for a stable snapshot; `refresh_now()` performs a synchronous
    one-shot collection using a short-lived CDP connection.
    """

    def __init__(
        self,
        cdp_port: int,
        *,
        poll_interval_s: float = 0.5,
        debounce_s: float = 0.25,
        telemetry_path: str | Path | None = None,
        broker=None,
        identity_id: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self.cdp_port = cdp_port
        self.poll_interval_s = max(0.1, poll_interval_s)
        self.debounce_s = max(0.0, debounce_s)
        self.telemetry_path = Path(telemetry_path) if telemetry_path else None
        # Network containment: block ATS submit POSTs unless a broker ticket is
        # open (dry-run fails closed at the network layer — a submit POST cannot
        # leave the browser). broker=None disables the guard (backward-compat).
        self.broker = broker
        self.identity_id = identity_id
        self.dry_run = dry_run
        self.route_install_failed = False
        self._lock = threading.Lock()
        self._latest: BrowserObservation | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._update_count = 0
        self._last_signature = ""
        self._last_emit_at = 0.0

    @property
    def update_count(self) -> int:
        with self._lock:
            return self._update_count

    def start(self) -> "BrowserStateStream":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._run,
            name=f"browser-state-stream-{self.cdp_port}",
            daemon=True,
        )
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def latest(self) -> BrowserObservation | None:
        with self._lock:
            return deepcopy(self._latest)

    def refresh_now(self, timeout_ms: int = 3000) -> BrowserObservation | None:
        obs = observe_cdp(self.cdp_port, timeout_ms=timeout_ms)
        self._publish(obs, force=True)
        return self.latest()

    def _run(self) -> None:
        pw = browser = None
        try:
            from playwright.sync_api import sync_playwright

            pw = sync_playwright().start()
            browser = pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{self.cdp_port}", timeout=5000
            )

            # --- CDP network containment (the always-on submit chokepoint) ---
            # This is the ONLY session-lifetime CDP connection, so a context
            # route installed here sees every request from every tab for the
            # whole apply. It makes a dry-run submit physically impossible and
            # gates real submits on an open broker ticket — including the raw
            # @playwright/mcp path that has no Python-side gate.
            def _guard(route):
                req = route.request
                open_ = bool(self.broker and self.identity_id and self.broker.ticket_open(self.identity_id))
                if should_block_request(req.method, req.url, ticket_open=open_, dry_run=self.dry_run):
                    self._publish(BrowserObservation(observed_at=time.time(),
                                                     error=f"BLOCKED_SUBMIT {req.method} {req.url}"))
                    route.abort()
                    return
                # Consume the one-shot ticket ONLY on the actual submit-shaped
                # request. A real form POSTs prefill uploads (resume/attachments)
                # to the same ATS host before submitting; consuming on any ATS
                # mutation burned the ticket at prefill time and the real submit
                # was then refused (live bug 2026-07-24).
                if open_ and is_submit_request(req.method, req.url):
                    self.broker.consume(self.identity_id)
                elif open_:
                    # Passive submit-endpoint capture: a mutating ATS request
                    # slipping through while the ticket is open but NOT matching
                    # is_submit_request is a candidate for the real (uncovered)
                    # submit path. Log it so the next run can extend _SUBMIT_HINT.
                    self._record_ats_mutation(req.method, req.url)
                route.continue_()

            _routed: set[int] = set()

            def _ensure_routes():
                for ctx in browser.contexts:
                    if id(ctx) not in _routed:
                        try:
                            ctx.route("**/*", _guard)
                            _routed.add(id(ctx))
                        except Exception:
                            # Fail-closed in LIVE mode: if we cannot install the
                            # route we cannot guarantee submits are gated. Flag it
                            # so the worker can downgrade to needs_review rather
                            # than proceed unguarded. (dry-run has the DOM blocker
                            # + server-side guards as backstops.)
                            if not self.dry_run:
                                self.route_install_failed = True
                                self._publish(BrowserObservation(
                                    observed_at=time.time(),
                                    error="ROUTE_INSTALL_FAILED context guard not installed"))
                            log.warning("browser stream route install failed", exc_info=True)

            _ensure_routes()

            while not self._stop.is_set():
                try:
                    _ensure_routes()  # cover contexts opened mid-session
                    page, tabs = _active_page_and_tabs(browser)
                    if page is not None:
                        self._publish(collect_browser_observation(page, tabs=tabs))
                except Exception as e:
                    self._publish(BrowserObservation(observed_at=time.time(), error=f"{type(e).__name__}: {e}"))
                self._stop.wait(self.poll_interval_s)
        except Exception as e:
            self._publish(BrowserObservation(observed_at=time.time(), error=f"{type(e).__name__}: {e}"), force=True)
        finally:
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                if pw is not None:
                    pw.stop()
            except Exception:
                pass

    def _record_ats_mutation(self, method: str, url: str) -> None:
        """Append one telemetry line for a non-submit ATS mutation seen while a
        ticket is open (submit-endpoint capture, live attempt #5). Bounded: the
        pure decision fires only for non-safe ATS-host mutations, and this is
        only reached while a ticket is open — low volume. Never raises."""
        row = ats_mutation_telemetry_row(method, url, ticket_open=True)
        if not row:
            return
        try:
            from applypilot import config

            path = config.LOG_DIR / f"ats_mutations_{self.cdp_port}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            log.debug("ats mutation telemetry write failed", exc_info=True)

    def _publish(self, obs: BrowserObservation, *, force: bool = False) -> None:
        signature = _observation_signature(obs)
        now = time.monotonic()
        if not force and signature == self._last_signature and (now - self._last_emit_at) < self.debounce_s:
            return
        self._last_signature = signature
        self._last_emit_at = now
        with self._lock:
            self._latest = deepcopy(obs)
            self._update_count += 1
        if self.telemetry_path:
            try:
                self.telemetry_path.parent.mkdir(parents=True, exist_ok=True)
                with self.telemetry_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(obs.to_dict(), ensure_ascii=False) + "\n")
            except Exception:
                log.debug("browser stream telemetry write failed", exc_info=True)


def observe_cdp(cdp_port: int, *, timeout_ms: int = 3000) -> BrowserObservation:
    """One-shot observation via CDP, useful for legacy helper integration."""
    pw = browser = None
    try:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{cdp_port}", timeout=timeout_ms
        )
        page, tabs = _active_page_and_tabs(browser)
        if page is None:
            return BrowserObservation(observed_at=time.time(), error="no_page")
        return collect_browser_observation(page, tabs=tabs)
    except Exception as e:
        return BrowserObservation(observed_at=time.time(), error=f"{type(e).__name__}: {e}")
    finally:
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if pw is not None:
                pw.stop()
        except Exception:
            pass


def _active_page_and_tabs(browser) -> tuple[Any | None, list[dict[str, Any]]]:
    pages = [p for ctx in browser.contexts for p in ctx.pages]
    tabs = []
    for i, page in enumerate(pages):
        try:
            tabs.append({"index": i, "url": page.url, "title": page.title()})
        except Exception:
            tabs.append({"index": i, "url": "", "title": ""})
    return (pages[-1] if pages else None), tabs


def _observation_signature(obs: BrowserObservation) -> str:
    payload = {
        "url": obs.url,
        "ready": obs.ready_state,
        "controls": [(c.control_id, c.selector, c.value, c.required, c.disabled, c.invalid) for c in obs.controls],
        "missing": obs.required_missing,
        "errors": obs.validation_errors,
        "buttons": [(b.control_id, b.selector, b.disabled) for b in obs.submit_buttons],
        "error": obs.error,
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)
