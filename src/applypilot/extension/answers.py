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

Tier 5 defaults ON, tier 6 defaults OFF:

* Tier 5 (answer bank) is enabled unless explicitly turned off --
  ``APPLYPILOT_ANSWERS=0`` (env, wins over everything), or
  ``answers_enabled: false`` in the persisted extension settings (see
  ``applypilot.extension.settings``). Its hits are either profile-derived
  seeds or the operator's own past answers -- both model-free -- so there
  is no reason to gate them behind an opt-in.
* Tier 6 (draft) stays opt-in: ``APPLYPILOT_DRAFTS=1`` (env) or
  ``drafts_enabled: true`` in the persisted settings, and tier 5 must also
  be enabled (from whichever source) -- a draft is just the "the bank
  missed" branch of the *same* ``AnswerCache.answer()`` call tier 5 makes,
  so there is no call to hang a draft off of if tier 5 is off. See
  ``drafts_enabled()``.
* Env vars, when set at all, always override the persisted settings file
  -- see ``applypilot.extension.settings.effective_settings()``.

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

import re
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from applypilot.apply.answer_cache import AnswerCache
from applypilot.extension import grounding, settings as ext_settings
from applypilot.extension.schema import FieldDescriptor, FillResult, SkipResult

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


def answers_enabled(app_dir: str | Path | None = None) -> bool:
    """Tier 5 gate. See ``applypilot.extension.settings.effective_settings``
    for the precedence (env override > persisted settings file > default
    True). ``app_dir=None`` (every pre-existing call site) never touches
    disk and resolves purely from the built-in default + any env var."""
    return ext_settings.effective_settings(app_dir)["answers_enabled"]


def drafts_enabled(app_dir: str | Path | None = None) -> bool:
    """Tier 6 gate -- see the module docstring for why this implies tier 5."""
    return ext_settings.effective_settings(app_dir)["drafts_enabled"]


def _max_draft_calls(app_dir: str | Path | None = None) -> int:
    return ext_settings.effective_settings(app_dir)["max_drafts"]


class DraftBudget:
    """Caps how many LLM draft calls one ``/resolve`` batch may make.

    An ATS "review" step can dump dozens of unanswered free-text fields on
    the extension at once, and the popup is waiting synchronously on this
    HTTP response -- an LLM call per field, at several seconds each, would
    turn one page into a multi-minute hang. Share one instance across a
    ``resolve_fields()`` batch (``resolve.py`` does this); fields beyond
    the cap are skipped with an explicit reason instead of queued.
    """

    def __init__(self, limit: int | None = None, app_dir: str | Path | None = None) -> None:
        self.limit = _max_draft_calls(app_dir) if limit is None else limit
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

    ``bank_path`` must be the ACTIVE PROFILE's own ``answer_bank.json``. With
    no path, the cache holds profile-derived seeds only and reads no file at
    all. It used to default to the repo-root bank — one person's real past
    answers — which put that person's answers (and personal background) into
    anyone else's applications. A bank is personal; there is no safe default
    other than none.

    Persistence is disabled on the instance (``AnswerCache._persist`` only
    writes when ``self.bank_path`` is truthy): an LLM miss must never be
    appended to a file the operator has never reviewed.
    """
    path = Path(bank_path) if bank_path is not None else None
    if path is not None and not path.exists():
        path = None
    cache = AnswerCache(profile, bank_path=path, threshold=_THRESHOLD)
    cache.bank_path = None
    return cache


# Questions whose honest answer is about ONE specific employer. A cached answer
# to one of these was written for some other company, so reusing it pastes
# "I'm drawn to Discord's…" into a Viasat application — caught end-to-end the
# moment the answer bank was switched on by default. These go to a fresh draft
# (which knows which company this is) or are left for the operator, never to
# the cache. Profile-derived seeds are unaffected: they are not company-bound.
_COMPANY_DIRECTED = re.compile(
    r"\bwhy\b.{0,40}\b(work|join|apply|applying|interested|here|us|company|role|position|team)\b"
    r"|\bhow\s+did\s+you\s+(hear|learn|find|come\s+across)\b"
    r"|\bwhat\s+(excites|interests|attracts|draws|appeals|motivates)\s+you\b"
    r"|\bwhat\s+do\s+you\s+know\s+about\b"
    r"|\b(tell|share\s+with)\s+(us|our\s+recruiters?|the\s+(hiring\s+)?team)\b"
    r"|\bcover\s+letter\b"
    r"|\bmake\s+a\s+difference\b",
    re.I,
)


def is_company_directed(question: str) -> bool:
    return bool(_COMPANY_DIRECTED.search(question or ""))


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
    application never sees a 500 for this; the field just stays blank.

    Uses ``llm_util.get_llm_client()`` rather than ``applypilot.llm.get_client()``
    directly: if the operator has never set an LLM provider env var but has
    the Claude Code CLI installed, that fallback picks it up automatically
    (see llm_util's module docstring) -- everything else about this
    function (fail-soft, provider selection order otherwise) is unchanged.
    """
    try:
        from applypilot.extension.llm_util import get_llm_client

        msgs = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"{context}\n\nQUESTION: {question}".strip()},
        ]
        return (get_llm_client().chat(msgs, max_tokens=256, temperature=0.3) or "").strip()
    except Exception:
        return ""


