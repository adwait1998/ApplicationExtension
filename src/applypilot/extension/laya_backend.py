"""Tier 3 of the resolution ladder: Laya semantic field classification.

Laya (https://pypi.org/project/laya/, publisher Convai Innovations) is a
non-autoregressive *decision engine*, not a text generator. It answers
typed questions about a "state" in one forward pass with a calibrated
confidence. Here it answers exactly one question per field: "which stored
profile key (from a short pre-ranked candidate list) does this form field
want, or none of them?" It never invents a value — ``resolve.py`` still
resolves the winning key to an actual profile value and still applies its
own confidence gate and secret-path check; this module only classifies.

Everything here is optional and opt-in:

* Set ``APPLYPILOT_LAYA=1`` (or any truthy value, see ``_laya_enabled``) to
  activate this tier. Unset (the default) -> ``get_backend()`` returns
  ``None`` without importing ``laya`` or ``torch`` at all, so a user who
  never asked for Laya never pays its ~200MB import cost, let alone its
  ~800MB English-checkpoint RSS.
* Even when enabled, no model weights are loaded until the first
  ``classify()`` call (or an explicit ``warmup()``) — importing this
  module, and even calling ``get_backend()``, must stay fast because the
  service imports it at startup and polls ``/health`` regularly.
* Only the English checkpoint is ever requested (``model="english"``
  passed explicitly to every ``Router`` call). A bare ``Router(preload=True)``
  downloads all three checkpoints (~2.26GB, ~4.4GB peak RSS on this
  machine) — this use case is US-English job applications and needs only
  the ~807MB English one. ``model="english"`` also skips Laya's own
  language-detection pass entirely (see ``Router.route`` — explicit
  ``model`` short-circuits before script/language detection runs), which
  is a second reason it is safer than relying on auto-detection: a form
  field containing non-Latin text can never trigger a multilingual
  checkpoint load through this module.
* Every failure path (package missing, model missing, download failure,
  OOM, malformed response, unexpected exception of any kind) degrades to
  ``None``. A field silently left unresolved is an acceptable outcome; a
  500 on someone's real job application is not.
"""
from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Any

from applypilot.extension import matcher
from applypilot.extension.schema import FieldDescriptor

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime cost
    from laya import Router  # type: ignore

_ENV_VAR = "APPLYPILOT_LAYA"
_ENGLISH_MODEL = "english"
_QUESTION_ID = "profile_key"

# Short, human-readable descriptions for the profile-key vocabulary Laya is
# asked to choose among. A bare dotted path ("personal.postal_code") is
# poor input for a semantic classifier; these give it something meaningful
# to match a form field's label/name/placeholder against. Kept in sync
# with matcher.CANDIDATE_KEYS by the fallback in _describe() below, so a
# future key added there without a matching entry here still gets
# something reasonable rather than a bare dotted path.
_KEY_DESCRIPTIONS: dict[str, str] = {
    "personal.full_name": "the applicant's full name",
    "personal.email": "the applicant's email address",
    "personal.phone": "the applicant's phone number",
    "personal.address": "the applicant's street address",
    "personal.city": "the city the applicant lives in",
    "personal.province_state": "the state or province the applicant lives in",
    "personal.country": "the country the applicant lives in",
    "personal.postal_code": "the applicant's postal or zip code",
    "personal.linkedin_url": "the applicant's LinkedIn profile URL",
    "personal.github_url": "the applicant's GitHub profile URL",
    "personal.portfolio_url": "the applicant's portfolio or personal website URL",
    "personal.website_url": "the applicant's personal website URL",
    "experience.current_company": "the applicant's current employer",
    "experience.current_job_title": "the applicant's current job title",
}

_NONE_KEY = "none"
_NONE_DESCRIPTION = "no stored profile field answers this"


def _describe(key: str) -> str:
    """A short human description of a candidate profile key for Laya's
    choice criteria. Falls back to matcher's synonym vocabulary, then to
    the dotted path's own words, so a key added to matcher.CANDIDATE_KEYS
    without a matching entry here still gets a reasonable description
    instead of a bare dotted path (or a KeyError)."""
    described = _KEY_DESCRIPTIONS.get(key)
    if described:
        return described
    words = [w for w in key.replace("_", " ").replace(".", " ").split() if w not in ("personal", "experience")]
    syn = matcher._KEY_SYNONYMS.get(key)
    if syn:
        for word in syn:
            if word not in words:
                words.append(word)
    return " ".join(words) or key


def _laya_enabled() -> bool:
    """Opt-in gate. Unset/empty/"0"/"false"/"no"/"off" (case-insensitive)
    all mean disabled; anything else means enabled."""
    val = os.environ.get(_ENV_VAR, "").strip().lower()
    return val not in ("", "0", "false", "no", "off")


