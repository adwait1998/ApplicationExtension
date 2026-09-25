"""Local application log: counts only, applicant-set status, CSV export."""
import json

from fastapi.testclient import TestClient

from applypilot.extension import app_log
from applypilot.extension.server import create_app

URL = "https://job-boards.greenhouse.io/okx/jobs/7608993003"


def test_record_dedupes_refills_within_the_hour(tmp_path):
    p = tmp_path / "application_log.jsonl"
    a = app_log.record(p, URL, counts={"filled": 10, "needs_you": 3}, now=1000)
    b = app_log.record(p, URL, counts={"filled": 14}, now=1500)
    c = app_log.record(p, URL, counts={"filled": 2}, now=1000 + 7200)
    assert a["id"] == b["id"] != c["id"]
    assert b["fills"] == 2 and b["counts"]["filled"] == 14 and b["counts"]["needs_you"] == 0
    assert len(app_log.entries(p)) == 2


def test_status_and_csv(tmp_path):
    p = tmp_path / "application_log.jsonl"
    e = app_log.record(p, URL, title="Accounting Manager", company="OKX", counts={"filled": 15})
    assert app_log.set_status(p, e["id"], "applied") is True
    assert app_log.set_status(p, "nope", "applied") is False
    csv_text = app_log.to_csv(p)
    assert csv_text.splitlines()[0].startswith("date,status,company,title,url")
    assert "applied,OKX,Accounting Manager" in csv_text


def test_log_never_stores_field_values(tmp_path):
    p = tmp_path / "application_log.jsonl"
    app_log.record(p, URL, counts={"filled": 1, "email": "x@y.z"})
    raw = p.read_text(encoding="utf-8")
    assert "x@y.z" not in raw


def test_endpoints(tmp_path):
    (tmp_path / "profile.json").write_text(json.dumps({"personal": {}}), encoding="utf-8")
    app = create_app(app_dir=tmp_path, root=tmp_path, profile={"personal": {}})
    c = TestClient(app)
    h = {"X-ApplyPilot-Token": app.state.token}
    assert c.post("/log", json={"url": URL}).status_code == 401
    assert c.post("/log", headers=h, json={"url": "javascript:alert(1)"}).status_code == 422
    e = c.post("/log", headers=h, json={"url": URL, "counts": {"filled": 3}}).json()
    assert e["company"] == "okx" and e["status"] == "filled"
    assert (tmp_path / "application_log.jsonl").exists()
    assert c.post(f"/log/{e['id']}/status", headers=h, json={"status": "applied"}).json() == {"ok": True}
    assert c.post(f"/log/{e['id']}/status", headers=h, json={"status": "hacked"}).status_code == 422
    assert c.get("/log", headers=h).json()["entries"][0]["status"] == "applied"
    r = c.get("/log.csv", headers=h)
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"] and "applied" in r.text
