"""Reliability-v2 Phase D: semantic answer-cache for free-text screening Qs.

Screening questions repeat across companies with minor wording drift
("Why do you want to work here?" / "Why are you interested in this
role?"). Embedding them and serving from a profile-seeded + learned Q&A
bank means the LLM is called ONCE for a genuinely-new question, then
never again — LLM-call rate decays toward zero (the Stagehand
cost→0-on-repeats insight, applied to the language layer).

Dependency-free + offline: a deterministic hashed bag-of-words vector
(synonym-canonicalized) with cosine NN. The Embedder is a seam — a real
sentence-embedding model can be slotted later without touching callers.
The on-miss LLM is injectable so this is unit-tested at $0.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from applypilot.apply.canary import is_canary, resolve_canary

_DIM = 256
_STOP = {
    "the", "a", "an", "to", "of", "for", "in", "on", "at", "is", "are", "do",
    "you", "your", "we", "our", "this", "that", "with", "and", "or", "as",
    "be", "have", "has", "will", "would", "can", "could", "please", "any",
    "if", "it", "us", "by", "from", "what", "which", "how",
}
# Collapse recruiter-phrasing variants so paraphrases land on the same entry.
_CANON = [
    (r"\b(authoriz\w+|eligible|legally able)\b", "workauth"),
    (r"\b(sponsor\w*|visa)\b", "sponsorship"),
    (r"\b(relocat\w+|willing to move)\b", "relocate"),
    (r"\b(why .*interested|why .*want|why .*join|why .*role|why .*company)\b", "whyinterested"),
    (r"\b(years? of experience|how (long|many years)|experience do)\b", "yearsexp"),
    (r"\b(salary|compensation|pay expectation|expected (pay|comp))\b", "salary"),
    (r"\b(start date|when can you start|availab\w+|notice period)\b", "availability"),
    (r"\b(18 (years )?(or older|of age)|are you (over|at least) 18)\b", "age18"),
    (r"\b(previously (worked|employed)|worked (here|at|for))\b", "workedherebefore"),
    (r"\b(gender|race|ethnicit\w+|veteran|disabilit\w+)\b", "eeo"),
]


def _normalize(text: str) -> str:
    t = (text or "").lower()
    for pat, rep in _CANON:
        t = re.sub(pat, rep, t)
    return t


_MARKER_SET = {rep for _, rep in _CANON}


def _markers(text: str) -> frozenset[str]:
    """Canonical intent tokens present after normalization. Formulaic
    screening Qs collapse to one of these ('workauth', 'whyinterested',
    ...) regardless of filler — a far stronger match signal for short
    recruiter questions than raw bag-of-words cosine."""
    norm = _normalize(text)
    return frozenset(m for m in _MARKER_SET if re.search(r"\b" + m + r"\b", norm))


def _tokens(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+", _normalize(text))
    return [w for w in toks if w not in _STOP and len(w) > 1]


def embed(text: str) -> list[float]:
    """Deterministic L2-normalized hashed bag-of-words vector (offline)."""
    vec = [0.0] * _DIM
    for tok in _tokens(text):
        h = 0
        for ch in tok:
            h = (h * 131 + ord(ch)) & 0xFFFFFFFF
        vec[h % _DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # both L2-normalized


@dataclass
class AnswerResult:
    answer: str
    source: str          # "seed" | "cache" | "llm"
    similarity: float
    llm_called: bool
    matched_q: str | None = None


def _seed_bank(profile: dict) -> list[dict]:
    """Canonical screening Q→A derived from the profile. First-ever ask of
    a standard question is a hit (zero LLM) without any prior run."""
    p = profile or {}
    wa = p.get("work_authorization", {}) or {}
    exp = p.get("experience", {}) or {}
    auth = "Yes" if str(wa.get("legally_authorized_to_work", "")).lower() in ("true", "yes", "1") else "No"
    spon = "Yes" if str(wa.get("require_sponsorship", "")).lower() in ("true", "yes", "1") else "No"
    yrs = str(exp.get("years_of_experience_total", "") or exp.get("years", "") or "")
    seeds = [
        ("Are you legally authorized to work in the US?", auth),
        ("Will you now or in the future require sponsorship?", spon),
        ("Are you 18 years of age or older?", "Yes"),
        ("Have you previously worked here?", "No"),
        ("Are you willing to relocate?", "Open to remote; relocation negotiable for the right role."),
    ]
    if yrs:
        seeds.append(("How many years of experience do you have?", f"{yrs} years."))
    return [{"q": q, "a": a, "source": "seed"} for q, a in seeds]


class AnswerCache:
    def __init__(self, profile: dict, bank_path: str | Path | None = None,
                 *, threshold: float = 0.70) -> None:
        self.profile = profile or {}
        self.threshold = threshold
        self.bank_path = Path(bank_path) if bank_path else None
        self._entries: list[dict] = list(_seed_bank(self.profile))
        if self.bank_path and self.bank_path.exists():
            try:
                for e in json.loads(self.bank_path.read_text(encoding="utf-8")):
                    if isinstance(e, dict) and e.get("q") and e.get("a") is not None:
                        self._entries.append({"q": e["q"], "a": e["a"], "source": "cache"})
            except Exception:
                pass
        # Scrub any canary entries (seed or persisted) BEFORE embeddings are
        # built: a canary answer must only ever come from resolve_canary, never
        # a fuzzy bank hit — this de-poisons a bank written by an older build.
        self._entries = [e for e in self._entries if not is_canary(e.get("q", ""))]
        for e in self._entries:
            e["_vec"] = embed(e["q"])
            e["_mk"] = _markers(e["q"])

    def _nearest(self, q: str) -> tuple[dict | None, float]:
        qv = embed(q)
        qmk = _markers(q)
        best, best_s = None, -1.0
        mk_best, mk_best_s = None, -1.0
        for e in self._entries:
            s = _cosine(qv, e["_vec"])
            if s > best_s:
                best, best_s = e, s
            # Intent-key channel: same canonical marker set + minimal lexical
            # agreement → strong hit even when filler words differ
            # ("eligible to work in the US" ≈ "legally authorized to work").
            # `not is_canary(q)`: belt-and-suspenders — the force-hit must NEVER
            # fire for a canary (already blocked at answer()'s top guard, but
            # _nearest could be called directly).
            if not is_canary(q) and qmk and e.get("_mk") == qmk and s >= 0.30 and s > mk_best_s:
                mk_best, mk_best_s = e, s
        if mk_best is not None:
            # Report a confident similarity so it clears the threshold gate.
            return mk_best, max(mk_best_s, self.threshold)
        return best, best_s

    def _persist(self, q: str, a: str) -> None:
        if not self.bank_path:
            return
        try:
            existing = []
            if self.bank_path.exists():
                existing = json.loads(self.bank_path.read_text(encoding="utf-8"))
            existing.append({"q": q, "a": a})
            self.bank_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.bank_path.with_suffix(self.bank_path.suffix + ".tmp")
            tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=0),
                           encoding="utf-8")
            tmp.replace(self.bank_path)
        except Exception:
            pass

    def answer(self, question: str, *, context: str = "",
               llm_fn: Callable[[str, str], str] | None = None) -> AnswerResult:
        """Return an answer for `question`. Cache/seed hit → 0 LLM. Miss →
        one llm_fn call, then persisted so the next ask is a hit."""
        if is_canary(question):
            det = resolve_canary(question, self.profile)
            # canary: deterministic profile answer only — never cache, never
            # persist, never LLM. None -> "" so the caller keeps it UNRESOLVED.
            return AnswerResult(det if det is not None else "",
                                "profile" if det else "unresolved",
                                1.0 if det else 0.0, False, None)
        entry, sim = self._nearest(question)
        if entry is not None and sim >= self.threshold:
            return AnswerResult(entry["a"], entry.get("source", "cache"),
                                round(sim, 3), False, entry["q"])
        # miss → LLM
        fn = llm_fn or _default_llm_fn
        ans = (fn(question, context) or "").strip()
        rec = {"q": question, "a": ans, "source": "cache",
               "_vec": embed(question), "_mk": _markers(question)}
        self._entries.append(rec)
        self._persist(question, ans)
        return AnswerResult(ans, "llm", round(max(sim, 0.0), 3), True, None)


def _default_llm_fn(question: str, context: str) -> str:
    """Real on-miss path — uses the existing provider-flexible llm client."""
    from applypilot.llm import get_client
    msgs = [
        {"role": "system", "content":
         "Answer this job-application screening question concisely (1-3 "
         "sentences), truthfully, in the applicant's voice. Output ONLY the "
         "answer text."},
        {"role": "user", "content": f"{context}\n\nQUESTION: {question}".strip()},
    ]
    try:
        return get_client().chat(msgs, max_tokens=256, temperature=0.3)
    except Exception:
        return ""
