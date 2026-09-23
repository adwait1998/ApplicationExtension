"""Tests for the local extension service (applypilot.extension.server).

All $0, no network, no subprocess. The profile is always injected as an
inline dict via create_app(profile=...) — this test suite must never touch
E:\\applypilot-data or read a real profile.json.
"""
from __future__ import annotations
import threading

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from applypilot.extension.server import create_app, get_or_create_token, token_path  # noqa: E402

PROFILE = {
    "personal": {
        "full_name": "Nida Shah",
        "email": "nida@example.com",
        "phone": "555-867-5309",
        "linkedin_url": "https://linkedin.com/in/nidashah",
        "password": "hunter2",
    },
    "work_authorization": {
        "legally_authorized_to_work": True,
        "require_sponsorship": True,
    },
    "compensation": {
        "salary_expectation": "150000",
        "salary_currency": "USD",
    },
    "eeo_voluntary": {},
}


@pytest.fixture
def app_and_token(tmp_path):
    token = get_or_create_token(tmp_path)
    app = create_app(app_dir=tmp_path, profile=PROFILE)
    return app, token


@pytest.fixture
def client(app_and_token):
    app, _token = app_and_token
    return TestClient(app)


@pytest.fixture
def auth_headers(app_and_token):
    _app, token = app_and_token
    return {"X-ApplyPilot-Token": token}


# ---------------------------------------------------------------------------
# host binding
# ---------------------------------------------------------------------------


def test_refuses_non_local_host():
    with pytest.raises(ValueError):
        create_app(app_dir=None, profile=PROFILE, host="0.0.0.0")


def test_accepts_127_0_0_1(tmp_path):
    create_app(app_dir=tmp_path, profile=PROFILE, host="127.0.0.1")  # no raise


# ---------------------------------------------------------------------------
# token: generated, persisted, required on every request
# ---------------------------------------------------------------------------


def test_token_persisted_to_app_dir(tmp_path):
    token = get_or_create_token(tmp_path)
    assert token_path(tmp_path).exists()
    assert token_path(tmp_path).read_text(encoding="utf-8").strip() == token


def test_token_stable_across_calls(tmp_path):
    first = get_or_create_token(tmp_path)
    second = get_or_create_token(tmp_path)
    assert first == second


def test_health_without_token_is_401(client):
    resp = client.get("/health")
    assert resp.status_code == 401


def test_health_with_wrong_token_is_401(client):
    resp = client.get("/health", headers={"X-ApplyPilot-Token": "not-the-token"})
    assert resp.status_code == 401


def test_resolve_without_token_is_401(client):
    resp = client.post("/resolve", json={"url": "https://example.com", "fields": []})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_reports_tiers_without_laya(client, auth_headers):
    resp = client.get("/health", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["tiers_available"] == ["canary", "deterministic"]
    assert "laya" not in body["tiers_available"]


# ---------------------------------------------------------------------------
# CORS: restricted to chrome-extension:// origins
# ---------------------------------------------------------------------------


def test_cors_allows_chrome_extension_origin(client, auth_headers):
    resp = client.options(
        "/health",
        headers={
            "origin": "chrome-extension://abcdefghijklmnop",
            "access-control-request-method": "GET",
            **auth_headers,
        },
    )
    assert resp.headers.get("access-control-allow-origin") == "chrome-extension://abcdefghijklmnop"


def test_cors_rejects_other_origins(client):
    resp = client.options(
        "/health",
        headers={
            "origin": "https://evil.example.com",
            "access-control-request-method": "GET",
        },
    )
    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers.keys()}


# ---------------------------------------------------------------------------
# /profile — key names only, never values, never the secret path
# ---------------------------------------------------------------------------


