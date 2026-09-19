# tests/test_ui_registry.py
import os

from applypilot.webui import registry as reg


def test_open_writes_record_and_pidfile(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    rec = reg.open_batch(kind="live_apply", dry_run=False,
                         args=["apply", "--limit", "10"], pid=os.getpid())
    assert rec["id"] and rec["finished_at"] is None and rec["pid"] == os.getpid()
    d = tmp_path / "ui_runs"
    assert (d / f"{rec['id']}.json").exists()
    assert (d / "current.pid").read_text(encoding="utf-8").strip() == rec["id"]


def test_close_batch_records_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    rec = reg.open_batch(kind="live_apply", dry_run=False, args=[], pid=os.getpid())
    reg.close_batch(rec["id"], returncode=0, outcome={"applied": 3, "failed": 1, "needs_review": 2})
    got = reg.get_batch(rec["id"])
    assert got["returncode"] == 0 and got["outcome"]["applied"] == 3
    assert got["finished_at"] is not None
    assert not (tmp_path / "ui_runs" / "current.pid").exists()


def test_history_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    a = reg.open_batch(kind="live_apply", dry_run=True, args=[], pid=os.getpid())
    reg.close_batch(a["id"], returncode=0, outcome={})
    b = reg.open_batch(kind="live_apply", dry_run=False, args=[], pid=os.getpid())
    reg.close_batch(b["id"], returncode=0, outcome={})
    hist = reg.history(limit=10)
    assert [h["id"] for h in hist][:2] == [b["id"], a["id"]]


def test_reconcile_adopts_live_pid(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    rec = reg.open_batch(kind="live_apply", dry_run=False, args=[], pid=os.getpid())
    result = reg.reconcile_on_start()
    assert result["adopted"] and result["adopted"]["id"] == rec["id"]
    assert reg.get_batch(rec["id"])["finished_at"] is None


def test_reconcile_closes_dead_pid_as_orphan(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    rec = reg.open_batch(kind="live_apply", dry_run=False, args=[], pid=2_000_000_000)
    result = reg.reconcile_on_start()
    assert result["adopted"] is None
    got = reg.get_batch(rec["id"])
    assert got["finished_at"] is not None and got["returncode"] == reg.ORPHANED
    assert not (tmp_path / "ui_runs" / "current.pid").exists()


def test_pid_alive_is_windows_safe():
    assert reg.pid_alive(os.getpid()) is True
    assert reg.pid_alive(2_000_000_000) is False
