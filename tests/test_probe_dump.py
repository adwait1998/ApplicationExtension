import json

from applypilot.apply import probe_dump as pd
from applypilot.apply.browser_stream import BrowserObservation, ControlObservation


def test_serialize_observation_shape(tmp_path):
    obs = BrowserObservation(
        url="https://jobs.ashbyhq.com/acme/app",
        controls=[
            ControlObservation(label="Name", control_type="text", selector="#name",
                               required=True, frame_index=0, frame_url="https://jobs.ashbyhq.com/acme/app"),
            ControlObservation(label="Location", role="combobox", control_type="text",
                               selector="div._select_abc", frame_index=0, frame_url="x"),
        ],
        submit_buttons=[ControlObservation(label="Submit Application", control_type="button", selector="button[type=submit]")],
        page_text_sample="Apply to Acme",
    )
    out = pd.serialize(obs, ats="ashby", url="https://jobs.ashbyhq.com/acme/app")
    assert out["ats"] == "ashby"
    assert out["counts"]["controls"] == 2
    labels = [c["label"] for c in out["controls"]]
    assert "Name" in labels and "Location" in labels
    # widget-kind HINT is included so the parser author sees what browser_stream saw
    assert all("control_type" in c and "role" in c and "selector" in c for c in out["controls"])
    assert out["submit_buttons"][0]["label"] == "Submit Application"


def test_dump_writes_json(tmp_path):
    obs = BrowserObservation(url="u", controls=[ControlObservation(label="Email", control_type="text")])
    path = pd.dump(obs, ats="lever", url="u", out_dir=tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ats"] == "lever" and data["counts"]["controls"] == 1
    assert path.parent == tmp_path
