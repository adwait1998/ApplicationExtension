# tests/test_canary_parse.py
from applypilot.apply import canary_parse as cp
from applypilot.apply.v2 import ir


def _schema(ats, keys):
    fields = [ir.Field(field_id=k, frame_path=(), label_text=k, question_text=k,
                       semantic_key=k, widget=ir.Widget(kind="text"), options=ir.LAZY)
              for k in keys]
    return ir.FormSchema(ats=ats, company="c", url="u", steps=[ir.Step(0, fields, terminal=True)])


def test_score_parse_results_counts_pass_and_gap():
    # A parse "passes" when it recovers >=1 standard field (email/first_name/resume).
    results = [
        {"ats": "ashby", "schema": _schema("ashby", ["email", "resume"]), "error": None},
        {"ats": "ashby", "schema": _schema("ashby", ["custom.only"]), "error": None},   # gap
        {"ats": "ashby", "schema": None, "error": "parse crashed"},                     # gap
    ]
    report = cp.score(results)
    assert report["by_ats"]["ashby"]["n"] == 3
    assert report["by_ats"]["ashby"]["parsed"] == 1
    assert report["by_ats"]["ashby"]["parse_rate"] == round(1 / 3, 3)
    assert report["by_ats"]["ashby"]["gaps"] == 2


def test_format_canary_report_renders_per_ats():
    report = {"by_ats": {"ashby": {"n": 20, "parsed": 19, "parse_rate": 0.95, "gaps": 1},
                         "lever": {"n": 20, "parsed": 20, "parse_rate": 1.0, "gaps": 0}}}
    out = cp.format_report(report)
    assert "ashby" in out and "95%" in out and "lever" in out


def test_alarm_threshold_flags_low_parse_rate():
    report = {"by_ats": {"ashby": {"n": 20, "parsed": 14, "parse_rate": 0.70, "gaps": 6}}}
    alarms = cp.alarms(report, min_parse_rate=0.90)
    assert "ashby" in alarms                              # 70% < 90% -> churn alarm
