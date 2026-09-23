"""The resolution ladder. Applied per field, first match wins:

0. Secret guard   personal.password and anything on the denylist — never
                   emitted, checked again at the point of emission no
                   matter which tier produced the candidate.
1. Canary         applypilot.apply.canary, used verbatim. Work auth,
                   sponsorship, citizenship, salary, EEO, address, DOB,
                   clearance, export control. A canary hit NEVER falls
                   through to a lower tier — a canary with no resolvable
                   answer stays a canary skip (mirrors the pipeline's
                   invariant 7).
2. Deterministic  applypilot.extension.matcher — autocomplete, then
                   name/id/label regex.
3. Structured     applypilot.extension.structured — work_history[] /
                   education[], indexed by the scanner's section/
                   section_index. Like canary, a field this tier
                   recognises (kind + slot both established) NEVER falls
                   through to a lower tier — an out-of-range section index
                   stays a structured skip rather than risking Laya
                   guessing a value into the wrong position.
4. Laya           optional, lazy, absent by default. Gated on confidence.
5. Answer bank    applypilot.extension.answers — AnswerCache seed/cache
                   hit (a standard screening question, or one this bank
                   has answered before). Optional, off by default
                   (APPLYPILOT_ANSWERS=1).
6. Draft          same AnswerCache.answer() call as tier 5, but its LLM
                   missed the bank and generated new text. Filled, but
                   marked draft=True for distinct review in the UI.
                   Optional, off by default (APPLYPILOT_DRAFTS=1, which
                   also requires APPLYPILOT_ANSWERS=1 — see answers.py).
7. Unresolved     left for the human.
"""
from __future__ import annotations

import re
from typing import Protocol

from applypilot.apply import canary
from applypilot.extension import answers, matcher, structured
from applypilot.extension.schema import FieldDescriptor, FillPlan, FillResult, SkipResult

# ---------------------------------------------------------------------------
# Tier 0: secret guard
# ---------------------------------------------------------------------------

# Profile paths that must never be emitted, under any circumstance, by any
# tier. Checked twice: once against the field itself (name/label/autocomplete
# smell like a credential field) and once against whatever profile_key a
# tier produced — so a bug in a higher tier (e.g. a future Laya backend
# mis-classifying a field as personal.password) still cannot leak it.
SECRET_PROFILE_PATHS: set[str] = {"personal.password"}

_SECRET_FIELD_RE = re.compile(
    r"\b(password|passwd|pwd|ssn|social\s*security|credit\s*card|cvv|cvc|"
    r"api[_ -]?key|secret)\b",
    re.I,
)


def is_secret_path(path: str | None) -> bool:
    if not path:
        return False
    bare = path.split("#", 1)[0]
    return bare in SECRET_PROFILE_PATHS


def is_secret_field(field: FieldDescriptor) -> bool:
    hay = " ".join(
        str(x or "") for x in (field.label, field.name, field.autocomplete, field.placeholder)
    )
    return bool(_SECRET_FIELD_RE.search(hay))


def _secret_skip(field: FieldDescriptor) -> SkipResult:
    return SkipResult(
        id=field.id,
        source="secret_guard",
        reason="secret/credential field — never emitted",
        auto_fill=False,
    )


# ---------------------------------------------------------------------------
# Tier 4: Laya — narrow protocol only, absent by default
# ---------------------------------------------------------------------------


class LayaBackend(Protocol):
    """What a Laya backend must implement. classify() answers a `choice`
    question — which profile key (from candidate_keys) this field wants,
    with a calibrated confidence — never generates free text and never
    touches canary fields (the ladder never offers it one)."""

    def classify(
        self, field: FieldDescriptor, candidate_keys: list[str]
    ) -> tuple[str, float] | None: ...


_LAYA_CONFIDENCE_THRESHOLD = 0.75


def get_backend() -> LayaBackend | None:
    """Return the Laya backend if installed and usable, else None.

    Lazy and fully defensive: any import failure or construction error
    degrades to "absent" rather than raising. Laya is optional — the
    ladder (and /health's tiers_available) must be correct whether or not
    a separately-developed backend module exists yet.
    """
    try:
        from applypilot.extension import laya_backend  # type: ignore
    except ImportError:
        return None
    try:
        return laya_backend.get_backend()
    except Exception:
        return None


def tiers_available(laya: LayaBackend | None = None, app_dir=None) -> list[str]:
    tiers = ["canary", "deterministic"]
    backend = laya if laya is not None else get_backend()
    if backend is not None:
        tiers.append("laya")
    if answers.answers_enabled(app_dir):
        tiers.append("answer_bank")
    if answers.drafts_enabled(app_dir):
        tiers.append("draft")
    return tiers


