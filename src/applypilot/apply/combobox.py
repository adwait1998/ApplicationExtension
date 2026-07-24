"""Shared async-combobox ("type-ahead" / react-select with remote options) dance.

Greenhouse's Location (City) field — and many other ATS controls — are
react-select comboboxes whose options are fetched ASYNCHRONOUSLY (city
autocomplete). Typing text into them is NOT enough: React only commits a value
when an option from the async dropdown is actually selected. Free text is
dropped on blur (the "React-controlled reset"), so a naive fill reports success
while the field ends up EMPTY and client-side validation blocks submit
(`validation_location_persist`, observed live on Twilio 2026-07-24).

A second failure mode is picking the WRONG option: a blind ArrowDown+Enter on
an unfiltered async list can auto-select the first row (a Venezuela city was
selected live this way). So the dance must pick the BEST-MATCHING option, never
the first.

This module is the ONE shared implementation used by both the legacy
deterministic prefill (`apply/prefill.py`) and the LLM stream executor
(`apply/stream_executor.py`). It is intentionally a leaf module: it imports
nothing from prefill/stream_executor so those two can both depend on it without
an import cycle.

The pure option-matcher `_match_real_option` lives here too (prefill re-exports
it for backwards compatibility with its existing importers and tests).
"""
from __future__ import annotations

import logging
import re
import time

logger = logging.getLogger(__name__)


