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


def test_answers_enabled_by_default():
    # Tier 5 (answer bank) defaults ON -- its hits are model-free (a
    # profile-derived seed or the operator's own past answer), so there is
    # no reason to gate them behind an opt-in.
    assert answers.answers_enabled() is True


def test_drafts_disabled_by_default():
    # Tier 6 (draft) stays opt-in -- it puts model-written text under a
    # real person's name.
    assert answers.drafts_enabled() is False


def test_answers_can_be_explicitly_disabled_via_env(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "0")
    assert answers.answers_enabled() is False
    assert answers.drafts_enabled() is False  # drafts still requires answers


@pytest.mark.parametrize("val", ["", "0", "false", "False", "no", "off"])
def test_answers_disabled_for_falsy_values(monkeypatch, val):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", val)
    assert answers.answers_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "anything"])
def test_answers_enabled_for_truthy_values(monkeypatch, val):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", val)
    assert answers.answers_enabled() is True


def test_drafts_alone_now_works_since_answers_defaults_on(monkeypatch):
    # Previously (when tier 5 defaulted OFF) this was a deliberate no-op:
    # there was no AnswerCache call to hang a draft off of. Now that tier 5
    # itself defaults ON, setting only APPLYPILOT_DRAFTS=1 is enough.
    monkeypatch.setenv("APPLYPILOT_DRAFTS", "1")
    assert answers.answers_enabled() is True
    assert answers.drafts_enabled() is True


def test_drafts_require_both_flags(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")
    assert answers.drafts_enabled() is False  # answers alone isn't drafts
    monkeypatch.setenv("APPLYPILOT_DRAFTS", "1")
    assert answers.drafts_enabled() is True


# ---------------------------------------------------------------------------
# match() disabled / no-question short circuits
# ---------------------------------------------------------------------------


def test_match_returns_none_when_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "0")
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
    # Company-NEUTRAL on purpose. This used "Why do you want to work at this
    # company?" — but a real answer to that names the employer, and reusing it
    # across companies is the bug is_company_directed() now blocks.
    bank.write_text(json.dumps([
        {"q": "When can you start?", "a": "Two weeks after an offer."},
    ]), encoding="utf-8")

    def _explode(q, c):
        raise AssertionError("tier 5 alone must never call the LLM")

    result = answers.match(
        _field(label="What is your earliest available start date?"),  # paraphrase, not literal
        PROFILE, bank_path=bank, llm_fn=_explode,
    )
    assert isinstance(result, FillResult)
    assert result.source == "answer_bank"
    assert result.value == "Two weeks after an offer."
    assert result.auto_fill is True
    assert result.draft is False
    assert result.profile_key.startswith("answer_bank:")
    assert 0.0 <= result.confidence <= 1.0


def test_bank_hit_reports_the_matched_question_in_the_reason(monkeypatch, tmp_path):
    _enable(monkeypatch)
    bank = tmp_path / "bank.json"
    # Company-NEUTRAL on purpose. This used "Why do you want to work at this
    # company?" — but a real answer to that names the employer, and reusing it
    # across companies is the bug is_company_directed() now blocks.
    bank.write_text(json.dumps([{"q": "When can you start?",
                                  "a": "Two weeks after an offer."}]),
                     encoding="utf-8")
    result = answers.match(
        _field(label="What is your earliest available start date?"),
        PROFILE, bank_path=bank, llm_fn=lambda q, c: (_ for _ in ()).throw(AssertionError()),
    )
    assert isinstance(result, FillResult)
    assert "start" in result.reason.lower()


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
    # to "" rather than raise, and match() must then return None. Also
    # forces "no Claude CLI either" so this asserts genuine no-LLM-at-all
    # behaviour regardless of whether this machine happens to have the
    # Claude Code CLI installed (llm_util's fallback -- see bug 3 tests
    # below -- would otherwise pick it up here).
    for var in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL", "LLM_PROVIDER", "APPLYPILOT_LLM_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: None)
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


# ---------------------------------------------------------------------------
# Grounding enforcement: the system prompt TELLS the model to stay inside
# resume_facts; these assert that we also CHECK. Without this, the only thing
# between a fabricated employer and a real application is the operator
# noticing a blue badge.
# ---------------------------------------------------------------------------

