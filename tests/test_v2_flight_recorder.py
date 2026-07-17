import json

from applypilot.apply.v2 import flight_recorder as fr
from applypilot.apply.v2 import ir


def test_bundle_roundtrips_ir_provenance_dom_timings(tmp_path):
    rec = fr.FlightRecorder(run_dir=tmp_path, job_url="https://boards.greenhouse.io/acme/jobs/1",
                            ats="greenhouse", company="acme")
    rec.set_schema(ir.FormSchema("greenhouse", "acme", "u",
                                 [ir.Step(0, [], terminal=True)]))
    rec.record_field(field_fp="fp1", semantic_key="email", provenance="profile.personal.email",
                     driver="text", committed=True, locator_tier="label")
    rec.record_network("POST", "https://boards.greenhouse.io/acme/applications", 201)
    rec.record_phase("parse", 900); rec.record_phase("fill", 18000)
    rec.set_dom("<html><body><form>...captured real DOM...</form></body></html>")
    path = rec.commit(status="applied")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == "applied" and data["ats"] == "greenhouse"
    assert data["fields"][0]["provenance"] == "profile.personal.email"
    assert data["network"][0]["status"] == 201
    assert data["phases"]["parse"] == 900
    assert data["dom_html"].startswith("<html")          # real DOM captured