def _state_for(field: FieldDescriptor) -> dict[str, Any]:
    """Build the Laya "state" describing a form field, from whatever the
    content script actually observed. Never includes a value — only shape
    (label, name, placeholder, type, options), the same information the
    deterministic tier is given."""
    state: dict[str, Any] = {
        "field_label": field.label or "",
        "field_name": field.name or "",
        "field_type": field.type or "",
    }
    if field.placeholder:
        state["field_placeholder"] = field.placeholder
    if field.autocomplete:
        state["field_autocomplete"] = field.autocomplete
    if field.options:
        state["options"] = list(field.options)
    return state


def _questions_for(candidate_keys: list[str]) -> dict[str, Any]:
    """Build the single `choice` question. Criteria are the pre-ranked
    candidate keys (caller caps these at matcher.LAYA_MAX_CANDIDATES, so
    together with "none" this never exceeds Laya's ~10-option calibrated
    range) plus a "none" escape hatch."""
    criteria = {key: _describe(key) for key in candidate_keys}
    criteria[_NONE_KEY] = _NONE_DESCRIPTION
    return {
        _QUESTION_ID: {
            "type": "choice",
            "instructions": (
                "Which stored profile field should be used to answer this "
                "job-application form field?"
            ),
            "criteria": criteria,
        }
    }


class _LayaBackend:
    """Lazy wrapper around a single ``laya.Router``. Constructing this
    object does no model work; the Router is built (and, on first use, the
    English checkpoint is downloaded/loaded) only inside ``_ensure_router``,
    called from ``classify()`` or ``warmup()``.

    One Router instance is reused for the process lifetime so the loaded
    checkpoint stays resident (Router caches agents by name internally) —
    a fresh Router per call would pay the multi-hundred-ms load every time.
    """

    def __init__(self) -> None:
        self._router: "Router | None" = None
        self._unusable = False
        self._lock = threading.Lock()

    def _ensure_router(self) -> "Router | None":
        if self._router is not None or self._unusable:
            return self._router
        with self._lock:
            if self._router is not None or self._unusable:
                return self._router
            try:
                from laya import Router  # local import: this is the heavy dependency

                # preload defaults to False -- constructing a Router does not
                # download or build any checkpoint by itself. Never pass
                # preload=True here: with no `models` override that silently
                # downloads all three checkpoints (~2.26GB). The English
                # checkpoint is loaded lazily, and only it, via the explicit
                # model="english" argument passed to every predict() call
                # below.
                self._router = Router()
            except Exception:
                self._unusable = True
                self._router = None
        return self._router

    def warmup(self) -> bool:
        """Force the English checkpoint to load now instead of on the
        first real request. Best-effort and never raises; returns whether
        it succeeded."""
        router = self._ensure_router()
        if router is None:
            return False
        try:
            router.load(_ENGLISH_MODEL)
            return True
        except Exception:
            self._unusable = True
            self._router = None
            return False

    def classify(
        self, field: FieldDescriptor, candidate_keys: list[str]
    ) -> tuple[str, float] | None:
        """Answer "which candidate key does this field want?" Returns
        (key, confidence) with key guaranteed to be a member of
        candidate_keys, or None on "none", an out-of-range answer, or any
        failure at all (missing router, download failure, OOM, malformed
        response, ...). Never raises."""
        if not candidate_keys:
            return None
        router = self._ensure_router()
        if router is None:
            return None
        try:
            state = _state_for(field)
            questions = _questions_for(candidate_keys)
            result = router.predict(state, questions, model=_ENGLISH_MODEL)
            answer = result["answers"][_QUESTION_ID]
            choice = answer["choice"]
            confidence = float(answer["confidence"])
        except Exception:
            return None
        if choice not in candidate_keys:
            # covers "none" and any malformed/unexpected answer alike --
            # resolve.py only ever expects a key that was offered.
            return None
        return (choice, confidence)


_backend_lock = threading.Lock()
_backend_singleton: "_LayaBackend | None" = None
_backend_checked = False


def get_backend() -> _LayaBackend | None:
    """Return the Laya backend, or None if disabled, absent, or unusable.

    Cheap on the common paths: returns None immediately, with no import
    attempted, whenever APPLYPILOT_LAYA is unset (the default) -- this is
    what keeps the service's startup and /health polling fast for the vast
    majority of users who never opted in. Once enabled, the "is the laya
    package importable and constructible" check runs once and the result
    (instance or None) is cached for the life of the process, so repeated
    calls (tiers_available() is called on every /health request) never
    re-import or re-construct anything. Actual model-checkpoint loading is
    still deferred to classify()/warmup() on the returned instance.
    """
    global _backend_singleton, _backend_checked
    if not _laya_enabled():
        return None
    if _backend_checked:
        return _backend_singleton
    with _backend_lock:
        if _backend_checked:
            return _backend_singleton
        try:
            from laya import Router  # noqa: F401  -- import/construct probe only

            _backend_singleton = _LayaBackend()
        except Exception:
            _backend_singleton = None
        _backend_checked = True
    return _backend_singleton
