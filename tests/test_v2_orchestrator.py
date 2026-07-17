"""run_form_compiler orchestrator (Task 9). Fully injected: no Chrome, no
network. Every stage is a fake so the orchestrator's WIRING + fail-open/park
control flow is under test, not the browser."""
from applypilot import database as db
from applypilot.apply.v2 import orchestrator as orch
from applypilot.apply.v2 import ir


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


PROFILE = {"personal": {"full_name": "Nida Shah", "email": "nida@example.com",
                        "phone": "4081234567", "city": "San Jose"},
           "work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": True}}


def _schema():
    fields = [
        ir.Field("fn", (), "First name", "First name", "first_name", ir.Widget("text"), ir.LAZY, True),
        ir.Field("em", (), "Email", "Email", "email", ir.Widget("text"), ir.LAZY, True),
    ]
    return ir.FormSchema("greenhouse", "acme", "https://boards.greenhouse.io/acme/jobs/1",
                         [ir.Step(0, fields, advance_control={"role": "button", "name": "Submit"}, terminal=True)])


def test_orchestrator_happy_path_returns_v2_tier(tmp_path):
    conn = _conn(tmp_path)
    fakes = orch.Stages(
        parse=lambda page, company, url: _schema(),
        resolve=lambda schema, profile, conn: orch._passthrough_plan(schema, profile),
        run_oracle=lambda plan, schema, operator: plan,      # nothing needs oracle
        execute=lambda page, schema, plan, conn: orch.ExecStub(ready_to_submit=True,
                                                               committed_keys=["first_name", "email"]),
        submit=lambda page, schema: True,
        verify=lambda evidence, conn, dom_signals: orch.VerifyStub(verified=True, tier=1),
    )
    status, duration_ms, prefill = orch.run_form_compiler(
        job={"url": "https://boards.greenhouse.io/acme/jobs/1",
             "application_url": "https://boards.greenhouse.io/acme/jobs/1"},
        page=object(), profile=PROFILE, conn=conn, company="acme",
        operator=None, stages=fakes)
    assert status == "applied"
    assert prefill["tier_used"] == "v2_greenhouse"
    assert prefill["fields_filled"] == ["first_name", "email"]
    assert isinstance(duration_ms, int)


