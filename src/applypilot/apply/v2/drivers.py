"""WidgetDriver registry (spec §6.6). Each driver returns a read-back-verified
CommitResult; click success is never trusted (invariant 5). Drivers are PROMOTED
from prefill.py / adapters/greenhouse.py (see Task 6 Step 1 for exact sources) —
this module wraps them behind a uniform commit() dispatcher. Only react_select
opens a dropdown, and only at commit time (invariant 4).

Locator resolution seeds the field's locator_spec through healing.heal (the
10-tier ladder) and returns the WINNING tier so the executor can persist it to
the mapping cache. When the resolver hands us a hand-built Field whose
locator_spec is sparse (or empty), we fall the label/elem_id in from the Field's
own attributes so heal still has stable anchors to lock onto.

SPEED NOTE (Task 12 gate): the promoted react-select path is NOT sleep-free —
prefill._commit_combobox_keyboard / _select_combobox_by_label carry ~5 fixed
time.sleep() calls (~1.4s per react_select field). The dispatcher itself is
sleep-free (invariant 9); this tax lives in the promoted fill path and is left
intact here (no gold-plating). Measured cost is tracked against the §12.2 warm
budget in Task 12.
"""
from __future__ import annotations

from dataclasses import dataclass

from applypilot.apply.healing import heal
from applypilot.apply.v2.resolver import build_element_spec, PlannedField

# Promote (import) the winning implementations rather than re-authoring them.
# Signatures VERIFIED against prefill.py at authoring:
#   _select_combobox_robust(root, label_needles, preferred) -> bool
#   _combobox_committed(root, label_needles, preferred) -> bool
#   _visible_combobox_options(root) -> list[str]
#   _match_real_option(preferred, options) -> str | None
#   _set_greenhouse_location(root, value) -> bool
#   _set_greenhouse_phone_country(root) -> bool
from applypilot.apply.prefill import (  # noqa: F401 (kept re-exported for callers/tests)
    _select_combobox_robust,
    _combobox_committed,
    _visible_combobox_options,
    _match_real_option,
    _set_greenhouse_location,
    _set_greenhouse_phone_country,
)


@dataclass(frozen=True)
class CommitResult:
    committed: bool
    locator_tier: str | None = None
    error: str | None = None


def _element_spec(planned: PlannedField):
    """Front-end locator_spec -> healing.ElementSpec, enriched from the Field's
    own label_text / field_id when the spec is sparse. In the live resolver flow
    the locator_spec already carries these (frontend_greenhouse._locator_spec);
    the fallbacks only fire for hand-built Fields so heal always has an anchor."""
    f = planned.field
    ls = dict(f.locator_spec or {})
    if not ls.get("label") and f.label_text:
        ls["label"] = f.label_text
    if not ls.get("elem_id") and f.field_id:
        ls["elem_id"] = f.field_id
    return build_element_spec(ls)


def _locate(scope, planned: PlannedField, *, timeout_ms: int = 1500):
    """Resolve the target locator + winning healing tier (invariant: the tier is
    reported so the executor can cache the successful locator strategy)."""
    spec = _element_spec(planned)
    loc, tier = heal(scope, spec, timeout_ms=timeout_ms)
    return loc, tier


def _do_fill(loc, value, *, timeout_ms: int = 1500):
    """Extracted so tests can monkeypatch a no-op fill to exercise the read-back
    (a silent fill failure must still surface as committed=False)."""
    loc.fill(str(value), timeout=timeout_ms)


def _read_input_value(loc) -> str:
    try:
        return loc.input_value(timeout=1000) or ""
    except Exception:  # noqa: BLE001
        return ""


def _text(scope, planned: PlannedField) -> CommitResult:
    if planned.value is None:                    # optional field, no profile data
        return CommitResult(False, error="no_value")
    loc, tier = _locate(scope, planned)
    if loc is None:
        return CommitResult(False, error="not_located")
    try:
        _do_fill(loc, planned.value)
    except Exception as e:                        # noqa: BLE001
        return CommitResult(False, tier, str(e))
    # READ-BACK (never trust fill success) — invariant 5.
    got = _read_input_value(loc)
    return CommitResult(str(got).strip() == str(planned.value).strip(), tier)


def _textarea(scope, planned: PlannedField) -> CommitResult:
    """Long free-text with a driver-side char_limit clamp. The primary clamp is
    the operator's _validate (txt[:spec.char_limit]); the front-end never sets
    char_limit (maxlength is not in the observation — Task 4), so this only
    truncates when a Field explicitly carries one. Otherwise identical to _text."""
    if planned.value is None:
        return CommitResult(False, error="no_value")
    loc, tier = _locate(scope, planned)
    if loc is None:
        return CommitResult(False, error="not_located")
    value = str(planned.value)
    cl = planned.field.char_limit
    if cl:
        value = value[:cl]
    try:
        _do_fill(loc, value)
    except Exception as e:                        # noqa: BLE001
        return CommitResult(False, tier, str(e))
    got = _read_input_value(loc)
    return CommitResult(str(got).strip() == value.strip(), tier)


