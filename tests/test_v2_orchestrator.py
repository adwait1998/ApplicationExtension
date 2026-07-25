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


def _resume_schema():
    fields = [ir.Field("rz", (), "Resume/CV", "Resume/CV", "resume", ir.Widget("file"), ir.LAZY, True)]
    return ir.FormSchema("greenhouse", "acme", "https://boards.greenhouse.io/acme/jobs/1",
                         [ir.Step(0, fields, advance_control={"role": "button", "name": "Submit"}, terminal=True)])


def test_orchestrator_threads_resume_path_to_default_resolve(tmp_path):
    """run_form_compiler threads resume_path into the DEFAULT resolve stage. Here
    every stage EXCEPT resolve is injected, so the real resolver runs (closing
    over resume_path) and the executor fake receives a plan whose resume
    PlannedField carries the prologue-resolved path."""
    conn = _conn(tmp_path)
    captured = {}

    def _execute(page, schema, plan, conn):
        captured["plan"] = plan
        return orch.ExecStub(ready_to_submit=True, committed_keys=["resume"])

    fakes = orch.Stages(
        parse=lambda page, company, url: _resume_schema(),
        # resolve intentionally OMITTED -> default resolve (with resume_path) runs.
        run_oracle=lambda plan, schema, operator: plan,
        execute=_execute,
        submit=lambda page, schema: True,
        verify=lambda evidence, conn, dom_signals: orch.VerifyStub(verified=True, tier=1),
    )
    status, _, prefill = orch.run_form_compiler(
        job={"url": "u", "application_url": "u"}, page=object(), profile=PROFILE,
        conn=conn, company="acme", operator=None, stages=fakes,
        resume_path=r"C:\x\resume.pdf")
    assert status == "applied"
    plan = captured["plan"]
    resume_pfs = [pf for pf in plan.planned if pf.field.semantic_key == "resume"]
    assert len(resume_pfs) == 1
    assert resume_pfs[0].value == r"C:\x\resume.pdf"
    assert resume_pfs[0].binding == "profile.resume_path"
    assert resume_pfs[0].driver == "file"
    assert plan.needs_oracle == []                       # file widget never oracled


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


# ---------------------------------------------------------------------------
# Rehearsal-blocker fix: every dict-returning path stamps an int duration_ms
# into the prefill so write_review_log's prefill_duration_ms is populated (v2's
# prefill previously lacked the key that every legacy producer carries).
# ---------------------------------------------------------------------------

def _stages_for(path):
    """Injected Stages that drive run_form_compiler to a specific dict-prefill
    return path."""
    base = dict(
        parse=lambda page, company, url: _schema(),
        resolve=lambda schema, profile, conn: orch._passthrough_plan(schema, profile),
        run_oracle=lambda plan, schema, operator: plan,
        submit=lambda page, schema: True,
    )
    if path == "applied":
        base.update(
            execute=lambda page, schema, plan, conn: orch.ExecStub(
                ready_to_submit=True, committed_keys=["first_name", "email"]),
            verify=lambda evidence, conn, dom_signals: orch.VerifyStub(verified=True, tier=1))
    elif path == "incomplete":
        base.update(
            execute=lambda page, schema, plan, conn: orch.ExecStub(
                ready_to_submit=False, missing_required=["First name"]))
    elif path == "dry_run":
        base.update(
            execute=lambda page, schema, plan, conn: orch.ExecStub(
                ready_to_submit=True, committed_keys=["first_name"]),
            verify=lambda evidence, conn, dom_signals: orch.VerifyStub(verified=True, tier=1))
    elif path == "crashed_post_submit":
        def _boom(*a, **k):
            raise RuntimeError("crash after submit clicked")
        base.update(
            execute=lambda page, schema, plan, conn: orch.ExecStub(
                ready_to_submit=True, committed_keys=["first_name"]),
            verify=_boom)
    elif path == "unverified":
        base.update(
            execute=lambda page, schema, plan, conn: orch.ExecStub(
                ready_to_submit=True, committed_keys=["first_name"]),
            verify=lambda evidence, conn, dom_signals: orch.VerifyStub(
                verified=False, needs_review=True))
    return orch.Stages(**base)


def test_every_dict_prefill_path_stamps_int_duration_ms(tmp_path):
    """Each dict-returning run_form_compiler path (applied / v2_incomplete_required
    / v2_dry_run / v2_crashed_post_submit / unverified_submission) carries an int
    duration_ms in prefill that equals the returned tuple's duration — the key
    write_review_log reads. Sentinel paths keep prefill=None (asserted separately)."""
    cases = {
        "applied": ("applied", False),
        "incomplete": ("needs_review:v2_incomplete_required", False),
        "dry_run": ("needs_review:v2_dry_run", True),
        "crashed_post_submit": ("needs_review:v2_crashed_post_submit", False),
        "unverified": ("needs_review:unverified_submission", False),
    }
    for path, (expected_status, dry_run) in cases.items():
        conn = _conn(tmp_path)
        status, duration_ms, prefill = orch.run_form_compiler(
            job={"url": "u", "application_url": "u"}, page=object(), profile=PROFILE,
            conn=conn, company="acme", operator=None, stages=_stages_for(path),
            dry_run=dry_run)
        assert status == expected_status, path
        assert isinstance(prefill, dict), path
        assert "duration_ms" in prefill, path
        assert isinstance(prefill["duration_ms"], int), path
        assert prefill["duration_ms"] == duration_ms, path   # consistent with tuple
        assert prefill["tier_used"] == "v2_greenhouse", path  # unchanged


def test_sentinel_paths_keep_prefill_none(tmp_path):
    """Fail-open sentinel paths (parse crash, pre-submit crash) still return
    prefill=None — no duration_ms dict is fabricated on the legacy-fallback route."""
    conn = _conn(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("parse blew up")

    status, _, prefill = orch.run_form_compiler(
        job={"url": "u", "application_url": "u"}, page=object(), profile=PROFILE,
        conn=conn, company="acme", operator=None, stages=orch.Stages(parse=_boom))
    assert status == orch.FALLBACK_SENTINEL
    assert prefill is None


def test_default_parse_registry_miss_falls_back_to_greenhouse(monkeypatch):
    # Registry-miss regression (Task 8 carryover): an ATS with no registered
    # front-end (parser_for -> None) must fall back to the Greenhouse parser, never
    # KeyError/crash. _default_parse imports its deps at call time, so patch them at
    # their SOURCE modules.
    from applypilot.apply.browser_stream import BrowserObservation, ControlObservation
    import applypilot.apply.browser_stream as bs
    import applypilot.apply.prefill as pf
    obs = BrowserObservation(controls=[
        ControlObservation(control_id="c1", control_type="text", selector="#c1",
                           label="Email", visible=True)])
    monkeypatch.setattr(bs, "collect_browser_observation", lambda page: obs)
    monkeypatch.setattr(pf, "_detect_ats", lambda url: "no_such_ats")   # parser_for -> None
    schema = orch._default_parse(page=object(), company="acme", url="https://x/y")
    assert schema.ats == "greenhouse"                  # belt-and-suspenders GH fallback
    assert any(f.semantic_key == "email" for s in schema.steps for f in s.fields)
