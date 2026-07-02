"""Iter-7 regression (CRITICAL): dry-run must be structurally unable to submit.

Live incident 2026-06-12 (Twilio dry-run): `allow_submit` is an argument the
MODEL passes to stream_execute. The dry-run prohibition lived only in the
prompt; the model passed allow_submit=true, the server complied, and a real
application was submitted during a dry run. Dry-run is now enforced
server-side: the model's allow_submit cannot override it.
"""

from __future__ import annotations

import pytest

from applypilot.apply import launcher
from applypilot.apply.stream_executor import effective_allow_submit
from applypilot.apply.stream_mcp_server import main as stream_main


# --- pure enforcement core ---

@pytest.mark.parametrize("model_allow,dry_run,expected", [
    (True,  True,  False),  # THE INCIDENT: model says submit, dry-run says no
    (True,  False, True),   # live run, model confirmed → allowed
    (False, True,  False),
    (False, False, False),
])
def test_effective_allow_submit(model_allow, dry_run, expected):
    assert effective_allow_submit(model_allow, dry_run) is expected


# --- launcher plumbing: flag must reach the stream server args ---

def test_mcp_config_dry_run_flag_present():
    cfg = launcher._make_mcp_config(9222, dry_run=True)
    args = cfg["mcpServers"]["applypilot_stream"]["args"]
    assert "--dry-run" in args
    assert "--cdp-port" in args


def test_mcp_config_live_has_no_dry_run_flag():
    cfg = launcher._make_mcp_config(9222, dry_run=False)
    assert "--dry-run" not in cfg["mcpServers"]["applypilot_stream"]["args"]


def test_mcp_config_default_is_live():
    # Callers that don't pass dry_run must not accidentally block live submits.
    cfg = launcher._make_mcp_config(9222)
    assert "--dry-run" not in cfg["mcpServers"]["applypilot_stream"]["args"]


# --- server argparse accepts the flag ---

def test_dom_blocker_fails_open_without_browser():
    # Dead CDP port → injection must return False quickly, never raise.
    assert launcher._inject_dry_run_submit_blocker(1) is False


def test_dom_blocker_js_guards_all_three_paths():
    js = launcher._DRY_RUN_SUBMIT_BLOCKER_JS
    assert "addEventListener('submit'" in js
    assert "addEventListener('click'" in js
    assert "HTMLFormElement.prototype.submit" in js
    assert "requestSubmit" in js
    assert "preventDefault" in js


def test_stream_server_argparse_accepts_dry_run(monkeypatch):
    captured = {}

    def fake_build_server(port, dry_run=False):
        captured["port"] = port
        captured["dry_run"] = dry_run

        class _S:
            def run(self):
                pass
        return _S()

    monkeypatch.setattr("applypilot.apply.stream_mcp_server.build_server", fake_build_server)
    stream_main(["--cdp-port", "9223", "--dry-run"])
    assert captured == {"port": 9223, "dry_run": True}
    stream_main(["--cdp-port", "9224"])
    assert captured == {"port": 9224, "dry_run": False}
