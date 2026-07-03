from applypilot import database as db
from applypilot.submission_ledger import SubmissionLedger


def _led(tmp_path, name):
    db.init_db(tmp_path / name)
    return SubmissionLedger(db.get_connection(tmp_path / name))


def test_intent_then_confirm(tmp_path):
    led = _led(tmp_path, "s.db")
    led.record_intent("greenhouse:chime:1", worker_id=0)
    assert led.has_open_intent("greenhouse:chime:1") is True
    assert led.has_confirmed("greenhouse:chime:1") is False
    led.confirm("greenhouse:chime:1", confidence=0.9)
    assert led.has_open_intent("greenhouse:chime:1") is False
    assert led.has_confirmed("greenhouse:chime:1") is True


def test_intent_then_fail_clears_open(tmp_path):
    led = _led(tmp_path, "s2.db")
    led.record_intent("greenhouse:chime:2", worker_id=0)
    led.fail("greenhouse:chime:2", reason="validation_error")
    assert led.has_open_intent("greenhouse:chime:2") is False
    assert led.has_confirmed("greenhouse:chime:2") is False


def test_dangling_intent_is_durable(tmp_path):
    import sqlite3
    p = tmp_path / "s3.db"
    db.init_db(p)
    SubmissionLedger(db.get_connection(p)).record_intent("greenhouse:chime:3", worker_id=0)
    raw = sqlite3.connect(p); raw.row_factory = sqlite3.Row
    led2 = SubmissionLedger(raw)
    assert led2.has_open_intent("greenhouse:chime:3") is True   # survives a fresh connection
    assert led2.dangling_count() == 1


def test_already_confirmed_signal(tmp_path):
    led = _led(tmp_path, "s4.db")
    led.record_intent("greenhouse:chime:4", worker_id=0); led.confirm("greenhouse:chime:4", confidence=0.9)
    assert led.has_confirmed("greenhouse:chime:4") is True


def test_company_cooldown_count(tmp_path):
    led = _led(tmp_path, "s5.db")
    for jid in ("1", "2"):
        led.record_intent(f"greenhouse:acme:{jid}", worker_id=0)
        led.confirm(f"greenhouse:acme:{jid}", confidence=0.9)
    from datetime import datetime, timezone, timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    assert led.confirmed_count_for_token("acme", since) == 2
    assert led.confirmed_count_for_token("other", since) == 0