# Choice fields (select, radio group, checkbox, Workday dropdown) take one
# of a fixed set of answers. A stored answer is only usable if it IS one of
# them, and a draft never is: prose handed to a choice field ends up as
# whatever option the matcher can squeeze out of it ("I know relocation can
# be hard..." -> "No", because "know" contains "no").
_PROSE_WORDS = 6


def is_choice_field(field: FieldDescriptor) -> bool:
    t = (field.type or "").strip().lower()
    return (bool(field.options) or t in ("radio", "checkbox", "select-one", "select-multiple")
            or (field.tag or "").strip().lower() == "select"
            or (field.widget or "") == "wd-dropdown")


def _prefix_on_boundary(longer: str, shorter: str) -> bool:
    return longer.startswith(shorter) and not longer[len(shorter):len(shorter) + 1].isalnum()


def fits_choice(answer: str, field: FieldDescriptor) -> bool:
    """Exact option (case-insensitive), or an answer that starts with an
    option on a word boundary ("Yes, within the US" -> "Yes"), or a short
    answer an option starts with ("Yes" -> "Yes, I am authorized"). With the
    options unknown until the widget opens (Workday), only a short answer."""
    a = re.sub(r"\s+", " ", str(answer or "")).strip().lower()
    if not a:
        return False
    if (field.type or "").strip().lower() == "checkbox":
        return a in ("yes", "no", "true", "false")
    opts = [re.sub(r"\s+", " ", o).strip().lower() for o in (field.options or []) if o and o.strip()]
    if not opts:
        return len(a.split()) <= _PROSE_WORDS
    for o in opts:
        if a == o or _prefix_on_boundary(a, o):
            return True
        if len(a.split()) <= 3 and _prefix_on_boundary(o, a):
            return True
    return False


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


# ---------------------------------------------------------------------------
# "Have you previously worked here?" -- answered correctly, not just
# fuzzy-matched. The seed's generic "No" is right only for an applicant who
# has genuinely never worked at the company being applied to; the persisted
# answer_bank.json cache also holds several PAST real answers to this exact
# question phrased for a DIFFERENT employer ("...employed by Fanatics,
# Inc...", "...employed at Affirm...") which the ordinary fuzzy bank lookup
# could easily mis-serve onto an unrelated company's form. So this pattern
# is intercepted here, before the fuzzy AnswerCache.answer() call, and
# answered deterministically from work_history + the page URL instead.
# ---------------------------------------------------------------------------

_PREV_EMPLOYED_MARKERS: tuple[re.Pattern, ...] = (
    re.compile(r"previously\s+(?:worked|been\s+employed)", re.I),
    re.compile(r"worked\s+(?:here|for\s+(?:us|this\s+company))\s+before", re.I),
    re.compile(r"employed\s+by\s+(?:our|this|the)\s+company", re.I),
    re.compile(r"any\s+of\s+its\s+(?:subsidiaries|affiliates)", re.I),
    re.compile(r"former\s+employee", re.I),
    re.compile(r"(?:now,?\s+)?(?:or\s+)?have\s+you\s+ever\s+(?:worked|been\s+employed)", re.I),
    # Seen on real forms: "Have you worked at or been a consultant for SoFi
    # or any of its affiliates?" / "Are you currently employed with or have
    # been employed by SoFi..." / "Are you currently a SoFi employee?"
    re.compile(r"have\s+you\s+(?:ever\s+)?(?:worked|been\s+(?:employed|a\s+(?:consultant|contractor|"
               r"intern|temp)))\s+(?:at|for|with|by)\b", re.I),
    re.compile(r"\b(?:currently|previously|ever)\s+(?:been\s+)?employed\s+(?:with|by)\b", re.I),
    re.compile(r"\bare\s+you\s+(?:currently\s+)?(?:a|an)\s+[^?]{1,60}?\b(?:employee|contractor)\b", re.I),
)

