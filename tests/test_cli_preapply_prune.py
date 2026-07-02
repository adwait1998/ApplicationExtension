"""F2 regression: apply runs the freshness pre-check before dispatch.

$0 tests — no network, no subprocess. The network half (check_queue) is
covered in test_freshness.py; here we verify the CLI glue contract:
- `applypilot apply` exposes --prune/--no-prune defaulting to ON;
- `_preapply_prune` parks dead rows via the injected checker and never
  raises (a broken pre-check must not block an apply batch).
"""

from __future__ import annotations

import inspect

from applypilot import cli
from applypilot.freshness import PruneResult


def test_apply_command_has_prune_flag_defaulting_on():
    sig = inspect.signature(cli.apply)
    assert "prune" in sig.parameters
    default = sig.parameters["prune"].default
    # typer.Option default value is carried on .default of the OptionInfo
    assert getattr(default, "default", default) is True


def test_preapply_prune_invokes_checker_with_min_score(monkeypatch, tmp_path):
    monkeypatch.setattr("applypilot.config.DB_PATH", tmp_path / "x.db")
    calls = {}

    def fake_check_queue(conn, min_score, limit):
        calls["min_score"] = min_score
        calls["limit"] = limit
        return PruneResult(checked=3, expired=1, live=1, unknown=1, expired_urls=["j1"])

    cli._preapply_prune(min_score=9, check_queue_fn=fake_check_queue)
    assert calls == {"min_score": 9, "limit": 200}


def test_preapply_prune_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("applypilot.config.DB_PATH", tmp_path / "x.db")

    def explode(conn, min_score, limit):
        raise RuntimeError("network down")

    # Must not propagate — a broken pre-check cannot block the batch.
    cli._preapply_prune(min_score=8, check_queue_fn=explode)