def _drafting_env(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")
    monkeypatch.setenv("APPLYPILOT_DRAFTS", "1")


_GROUNDED_PROFILE = {
    "resume_facts": {"preserved_companies": ["Acme Corp"],
                     "preserved_school": "Arizona State University"},
    "experience": {"current_job_title": "Product Designer"},
}


def _grounding_field(label="Describe a project you are proud of", fid="g1"):
    from applypilot.extension.schema import FieldDescriptor
    return FieldDescriptor(
        id=fid, selector="#g", tag="textarea", type="textarea", name="",
        autocomplete="", label=label, placeholder="", required=False, options=[])


def test_draft_claiming_an_invented_employer_is_refused(tmp_path, monkeypatch):
    _drafting_env(monkeypatch)
    bank = tmp_path / "bank.json"
    bank.write_text("[]", encoding="utf-8")
    out = answers.match(
        _grounding_field(), _GROUNDED_PROFILE, bank_path=bank,
        llm_fn=lambda q, c: "I worked at Netflix on their recommendations team.")
    assert isinstance(out, SkipResult)
    assert out.source == "draft"
    assert "Netflix" in out.reason
    assert out.auto_fill is False


def test_draft_within_the_real_history_is_kept(tmp_path, monkeypatch):
    _drafting_env(monkeypatch)
    bank = tmp_path / "bank.json"
    bank.write_text("[]", encoding="utf-8")
    out = answers.match(
        _grounding_field(), _GROUNDED_PROFILE, bank_path=bank,
        llm_fn=lambda q, c: "At Acme Corp I rebuilt the onboarding flow.")
    assert isinstance(out, FillResult)
    assert out.draft is True
    assert out.source == "draft"


def test_grounding_never_blocks_an_answer_bank_hit(tmp_path, monkeypatch):
    """Tier 5 answers are the operator's OWN past words — they are ground
    truth by definition and must not be second-guessed by the draft check."""
    _drafting_env(monkeypatch)
    bank = tmp_path / "bank.json"
    bank.write_text(json.dumps([{
        "q": "Describe a project you are proud of",
        "a": "At Netflix I rebuilt the recommendations UI.",
    }]), encoding="utf-8")
    out = answers.match(_grounding_field(), _GROUNDED_PROFILE, bank_path=bank,
                        llm_fn=lambda q, c: "should not be called")
    assert isinstance(out, FillResult)
    assert out.source == "answer_bank"
    assert out.draft is False


# ---------------------------------------------------------------------------
# "Have you previously worked here?" -- the real Viasat wording, plus real
# curated-bank phrasings (Fanatics/Affirm) the persisted answer_bank.json
# already carries. Verifies the employer is identified correctly (question
# text first, then URL) and that the documented fallback fires -- and is
# labelled as a fallback -- when it can't be.
# ---------------------------------------------------------------------------

VIASAT_QUESTION = (
    "Have you previously been employed by our company or any of "
    "its subsidiaries or affiliates?"
)
FANATICS_QUESTION = (
    "Are you now, or have you ever been employed by Fanatics, Inc. "
    "or any of its affiliates or subsidiaries?"
)
AFFIRM_QUESTION = "Have you previously been employed at Affirm for any length of time?"


@pytest.mark.parametrize("question", [VIASAT_QUESTION, FANATICS_QUESTION, AFFIRM_QUESTION,
                                       "Have you previously worked here?", "Are you a former employee?",
                                       "Have you worked for us before?"])
def test_is_previously_employed_question_matches_real_wordings(question):
    assert answers._is_previously_employed_question(question) is True


def test_is_previously_employed_question_does_not_match_unrelated_questions():
    assert answers._is_previously_employed_question("Why do you want to work here?") is False
    assert answers._is_previously_employed_question("Are you 18 years of age or older?") is False


def test_company_from_question_extracts_named_employer():
    assert answers._company_from_question(FANATICS_QUESTION) == "Fanatics"
    assert answers._company_from_question(AFFIRM_QUESTION) == "Affirm"
    # The generic Viasat phrasing names no company in the question itself.
    assert answers._company_from_question(VIASAT_QUESTION) == ""


@pytest.mark.parametrize("url", [
    "https://viasat.wd1.myworkdayjobs.com/en-US/Viasat_Careers/job/Remote/Product-Designer_R12345",
    "https://www.viasat.com/careers/apply/12345",
    "https://boards.greenhouse.io/viasat/jobs/6789",
    "https://jobs.lever.co/viasat/abc-123",
    "https://viasat.avature.net/careers/JobDetail/Product-Designer/12345",
])
def test_company_from_url_identifies_viasat_across_ats_shapes(url):
    assert answers._company_from_url(url) == "viasat"


def test_company_from_url_empty_for_no_url():
    assert answers._company_from_url("") == ""


def test_previously_employed_yes_when_company_in_work_history():
    profile = {"work_history": [{"title": "Designer", "company": "Viasat Inc."}]}
    out = answers.previously_employed_check(
        _field(id="f0", label=VIASAT_QUESTION), profile,
        "https://viasat.wd1.myworkdayjobs.com/job/1", VIASAT_QUESTION,
    )
    assert isinstance(out, FillResult)
    assert out.value == "Yes"
    assert out.source == "answer_bank"
    assert out.profile_key == "answer_bank:employer_check"
    assert out.auto_fill is True


def test_previously_employed_no_when_company_not_in_work_history():
    profile = {"work_history": [{"title": "Designer", "company": "Acme Corp"}]}
    out = answers.previously_employed_check(
        _field(id="f0", label=VIASAT_QUESTION), profile,
        "https://viasat.wd1.myworkdayjobs.com/job/1", VIASAT_QUESTION,
    )
    assert isinstance(out, FillResult)
    assert out.value == "No"
    assert out.source == "answer_bank"


def test_previously_employed_falls_back_to_seed_default_when_employer_unknown():
    out = answers.previously_employed_check(
        _field(id="f0", label=VIASAT_QUESTION), PROFILE, "", VIASAT_QUESTION,
    )
    assert isinstance(out, FillResult)
    assert out.value == "No"
    assert out.source == "answer_bank"
    assert out.profile_key == "answer_bank:seed"
    # The fallback is documented in the reason, per the task's requirement.
    assert "could not be identified" in out.reason


def test_previously_employed_company_named_in_question_wins_over_url():
    # FANATICS_QUESTION names the employer directly -- that must be used
    # even if the URL (deliberately mismatched here) suggests otherwise.
    profile = {"work_history": [{"company": "Fanatics"}]}
    out = answers.previously_employed_check(
        _field(id="f0", label=FANATICS_QUESTION), profile,
        "https://careers.someunrelatedsite.com/job/1", FANATICS_QUESTION,
    )
    assert out.value == "Yes"


def test_match_routes_previously_employed_through_the_deterministic_check(monkeypatch, tmp_path):
    # End to end through match() (not the check function directly): tier 5
    # enabled, a bank/cache seeded with a WRONG-for-this-company answer that
    # would otherwise be a tempting fuzzy hit -- proves the deterministic
    # check runs before, and instead of, the fuzzy AnswerCache lookup.
    _enable(monkeypatch)
    bank = tmp_path / "bank.json"
    bank.write_text(json.dumps([{"q": FANATICS_QUESTION,
                                  "a": "No, I have not been employed by Fanatics, Inc."}]),
                     encoding="utf-8")
    profile = {**PROFILE, "work_history": [{"company": "Viasat"}]}
    result = answers.match(
        _field(label=VIASAT_QUESTION), profile, bank_path=bank,
        url="https://viasat.wd1.myworkdayjobs.com/job/1",
        llm_fn=lambda q, c: (_ for _ in ()).throw(AssertionError("must not reach the LLM")),
    )
    assert isinstance(result, FillResult)
    assert result.value == "Yes"
    assert result.profile_key == "answer_bank:employer_check"


def test_canary_shaped_previously_employed_style_question_is_unaffected():
    # Sanity: the new check's markers must never overlap canary's own.
    from applypilot.apply import canary
    assert canary.is_canary(VIASAT_QUESTION) is False


# ---------------------------------------------------------------------------
# Bug 3: when no LLM provider env var is set but the Claude Code CLI is
# installed, drafts should use it automatically rather than requiring the
# operator to export an environment variable. Both find_claude_binary()
# and the CLI subprocess itself are monkeypatched so this is deterministic
# and $0/offline regardless of what is actually installed on the machine
# running the suite.
# ---------------------------------------------------------------------------


def test_real_llm_fn_falls_back_to_claude_cli_when_no_provider_configured(monkeypatch, tmp_path):
    for var in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL", "LLM_PROVIDER", "APPLYPILOT_LLM_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: "/fake/claude.cmd")

    calls = []

    class _FakeClaudeClient:
        def __init__(self, model):
            calls.append(model)

        def chat(self, messages, **kwargs):
            return "drafted via claude cli"

    monkeypatch.setattr("applypilot.llm.ClaudeCodeClient", _FakeClaudeClient)

    result = answers._real_llm_fn("Tell us about yourself.", "context")
    assert result == "drafted via claude cli"
    assert calls == ["sonnet"]