# A company name volunteered directly in the question text itself --
# "...employed by Fanatics, Inc..." / "...employed at Affirm...". Written
# with an explicit capital first letter (like grounding.py's _ORG) because
# case IS the signal that separates a real proper noun from a generic
# "employed by our company" filler phrase, which is deliberately NOT matched
# here (lowercase "our").
_COMPANY_IN_QUESTION_RE = re.compile(
    r"employed\s+(?:by|at)\s+(?P<name>[A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})"
)
_WORKED_AT_QUESTION_RE = re.compile(
    r"worked\s+(?:at|for)\s+(?P<name>[A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})"
)
_NAME = r"(?P<name>[A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})"
# More phrasings seen on real forms.
_MORE_COMPANY_IN_QUESTION_RES = (
    re.compile(r"(?:consultant|contractor|intern|employee)\s+(?:for|of|at|with)\s+" + _NAME),
    re.compile(r"employed\s+with\s+(?:or\s+[^?]{0,40}?\s+)?" + _NAME),
    re.compile(r"\b(?:a|an)\s+(?:current\s+|former\s+)?" + _NAME + r"(?:\s+or\s+[^?]{0,60}?)?\s+employee\b"),
)

# Host labels that identify the ATS platform, not the employer -- stripped
# before treating a host label as the company name.
_GENERIC_HOST_LABELS = {
    "www", "careers", "career", "jobs", "job", "apply", "boards", "talent",
    "recruiting", "recruitment", "hire", "hiring", "join",
}
_WORKDAY_HOST_MARKER = "myworkdayjobs.com"
_PATH_COMPANY_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com")
_SUBDOMAIN_COMPANY_HOSTS = (
    "avature.net", "icims.com", "successfactors.com", "taleo.net",
    "smartrecruiters.com", "jobvite.com", "bamboohr.com",
)

_LEGAL_SUFFIX_RE = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|co|company|gmbh|plc)\b\.?", re.I
)


def _is_previously_employed_question(question: str) -> bool:
    q = question or ""
    return any(p.search(q) for p in _PREV_EMPLOYED_MARKERS)


def _company_from_question(question: str) -> str:
    """A company name volunteered directly in the question's own text, if
    any -- the strongest possible signal, since it needs no guessing at
    all. Empty string when the question only uses a generic phrase like
    "employed by our company"."""
    for pattern in (_COMPANY_IN_QUESTION_RE, _WORKED_AT_QUESTION_RE) + _MORE_COMPANY_IN_QUESTION_RES:
        m = pattern.search(question or "")
        if m:
            name = (m.group("name") or "").strip(" ,.'\"")
            if name:
                return name
    return ""


def _company_from_url(url: str) -> str:
    """Best-effort employer name from the page URL -- handles the common ATS
    shapes: Workday (company is the first host label), Greenhouse/Lever/
    Ashby/Workable (company is the first path segment), company-branded
    subdomains (Avature/iCIMS/SuccessFactors/Taleo/SmartRecruiters/Jobvite/
    BambooHR), and a plain company site (registrable-domain label)."""
    if not url:
        return ""
    try:
        parsed = urlparse(url if "//" in url else f"//{url}")
    except Exception:
        return ""
    host = (parsed.hostname or "").lower()
    if not host:
        return ""

    if _WORKDAY_HOST_MARKER in host:
        labels = host.split(".")
        if labels and labels[0] not in _GENERIC_HOST_LABELS:
            return labels[0]
        return ""

    if any(marker in host for marker in _PATH_COMPANY_HOSTS):
        parts = [p for p in (parsed.path or "").split("/") if p]
        return parts[0].lower() if parts else ""

    for marker in _SUBDOMAIN_COMPANY_HOSTS:
        if host == marker or host.endswith("." + marker):
            sub = host[: -(len(marker) + 1)] if host.endswith("." + marker) else ""
            sub_labels = [s for s in sub.split(".") if s and s not in _GENERIC_HOST_LABELS]
            return sub_labels[-1] if sub_labels else ""

    labels = [lbl for lbl in host.split(".") if lbl]
    while labels and labels[0] in _GENERIC_HOST_LABELS:
        labels = labels[1:]
    if len(labels) >= 2:
        return labels[-2]  # e.g. "viasat" from "viasat.com" / "www.viasat.com"
    return labels[0] if labels else ""


