"""Common screening questions, answered only from the applicant's own
explicit settings (``profile.screening.*``, set in the extension's
"Common screening questions" section).

These are the repetitive questions nearly every application asks and that
no résumé answers: criminal record, background-check and drug-test
consent, non-compete, willingness to travel, how you heard about the job.
Most of them are attestations, so the rules mirror the canary tier:

* A recognised attestation NEVER falls through to the answer bank or an
  LLM draft. If the applicant has not set an answer it stays a skip that
  says where to set one.
* Only plainly-worded questions are answered. Anything phrased as a
  statement to certify, negated, or asking something the setting does not
  entail ("have you been arrested?" is not "have you been convicted?")
  is left for the human.
* A criminal-record setting of "Yes" is never auto-answered: every such
  question then depends on details (when, what, expunged) that only the
  applicant can judge.

"How did you hear about us?" is the one non-attestation here: with no
setting it returns None so the lower tiers may still answer it.
"""
from __future__ import annotations

import re

from applypilot.extension.schema import FieldDescriptor, FillResult, SkipResult

_CRIMINAL_RE = re.compile(
    r"\b(convicted|convictions?|felon\w*|misdemeanou?r\w*|criminal|"
    r"sex\s*offen\w+|pleaded guilty|pled guilty|no contest|arrest\w*|"
    r"charged with|criminal charges?|pending charges?)\b", re.I)
_BACKGROUND_RE = re.compile(r"\bbackground\s*(check|screen\w*|investigation)s?\b", re.I)
_DRUG_RE = re.compile(r"\b(drug|substance|alcohol)\s*(and alcohol\s*)?(test\w*|screen\w*)\b", re.I)
_NON_COMPETE_RE = re.compile(
    r"\b(non[-\s]?compete|non[-\s]?solicit\w*|restrictive\s+covenants?)\b", re.I)
_TRAVEL_RE = re.compile(r"\btravel\b", re.I)
_TRAVEL_QUESTION_RE = re.compile(r"\b(willing|able|open|comfortable|available)\b", re.I)
_HOW_HEARD_RE = re.compile(
    r"\bhow did you (first )?(hear|learn|find out|find)\b|\bwhere did you (hear|learn|find)\b"
    r"|\bhear about (us|this|the)\b|\bhow you heard\b|\breferral source\b|\bsource of (application|referral)\b",
    re.I)

# A question the applicant is being asked, not a statement they are asked to
# certify ("I certify that I have never been convicted...") or a follow-up
# ("If yes, please explain").
_PLAIN_QUESTION_START = re.compile(
    r"(^|[:.?]\s+|^[^A-Za-z]+)(\d+[.)]\s*)?(have|has|are|were|do|did|will|would|can|could|is)\s+you\b",
    re.I)
_NEGATION_RE = re.compile(r"\b(not|never|no)\b|n't\b", re.I)
# "(Yes/No)", "Yes or No" in a label are answer hints, not negation.
_ANSWER_HINT_RE = re.compile(r"\byes\s*(/|or)\s*no\b|\bno\s*(/|or)\s*yes\b", re.I)
# Consent to a check vs. a claim about its outcome ("can you pass ...?").
_CONSENT_RE = re.compile(r"\b(consent|willing|agree|authori[sz]e|submit to|undergo|complete)\b", re.I)
_OUTCOME_RE = re.compile(r"\b(pass|clear|results?)\b", re.I)
_STATEMENT_RE = re.compile(r"\b(i certify|i attest|i acknowledge|i understand|i agree|i confirm|"
                           r"i have read|penalty of perjury|if (yes|so))\b", re.I)
# Questions a "never convicted" setting does not entail: an arrest, a
# charge, a pending case, or a scope that includes things people commonly
# do not count as convictions.
_CRIMINAL_OUT_OF_SCOPE_RE = re.compile(
    r"\b(arrest\w*|charged|charges?|pending|indict\w*|investigation|probation|parole|"
    r"traffic|juvenile|court[-\s]?martial|military|expunge\w*|seal\w*|pardon\w*|"
    r"including|include)\b", re.I)
_PERCENT_RE = re.compile(r"(\d{1,3})\s*%")

_YES = {"yes", "y", "true"}
_NO = {"no", "n", "false"}