def _canary_category(question: str) -> str:
    """Best-effort label for observability only (which canary rule fired) —
    resolution itself always goes through canary.is_canary/resolve_canary
    verbatim, this never influences the decision."""
    q = question or ""
    for name, rx in canary._MARKERS.items():
        if rx.search(q):
            return name
    return "canary"


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def resolve_field(
    field: FieldDescriptor,
    profile: dict,
    laya: LayaBackend | None = None,
    *,
    answer_cache: "answers.AnswerCache | None" = None,
    draft_budget: "answers.DraftBudget | None" = None,
    app_dir=None,
    url: str = "",
) -> FillResult | SkipResult:
    # tier 0
    if is_secret_field(field):
        return _secret_skip(field)

    label = field.label or field.name or field.placeholder or ""

    # tier 1: canary — never falls through to a lower tier
    if canary.is_canary(label):
        answer = canary.resolve_canary(label, profile)
        category = _canary_category(label)
        if answer:
            return FillResult(
                id=field.id,
                value=answer,
                source="canary",
                profile_key=f"canary:{category}",
                confidence=1.0,
                auto_fill=True,
                reason=f"canary match ({category})",
            )
        return SkipResult(
            id=field.id,
            source="canary",
            reason=f"canary:{category} not resolvable from profile — answer this yourself",
            auto_fill=False,
        )

    # tier 5 pre-empt: "have you previously worked here?" (see answers.py's
    # previously_employed_check docstring). This is a screening QUESTION,
    # not a structured work-history FIELD, but its long-form wording
    # routinely contains the bare word "company"/"employer" -- exactly what
    # tier 3 uses to decide an unsectioned field is a work-history "company"
    # box. Checked here, before tiers 2/3 get a chance to misfire on that
    # incidental keyword, and still gated on answers_enabled() so a
    # disabled tier 5 leaves this field to behave exactly as it did before
    # this check existed.
    if answers.answers_enabled(app_dir):
        prev_employed = answers.previously_employed_check(field, profile, url, label)
        if prev_employed is not None:
            if is_secret_path(prev_employed.profile_key):
                return _secret_skip(field)
            return prev_employed

    # tier 2: deterministic
    det = matcher.match(field, profile)
    if det is not None:
        if isinstance(det, FillResult) and is_secret_path(det.profile_key):
            return _secret_skip(field)
        return det

    # tier 3: structured (work_history / education, indexed by section) —
    # never falls through once it recognises the field, same invariant as
    # canary: an unresolvable structured field stays a structured skip.
    struct = structured.match(field, profile)
    if struct is not None:
        if isinstance(struct, FillResult) and is_secret_path(struct.profile_key):
            return _secret_skip(field)
        return struct

    # tier 4: laya (optional)
    backend = laya if laya is not None else get_backend()
    if backend is not None:
        # Pre-ranked and capped: Laya's confidence is only calibrated up to
        # ~10 options, and the confidence gate below is the only thing
        # standing between it and a wrong value in a real application.
        candidates = matcher.rank_candidates(field)
        try:
            result = backend.classify(field, candidates)
        except Exception:
            result = None
        if result is not None:
            key, confidence = result
            if confidence >= _LAYA_CONFIDENCE_THRESHOLD and not is_secret_path(key):
                value = matcher.value_for_key(key, profile)
                if value:
                    return FillResult(
                        id=field.id,
                        value=value,
                        source="laya",
                        profile_key=key.split("#", 1)[0],
                        confidence=confidence,
                        auto_fill=True,
                        reason=f"laya classification (confidence {confidence:.2f})",
                    )

    # tier 5/6: answer bank + draft — one AnswerCache.answer() call, split
    # on its returned source. Optional, off by default; answers.match()
    # itself returns None immediately when APPLYPILOT_ANSWERS is unset, so
    # this is a no-op call for everyone who hasn't opted in.
    ans = answers.match(
        field, profile, cache=answer_cache, budget=draft_budget, app_dir=app_dir, url=url
    )
    if ans is not None:
        if isinstance(ans, FillResult) and is_secret_path(ans.profile_key):
            return _secret_skip(field)
        return ans

    # tier 7: unresolved
    return SkipResult(
        id=field.id,
        source="unresolved",
        reason="no deterministic match — left for you to fill",
        auto_fill=False,
    )


def resolve_fields(
    fields: list[FieldDescriptor], profile: dict, app_dir=None, url: str = "",
    bank_path=None,
) -> FillPlan:
    """Resolve a batch of fields into a FillPlan. Values are returned only
    for the fields actually passed in — the profile itself never leaves
    this function. ``app_dir`` selects which persisted extension settings
    (see settings.py) apply; ``url`` is the page's URL (server.py passes
    the incoming request's ``url``), used by tier 5's "previously employed
    here?" check to identify the employer being applied to. ``bank_path``
    is the ACTIVE PROFILE's own answer_bank.json — a bank is personal, so
    with none given only profile-derived seeds are used."""
    backend = get_backend()
    # One AnswerCache and one draft budget for the whole batch: the answer
    # bank only needs loading once per request, and the draft cap (tier 6)
    # is only meaningful shared across every field in it — see
    # answers.DraftBudget. Built only when actually enabled, so a caller
    # who never opted in never pays for loading the bank file at all.
    answer_cache = (answers.make_cache(profile, bank_path)
                    if answers.answers_enabled(app_dir) else None)
    draft_budget = answers.DraftBudget(app_dir=app_dir) if answers.drafts_enabled(app_dir) else None
    plan = FillPlan(tiers_available=tiers_available(backend, app_dir))
    for f in fields:
        result = resolve_field(
            f, profile, laya=backend, answer_cache=answer_cache, draft_budget=draft_budget,
            app_dir=app_dir, url=url,
        )
        if isinstance(result, FillResult):
            plan.fills.append(result)
        else:
            plan.skipped.append(result)
    return plan