def _normalize_company_name(name: str) -> str:
    s = _LEGAL_SUFFIX_RE.sub(" ", (name or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _worked_at_company(profile: dict, employer: str) -> bool:
    target = _normalize_company_name(employer)
    if not target:
        return False
    for row in (profile or {}).get("work_history", []) or []:
        if not isinstance(row, dict):
            continue
        comp = _normalize_company_name(row.get("company", ""))
        if comp and (comp == target or comp in target or target in comp):
            return True
    return False


def previously_employed_check(
    field: FieldDescriptor, profile: dict, url: str, question: str | None = None
) -> FillResult | None:
    """Tier 5's deterministic handling of "have you previously worked
    here?" in all its long-form phrasings. Returns None when the question
    doesn't match this pattern at all.

    Public (no leading underscore): resolve.py calls this directly, ahead
    of tiers 2/3, not just from match() below. Reason: this is a screening
    QUESTION, not a structured work-history FIELD, but its long-form
    wording routinely contains the bare word "company"/"employer" (e.g.
    "...employed by our company..."), which is exactly the keyword tier 3
    (structured) uses to decide a field with no section context is a
    work-history "company" box. Left to run in tier order, tier 3 would
    claim the field first and fill it with the applicant's OWN current
    employer -- the opposite of a "have you worked HERE before" answer.
    resolve.py pre-empts that by calling this right after tier 1 (canary),
    still gated on ``answers_enabled()`` so a disabled tier 5 leaves this
    field to behave exactly as before this function existed.
    """
    question = question if question is not None else (field.label or field.name or field.placeholder or "").strip()
    if not _is_previously_employed_question(question):
        return None

    employer = _company_from_question(question) or _company_from_url(url)

    if employer:
        matched = _worked_at_company(profile, employer)
        value = "Yes" if matched else "No"
        reason = (
            f"answer bank: checked your work history against the employer "
            f"you're applying to ({employer}) — "
            + ("a match was found" if matched else "no match was found")
        )
        return FillResult(
            id=field.id,
            value=value,
            source="answer_bank",
            profile_key="answer_bank:employer_check",
            confidence=0.95,
            auto_fill=True,
            reason=reason,
        )

    # The employer could not be identified, so "No" is a likely answer, not
    # a checked fact: filled, but marked for review (draft=True renders it
    # with the drafts, not as a green profile fact).
    return FillResult(
        id=field.id,
        value="No",
        source="answer_bank",
        profile_key="answer_bank:seed",
        confidence=0.6,
        auto_fill=True,
        reason=(
            "likely No — the employer being applied to could not be identified "
            "from the page URL or the question text, so this was not checked "
            "against your work history; verify before submitting"
        ),
        draft=True,
    )


def match(
    field: FieldDescriptor,
    profile: dict,
    *,
    cache: AnswerCache | None = None,
    budget: DraftBudget | None = None,
    bank_path: str | Path | None = None,
    llm_fn: Callable[[str, str], str] | None = None,
    app_dir: str | Path | None = None,
    url: str = "",
) -> FillResult | SkipResult | None:
    """Tiers 5 (answer bank) and 6 (draft), one ``AnswerCache.answer()``
    call split on its returned ``source``.

    Returns ``None`` when tier 5 is disabled, the field has no question
    text, or anything fails -- the ladder then falls through to its own
    generic "unresolved" skip, exactly as if this tier did not exist.
    ``cache``/``budget`` let ``resolve_fields()`` share one instance across
    a whole batch (required for the draft cap to mean anything); a direct
    call without them builds private, single-call ones. ``app_dir`` selects
    which persisted extension settings apply (see settings.py); ``url`` is
    the page's URL, used only by the "previously employed here?" check
    below to identify the employer being applied to.
    """
    if not answers_enabled(app_dir):
        return None

    question = (field.label or field.name or field.placeholder or "").strip()
    if not question:
        return None

    prev_employed = previously_employed_check(field, profile, url, question)
    if prev_employed is not None:
        # profile_key is always "answer_bank:*" here, never a secret path --
        # resolve.py's own is_secret_path() guard on the returned FillResult
        # still applies regardless, same as every other tier.
        return prev_employed

    drafting = drafts_enabled(app_dir)
    if is_company_directed(question):
        # Seeds only: a stored answer to "why do you want to work here?" was
        # written for some other employer. With no bank entries to match, the
        # lookup misses and falls through to a fresh draft (drafts on) or to
        # the operator (drafts off) — never to another company's answer.
        ac = make_cache(profile, None)
    else:
        ac = cache if cache is not None else make_cache(profile, bank_path)
    ctx = _context_for(profile, field)
    choice = is_choice_field(field)

    if drafting and not choice:
        real_fn = llm_fn or _real_llm_fn
        b = budget if budget is not None else DraftBudget(app_dir=app_dir)
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
        if choice and not fits_choice(result.answer, field):
            return None  # a stored answer that is not one of this field's choices
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
