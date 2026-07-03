"""Phase 1 Task 8: canary questions must NEVER be fuzzy-served, cached,
persisted, or LLM-answered by the answer-cache — only the deterministic
resolver decides. Kills the v1 marker-channel force-hit bug that mis-served
a sponsorship/citizenship attestation.

$0 — no model; the on-miss LLM is an injected fn that raises if reached.
"""
import json

from applypilot.apply.answer_cache import AnswerCache

PROFILE = {"work_authorization": {"legally_authorized_to_work": False, "require_sponsorship": True}}


def test_canary_question_never_marker_force_hit():
    ac = AnswerCache(PROFILE)
    r = ac.answer("Are you able to work WITHOUT sponsorship?",
                  llm_fn=lambda q, c: (_ for _ in ()).throw(AssertionError("LLM must not run for canary")))
    # canary -> deterministic resolver ("No") or unresolved, never a fuzzy seed "Yes"
    assert r.answer in ("No", "no", "", None)


def test_canary_answer_not_persisted(tmp_path):
    bank = tmp_path / "bank.json"
    ac = AnswerCache(PROFILE, bank_path=bank)
    ac.answer("What is your expected salary?", llm_fn=lambda q, c: "should-not-persist")
    saved = json.loads(bank.read_text()) if bank.exists() else []
    assert all("salary" not in e.get("q", "").lower() for e in saved)


def test_poisoned_bank_entry_scrubbed_on_load(tmp_path):
    bank = tmp_path / "bank.json"
    bank.write_text(json.dumps([{"q": "Will you require sponsorship?", "a": "No"}]))  # WRONG for this profile
    ac = AnswerCache(PROFILE, bank_path=bank)
    r = ac.answer("Will you require sponsorship?", llm_fn=lambda q, c: "unused")
    assert r.answer != "No"  # not served from the poisoned bank


def test_non_canary_still_caches(tmp_path):
    bank = tmp_path / "bank.json"
    ac = AnswerCache(PROFILE, bank_path=bank)
    ac.answer("Why are you interested in this role?", llm_fn=lambda q, c: "I love the mission.")
    assert any("interested" in e.get("q", "").lower() for e in json.loads(bank.read_text()))