def _yes_no(value) -> str | None:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    v = str(value or "").strip().lower()
    if v in _YES:
        return "Yes"
    if v in _NO:
        return "No"
    return None


def family_of(question: str) -> str | None:
    q = question or ""
    # A consent question that mentions criminal history ("Do you consent to a
    # background check, including criminal history?") is about the check.
    if (_BACKGROUND_RE.search(q) or _DRUG_RE.search(q)) and _CONSENT_RE.search(q):
        return "background_check" if _BACKGROUND_RE.search(q) else "drug_test"
    if _CRIMINAL_RE.search(q):
        return "criminal"
    if _BACKGROUND_RE.search(q):
        return "background_check"
    if _DRUG_RE.search(q):
        return "drug_test"
    if _NON_COMPETE_RE.search(q):
        return "non_compete"
    if _HOW_HEARD_RE.search(q):
        return "how_heard"
    if _TRAVEL_RE.search(q) and _TRAVEL_QUESTION_RE.search(q):
        return "travel"
    return None


# Which setting answers which family.
_KEYS = {
    "criminal": "criminal_conviction",
    "background_check": "background_check_consent",
    "drug_test": "drug_test_consent",
    "non_compete": "non_compete",
    "travel": "willing_to_travel",
    "how_heard": "how_heard",
}

_LABELS = {
    "criminal": "criminal record",
    "background_check": "background check",
    "drug_test": "drug test",
    "non_compete": "non-compete",
    "travel": "travel",
    "how_heard": "how you heard about the job",
}


def _answer(family: str, question: str, setting) -> tuple[str | None, str]:
    """(value, reason-when-None)."""
    if family == "how_heard":
        text = str(setting or "").strip()
        return (text or None), "no default set"

    yn = _yes_no(setting)
    if yn is None:
        return None, "not set"
    # Judge the wording on the question itself: a trailing instruction ("...?
    # If yes, please explain.") does not turn a plain question into a
    # statement, but a label that STARTS as a statement or follow-up does.
    head = question.split("?", 1)[0]
    if not _PLAIN_QUESTION_START.search(question) or _STATEMENT_RE.search(head):
        return None, "worded as a statement or follow-up, not a plain question"
    # Negation is judged on the question itself, minus "(Yes/No)" hints and
    # the legal phrase "no contest": the stored answer is only the right
    # answer to the positively-worded question.
    wording = _ANSWER_HINT_RE.sub(" ", question)
    wording = re.sub(r"\bno contest\b", " ", wording, flags=re.I)
    if family == "criminal":
        if yn != "No":
            return None, "details matter for this one — answer it yourself"
        if _CRIMINAL_OUT_OF_SCOPE_RE.search(question):
            return None, "asks about more than convictions (arrests, charges, traffic, ...)"
        if _NEGATION_RE.search(wording):
            return None, "negated wording — answer this yourself"
        return "No", ""
    if _NEGATION_RE.search(wording):
        return None, "negated wording — answer this yourself"
    if family in ("background_check", "drug_test") and _OUTCOME_RE.search(question) \
            and not _CONSENT_RE.search(question):
        return None, "asks about the outcome, not your consent — answer this yourself"
    if family == "travel":
        pcts = [int(p) for p in _PERCENT_RE.findall(question)]
        if yn == "Yes" and any(p > 50 for p in pcts):
            return None, "asks about travel over 50% — answer this yourself"
    return yn, ""


def match(field: FieldDescriptor, profile: dict) -> FillResult | SkipResult | None:
    question = (field.label or field.name or field.placeholder or "").strip()
    family = family_of(question)
    if family is None:
        return None
    key = _KEYS[family]
    settings = (profile or {}).get("screening") or {}
    value, why = _answer(family, question, settings.get(key) if isinstance(settings, dict) else None)
    if value:
        return FillResult(
            id=field.id,
            value=value,
            source="screening",
            profile_key=f"screening.{key}",
            confidence=1.0,
            auto_fill=True,
            reason=f"your '{_LABELS[family]}' setting",
        )
    if family == "how_heard":
        return None  # not an attestation: lower tiers may still answer it
    return SkipResult(
        id=field.id,
        source="screening",
        reason=(f"{_LABELS[family]} question — {why}. "
                "Set a default in Settings → Common screening questions."
                if why == "not set" else f"{_LABELS[family]} question — {why}"),
        auto_fill=False,
    )
