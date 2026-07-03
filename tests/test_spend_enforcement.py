from applypilot import database as db
from applypilot.spend_ledger import SpendLedger

def test_set_and_clear_pause(tmp_path):
    db.init_db(tmp_path / "e.db")
    conn = db.get_connection(tmp_path / "e.db")
    assert db.paused_reason(conn) is None
    db.set_paused(conn, "budget")
    assert db.paused_reason(conn) == "budget"
    db.set_paused(conn, None)
    assert db.paused_reason(conn) is None

def test_over_cap_ledger_signal(tmp_path):
    led = SpendLedger(tmp_path / "l.jsonl", daily_cap_usd=0.01)
    led.record(stage="apply", model="claude-sonnet-5", tokens_in=100000, tokens_out=50000)
    assert led.over_cap() is True

def test_engine_control_table_exists_on_fresh_db(tmp_path):
    db.init_db(tmp_path / "e2.db")
    conn = db.get_connection(tmp_path / "e2.db")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(engine_control)")}
    assert {"key", "value", "updated_at"} <= cols
