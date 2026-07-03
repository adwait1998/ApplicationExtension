import json
from applypilot import database as db
from applypilot.gate.engine import gate_job

PROFILE = {"geo": {"remote_ok": True, "remote_scope": "US", "onsite_regions": ["us-ca"]},
           "seniority": {"accept_bands": ["senior"], "ic_only": True},
           "needs_sponsorship": False, "workday_accounts": []}

def test_store_gated_persists_verdict(tmp_path):
    db.init_db(tmp_path / "t.db")
    conn = db.get_connection(tmp_path / "t.db")
    job = {"url": "https://boards.greenhouse.io/chime/jobs/1", "title": "Senior Product Designer",
           "location": "Remote - US", "full_description": "design", "site": "Chime (greenhouse)",
           "application_url": "https://boards.greenhouse.io/chime/jobs/1"}
    g = gate_job(job, PROFILE)
    assert db.store_gated(conn, job, g, strategy="ats_api:greenhouse") is True
    row = dict(conn.execute("SELECT gate_result, identity_id, gate_reasons, automatability FROM jobs").fetchone())
    assert row["gate_result"] == "eligible"
    assert row["identity_id"] == "greenhouse:chime:1"
    assert json.loads(row["gate_reasons"])[0]["rule"]
    assert row["automatability"] == "auto"

def test_store_gated_dedups_on_url(tmp_path):
    db.init_db(tmp_path / "t1.db")
    conn = db.get_connection(tmp_path / "t1.db")
    job = {"url": "u1", "title": "Senior Product Designer", "location": "Remote - US"}
    g = gate_job(job, PROFILE)
    assert db.store_gated(conn, job, g, strategy="s") is True
    assert db.store_gated(conn, job, g, strategy="s") is False  # IntegrityError -> False

def test_update_gate_restamps(tmp_path):
    db.init_db(tmp_path / "t2.db")
    conn = db.get_connection(tmp_path / "t2.db")
    conn.execute("INSERT INTO jobs (url, title, location, full_description) VALUES ('u2','Design Manager','Remote - US','x')")
    conn.commit()
    row = dict(conn.execute("SELECT * FROM jobs WHERE url='u2'").fetchone())
    db.update_gate(conn, "u2", gate_job(row, PROFILE))
    got = dict(conn.execute("SELECT gate_result, gate_version FROM jobs WHERE url='u2'").fetchone())
    assert got["gate_result"] == "ineligible" and got["gate_version"] == 1

def test_only_gated_eligible_jobs_are_pending_score(tmp_path):
    db.init_db(tmp_path / "t3.db")
    conn = db.get_connection(tmp_path / "t3.db")
    # ungated legacy row: NOT pending_score
    conn.execute("INSERT INTO jobs (url, full_description) VALUES ('legacy','desc')")
    # gated ineligible: NOT pending_score
    conn.execute("INSERT INTO jobs (url, full_description, gated_at, gate_result) VALUES ('bad','d','t','ineligible')")
    # gated eligible: IS pending_score
    conn.execute("INSERT INTO jobs (url, full_description, gated_at, gate_result) VALUES ('good','d','2026-07-02T00:00:00+00:00','eligible')")
    conn.commit()
    urls = {r["url"] for r in db.get_jobs_by_stage(conn, "pending_score")}
    assert urls == {"good"}

def test_gate_job_tolerates_non_string_fields():
    from applypilot.gate.engine import gate_job
    r = gate_job({"url": "u", "title": 3.5, "location": None, "salary": 12}, PROFILE)
    assert r["gate_result"] in ("eligible", "ineligible", "unknown")  # no crash

def test_store_gated_respects_explicit_none_full_description(tmp_path):
    db.init_db(tmp_path / "t5.db")
    conn = db.get_connection(tmp_path / "t5.db")
    job = {"url": "sx1", "title": "Senior Product Designer", "location": "Remote - US",
           "description": "thin snippet", "full_description": None}
    from applypilot.gate.engine import gate_job as gj
    db.store_gated(conn, job, gj(job, PROFILE), strategy="smart_extract")
    row = conn.execute("SELECT full_description FROM jobs WHERE url='sx1'").fetchone()
    assert row["full_description"] is None   # must await enrichment, not score on the snippet

def test_pending_gate_includes_ungated_and_stale_gated(tmp_path):
    db.init_db(tmp_path / "t4.db")
    conn = db.get_connection(tmp_path / "t4.db")
    # ungated -> pending_gate
    conn.execute("INSERT INTO jobs (url, full_description) VALUES ('a','d')")
    # gated BEFORE enrichment finished (detail_scraped_at > gated_at) -> re-gate
    conn.execute("INSERT INTO jobs (url, full_description, gated_at, detail_scraped_at, gate_result) "
                 "VALUES ('b','d','2026-07-01T00:00:00+00:00','2026-07-02T00:00:00+00:00','eligible')")
    # freshly gated after enrich -> NOT pending_gate
    conn.execute("INSERT INTO jobs (url, full_description, gated_at, detail_scraped_at, gate_result) "
                 "VALUES ('c','d','2026-07-02T01:00:00+00:00','2026-07-02T00:00:00+00:00','eligible')")
    conn.commit()
    urls = {r["url"] for r in db.get_jobs_by_stage(conn, "pending_gate")}
    assert urls == {"a", "b"}


def test_launcher_ident_matches_gate_identity_for_aggregator_rows():
    """Important-3 regression: the launcher's runtime identity must key off the
    EFFECTIVE apply URL (application_url or url), matching gate_job's stored
    identity_id — so aggregator rows (url != application_url) fold consistently
    across the broker/ledger/gate. For ats_boards (url == application_url) both
    already agree; this locks the aggregator case."""
    from applypilot.identity import identity_id
    from applypilot.gate.engine import gate_job
    # aggregator listing url differs from the resolved greenhouse application_url
    job = {"url": "https://www.linkedin.com/jobs/view/999",
           "application_url": "https://boards.greenhouse.io/chime/jobs/8141068002",
           "title": "Senior Product Designer", "location": "Remote - US", "site": "Chime"}
    gate = gate_job(job, PROFILE)
    # gate stores identity from application_url; launcher now computes the same basis
    effective = job.get("application_url") or job["url"]
    launcher_ident = identity_id(effective, company=job.get("site"),
                                 title=job.get("title"), location=job.get("location"))
    assert gate["identity_id"] == launcher_ident == "greenhouse:chime:8141068002"
