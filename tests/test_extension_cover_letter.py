"""Cover-letter drafts: job context from DB / public API / page text, the
pipeline's prompt + validator, and the grounding refusal."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from applypilot.extension import cover_letter, job_context, llm_util
from applypilot.extension.server import create_app

PROFILE = {"personal": {"full_name": "Taylor Morgan", "preferred_name": "Taylor"},
           "skills_boundary": {"tools": ["Figma", "Prototyping"]},
           "work_history": [{"title": "Product Designer", "company": "Example Labs", "start": "2021",
                             "description": "- Redesigned onboarding, +18% activation"}]}
JOB = {"title": "Senior Product Designer", "company": "Acme", "description": "Own the design of Acme's " * 20}
GOOD = ("Dear Hiring Manager,\n\nAt Example Labs I redesigned onboarding in Figma and lifted activation 18%.\n\n"
        "Happy to walk through any of this in more detail.\n\nTaylor")


@pytest.mark.parametrize("url,want", [
    ("https://job-boards.greenhouse.io/okx/jobs/7608993003", ("greenhouse", "okx", "7608993003")),
    ("https://boards.greenhouse.io/faire/jobs/8746116002?gh_jid=8746116002", ("greenhouse", "faire", "8746116002")),
    ("https://job-boards.greenhouse.io/embed/job_app?for=sofi&token=7990744003", ("greenhouse", "sofi", "7990744003")),
    ("https://jobs.lever.co/spotify/2193db3f-77c5-43b8-b030-8f92c9882bf1/apply",
     ("lever", "spotify", "2193db3f-77c5-43b8-b030-8f92c9882bf1")),
    ("https://jobs.ashbyhq.com/commure/260bc248-ed7d-4d10-bcc5-158139231a38/application",
     ("ashby", "commure", "260bc248-ed7d-4d10-bcc5-158139231a38")),
])
def test_parse_ats_url(url, want):
    p = job_context.parse_ats_url(url)
    assert (p["ats"], p["slug"], p["job_id"]) == want


def test_parse_workday_url_with_locale_and_apply():
    p = job_context.parse_ats_url(
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/"
        "Senior-Designer_JR1234/apply")
    assert p["ats"] == "workday" and p["slug"] == "nvidia" and p["site"] == "NVIDIAExternalCareerSite"
    assert p["job_path"] == "US-CA-Santa-Clara/Senior-Designer_JR1234"


def test_non_ats_url_is_not_parsed():
    assert job_context.parse_ats_url("https://example.com/careers/123") is None


def test_job_from_api_uses_the_public_posting():
    calls = []

    def fetch(url):
        calls.append(url)
        return {"title": "Designer", "content": "&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;", "company_name": "OKX"}

    job = job_context.job_from_api("https://job-boards.greenhouse.io/okx/jobs/1", fetch)
    assert calls == ["https://boards-api.greenhouse.io/v1/boards/okx/jobs/1"]
    assert job["title"] == "Designer" and job["company"] == "OKX" and "Build & ship" in job["description"]


def test_db_first_then_api_then_page_text(tmp_path):
    db = tmp_path / "applypilot.db"
    con = sqlite3.connect(db)
    con.execute("create table jobs (url text, application_url text, title text, site text, "
                "full_description text, description text)")
    con.execute("insert into jobs values (?,?,?,?,?,?)", ("https://x.test/j/1", "https://jobs.lever.co/acme/abc",
                                                          "DB Title", "acme", "From the DB " * 30, ""))
    con.commit()
    con.close()
    boom = lambda url: (_ for _ in ()).throw(AssertionError("API must not be called when the DB has it"))
    job = job_context.job_context(["https://jobs.lever.co/acme/abc/apply"], db_path=db, fetch=boom)
    assert job["source"] == "your jobs db" and job["title"] == "DB Title"
    job2 = job_context.job_context(["https://example.com/careers/1"], page_text="A long posting. " * 30,
                                   db_path=db, fetch=boom)
    assert job2["source"] == "page text"
    assert job_context.job_context(["https://example.com/careers/1"], page_text="short", db_path=db,
                                   fetch=boom) is None


def test_draft_uses_pipeline_prompt_and_passes_validation():
    seen = []
    out = cover_letter.draft_cover_letter(PROFILE, JOB, "", lambda m: seen.append(m) or GOOD)
    assert out["text"].startswith("Dear Hiring Manager") and len(seen) == 1
    assert "Taylor" in seen[0][0]["content"]          # the pipeline's sign-off instruction
    assert "Example Labs" in seen[0][1]["content"]    # built from the profile when no resume text


def test_draft_claiming_a_foreign_employer_is_refused():
    lie = GOOD.replace("At Example Labs I", "At Google I")
    with pytest.raises(cover_letter.CoverLetterError, match="Google"):
        cover_letter.draft_cover_letter(PROFILE, JOB, "", lambda m: lie)


def test_no_job_description_is_an_honest_error():
    with pytest.raises(cover_letter.CoverLetterError, match="description"):
        cover_letter.draft_cover_letter(PROFILE, {}, "", lambda m: GOOD)


def _client(tmp_path, monkeypatch, available=True):
    (tmp_path / "profile.json").write_text(json.dumps(PROFILE), encoding="utf-8")
    monkeypatch.setattr(llm_util, "llm_available", lambda: (available, "fake"))

    class Fake:
        def chat(self, messages, **kw):
            return GOOD

    monkeypatch.setattr(llm_util, "get_llm_client", lambda: Fake())
    app = create_app(app_dir=tmp_path, root=tmp_path, profile=PROFILE)
    return TestClient(app), {"X-ApplyPilot-Token": app.state.token}


def test_endpoint_drafts_from_page_text(tmp_path, monkeypatch):
    client, h = _client(tmp_path, monkeypatch)
    r = client.post("/cover-letter", headers=h, json={"urls": ["https://example.com/careers/1"],
                                                      "page_text": "We are hiring a designer. " * 20})
    assert r.status_code == 200
    body = r.json()
    assert body["draft"] is True and body["text"].startswith("Dear Hiring Manager")
    assert body["job"]["source"] == "page text"


def test_endpoint_without_a_model_says_so(tmp_path, monkeypatch):
    client, h = _client(tmp_path, monkeypatch, available=False)
    assert client.post("/cover-letter", headers=h, json={"urls": []}).status_code == 503


@pytest.mark.parametrize("text,flagged", [
    ("Dear Hiring Manager,\n\nAt Google I redesigned onboarding.", ["Google"]),
    ("As a senior designer at Meta, I led growth.", ["Meta"]),
    ("Stripe, where I led the checkout redesign, taught me a lot.", ["Stripe"]),
    ("I spent two years with Airbnb.", ["Airbnb"]),
    ("At Example Labs, I redesigned onboarding.", []),
    ("At Acme, I would bring my design systems work.", []),      # the company applied to
    ("At the end of the day, I care about users.", []),
])
def test_grounding_catches_cover_letter_claim_shapes(text, flagged):
    from applypilot.extension.grounding import find_unsupported_claims
    assert find_unsupported_claims(text, PROFILE, target_company="Acme") == flagged
