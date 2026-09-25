"""A résumé for a different person must not silently replace the active
profile's identity (résumé files + drafted details)."""
import json

import pytest
from fastapi.testclient import TestClient

from applypilot.extension import resume_import
from applypilot.extension.server import create_app

OTHER = "Jordan Rivera\njordan@example.com | (555) 010-2000\n\nEXPERIENCE\nAcme — Engineer 2020-2024\n"
SAME = "Taylor Q. Morgan\ntaylor@example.com\n\nEXPERIENCE\nAcme — Designer 2020-2024\n"
HEADING = "RESUME\nTaylor Morgan\ntaylor@example.com\n"
PROFILE = {"personal": {"full_name": "Taylor Morgan"}}


def _import(tmp_path, text, profile=PROFILE, **kw):
    return resume_import.import_resume(filename="resume.txt", data=text.encode(), existing_profile=profile,
                                       profile_dir=tmp_path, llm_fn=lambda _t: "{}", **kw)


def test_other_persons_resume_is_refused_before_anything_is_written(tmp_path):
    with pytest.raises(resume_import.IdentityMismatch) as ei:
        _import(tmp_path, OTHER)
    assert ei.value.resume_name == "Jordan Rivera" and ei.value.profile_name == "Taylor Morgan"
    assert list(tmp_path.iterdir()) == []   # no resume.txt / resume.* written


def test_explicit_identity_change_is_allowed(tmp_path):
    r = _import(tmp_path, OTHER, allow_identity_change=True)
    assert (tmp_path / r.saved_filename).exists()


@pytest.mark.parametrize("text,profile", [
    (SAME, PROFILE),                         # middle initial
    (OTHER, {"personal": {"full_name": ""}}),  # first profile ever
    (OTHER, {}),
    (HEADING, PROFILE),                      # first line is a heading, not a name
])
def test_not_a_mismatch(tmp_path, text, profile):
    _import(tmp_path, text, profile=profile)


def test_endpoint_returns_structured_409_and_honors_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.llm.get_client", lambda: (_ for _ in ()).throw(RuntimeError("no llm")))
    (tmp_path / "profile.json").write_text(json.dumps(PROFILE), encoding="utf-8")
    app = create_app(app_dir=tmp_path, root=tmp_path)
    client = TestClient(app)
    h = {"X-ApplyPilot-Token": app.state.token}
    f = {"file": ("resume.txt", OTHER.encode(), "text/plain")}
    r = client.post("/profile/import-resume", headers=h, files=f)
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "identity_mismatch" and d["resume_name"] == "Jordan Rivera"
    assert not (tmp_path / "resume.txt").exists()
    r2 = client.post("/profile/import-resume", headers=h, files=f, data={"allow_identity_change": "true"})
    assert r2.status_code == 200 and (tmp_path / "resume.txt").exists()
