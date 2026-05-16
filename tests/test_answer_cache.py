"""Reliability-v2 Phase D done-when: semantic answer-cache.

- profile-seeded standard question → 0 LLM
- novel question → exactly 1 LLM call, then persisted
- same/paraphrased question again → 0 LLM (cache hit)
- distinct question → new LLM call
- bank persists across instances

$0 — the on-miss LLM is an injected counter, no model.
"""
from __future__ import annotations

import pytest

from applypilot.apply.answer_cache import AnswerCache, embed, _cosine


PROFILE = {
    "personal": {"full_name": "Nida Shah"},
    "work_authorization": {"legally_authorized_to_work": True,
                           "require_sponsorship": True},
    "experience": {"years_of_experience_total": "5"},
}


class _LLM:
    def __init__(self):
        self.calls = []

    def __call__(self, q, ctx):
        self.calls.append(q)
        return f"LLM answer #{len(self.calls)} for: {q[:30]}"


def test_seeded_question_zero_llm():
    llm = _LLM()
    c = AnswerCache(PROFILE)
    r = c.answer("Are you legally authorized to work in the US?", llm_fn=llm)
    assert r.llm_called is False
    assert r.source == "seed"
    assert r.answer == "Yes"
    assert llm.calls == []


def test_seeded_paraphrase_still_hits():
    llm = _LLM()
    c = AnswerCache(PROFILE)
    # Different wording, same intent → canonicalization should still hit.
    r = c.answer("Are you eligible to work in the United States?", llm_fn=llm)
    assert r.llm_called is False
    assert r.answer == "Yes"
    r2 = c.answer("Will you require visa sponsorship now or in the future?", llm_fn=llm)
    assert r2.llm_called is False
    assert r2.answer == "Yes"
    assert llm.calls == []


def test_novel_question_calls_llm_once_then_cached():
    llm = _LLM()
    c = AnswerCache(PROFILE)
    q = "Describe a design system you built end to end."
    r1 = c.answer(q, llm_fn=llm)
    assert r1.llm_called is True
    assert r1.source == "llm"
    assert len(llm.calls) == 1
    # Ask again → served from cache, no second LLM call.
    r2 = c.answer(q, llm_fn=llm)
    assert r2.llm_called is False
    assert r2.answer == r1.answer
    assert len(llm.calls) == 1


def test_paraphrase_of_learned_answer_hits_cache():
    llm = _LLM()
    c = AnswerCache(PROFILE)
    c.answer("Why do you want to work at this company?", llm_fn=llm)
    assert len(llm.calls) == 1
    # Near-paraphrase → canonicalized to the same key → cache hit.
    r = c.answer("Why are you interested in this role?", llm_fn=llm)
    assert r.llm_called is False
    assert len(llm.calls) == 1


def test_distinct_question_triggers_new_llm_call():
    llm = _LLM()
    c = AnswerCache(PROFILE)
    c.answer("Describe your proudest shipped product.", llm_fn=llm)
    c.answer("What accessibility frameworks have you used?", llm_fn=llm)
    assert len(llm.calls) == 2  # genuinely different → not a false cache hit


def test_bank_persists_across_instances(tmp_path):
    bank = tmp_path / "answer_bank.json"
    llm = _LLM()
    c1 = AnswerCache(PROFILE, bank_path=bank)
    q = "What is your approach to design critique?"
    a1 = c1.answer(q, llm_fn=llm).answer
    assert len(llm.calls) == 1
    assert bank.exists()
    # Fresh instance (new process simulation) → reloads the persisted answer.
    llm2 = _LLM()
    c2 = AnswerCache(PROFILE, bank_path=bank)
    r = c2.answer(q, llm_fn=llm2)
    assert r.llm_called is False
    assert r.answer == a1
    assert llm2.calls == []


def test_embed_is_deterministic_and_normalized():
    v1, v2 = embed("Why do you want this role?"), embed("Why do you want this role?")
    assert v1 == v2
    # L2-normalized → self-cosine ≈ 1
    assert abs(_cosine(v1, v1) - 1.0) < 1e-9
    # unrelated short questions: low similarity
    assert _cosine(embed("salary expectations?"),
                   embed("describe a hard bug you fixed")) < 0.5
