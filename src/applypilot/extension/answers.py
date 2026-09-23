"""Tiers 5 and 6 of the resolution ladder: answer bank + LLM draft.

Both tiers are wiring around one already-built piece of machinery,
``applypilot.apply.answer_cache.AnswerCache`` (Reliability-v2 Phase D):
it embeds an incoming question, finds the nearest previously-answered one
(profile-derived seeds, plus a persisted Q&A bank), and calls an LLM only
on a genuine miss.

::

    AnswerCache.answer() -> AnswerResult(answer, source, similarity, ...)
        source == "seed" | "cache"  -> tier 5: answer bank, auto-fill
        source == "llm"             -> tier 6: draft, auto-fill but draft=True
        anything else (canary short-circuit, e.g. "profile"/"unresolved")
                                     -> not ours; match() returns None

Both tiers are off by default:

* Tier 5 needs ``APPLYPILOT_ANSWERS=1``.
* Tier 6 needs ``APPLYPILOT_DRAFTS=1`` **and** ``APPLYPILOT_ANSWERS=1``. A
  draft is just the "the bank missed" branch of the *same*
  ``AnswerCache.answer()`` call tier 5 makes -- there is no call to hang a
  draft off of if tier 5 itself is off, so ``APPLYPILOT_DRAFTS=1`` alone
  (bank flag unset) is a deliberate no-op, not a partial feature. See
  ``drafts_enabled()``.

Non-negotiables this module exists to uphold (see the design spec,
``docs/superpowers/specs/2026-09-24-copilot-full-helper-design.md``):

1. **Never persist unreviewed drafts.** ``make_cache()`` loads
   ``bank_path`` (default: the repo-root ``answer_bank.json`` -- the 98
   curated real Q&A pairs) for *matching only*, then clears
   ``AnswerCache.bank_path`` on the instance so its own ``_persist()``
   becomes a no-op. An LLM miss must never be silently appended to the
   file the operator has never reviewed.
2. **Canaries never reach these tiers.** Tier 1 in ``resolve.py`` is
   already terminal for a canary-classified label, so a canary field
   never calls ``match()`` at all. Belt-and-suspenders: ``AnswerCache``
   itself short-circuits ``is_canary()`` questions to ``resolve_canary``
   before ever touching the bank/LLM, so even a direct ``match()`` call on
   a canary-shaped question cannot produce an ``answer_bank``/``draft``
   fill (that "profile"/"unresolved" source falls through to ``None``
   below, unhandled by design).
3. **Grounded drafts.** ``_context_for()`` carries ``resume_facts``
   (``preserved_companies``, ``preserved_projects``, ``real_metrics``)
   plus current title / years of experience / target role, and
   ``_SYSTEM_PROMPT`` instructs the model to use only what it was given
   and to answer vaguely rather than invent a specific, unsupported claim.
4. **Fail soft.** No LLM configured, network down, malformed response,
   embedding failure -> ``match()`` returns ``None`` and the field falls
   through to the ladder's generic "unresolved" skip. A blank box beats a
   500 on someone's real application.
5. **Latency-capped.** One ``/resolve`` request can carry many unanswered
   fields; an LLM call per field could take many seconds each. ``DraftBudget``
   caps how many of those this module will actually place per batch
   (default 5, ``APPLYPILOT_MAX_DRAFTS``); the rest are skipped with a
   reason that says so, rather than the request hanging.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from applypilot.apply.answer_cache import AnswerCache
from applypilot.extension import grounding
from applypilot.extension.schema import FieldDescriptor, FillResult, SkipResult

_ANSWERS_ENV = "APPLYPILOT_ANSWERS"
_DRAFTS_ENV = "APPLYPILOT_DRAFTS"
_MAX_DRAFTS_ENV = "APPLYPILOT_MAX_DRAFTS"
_DEFAULT_MAX_DRAFTS = 5

# Matches AnswerCache's own default (see answer_cache.py); named here so a
# future change to one is a deliberate decision about the other, not a
# silent drift.
_THRESHOLD = 0.70

# The repo-root answer_bank.json -- 98 real Q/A pairs harvested from the
# operator's actual past applications (see the design spec). Deliberately
# NOT config.APP_DIR/answer_bank.json: that is the live, ever-growing bank
# the autonomous pipeline (apply/launcher.py) reads/writes at
# E:\applypilot-data, a different file this module must never touch (tests
# for this module must never go near it either). This module only ever
# READS a bank -- see make_cache()'s persistence-no-op contract.
_DEFAULT_BANK_PATH = Path(__file__).resolve().parents[3] / "answer_bank.json"


def _env_truthy(name: str) -> bool:
    """Unset/empty/"0"/"false"/"no"/"off" (case-insensitive) = disabled;
    anything else = enabled. Mirrors laya_backend._laya_enabled() exactly."""
    val = os.environ.get(name, "").strip().lower()
    return val not in ("", "0", "false", "no", "off")


def answers_enabled() -> bool:
    """Tier 5 gate."""
    return _env_truthy(_ANSWERS_ENV)


def drafts_enabled() -> bool:
    """Tier 6 gate -- see the module docstring for why this implies tier 5."""
    return answers_enabled() and _env_truthy(_DRAFTS_ENV)


def _max_draft_calls() -> int:
    raw = os.environ.get(_MAX_DRAFTS_ENV, "")
    try:
        n = int(raw)
    except ValueError:
        return _DEFAULT_MAX_DRAFTS
    return n if n >= 0 else _DEFAULT_MAX_DRAFTS


class DraftBudget:
    """Caps how many LLM draft calls one ``/resolve`` batch may make.

    An ATS "review" step can dump dozens of unanswered free-text fields on
    the extension at once, and the popup is waiting synchronously on this
    HTTP response -- an LLM call per field, at several seconds each, would
    turn one page into a multi-minute hang. Share one instance across a
    ``resolve_fields()`` batch (``resolve.py`` does this); fields beyond
    the cap are skipped with an explicit reason instead of queued.
    """

    def __init__(self, limit: int | None = None) -> None:
        self.limit = _max_draft_calls() if limit is None else limit
        self.used = 0

    def take(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


class _BudgetExhausted(Exception):
    """Internal control-flow signal: the wrapped llm_fn raises this instead
    of calling the real one once the batch's draft cap is spent, so
    match() can tell "no budget left" apart from "the LLM ran and
    returned nothing". Caught immediately in match(); never escapes this
    module."""


def _budgeted(real_fn: Callable[[str, str], str], budget: DraftBudget) -> Callable[[str, str], str]:
    def _fn(question: str, context: str) -> str:
        if not budget.take():
            raise _BudgetExhausted()
        return real_fn(question, context)

    return _fn


def make_cache(profile: dict, bank_path: str | Path | None = None) -> AnswerCache:
    """Build the AnswerCache tiers 5/6 share, with persistence disabled.

    Loads ``bank_path`` (default: the repo-root ``answer_bank.json``) so
    matching sees the full curated bank, then clears the *instance's*
    ``bank_path`` so ``AnswerCache._persist()`` -- which only writes when
    ``self.bank_path`` is truthy -- becomes a no-op. This is the one thing
    that must never regress: an LLM miss must never be appended to a file
    the operator has never reviewed.
    """
    path = _DEFAULT_BANK_PATH if bank_path is None else Path(bank_path)
    cache = AnswerCache(profile, bank_path=path, threshold=_THRESHOLD)
    cache.bank_path = None
    return cache


_SYSTEM_PROMPT = (
    "You are drafting an answer to a job-application screening question "
    "that will appear under a real applicant's name on a real form. "
    "Answer concisely (1-3 sentences), in the applicant's voice, using "
    "ONLY the facts given to you below (current title, years of "
    "experience, target role, and the explicitly listed companies, "
    "projects and metrics). Never invent an employer, job title, project, "
    "date, or number that is not explicitly given below. If the given "
    "facts do not support a specific claim, answer honestly in general "
    "terms rather than fabricate a specific detail -- a vaguer true answer "
    "is always better than a fabricated specific one. Output ONLY the "
    "answer text, nothing else."
)


def _context_for(profile: dict, field: FieldDescriptor | None = None) -> str:
    """Grounding context for the draft prompt: resume_facts plus a little
    profile shape. This is what makes non-negotiable #3 real -- everything
    the model is allowed to claim has to come from here."""
    p = profile or {}
    exp = p.get("experience", {}) or {}
    facts = p.get("resume_facts", {}) or {}

    lines: list[str] = []
    if exp.get("current_title"):
        lines.append(f"Current title: {exp['current_title']}")
    if exp.get("target_role"):
        lines.append(f"Target role: {exp['target_role']}")
    if exp.get("years_of_experience_total"):
        lines.append(f"Years of experience: {exp['years_of_experience_total']}")

    companies = facts.get("preserved_companies") or []
    if companies:
        lines.append("Real companies you may reference: " + ", ".join(companies))
    projects = facts.get("preserved_projects") or []
    if projects:
        lines.append("Real projects you may reference: " + ", ".join(projects))
    metrics = facts.get("real_metrics") or []
    if metrics:
        lines.append("Real metrics you may cite: " + ", ".join(metrics))

    if field is not None and field.options:
        lines.append("If relevant, answer consistently with these listed "
                      "options: " + ", ".join(field.options))

    return "\n".join(lines)


def _real_llm_fn(question: str, context: str) -> str:
    """The genuine tier-6 LLM call. Fails soft to "" on any error at all --
    no provider configured, network down, malformed response -- so a real
    application never sees a 500 for this; the field just stays blank."""
    try:
        from applypilot.llm import get_client

        msgs = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"{context}\n\nQUESTION: {question}".strip()},
        ]
        return (get_client().chat(msgs, max_tokens=256, temperature=0.3) or "").strip()
    except Exception:
        return ""


def _refuse_llm(question: str, context: str) -> str:
    """Used whenever drafts are disabled: tier 5 alone must never invoke
    any LLM, even on a genuine bank miss -- not the real one, not an
    injected one. Always "" -> AnswerCache reports a miss with no answer,
    which match() below treats as "no opinion" and lets fall through."""
    return ""


def _target_company(field: FieldDescriptor | None) -> str:
    """Best-effort name of the employer being applied to. Naming them in an
    answer is normal and truthful, so the grounding check must not treat it as
    a fabricated employer. Derived from the field's section/label text only —
    this module never sees the page URL."""
    if field is None:
        return ""
    for text in (getattr(field, "section", ""), getattr(field, "label", "")):
        for token in ("at ", "for ", "join "):
            idx = (text or "").lower().find(token)
            if idx >= 0:
                tail = text[idx + len(token):].strip(" ?.,:;!")
                if tail:
                    return tail
    return ""


def match(
    field: FieldDescriptor,
    profile: dict,
    *,
    cache: AnswerCache | None = None,
    budget: DraftBudget | None = None,
    bank_path: str | Path | None = None,
    llm_fn: Callable[[str, str], str] | None = None,
) -> FillResult | SkipResult | None:
    """Tiers 5 (answer bank) and 6 (draft), one ``AnswerCache.answer()``
    call split on its returned ``source``.

    Returns ``None`` when tier 5 is disabled, the field has no question
    text, or anything fails -- the ladder then falls through to its own
    generic "unresolved" skip, exactly as if this tier did not exist.
    ``cache``/``budget`` let ``resolve_fields()`` share one instance across
    a whole batch (required for the draft cap to mean anything); a direct
    call without them builds private, single-call ones.
    """
    if not answers_enabled():
        return None

    question = (field.label or field.name or field.placeholder or "").strip()
    if not question:
        return None

    drafting = drafts_enabled()
    ac = cache if cache is not None else make_cache(profile, bank_path)
    ctx = _context_for(profile, field)

    if drafting:
        real_fn = llm_fn or _real_llm_fn
        b = budget if budget is not None else DraftBudget()
        wrapped = _budgeted(real_fn, b)
    else:
        # Drafts off: never let a bank miss reach any LLM, real or injected.
        wrapped = _refuse_llm
        b = None

    try:
        result = ac.answer(question, context=ctx, llm_fn=wrapped)
    except _BudgetExhausted:
        return SkipResult(
            id=field.id,
            source="draft",
            reason=f"draft cap reached ({b.limit if b else 0} per request) — left for you to fill",
            auto_fill=False,
        )
    except Exception:
        return None

    if result.source in ("seed", "cache") and result.answer:
        matched = f': "{result.matched_q}"' if result.matched_q else ""
        return FillResult(
            id=field.id,
            value=result.answer,
            source="answer_bank",
            profile_key=f"answer_bank:{result.source}",
            confidence=round(float(result.similarity), 3),
            auto_fill=True,
            reason=f"answer bank match (similarity {result.similarity:.2f}){matched}",
        )

    if result.source == "llm" and result.answer:
        # Grounding was, until this check, only an instruction in the system
        # prompt — nothing verified the model obeyed it. A draft that claims
        # employment somewhere the applicant has never worked is the worst
        # output this tier can produce, and the DRAFT badge only helps if the
        # operator happens to read carefully. So an unsupported employment
        # claim is REFUSED outright: an empty box is strictly better.
        unsupported = grounding.find_unsupported_claims(
            result.answer, profile, target_company=_target_company(field)
        )
        if unsupported:
            return SkipResult(
                id=field.id,
                source="draft",
                reason=("draft refused — it claimed experience at "
                        f"{', '.join(unsupported)}, which is not in your history. "
                        "Answer this one yourself."),
                auto_fill=False,
            )
        return FillResult(
            id=field.id,
            value=result.answer,
            source="draft",
            profile_key="draft:llm",
            confidence=round(float(result.similarity), 3),
            auto_fill=True,
            reason="drafted by LLM from your resume facts — not from your profile, review before submitting",
            draft=True,
        )

    # Canary short-circuit ("profile"/"unresolved") or an empty answer from
    # either path -- not ours to fill. Let the ladder's own tier 7 handle it.
    return None