def test_real_llm_fn_prefers_an_explicit_provider_over_claude_cli(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.delenv("LLM_URL", raising=False)

    class _FakeGeminiClient:
        def chat(self, messages, **kwargs):
            return "drafted via gemini"

    monkeypatch.setattr("applypilot.llm.get_client", lambda: _FakeGeminiClient())

    def _boom():
        raise AssertionError("must not fall back to Claude CLI when a provider is configured")

    monkeypatch.setattr("applypilot.config.find_claude_binary", _boom)

    result = answers._real_llm_fn("Tell us about yourself.", "context")
    assert result == "drafted via gemini"


def test_real_llm_fn_fails_soft_when_neither_provider_nor_cli_available(monkeypatch):
    for var in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL", "LLM_PROVIDER", "APPLYPILOT_LLM_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: None)

    result = answers._real_llm_fn("Tell us about yourself.", "context")
    assert result == ""


# ---------------------------------------------------------------------------
# Found end-to-end the moment the answer bank was switched on by default: for
# an application to one company it filled "Why do you want to work here?" with
# "I'm drawn to Discord's…" and "How did you hear about us?" with "I've been
# following Fanatics'…" — real answers written for OTHER employers. And the
# default bank was one person's file, so anyone else got her answers too.
# ---------------------------------------------------------------------------

