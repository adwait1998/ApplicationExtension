"""Tailored résumé for the page: pipeline tailoring behind the extension's
provider gate; only judge-passed versions are offered."""
import json

import pytest
from fastapi.testclient import TestClient

from applypilot.extension import llm_util, tailor
from applypilot.extension.server import create_app
from applypilot.scoring import tailor as pipeline_tailor

JOB = {"title": "Senior Product Designer", "company": "Acme", "description": "Design Acme's product. " * 20}


def _fake_pdf(text_path):
    out = text_path.with_suffix(".pdf")
    out.write_bytes(b"%PDF-1.4 fake")
    return out


def _approved(*a, **k):
    return "Taylor Morgan\nSUMMARY\nDesigner.", {"status": "approved", "validator": {"warnings": ["w1"]},
                                                  "judge": {"verdict": "PASS", "issues": "none"}}


def test_approved_version_is_stored_and_found(tmp_path):
    out = tailor.tailor_for_page({}, JOB, "base resume", "https://jobs.lever.co/acme/1", tmp_path,
                                 client=object(), tailor_fn=_approved, pdf_fn=_fake_pdf)
    assert out["status"] == "approved" and out["warnings"] == ["w1"] and out["text"].startswith("Taylor")
    meta, pdf = tailor.find_tailored(tmp_path, out["id"])
    assert pdf.read_bytes().startswith(b"%PDF") and meta["company"] == "Acme"


@pytest.mark.parametrize("tid", ["../../etc/passwd", "a/b", "", "A" * 200])
def test_ids_cannot_escape_the_directory(tmp_path, tid):
    assert tailor.find_tailored(tmp_path, tid) is None


def test_failed_fabrication_checks_are_refused(tmp_path):
    def failed(*a, **k):
        return "text", {"status": "failed_validation", "validator": {"errors": ["invented employer: Google"]},
                        "judge": None}
    with pytest.raises(tailor.TailorError, match="Google"):
        tailor.tailor_for_page({}, JOB, "base", "u", tmp_path, client=object(), tailor_fn=failed, pdf_fn=_fake_pdf)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("job,resume,msg", [({}, "base", "description"), (JOB, "", "résumé")])
def test_missing_inputs_are_honest_errors(tmp_path, job, resume, msg):
    with pytest.raises(tailor.TailorError, match=msg):
        tailor.tailor_for_page({}, job, resume, "u", tmp_path, client=object(), tailor_fn=_approved,
                               pdf_fn=_fake_pdf)


def test_pipeline_tailor_and_judge_use_the_supplied_client(monkeypatch):
    monkeypatch.setattr(pipeline_tailor, "get_client",
                        lambda: (_ for _ in ()).throw(AssertionError("pipeline client used")))
    calls = []

    class Fake:
        def chat(self, messages, **kw):
            calls.append(1)
            return "VERDICT: PASS\nISSUES: none"

    verdict = pipeline_tailor.judge_tailored_resume("orig", "tailored", "Designer", {}, client=Fake())
    assert verdict["passed"] and calls


def _client(tmp_path, monkeypatch, local=True):
    (tmp_path / "profile.json").write_text(json.dumps({"personal": {"full_name": "Taylor Morgan"}}),
                                           encoding="utf-8")
    (tmp_path / "resume.txt").write_text("Taylor Morgan\nDesigner", encoding="utf-8")
    monkeypatch.setattr(llm_util, "provider_info", lambda: {"available": True, "provider": "local" if local else "gemini",
                                                            "label": "x", "local": local})
    monkeypatch.setattr(llm_util, "llm_available", lambda: (True, "local" if local else "gemini"))
    monkeypatch.delenv("APPLYPILOT_CLOUD_LLM", raising=False)
    monkeypatch.setattr(llm_util, "get_llm_client", lambda *a, **k: object())
    monkeypatch.setattr(pipeline_tailor, "tailor_resume", _approved)
    monkeypatch.setattr(tailor, "_default_pdf", _fake_pdf)
    app = create_app(app_dir=tmp_path, root=tmp_path, profile={"personal": {"full_name": "Taylor Morgan"}})
    return TestClient(app), {"X-ApplyPilot-Token": app.state.token}


def test_endpoint_round_trip(tmp_path, monkeypatch):
    c, h = _client(tmp_path, monkeypatch)
    r = c.post("/resume/tailor", headers=h, json={"urls": ["https://example.com/careers/1"],
                                                   "page_text": "We are hiring a designer. " * 20})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    pdf = c.get(f"/resume/tailored/{tid}", headers=h)
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert 'filename="Taylor Morgan - Resume.pdf"' in pdf.headers["content-disposition"]
    assert c.get("/resume/tailored/nope", headers=h).status_code == 404


def test_endpoint_respects_the_cloud_gate(tmp_path, monkeypatch):
    c, h = _client(tmp_path, monkeypatch, local=False)
    r = c.post("/resume/tailor", headers=h, json={"urls": [], "page_text": "x" * 300})
    assert r.status_code == 403