def _file(scope, planned: PlannedField) -> CommitResult:
    if not planned.value:
        return CommitResult(False, error="no_value")
    loc, tier = _locate(scope, planned)
    if loc is None:
        return CommitResult(False, error="not_located")
    try:
        loc.set_input_files(planned.value, timeout=2000)   # greenhouse.py:497 pattern
    except Exception as e:                        # noqa: BLE001
        return CommitResult(False, tier, str(e))
    # READ-BACK: the input carries a filename (chip settle handled by executor).
    try:
        got = loc.evaluate("el => (el.files && el.files.length) ? el.files[0].name : ''")
    except Exception:                             # noqa: BLE001
        got = ""
    return CommitResult(bool(got), tier)


def _react_select(scope, planned: PlannedField) -> CommitResult:
    """Lazy-enumerate at commit, map intent -> real option, portal-click then
    keyboard fallback, read-back via _combobox_committed (invariants 4/5/6).

    _select_combobox_robust folds BOTH paths: portal-click (verified via
    _combobox_committed) and, on the desync, the keyboard commit — which itself
    opens the dropdown, reads the REAL options (_visible_combobox_options), maps
    the intent to a real option (_match_real_option, index-safe), and only ever
    types a string that exists. A non-matching intent commits nothing."""
    f = planned.field
    needles = tuple(n for n in (f.label_text, f.question_text) if n)
    intent = (planned.option_intent or "").strip()
    if not intent:                                # optional enumerated field, no data
        return CommitResult(False, error="no_intent")
    # The intent string is preferred[0]; the promoted code maps it to a REAL
    # option (index-safe) so invariant 6 holds without a separate index dance.
    ok = _select_combobox_robust(scope, needles, (intent,))
    if not ok:
        return CommitResult(False, error="no_matching_option")
    # The read-back IS _combobox_committed — the real committed state, never the
    # click result.
    committed = _combobox_committed(scope, needles, (intent,))
    _, tier = _locate(scope, planned)            # best-effort tier for telemetry
    return CommitResult(bool(committed), tier)


def _native_select(scope, planned: PlannedField) -> CommitResult:
    intent = planned.option_intent
    if not intent:
        return CommitResult(False, error="no_intent")
    loc, tier = _locate(scope, planned)
    if loc is None:
        return CommitResult(False, error="not_located")
    try:
        loc.select_option(label=intent, timeout=1500)
    except Exception:                             # noqa: BLE001
        # fall back to value/index matching against real options
        try:
            loc.select_option(intent, timeout=1500)
        except Exception as e:                    # noqa: BLE001
            return CommitResult(False, tier, str(e))
    # READ-BACK: the currently-selected option's text.
    try:
        got = loc.evaluate(
            "el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : ''")
    except Exception:                             # noqa: BLE001
        got = ""
    return CommitResult(str(intent).lower() in str(got).lower(), tier)


def _typeahead_location(scope, planned: PlannedField) -> CommitResult:
    if not planned.value:
        return CommitResult(False, error="no_value")
    ok = _set_greenhouse_location(scope, planned.value)   # self-verifying read-back inside
    return CommitResult(bool(ok), "location_typeahead")


def _phone_intl(scope, planned: PlannedField) -> CommitResult:
    # phone_intl sets the COUNTRY combobox; the phone-number digits are a
    # separate `text` field the executor sequences after this.
    ok = _set_greenhouse_phone_country(scope)             # self-verifying read-back inside
    return CommitResult(bool(ok), "phone_country")


_REGISTRY = {
    "text": _text,
    "textarea": _textarea,
    "file": _file,
    "react_select": _react_select,
    "native_select": _native_select,
    "typeahead_location": _typeahead_location,
    "phone_intl": _phone_intl,
}


def commit(scope, planned: PlannedField) -> CommitResult:
    """Dispatch on planned.driver; every driver ends in a read-back. Driver
    exceptions are wrapped into CommitResult(False, error=...) — one field never
    kills the whole fill (this wrapper is the last line of defense, not the
    design; each driver already handles its own None/empty inputs)."""
    driver = _REGISTRY.get(planned.driver)
    if driver is None:
        return CommitResult(False, error=f"no_driver:{planned.driver}")
    try:
        return driver(scope, planned)
    except Exception as e:                        # noqa: BLE001
        return CommitResult(False, error=str(e))
