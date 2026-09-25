"""Answer memory: the applicant's own answers, saved on an explicit click."""
import json

import pytest
from fastapi.testclient import TestClient

from applypilot.extension import answer_memory, answers, resolve
from applypilot.extension.schema import FieldDescriptor, FillResult
from applypilot.extension.server import create_app, get_or_create_token


@pytest.mark.parametrize("q,a", [
    ("Are you open to relocation?", "Yes, within the US"),
    ("What is your notice period?", "Two weeks"),
    ("Which design tools do you use daily?", "Figma, FigJam, Framer"),
])
def test_ordinary_answers_are_learnable(q, a):
    assert answer_memory.learnable(q, a) == (True, "")


@pytest.mark.parametrize("q,a", [
    ("Are you legally authorized to work in the US?", "Yes"),   # canary
    ("Gender", "Female"),                                        # canary (EEO)
    ("Desired salary", "150000"),                                # canary
    ("Have you ever been convicted of a felony?", "No"),         # screening attestation
    ("Why do you want to work at Acme?", "Because ..."),         # company-directed
    ("Password", "hunter2"),                                     # credential
    ("Notice period", ""),                                       # empty
    ("", "something"),                                           # no question
])
def test_never_learned(q, a):
    ok, why = answer_memory.learnable(q, a)
    assert ok is False and why


def test_learn_dedupes_latest_wins_and_preserves_foreign_entries(tmp_path):
    bank = tmp_path / "answer_bank.json"
    bank.write_text(json.dumps([{"q": "Earlier pipeline question?", "a": "old"}]), encoding="utf-8")
    r1 = answer_memory.learn(bank, [{"question": "What is your notice period?", "answer": "Two weeks"}])
    r2 = answer_memory.learn(bank, [{"question": "What is your notice period? *", "answer": "One month"},
                                    {"question": "Gender", "answer": "Female"}])
    assert r1["saved"] == ["What is your notice period?"]
    assert r2["saved"] == ["What is your notice period? *"]
    assert [s["question"] for s in r2["skipped"]] == ["Gender"]
    data = json.loads(bank.read_text(encoding="utf-8"))
    notice = [e for e in data if "notice" in e["q"].lower()]
    assert len(notice) == 1 and notice[0]["a"] == "One month" and notice[0]["source"] == "you"
    assert {"q": "Earlier pipeline question?", "a": "old"} in data


def test_learned_answer_is_filled_next_time(tmp_path):
    bank = tmp_path / "answer_bank.json"
    answer_memory.learn(bank, [{"question": "What is your notice period?", "answer": "Two weeks"}])
    cache = answers.make_cache({}, bank)
    r = resolve.resolve_field(FieldDescriptor(id="n", label="What is your notice period?"), {},
                              answer_cache=cache)
    assert isinstance(r, FillResult) and r.value == "Two weeks" and r.source == "answer_bank"


def test_forget(tmp_path):
    bank = tmp_path / "answer_bank.json"
    answer_memory.learn(bank, [{"question": "What is your notice period?", "answer": "Two weeks"}])
    assert answer_memory.forget(bank, "what is your notice period") is True
    assert answer_memory.forget(bank, "what is your notice period") is False
    assert answer_memory.list_answers(bank) == []


def test_endpoints_round_trip(tmp_path):
    token = get_or_create_token(tmp_path)
    (tmp_path / "profile.json").write_text(json.dumps({"personal": {"full_name": "T"}}), encoding="utf-8")
    # root=tmp_path: without it the data root defaults to the REAL one and
    # these writes would land in the operator's own answer bank.
    client = TestClient(create_app(app_dir=tmp_path, root=tmp_path,
                                   profile={"personal": {"full_name": "T"}}))
    h = {"X-ApplyPilot-Token": token}
    assert client.post("/answers/learn", json={"items": []}).status_code in (401, 403)
    r = client.post("/answers/learn", headers=h, json={"items": [
        {"question": "What is your notice period?", "answer": "Two weeks"},
        {"question": "Are you legally authorized to work in the US?", "answer": "Yes"}]}).json()
    assert r["saved"] == ["What is your notice period?"] and len(r["skipped"]) == 1
    assert (tmp_path / "answer_bank.json").exists()  # written HERE, not in the real data dir
    listed = client.get("/answers", headers=h).json()["answers"]
    assert listed[0]["question"] == "What is your notice period?" and listed[0]["source"] == "you"
    assert client.post("/answers/forget", headers=h,
                       json={"question": "What is your notice period?"}).json() == {"forgotten": True}
    assert client.get("/answers", headers=h).json()["answers"] == []