def _norm_opt(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _match_real_option(preferred: tuple[str, ...], options: list[str]) -> str | None:
    """Map our intended values to the closest REAL dropdown option.

    Pure + unit-testable. Priority: exact (normalized) -> preferred is a
    substring of an option (our "Yes" inside "Yes, authorized...") -> option
    is a substring of preferred -> strong token overlap (>=60% of the
    shorter side's words). Returns the REAL option text, or None if
    nothing reasonably matches (caller fails fast instead of blind-typing).
    """
    opts = [o for o in (options or []) if o and o.strip()]
    if not opts:
        return None
    npref = [(_norm_opt(p), p) for p in preferred if p]
    nopts = [(_norm_opt(o), o) for o in opts]
    # 1. exact normalized
    for np, _ in npref:
        for no, orig in nopts:
            if np and np == no:
                return orig
    # 2. preferred subset-of option  (most common: "yes" in "yes, i am authorized...")
    for np, _ in npref:
        if len(np) < 2:
            continue
        for no, orig in nopts:
            if np in no:
                return orig
    # 3. option subset-of preferred
    for np, _ in npref:
        for no, orig in nopts:
            if len(no) >= 2 and no in np:
                return orig
    # 4. token overlap >=60% of the shorter token set
    for np, _ in npref:
        pw = set(re.findall(r"[a-z0-9]+", np))
        if not pw:
            continue
        for no, orig in nopts:
            ow = set(re.findall(r"[a-z0-9]+", no))
            if not ow:
                continue
            inter = len(pw & ow)
            if inter and inter / min(len(pw), len(ow)) >= 0.6:
                return orig
    return None


def _reflects(readback: str, target: str) -> bool:
    """Does the read-back value reasonably reflect the option we picked?"""
    if not readback or not target:
        return False
    return _match_real_option((target,), [readback]) is not None


# --- browser-side helpers (operate on a Playwright page/frame `root` and a
#     Playwright `loc` locator; all failures are swallowed to a safe default) ---

# Options render in a portal/listbox. We read VISIBLE option nodes across the
# common shapes: react-select (`.select__option`), ARIA listboxes
# (`[role="option"]`, `[role="listbox"] *`), and legacy `.Select-option`.
_OPTION_SELECTOR = (
    '[role="listbox"] [role="option"], [role="option"], '
    '[class*="select__option"]:not([class*="noresults"]), '
    '.Select-option, li[role="option"]'
)


def _read_async_options(root) -> list[str]:
    """Return the currently-rendered, visible async option texts (deduped)."""
    try:
        return list(root.evaluate(
            """(sel) => {
              const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
              const visible = el => {
                if (!el || el.hasAttribute('hidden')) return false;
                const st = getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                const box = el.getBoundingClientRect();
                return box.width > 0 && box.height > 0;
              };
              const seen = new Set(); const out = [];
              for (const el of document.querySelectorAll(sel)) {
                if (!visible(el)) continue;
                const t = norm(el.textContent);
                if (t && t.length <= 250 && !seen.has(t)) { seen.add(t); out.push(t); }
              }
              return out;
            }""",
            _OPTION_SELECTOR,
        )) or []
    except Exception:
        return []


def _poll_async_options(root, timeout_ms: int, poll_ms: int = 100) -> list[str]:
    """Poll (retry-on-empty) up to `timeout_ms` for the async options to appear."""
    deadline = time.monotonic() + max(0.0, timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        opts = _read_async_options(root)
        if opts:
            # Let a still-streaming async list finish populating, then re-read.
            time.sleep(0.12)
            return _read_async_options(root) or opts
        time.sleep(max(0.02, poll_ms / 1000.0))
    return []


def _click_async_option(root, target: str) -> bool:
    """Click the rendered option whose text best matches `target` (exact first)."""
    try:
        return bool(root.evaluate(
            """({want, sel}) => {
              const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const w = norm(want);
              if (!w) return false;
              const visible = el => {
                if (!el || el.hasAttribute('hidden')) return false;
                const st = getComputedStyle(el);
                return st.display !== 'none' && st.visibility !== 'hidden';
              };
              const els = Array.from(document.querySelectorAll(sel)).filter(visible);
              let opt = els.find(el => norm(el.textContent) === w);
              if (!opt) opt = els.find(el => {
                const t = norm(el.textContent);
                return t && (t.includes(w) || w.includes(t));
              });
              if (!opt) return false;
              opt.scrollIntoView({block: 'center'});
              opt.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0}));
              opt.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, button: 0}));
              opt.click();
              return true;
            }""",
            {"want": target, "sel": _OPTION_SELECTOR},
        ))
    except Exception:
        return False


def _read_committed_value(loc) -> str:
    """Read the SELECTED value from a combobox, NOT the typed free text.

    Checks, in order: a rendered react-select single-value / chip in a bounded
    ancestor container, a committed hidden input, the control's own input value,
    then (for div-style comboboxes) its own textContent. Placeholder strings
    ("Select...") are treated as empty.
    """
    try:
        return str(loc.evaluate(
            """(el) => {
              const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
              const placeholder = t => !t || /^(select|choose|search)\\b/i.test(t)
                                    || /^select\\.\\.\\.$/i.test(t);
              let container = el;
              for (let i = 0; i < 6 && container; i++, container = container.parentElement) {
                const chip = container.querySelector(
                  '[class*="single-value" i], [class*="singleValue" i], '
                  + '[class*="multi-value" i], [class*="multiValue" i], .select__single-value'
                );
                if (chip) { const t = norm(chip.textContent); if (!placeholder(t)) return t; }
                const hid = container.querySelector('input[type="hidden"]');
                if (hid) { const v = norm(hid.value); if (v && !placeholder(v)) return v; }
              }
              if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
                const v = norm(el.value);
                if (!placeholder(v)) return v;
              }
              const own = norm(el.textContent);
              if (!placeholder(own)) return own;
              return '';
            }"""
        ) or "")
    except Exception:
        return ""


def _type_query(loc, query: str, timeout_ms: int) -> None:
    """Focus/open the combobox, clear any prior text, and type `query`."""
    try:
        loc.scroll_into_view_if_needed(timeout=1000)
    except Exception:
        pass
    try:
        loc.click(timeout=min(timeout_ms, 1500))
    except Exception:
        pass
    try:
        loc.fill("", timeout=800)
    except Exception:
        pass
    if query:
        try:
            loc.type(query, delay=20, timeout=min(timeout_ms, 3000))
        except Exception:
            pass


def _commit_by_click(root, loc, target: str, settle_ms: int) -> str | None:
    """Click the matching option element; return the read-back value if it commits."""
    if not _click_async_option(root, target):
        return None
    time.sleep(max(0.0, settle_ms / 1000.0))
    readback = _read_committed_value(loc)
    if readback and _reflects(readback, target):
        return readback
    return None


def _commit_by_keyboard(root, loc, target: str, timeout_ms: int, settle_ms: int) -> str | None:
    """Fallback for react-select desync: re-filter to the target so it becomes
    the sole/first option, VERIFY the first option matches, then ArrowDown+Enter.

    This never blind-picks: Enter is only pressed after confirming the first
    visible option reasonably matches `target`.
    """
    try:
        loc.fill("", timeout=800)
    except Exception:
        pass
    try:
        loc.type(target[:40], delay=20, timeout=min(timeout_ms, 3000))
    except Exception:
        return None
    opts = _poll_async_options(root, min(timeout_ms, 2500))
    if not opts or not _reflects(opts[0], target):
        return None
    try:
        loc.press("ArrowDown", timeout=800)
        loc.press("Enter", timeout=800)
    except Exception:
        pass
    time.sleep(max(0.0, settle_ms / 1000.0))
    readback = _read_committed_value(loc)
    if readback and _reflects(readback, target):
        return readback
    # Last resort: click the option element directly by its (now distinctive) text.
    return _commit_by_click(root, loc, target, settle_ms)


def select_async_combobox_option(
    root,
    loc,
    query: str,
    preferred: tuple[str, ...],
    *,
    require_option: bool = True,
    timeout_ms: int = 5000,
    settle_ms: int = 350,
) -> str | None:
    """Type into an async react-select combobox, wait for the remote options to
    populate, pick the BEST-MATCHING option, and return the committed selected
    value (read back from the control, NOT the typed text).

    Args:
      root: Playwright page/frame (for reading & clicking portal options).
      loc:  Playwright locator for the combobox input/trigger to drive.
      query: text to type to trigger the async options (e.g. the city).
      preferred: intended value(s) to match against the REAL options.
      require_option: when True (Location fields), a control that shows NO
        options and merely retained the typed free text is treated as a
        FAILURE (React drops it on blur) and None is returned — never a fake
        success. When False (generic executor comboboxes), a plain free-text
        combobox that retained the typed value is accepted.
      timeout_ms: bounded wait for the async options to appear.

    Returns the committed selected value string, or None if no matching option
    could be selected (caller must report the field UNFILLED — not a fake fill).
    """
    query = (query or "").strip()
    prefs = tuple(p for p in (preferred or ()) if p) or ((query,) if query else ())

    _type_query(loc, query, timeout_ms)

    options = _poll_async_options(root, timeout_ms)
    if options:
        target = _match_real_option(prefs, options)
        if target is None:
            # Options are present but none reasonably match — do NOT blind-pick
            # the first (that is how a Venezuela city got selected live).
            logger.debug(
                "combobox: no preferred %r matched async options %r — unfilled",
                prefs, options[:12],
            )
            return None
        committed = _commit_by_click(root, loc, target, settle_ms)
        if committed:
            return committed
        return _commit_by_keyboard(root, loc, target, timeout_ms, settle_ms)

    # No async options ever appeared.
    if require_option:
        return None
    # Plain free-text combobox: accept only if the control genuinely retained
    # the typed value (a committed single-value / input value that matches).
    readback = _read_committed_value(loc)
    if readback and _reflects(readback, query):
        return readback
    return None
