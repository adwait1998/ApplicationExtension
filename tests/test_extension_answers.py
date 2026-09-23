"""Tests for tiers 5/6 of the resolution ladder (applypilot.extension.answers).

All $0, no network: every LLM path is either an injected fake llm_fn or a
genuine "no provider configured" failure (this test env sets none of
GEMINI_API_KEY/OPENAI_API_KEY/LLM_URL/LLM_PROVIDER, so _real_llm_fn's own
try/except degrades to "" without ever attempting a connection). Bank
matching always goes through a tmp-path copy — the real repo-root
answer_bank.json is read only by the one test whose entire point is
proving it is never written to, and even that test copies content into a
tmp file rather than touching the real one.
"""
from __future__ import annotations

import json

import pytest

from applypilot.extension import answers
from applypilot.extension.schema import FieldDescriptor, FillResult, SkipResult

PROFILE = {
    "personal": {"full_name": "Nida Shah", "email": "nida@example.com"},
    "work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": True},
    "experience": {
        "current_title": "Senior Product Designer",
        "target_role": "Staff Product Designer",
        "years_of_experience_total": "5",
    },
    "resume_facts": {
        "preserved_companies": ["Acme Corp", "Globex"],
        "preserved_projects": ["Design System Rebuild"],
        "real_metrics": ["reduced onboarding drop-off by 30%"],
    },
}


def _field(**kwargs) -> FieldDescriptor:
    base = dict(id="f0", selector="#x", tag="input", type="text", name="",
                autocomplete="", label="", placeholder="", required=False, options=[])
    base.update(kwargs)
    return FieldDescriptor(**base)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Both gates read the live environment on every call (no cached
    # singleton, unlike laya) -- still worth an autouse reset so a real
    # shell env (e.g. an operator's own APPLYPILOT_ANSWERS=1) can never
    # leak into a test's assumptions about the default-off state.
    monkeypatch.delenv("APPLYPILOT_ANSWERS", raising=False)
    monkeypatch.delenv("APPLYPILOT_DRAFTS", raising=False)
    monkeypatch.delenv("APPLYPILOT_MAX_DRAFTS", raising=False)
    yield


def _enable(monkeypatch, *, answers_on=True, drafts_on=False, max_drafts=None):
    if answers_on:
        monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")
    if drafts_on:
        monkeypatch.setenv("APPLYPILOT_DRAFTS", "1")
    if max_drafts is not None:
        monkeypatch.setenv("APPLYPILOT_MAX_DRAFTS", str(max_drafts))


# ---------------------------------------------------------------------------
# env gates
# ---------------------------------------------------------------------------


def test_answers_disabled_by_default():
    assert answers.answers_enabled() is False
    assert answers.drafts_enabled() is False


@pytest.mark.parametrize("val", ["", "0", "false", "False", "no", "off"])
def test_answers_disabled_for_falsy_values(monkeypatch, val):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", val)
    assert answers.answers_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "anything"])
def test_answers_enabled_for_truthy_values(monkeypatch, val):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", val)
    assert answers.answers_enabled() is True


def test_drafts_alone_without_answers_flag_is_a_noop(monkeypatch):
    # The interaction this module documents: APPLYPILOT_DRAFTS=1 with
    # APPLYPILOT_ANSWERS unset must NOT turn drafting on -- there is no
    # AnswerCache call to hang a draft off of.
    monkeypatch.setenv("APPLYPILOT_DRAFTS", "1")
    assert answers.answers_enabled() is False
    assert answers.drafts_enabled() is False


