from applypilot import database as db
from applypilot.apply.v2 import ir
from applypilot.apply.v2 import resolver as rz
from applypilot.apply.v2 import mapping_cache as mc


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


PROFILE = {
    "personal": {"full_name": "Nida Shah", "email": "nida@example.com",
                 "phone": "4081234567", "city": "San Jose",
                 "linkedin_url": "https://linkedin.com/in/nidashah"},
    "work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": True},
    "eeo_voluntary": {"gender": "Decline to self-identify",
                      "race_ethnicity": "Decline to self-identify"},
}


def _field(sem, kind="text", label="x", options=ir.LAZY, required=True, fid="id1"):
    return ir.Field(field_id=fid, frame_path=(), label_text=label, question_text=label,
                    semantic_key=sem, widget=ir.Widget(kind=kind), options=options, required=required)


def _schema(fields):
    return ir.FormSchema(ats="greenhouse", company="acme", url="u",
                         steps=[ir.Step(index=0, fields=fields, terminal=True)])


def test_semantic_key_resolves_from_profile_path(tmp_path):
    conn = _conn(tmp_path)
    schema = _schema([_field("first_name", label="First name"),
                      _field("email", label="Email")])
    plan = rz.resolve(schema, PROFILE, conn=conn)
    byk = {pf.field.semantic_key: pf for pf in plan.planned}
    assert byk["first_name"].value == "Nida"        # first token of full_name
    assert byk["email"].value == "nida@example.com"
    assert byk["first_name"].binding == "profile.personal.full_name"
    assert plan.needs_oracle == []                  # both resolved deterministically


def test_canary_never_oracled_and_only_from_profile(tmp_path):
    conn = _conn(tmp_path)
    schema = _schema([_field("work_auth", kind="react_select",
                             label="Are you authorized to work?", options=ir.LAZY),
                      _field("sponsorship", kind="react_select",
                             label="Do you require sponsorship?", options=ir.LAZY)])
    plan = rz.resolve(schema, PROFILE, conn=conn)
    byk = {pf.field.semantic_key: pf for pf in plan.planned}
    # canary -> resolved to an INTENT string from profile; never queued to oracle
    assert byk["work_auth"].option_intent == "yes"          # legally_authorized_to_work True
    assert byk["sponsorship"].option_intent == "yes"        # require_sponsorship True
    assert all(f.semantic_key not in ("work_auth", "sponsorship") for f in plan.needs_oracle)


def test_eeo_resolves_to_decline_default_not_oracle(tmp_path):
    conn = _conn(tmp_path)
    schema = _schema([_field("eeo.gender", kind="react_select", label="Gender", options=ir.LAZY),
                      _field("eeo.veteran", kind="react_select", label="Veteran status", options=ir.LAZY)])
    plan = rz.resolve(schema, PROFILE, conn=conn)
    byk = {pf.field.semantic_key: pf for pf in plan.planned}
    assert byk["eeo.gender"].option_intent == "decline to self-identify"
    assert byk["eeo.veteran"].option_intent  # a safe decline default even absent in profile
    assert plan.needs_oracle == []                  # EEO has safe canonical answers


def test_custom_question_unresolved_goes_to_oracle(tmp_path):
    conn = _conn(tmp_path)
    schema = _schema([_field("custom.why_us", kind="textarea",
                             label="Why do you want to work here?", options=None)])
    plan = rz.resolve(schema, PROFILE, conn=conn)
    assert len(plan.needs_oracle) == 1
    assert plan.needs_oracle[0].semantic_key == "custom.why_us"


def test_mapping_cache_hit_skips_reresolution(tmp_path):
    conn = _conn(tmp_path)
    f = _field("custom.why_us", kind="textarea",
               label="Why do you want to work here?", options=None)
    fp = ir.field_fp(f)
    # a previously-learned binding to a cached answer
    mc.put(conn, "greenhouse", fp, binding="answer:qfp123", widget_driver="textarea")
    schema = _schema([f])
    # seed the answer bank with the qfp the binding points at (via context)
    plan = rz.resolve(schema, PROFILE, conn=conn,
                      answer_lookup=lambda binding: "Cached why-us answer"
                      if binding == "answer:qfp123" else None)
    byk = {pf.field.semantic_key: pf for pf in plan.planned}
    assert byk["custom.why_us"].value == "Cached why-us answer"
    assert plan.needs_oracle == []                  # cache hit -> no oracle


def test_hard_refusal_parks_uncovered_legal_attestation(tmp_path):
    conn = _conn(tmp_path)
    # a legal attestation the profile does not cover -> park, never guess (spec §6.4)
    schema = _schema([_field("custom.felony_attestation", kind="react_select",
                             label="I attest under penalty of perjury...", options=ir.LAZY)])
    plan = rz.resolve(schema, PROFILE, conn=conn)
    parked = [pf for pf in plan.planned if pf.park]
    # either parked directly, or routed to oracle which will cannot_answer -> park.
    assert parked or plan.needs_oracle    # not silently filled with a guess


def test_build_element_spec_maps_locator_dict():
    from applypilot.apply.healing import ElementSpec
    spec = rz.build_element_spec({"label": "Email", "role": "textbox",
                                  "elem_id": "em", "name": "Email"})
    assert isinstance(spec, ElementSpec)
    assert spec.label == "Email" and spec.elem_id == "em"
