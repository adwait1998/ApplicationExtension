from applypilot.apply.v2 import ir


def test_widget_kinds_and_semantic_keys_are_enumerated():
    # widget kinds cover the spec §6.3 set; LAZY sentinel exists.
    assert {"text", "textarea", "native_select", "react_select", "radio_group",
            "checkbox", "file", "date", "phone_intl", "typeahead_location"} <= set(ir.WidgetKind.ALL)
    assert ir.LAZY is not None                            # sentinel exists
    # canary semantic keys are flagged
    assert ir.is_canary_key("work_auth") and ir.is_canary_key("sponsorship")
    assert ir.is_canary_key("salary") and not ir.is_canary_key("first_name")


def test_field_fp_stable_across_id_class_churn():
    # Same label + widget + options-shape, different auto-generated id -> same fp.
    a = ir.Field(field_id="input_r3f9a2ab12cd", frame_path=(), label_text="First name",
                 question_text="First name", semantic_key="first_name",
                 widget=ir.Widget(kind="text"), options=ir.LAZY, required=True)
    b = ir.Field(field_id="input_9xk21zff8801", frame_path=(), label_text="First name",
                 question_text="First name", semantic_key="first_name",
                 widget=ir.Widget(kind="text"), options=ir.LAZY, required=True)
    assert ir.field_fp(a) == ir.field_fp(b)              # id churn does not move the fp


def test_field_fp_distinguishes_widget_and_options_shape():
    base = dict(field_id="x", frame_path=(), label_text="Gender", question_text="Gender",
                semantic_key="eeo.gender", required=False)
    txt = ir.Field(widget=ir.Widget(kind="text"), options=ir.LAZY, **base)
    sel = ir.Field(widget=ir.Widget(kind="react_select"), options=ir.LAZY, **base)
    assert ir.field_fp(txt) != ir.field_fp(sel)          # widget kind is part of the key


def test_template_and_questions_fp_split():
    # template_fp = step structure + standard (semantic-keyed) fields;
    # questions_fp = the custom.* delta. Two forms sharing the GH template but
    # differing only in custom questions share template_fp, differ in questions_fp.
    std = [ir.Field(field_id="f", frame_path=(), label_text="Email", question_text="Email",
                    semantic_key="email", widget=ir.Widget(kind="text"), options=ir.LAZY, required=True)]
    cust_a = ir.Field(field_id="c", frame_path=(), label_text="Why us?", question_text="Why us?",
                      semantic_key="custom.why_us", widget=ir.Widget(kind="textarea"),
                      options=ir.LAZY, required=True)
    cust_b = ir.Field(field_id="c", frame_path=(), label_text="Salary?", question_text="Salary?",
                      semantic_key="custom.salary_expectation", widget=ir.Widget(kind="text"),
                      options=ir.LAZY, required=True)
    fa = ir.FormSchema(ats="greenhouse", company="acme", url="u",
                       steps=[ir.Step(index=0, fields=std + [cust_a], terminal=True)])
    fb = ir.FormSchema(ats="greenhouse", company="acme", url="u",
                       steps=[ir.Step(index=0, fields=std + [cust_b], terminal=True)])
    assert fa.template_fp() == fb.template_fp()           # same standard skeleton
    assert fa.questions_fp() != fb.questions_fp()         # different custom delta


def test_question_fp_stable_and_options_lazy_by_default():
    f = ir.Field(field_id="q", frame_path=("main", "iframe0"), label_text="Are you 18+?",
                 question_text="Are you 18 or older?", semantic_key=None,
                 widget=ir.Widget(kind="native_select"), options=ir.LAZY, required=True)
    assert f.options is ir.LAZY                            # never eagerly enumerated
    assert ir.question_fp(f) == ir.question_fp(f)          # deterministic
    assert f.frame_path == ("main", "iframe0")            # frame path is part of identity