def test_drafts_require_both_flags(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")
    assert answers.drafts_enabled() is False  # answers alone isn't drafts
    monkeypatch.setenv("APPLYPILOT_DRAFTS", "1")
    assert answers.drafts_enabled() is True


# ---------------------------------------------------------------------------
# match() disabled / no-question short circuits
# ---------------------------------------------------------------------------


def test_match_returns_none_when_disabled():
    assert answers.match(_field(label="Why do you want to work here?"), PROFILE) is None


def test_match_returns_none_for_a_field_with_no_question_text(monkeypatch):
    _enable(monkeypatch)
    assert answers.match(_field(label="", name="", placeholder=""), PROFILE) is None


# ---------------------------------------------------------------------------
# tier 5: answer bank -- the real shape (paraphrase, not literal match)
# ---------------------------------------------------------------------------


def test_bank_hit_on_a_paraphrase_not_a_literal_match(monkeypatch, tmp_path):
    _enable(monkeypatch)
    bank = tmp_path / "bank.json"
    bank.write_text(json.dumps([
        {"q": "Why do you want to work at this company?", "a": "I admire the mission and want to contribute."},
    ]), encoding="utf-8")

    def _explode(q, c):
        raise AssertionError("tier 5 alone must never call the LLM")

    result = answers.match(
        _field(label="Why are you interested in this role?"),  # paraphrase, not literal
        PROFILE, bank_path=bank, llm_fn=_explode,
    )
    assert isinstance(result, FillResult)
    assert result.source == "answer_bank"
    assert result.value == "I admire the mission and want to contribute."
    assert result.auto_fill is True
    assert result.draft is False
    assert result.profile_key.startswith("answer_bank:")
    assert 0.0 <= result.confidence <= 1.0


def test_bank_hit_reports_the_matched_question_in_the_reason(monkeypatch, tmp_path):
    _enable(monkeypatch)
    bank = tmp_path / "bank.json"
    bank.write_text(json.dumps([{"q": "Why do you want to join this company?",
                                  "a": "I admire the mission and want to contribute."}]),
                     encoding="utf-8")
    result = answers.match(
        _field(label="Why are you interested in working here?"),
        PROFILE, bank_path=bank, llm_fn=lambda q, c: (_ for _ in ()).throw(AssertionError()),
    )
    assert isinstance(result, FillResult)
    assert "why" in result.reason.lower() or "join" in result.reason.lower()


def test_seed_bank_answers_a_standard_question_with_zero_bank_file(monkeypatch, tmp_path):
    # Profile-derived seeds (see answer_cache._seed_bank) work even with no
    # bank_path file on disk at all.
    _enable(monkeypatch)
    missing = tmp_path / "does_not_exist.json"
    result = answers.match(
        _field(label="Are you 18 years of age or older?"),
        PROFILE, bank_path=missing, llm_fn=lambda q, c: (_ for _ in ()).throw(AssertionError()),
    )
    assert isinstance(result, FillResult)
    assert result.source == "answer_bank"
    assert result.value == "Yes"


# ---------------------------------------------------------------------------
# tier 6: draft -- a genuine miss, with drafts on
# ---------------------------------------------------------------------------


def test_genuine_miss_produces_a_draft_when_enabled(monkeypatch, tmp_path):
    _enable(monkeypatch, drafts_on=True)
    bank = tmp_path / "bank.json"
    bank.write_text("[]", encoding="utf-8")
    calls = []

    def fake_llm(q, c):
        calls.append((q, c))
        return "I led the redesign of our onboarding flow, drawing on my product design background."

    result = answers.match(
        _field(label="Describe a time you improved a user flow."),
        PROFILE, bank_path=bank, llm_fn=fake_llm,
    )
    assert len(calls) == 1
    assert isinstance(result, FillResult)
    assert result.source == "draft"
    assert result.draft is True
    assert result.auto_fill is True
    assert result.value == "I led the redesign of our onboarding flow, drawing on my product design background."
    assert "review" in result.reason.lower()


def test_draft_context_carries_resume_facts_and_profile_shape(monkeypatch, tmp_path):
    # Non-negotiable #3: the prompt must carry resume_facts + useful
    # profile context. Assert directly on what the injected llm_fn saw.
    _enable(monkeypatch, drafts_on=True)
    bank = tmp_path / "bank.json"
    bank.write_text("[]", encoding="utf-8")
    seen = {}

    def fake_llm(q, c):
        seen["question"] = q
        seen["context"] = c
        return "Grounded answer."

    answers.match(_field(label="Tell us about a project you are proud of."),
                  PROFILE, bank_path=bank, llm_fn=fake_llm)

    ctx = seen["context"]
    assert "Acme Corp" in ctx
    assert "Globex" in ctx
    assert "Design System Rebuild" in ctx
    assert "reduced onboarding drop-off by 30%" in ctx
    assert "Senior Product Designer" in ctx
    assert "Staff Product Designer" in ctx
    assert "5" in ctx


def test_drafts_disabled_never_calls_the_llm_even_on_a_genuine_miss(monkeypatch, tmp_path):
    # Tier 5 alone (APPLYPILOT_ANSWERS=1, no APPLYPILOT_DRAFTS) must never
    # invoke any LLM, real or injected -- a bank miss just falls through.
    _enable(monkeypatch, drafts_on=False)
    bank = tmp_path / "bank.json"
    bank.write_text("[]", encoding="utf-8")

    def _explode(q, c):
        raise AssertionError("must not be called with drafts disabled")

    result = answers.match(
        _field(label="Describe your ideal team culture."),
        PROFILE, bank_path=bank, llm_fn=_explode,
    )
    assert result is None


def test_empty_llm_answer_falls_through_to_none(monkeypatch, tmp_path):
    _enable(monkeypatch, drafts_on=True)
    bank = tmp_path / "bank.json"
    bank.write_text("[]", encoding="utf-8")
    result = answers.match(
        _field(label="Anything else we should know?"),
        PROFILE, bank_path=bank, llm_fn=lambda q, c: "   ",
    )
    assert result is None


def test_llm_exception_fails_soft_to_none(monkeypatch, tmp_path):
    # Non-negotiable #4: no crash, no 500 -- an injected fn that misbehaves
    # (doesn't fail-soft itself) must still degrade to a plain None here.
    _enable(monkeypatch, drafts_on=True)
    bank = tmp_path / "bank.json"
    bank.write_text("[]", encoding="utf-8")

    def _boom(q, c):
        raise RuntimeError("network is down")

    result = answers.match(
        _field(label="What draws you to this company?"),
        PROFILE, bank_path=bank, llm_fn=_boom,
    )
    assert result is None


def test_no_llm_configured_fails_soft(monkeypatch, tmp_path):
    # The real default path, with no provider env vars set at all (true in
    # this test environment) -- _real_llm_fn's own try/except must degrade
    # to "" rather than raise, and match() must then return None.
    for var in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL", "LLM_PROVIDER", "APPLYPILOT_LLM_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    _enable(monkeypatch, drafts_on=True)
    bank = tmp_path / "bank.json"
    bank.write_text("[]", encoding="utf-8")
    result = answers.match(_field(label="Completely novel question nobody has ever asked."),
                            PROFILE, bank_path=bank)
    assert result is None


def test_match_falls_back_to_module_default_llm_fn(monkeypatch, tmp_path):
    # No llm_fn injected -> answers._real_llm_fn is used. Verified by
    # monkeypatching the module attribute itself (the production fallback
    # path resolve.py relies on) rather than passing llm_fn explicitly.
    _enable(monkeypatch, drafts_on=True)
    bank = tmp_path / "bank.json"
    bank.write_text("[]", encoding="utf-8")
    calls = []
    monkeypatch.setattr(answers, "_real_llm_fn", lambda q, c: calls.append(q) or "fallback answer")

    result = answers.match(_field(label="What makes you a strong fit?"), PROFILE, bank_path=bank)
    assert calls == ["What makes you a strong fit?"]
    assert isinstance(result, FillResult)
    assert result.value == "fallback answer"


# ---------------------------------------------------------------------------
# draft cap
# ---------------------------------------------------------------------------


def test_draft_budget_default_limit_is_five():
    assert answers.DraftBudget().limit == 5


def test_draft_budget_configurable_via_env(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_MAX_DRAFTS", "2")
    assert answers.DraftBudget().limit == 2


def test_draft_budget_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_MAX_DRAFTS", "not-a-number")
    assert answers.DraftBudget().limit == 5


def test_draft_budget_take_stops_at_the_limit():
    budget = answers.DraftBudget(limit=2)
    assert budget.take() is True
    assert budget.take() is True
    assert budget.take() is False
    assert budget.take() is False


def test_cap_enforced_across_a_shared_budget(monkeypatch, tmp_path):
    _enable(monkeypatch, drafts_on=True)
    bank = tmp_path / "bank.json"
    bank.write_text("[]", encoding="utf-8")
    cache = answers.make_cache(PROFILE, bank_path=bank)
    budget = answers.DraftBudget(limit=2)
    calls = []

    def fake_llm(q, c):
        calls.append(q)
        return f"draft for: {q}"

    questions = [
        "Describe your design process end to end.",
        "What is the hardest bug you have ever fixed.",
        "Tell me about a time you disagreed with a stakeholder.",
    ]
    results = [
        answers.match(_field(id=f"f{i}", label=q), PROFILE,
                      cache=cache, budget=budget, llm_fn=fake_llm)
        for i, q in enumerate(questions)
    ]

    assert len(calls) == 2  # the real LLM ran exactly twice, not three times
    assert isinstance(results[0], FillResult) and results[0].source == "draft"
    assert isinstance(results[1], FillResult) and results[1].source == "draft"
    assert isinstance(results[2], SkipResult)
    assert results[2].source == "draft"
    assert "cap" in results[2].reason.lower()
    assert results[2].auto_fill is False


def test_cap_of_zero_skips_every_draft_with_a_reason(monkeypatch, tmp_path):
    _enable(monkeypatch, drafts_on=True, max_drafts=0)
    bank = tmp_path / "bank.json"
    bank.write_text("[]", encoding="utf-8")
    result = answers.match(
        _field(label="Something totally novel that will never be in the bank."),
        PROFILE, bank_path=bank, llm_fn=lambda q, c: (_ for _ in ()).throw(AssertionError()),
    )
    assert isinstance(result, SkipResult)
    assert result.source == "draft"
    assert "cap" in result.reason.lower()


# ---------------------------------------------------------------------------
# non-negotiable #1: never persist an unreviewed draft
# ---------------------------------------------------------------------------


def test_bank_file_byte_identical_after_forced_llm_miss(tmp_path, monkeypatch):
    _enable(monkeypatch, drafts_on=True)
    bank = tmp_path / "answer_bank.json"
    original = [{"q": "Describe a project you led end to end.",
                 "a": "I led the redesign of our checkout flow."}]
    bank.write_text(json.dumps(original, indent=2), encoding="utf-8")
    before = bank.read_bytes()

    calls = []

    def fake_llm(q, c):
        calls.append(q)
        return "This text must never be written into the bank file."

    result = answers.match(
        _field(label="What is a completely novel question never seen before?"),
        PROFILE, bank_path=bank, llm_fn=fake_llm,
    )

    assert calls  # confirm this really was a miss that reached the LLM
    assert isinstance(result, FillResult)
    assert result.source == "draft"
    after = bank.read_bytes()
    assert after == before, "an unreviewed LLM draft was written back into the answer bank"


def test_make_cache_clears_bank_path_so_persist_is_a_noop(tmp_path):
    bank = tmp_path / "bank.json"
    bank.write_text("[]", encoding="utf-8")
    cache = answers.make_cache(PROFILE, bank_path=bank)
    assert cache.bank_path is None


def test_default_bank_path_points_at_the_repo_root_file():
    # No I/O -- just locks in *which* file production defaults to, so this
    # doesn't silently drift to config.APP_DIR's live, ever-growing bank.
    assert answers._DEFAULT_BANK_PATH.name == "answer_bank.json"
    assert answers._DEFAULT_BANK_PATH.parent.name == "auto-apply-pipeline"


# ---------------------------------------------------------------------------
# non-negotiable #2: canaries never reach these tiers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", [
    "Will you now or in the future require sponsorship to work in the United States?",
    "Are you legally authorized to work in the United States?",
    "What is your desired salary?",
])
def test_canary_shaped_question_never_produces_answer_bank_or_draft(monkeypatch, tmp_path, label):
    _enable(monkeypatch, drafts_on=True)
    bank = tmp_path / "bank.json"
    # Seed a (wrong-for-this-field) bank entry that would otherwise be a
    # tempting fuzzy hit, to prove the canary short-circuit inside
    # AnswerCache itself is what's blocking this, not just an empty bank.
    bank.write_text(json.dumps([{"q": label, "a": "definitely not a canary answer"}]),
                     encoding="utf-8")

    def _explode(q, c):
        raise AssertionError("canary question must never reach the LLM")

    result = answers.match(_field(label=label), PROFILE, bank_path=bank, llm_fn=_explode)
    assert result is None