_OTHER_CO_BANK = [
    {"q": "Why do you want to work here?",
     "a": "I'm drawn to Discord's innovative approach to community."},
    {"q": "How did you hear about us?",
     "a": "I've been following Fanatics' growth for years."},
    {"q": "Do you want to tell something to our recruiters that can make a difference?",
     "a": "Stripe's mission deeply resonates with me."},
    {"q": "What is your notice period?", "a": "Two weeks."},
]


def _bank(tmp_path, entries=_OTHER_CO_BANK):
    p = tmp_path / "answer_bank.json"
    p.write_text(json.dumps(entries), encoding="utf-8")
    return p


def _q(label, fid="q"):
    return FieldDescriptor(id=fid, selector="#q", tag="textarea", type="textarea", name="",
                           autocomplete="", label=label, placeholder="", required=False,
                           options=[])


@pytest.mark.parametrize("question", [
    "Why do you want to work here?",
    "How did you hear about us?",
    "Do you want to tell something to our recruiters that can make a difference?",
    "Why are you interested in joining our team?",
    "What excites you about this role?",
])
def test_company_directed_questions_never_reuse_another_companys_answer(tmp_path, monkeypatch, question):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")
    monkeypatch.delenv("APPLYPILOT_DRAFTS", raising=False)   # drafts off
    out = answers.match(_q(question), {"personal": {}}, bank_path=_bank(tmp_path))
    if isinstance(out, FillResult):
        for co in ("Discord", "Fanatics", "Stripe"):
            assert co not in out.value, f"reused {co}'s answer for {question!r}"
    assert out is None or not isinstance(out, FillResult)


def test_company_directed_question_goes_to_a_fresh_draft_when_drafts_are_on(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")
    monkeypatch.setenv("APPLYPILOT_DRAFTS", "1")
    out = answers.match(_q("Why do you want to work here?"), {"personal": {}},
                        bank_path=_bank(tmp_path),
                        llm_fn=lambda q, c: "I want to build accessible products.")
    assert isinstance(out, FillResult)
    assert out.source == "draft" and out.draft is True
    assert "Discord" not in out.value


def test_generic_questions_still_reuse_the_bank(tmp_path, monkeypatch):
    """The guard must not neuter the bank: company-neutral answers are exactly
    what it is for."""
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")
    out = answers.match(_q("What is your notice period?"), {"personal": {}},
                        bank_path=_bank(tmp_path))
    assert isinstance(out, FillResult) and out.value == "Two weeks."


def test_no_bank_path_means_seeds_only_never_another_persons_file(monkeypatch):
    """With no bank supplied, nothing from any file may appear."""
    monkeypatch.setenv("APPLYPILOT_ANSWERS", "1")
    cache = answers.make_cache({"personal": {}}, None)
    assert all(e.get("source") == "seed" for e in cache._entries)


def test_missing_bank_file_is_seeds_only(tmp_path):
    cache = answers.make_cache({"personal": {}}, tmp_path / "does-not-exist.json")
    assert all(e.get("source") == "seed" for e in cache._entries)