def test_orchestrator_parse_failure_fails_open(tmp_path):
    conn = _conn(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("DOM churn: cannot parse")

    fakes = orch.Stages(parse=_boom)
    status, duration_ms, prefill = orch.run_form_compiler(
        job={"url": "u", "application_url": "u"}, page=object(), profile=PROFILE,
        conn=conn, company="acme", operator=None, stages=fakes)
    assert status == orch.FALLBACK_SENTINEL          # -> dispatch runs legacy (invariant 2)
    assert prefill is None or prefill.get("tier_used") == "v2_greenhouse_fallback"
    assert isinstance(duration_ms, int)


def test_orchestrator_resolve_failure_fails_open(tmp_path):
    conn = _conn(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("resolver blew up")

    fakes = orch.Stages(
        parse=lambda page, company, url: _schema(),
        resolve=_boom,
    )
    status, _, prefill = orch.run_form_compiler(
        job={"url": "u", "application_url": "u"}, page=object(), profile=PROFILE,
        conn=conn, company="acme", operator=None, stages=fakes)
    assert status == orch.FALLBACK_SENTINEL          # pre-submit crash -> legacy
    assert prefill is None


def test_orchestrator_parks_when_not_ready(tmp_path):
    conn = _conn(tmp_path)
    fakes = orch.Stages(
        parse=lambda page, company, url: _schema(),
        resolve=lambda schema, profile, conn: orch._passthrough_plan(schema, profile),
        run_oracle=lambda plan, schema, operator: plan,
        execute=lambda page, schema, plan, conn: orch.ExecStub(ready_to_submit=False,
                                                               missing_required=["First name"]),
    )
    status, _, prefill = orch.run_form_compiler(
        job={"url": "u", "application_url": "u"}, page=object(), profile=PROFILE,
        conn=conn, company="acme", operator=None, stages=fakes)
    assert status.startswith("needs_review")         # never submits an incomplete form
    assert prefill["tier_used"] == "v2_greenhouse"


def test_orchestrator_dry_run_never_submits(tmp_path):
    conn = _conn(tmp_path)
    submitted = {"called": False}

    def _submit(page, schema):
        submitted["called"] = True
        return True

    fakes = orch.Stages(
        parse=lambda page, company, url: _schema(),
        resolve=lambda schema, profile, conn: orch._passthrough_plan(schema, profile),
        run_oracle=lambda plan, schema, operator: plan,
        execute=lambda page, schema, plan, conn: orch.ExecStub(ready_to_submit=True,
                                                               committed_keys=["first_name"]),
        submit=_submit,
        verify=lambda evidence, conn, dom_signals: orch.VerifyStub(verified=True, tier=1),
    )
    status, _, prefill = orch.run_form_compiler(
        job={"url": "u", "application_url": "u"}, page=object(), profile=PROFILE,
        conn=conn, company="acme", operator=None, stages=fakes, dry_run=True)
    assert status == "needs_review:v2_dry_run"
    assert submitted["called"] is False              # dry-run must not click submit
    assert prefill["tier_used"] == "v2_greenhouse"


def test_orchestrator_post_submit_crash_does_not_fall_back(tmp_path):
    conn = _conn(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("crash after submit clicked")

    fakes = orch.Stages(
        parse=lambda page, company, url: _schema(),
        resolve=lambda schema, profile, conn: orch._passthrough_plan(schema, profile),
        run_oracle=lambda plan, schema, operator: plan,
        execute=lambda page, schema, plan, conn: orch.ExecStub(ready_to_submit=True,
                                                               committed_keys=["first_name"]),
        submit=lambda page, schema: True,
        verify=_boom,                                 # crash in the submit->verify block
    )
    status, _, prefill = orch.run_form_compiler(
        job={"url": "u", "application_url": "u"}, page=object(), profile=PROFILE,
        conn=conn, company="acme", operator=None, stages=fakes)
    # A submit may already have fired -> NEVER the sentinel (would risk a
    # double-submit re-apply). Leave the dangling INTENT for reconciliation.
    assert status == "needs_review:v2_crashed_post_submit"
    assert status != orch.FALLBACK_SENTINEL
    assert prefill["tier_used"] == "v2_greenhouse"


def test_orchestrator_unverified_submission_needs_review(tmp_path):
    conn = _conn(tmp_path)
    fakes = orch.Stages(
        parse=lambda page, company, url: _schema(),
        resolve=lambda schema, profile, conn: orch._passthrough_plan(schema, profile),
        run_oracle=lambda plan, schema, operator: plan,
        execute=lambda page, schema, plan, conn: orch.ExecStub(ready_to_submit=True,
                                                               committed_keys=["first_name"]),
        submit=lambda page, schema: True,
        verify=lambda evidence, conn, dom_signals: orch.VerifyStub(verified=False, needs_review=True),
    )
    status, _, prefill = orch.run_form_compiler(
        job={"url": "u", "application_url": "u"}, page=object(), profile=PROFILE,
        conn=conn, company="acme", operator=None, stages=fakes)
    assert status == "needs_review:unverified_submission"
    assert prefill["tier_used"] == "v2_greenhouse"


def test_run_oracle_folds_free_text_and_parks_enumerated(tmp_path):
    """The oracle fold: free-text custom answers -> filled PlannedField; an
    enumerated custom field (options LAZY at parse) the oracle can't index ->
    park (Phase-4 refinement). Keeps it honest (invariant 4)."""
    from applypilot.apply.v2 import operator as op

    free = ir.Field("q1", (), "Why us?", "Why us?", "custom.why", ir.Widget("textarea"), ir.LAZY, True)
    enum = ir.Field("q2", (), "Seniority", "Seniority", "custom.level",
                    ir.Widget("react_select"), ir.LAZY, True)
    schema = ir.FormSchema("greenhouse", "acme", "u",
                           [ir.Step(0, [free, enum], terminal=True)])
    plan = orch.resolver.FillPlan(planned=[], needs_oracle=[free, enum])

    class FakeOperator:
        def resolve_fields(self, request):
            ans = {}
            for spec in request.fields:
                if spec.widget_kind in ("text", "textarea"):
                    ans[spec.field_fp] = op.FieldAnswer(spec.field_fp, text="Because reasons.")
                else:
                    # enumerated with options=[] -> can't answer -> park
                    ans[spec.field_fp] = op.FieldAnswer(spec.field_fp, cannot_answer=True)
            return op.FieldAnswers(by_fp=ans)

    out = orch._run_oracle(plan, schema, FakeOperator())
    assert out.needs_oracle == []                    # all folded out
    by_key = {pf.field.semantic_key: pf for pf in out.planned}
    assert by_key["custom.why"].value == "Because reasons."
    assert by_key["custom.why"].park is False
    assert by_key["custom.level"].park is True       # enumerated custom -> parked


def test_run_oracle_no_op_without_operator():
    plan = orch.resolver.FillPlan(planned=[], needs_oracle=[
        ir.Field("q1", (), "Q", "Q", "custom.q", ir.Widget("text"), ir.LAZY, True)])
    schema = ir.FormSchema("greenhouse", "acme", "u", [ir.Step(0, [], terminal=True)])
    out = orch._run_oracle(plan, schema, None)       # operator=None -> untouched
    assert out is plan
    assert len(out.needs_oracle) == 1
