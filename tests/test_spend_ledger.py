from applypilot.spend_ledger import SpendLedger, estimate_cost


def test_records_and_sums(tmp_path):
    led = SpendLedger(tmp_path / "ledger.jsonl")
    led.record(stage="score", model="gemini-2.0-flash", tokens_in=1000, tokens_out=200, identity_id="greenhouse:x:1")
    led.record(stage="apply", model="gemini-2.0-flash", tokens_in=500, tokens_out=100)
    assert led.spent_today() > 0
    assert len(list(led.entries())) == 2


def test_over_cap_detected_daily(tmp_path):
    led = SpendLedger(tmp_path / "l.jsonl", daily_cap_usd=0.0)
    led.record(stage="score", model="gpt-4o-mini", tokens_in=100000, tokens_out=1000)
    assert led.over_cap() is True


def test_over_cap_detected_monthly(tmp_path):
    led = SpendLedger(tmp_path / "l2.jsonl", daily_cap_usd=None, monthly_cap_usd=0.0)
    led.record(stage="score", model="gpt-4o-mini", tokens_in=100000, tokens_out=1000)
    assert led.over_cap() is True


def test_no_cap_never_over(tmp_path):
    led = SpendLedger(tmp_path / "l3.jsonl")  # no caps
    led.record(stage="score", model="gpt-4o-mini", tokens_in=100000, tokens_out=1000)
    assert led.over_cap() is False


def test_estimate_cost_positive():
    assert estimate_cost("claude-haiku-4-5-20251001", 50000, 20000) > 0


def test_unknown_model_uses_default_rate(tmp_path):
    # an unrecognized model still yields a positive, finite cost (default rate)
    assert estimate_cost("some-unknown-model", 1000, 1000) > 0


def test_entries_survives_missing_file(tmp_path):
    led = SpendLedger(tmp_path / "nope.jsonl")
    assert list(led.entries()) == []
    assert led.spent_today() == 0