def test_profile_endpoint_returns_key_names_not_values(client, auth_headers):
    resp = client.get("/profile", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "personal.email" in body["keys"]
    assert "personal.password" not in body["keys"]
    dumped = str(body)
    assert "nida@example.com" not in dumped
    assert "hunter2" not in dumped


# ---------------------------------------------------------------------------
# /resolve — only returns values for fields actually submitted
# ---------------------------------------------------------------------------


def test_resolve_returns_only_submitted_fields(client, auth_headers):
    body = {
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "fields": [
            {"id": "f0", "tag": "input", "type": "text", "name": "email",
             "autocomplete": "email", "label": "Email"},
        ],
    }
    resp = client.post("/resolve", json=body, headers=auth_headers)
    assert resp.status_code == 200
    plan = resp.json()
    assert len(plan["fills"]) == 1
    assert plan["fills"][0]["id"] == "f0"
    assert plan["fills"][0]["value"] == "nida@example.com"
    # the whole profile must never come back — only what was asked about
    assert "phone" not in str(plan)
    assert "hunter2" not in str(plan)


def test_resolve_password_field_returns_nothing(client, auth_headers):
    body = {
        "url": "https://example.com",
        "fields": [{"id": "fpw", "tag": "input", "type": "password", "label": "Password"}],
    }
    resp = client.post("/resolve", json=body, headers=auth_headers)
    plan = resp.json()
    assert plan["fills"] == []
    assert plan["skipped"][0]["id"] == "fpw"
    assert "hunter2" not in str(plan)


# ---------------------------------------------------------------------------
# Test the real thing: a realistic Greenhouse-shaped field list end to end
# through the actual HTTP API.
# ---------------------------------------------------------------------------


def test_realistic_greenhouse_application_form(client, auth_headers):
    fields = [
        {"id": "f0", "tag": "input", "type": "text", "name": "first_name",
         "autocomplete": "given-name", "label": "First Name", "required": True},
        {"id": "f1", "tag": "input", "type": "text", "name": "last_name",
         "autocomplete": "family-name", "label": "Last Name", "required": True},
        {"id": "f2", "tag": "input", "type": "email", "name": "email",
         "autocomplete": "email", "label": "Email", "required": True},
        {"id": "f3", "tag": "input", "type": "tel", "name": "phone",
         "autocomplete": "tel", "label": "Phone"},
        {"id": "f4", "tag": "input", "type": "text", "name": "urls[LinkedIn]",
         "autocomplete": "", "label": "LinkedIn Profile"},
        {"id": "f5", "tag": "select", "type": "", "name": "question_work_auth",
         "label": "Are you legally authorized to work in the United States?", "required": True,
         "options": ["Yes", "No"]},
        {"id": "f6", "tag": "select", "type": "", "name": "question_sponsorship",
         "label": "Will you now or in the future require sponsorship for employment visa status?",
         "required": True, "options": ["Yes", "No"]},
        {"id": "f7", "tag": "input", "type": "text", "name": "question_salary",
         "label": "Desired salary"},
        {"id": "f8", "tag": "input", "type": "file", "name": "resume",
         "label": "Resume/CV", "required": True},
        {"id": "f9", "tag": "textarea", "type": "", "name": "question_why",
         "label": "Why do you want to work here?"},
    ]
    resp = client.post(
        "/resolve",
        json={"url": "https://boards.greenhouse.io/acme/jobs/123", "fields": fields},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    plan = resp.json()

    fills_by_id = {f["id"]: f for f in plan["fills"]}
    skipped_by_id = {s["id"]: s for s in plan["skipped"]}

    # ordinary fields fill
    assert fills_by_id["f0"]["value"] == "Nida"
    assert fills_by_id["f1"]["value"] == "Shah"
    assert fills_by_id["f2"]["value"] == "nida@example.com"
    assert fills_by_id["f3"]["value"] == "555-867-5309"
    assert fills_by_id["f4"]["value"] == "https://linkedin.com/in/nidashah"
    for fid in ("f0", "f1", "f2", "f3", "f4"):
        assert fills_by_id[fid]["source"] == "deterministic"

    # the two work-auth questions come from the canary tier
    assert fills_by_id["f5"]["source"] == "canary"
    assert fills_by_id["f5"]["value"] == "Yes"
    assert fills_by_id["f6"]["source"] == "canary"
    assert fills_by_id["f6"]["value"] == "Yes"  # profile requires sponsorship

    # salary behaves per the profile (present -> filled, via canary)
    assert fills_by_id["f7"]["source"] == "canary"
    assert fills_by_id["f7"]["value"] == "150000 USD"

    # resume file input is never auto-filled
    assert "f8" in skipped_by_id
    assert "f8" not in fills_by_id

    # free-text question is NOT answered
    assert "f9" in skipped_by_id
    assert "f9" not in fills_by_id
    assert skipped_by_id["f9"]["source"] == "unresolved"

    assert plan["tiers_available"] == ["canary", "deterministic"]


# ---------------------------------------------------------------------------
# Laya warmup at startup: loading the checkpoint costs ~28s, and doing it
# lazily would dump that entire cliff on whichever form the operator opened
# first. Warming in a daemon thread must never delay or break startup.
# ---------------------------------------------------------------------------

def test_startup_warms_laya_in_the_background_when_available(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from applypilot.extension import resolve, server as srv

    called = threading.Event()

    class SlowBackend:
        def classify(self, field, candidate_keys):
            return None

        def warmup(self):
            called.set()
            return True

    monkeypatch.setattr(resolve, "get_backend", lambda: SlowBackend())
    app = srv.create_app(app_dir=tmp_path)
    with TestClient(app):
        assert called.wait(timeout=5), "warmup() was never invoked at startup"


def test_startup_survives_a_backend_that_cannot_warm(tmp_path, monkeypatch):
    """A broken optional tier must not stop the service serving the tiers that
    do work."""
    from fastapi.testclient import TestClient
    from applypilot.extension import resolve, server as srv

    class Exploding:
        def classify(self, field, candidate_keys):
            return None

        def warmup(self):
            raise RuntimeError("no model for you")

    monkeypatch.setattr(resolve, "get_backend", lambda: Exploding())
    app = srv.create_app(app_dir=tmp_path)
    with TestClient(app) as client:
        token = (tmp_path / "extension_token.txt").read_text(encoding="utf-8").strip()
        r = client.get("/health", headers={"X-ApplyPilot-Token": token})
        assert r.status_code == 200


def test_startup_is_a_noop_without_laya(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from applypilot.extension import resolve, server as srv

    monkeypatch.setattr(resolve, "get_backend", lambda: None)
    app = srv.create_app(app_dir=tmp_path)
    with TestClient(app) as client:
        token = (tmp_path / "extension_token.txt").read_text(encoding="utf-8").strip()
        r = client.get("/health", headers={"X-ApplyPilot-Token": token})
        assert r.status_code == 200
        assert "laya" not in r.json()["tiers_available"]


def test_extension_default_port_matches_the_serve_extension_cli_default():
    """The extension's default Service URL and the CLI's --port default live in
    different languages with nothing tying them together. They drifted once
    already: the extension shipped pointing at 8765, the web dashboard's port,
    so it could never reach the service on 8787 out of the box."""
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    cli = (repo / "src" / "applypilot" / "cli.py").read_text(encoding="utf-8")
    line = next((ln for ln in cli.splitlines()
                 if "Copilot extension service" in ln and "typer.Option(" in ln), None)
    assert line, "could not find serve-extension's --port default in cli.py"
    m = re.search(r"typer\.Option\((\d+)", line)
    assert m, f"no numeric default in: {line.strip()}"
    cli_port = m.group(1)

    for rel in ("extension/background.js", "extension/options.js",
                "extension/options.html", "extension/README.md"):
        text = (repo / rel).read_text(encoding="utf-8")
        ports = set(re.findall(r"127\.0\.0\.1:(\d+)", text))
        assert ports, f"{rel} names no 127.0.0.1 port"
        assert ports == {cli_port}, (
            f"{rel} points at {sorted(ports)} but serve-extension listens on {cli_port}")
