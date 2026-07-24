"""pending_score gate must score sponsorship-UNKNOWN jobs too, not only 'eligible'.

For a visa profile ~all postings gate to 'unknown' (sponsorship not stated ->
review), never 'eligible'. If pending_score requires gate_result='eligible' the
funnel is structurally starved: unknown jobs never get a fit_score -> never reach
score>=8 -> the operator-approve path (needs score>=8) has nothing to surface.
The fix widens the predicate to gate_result IN ('eligible','unknown'); 'ineligible'
(hard rejections) stay UNSCORED. The database predicate and the pipeline COUNT
mirror (_PENDING_SQL['score']) must stay in exact lockstep.
"""
from applypilot import database as db
from applypilot.pipeline import _PENDING_SQL


def _mk(tmp_path, name="t.db"):
    path = tmp_path / name
    db.init_db(path)
    return db.get_connection(path)


def test_unknown_gate_job_is_pending_score(tmp_path):
    conn = _mk(tmp_path)
    conn.execute(
        "INSERT INTO jobs (url, full_description, gated_at, gate_result) "
        "VALUES ('unk','desc','2026-07-02T00:00:00+00:00','unknown')"
    )
    conn.commit()
    urls = {r["url"] for r in db.get_jobs_by_stage(conn, "pending_score")}
    assert "unk" in urls


def test_eligible_gate_job_still_pending_score(tmp_path):
    conn = _mk(tmp_path)
    conn.execute(
        "INSERT INTO jobs (url, full_description, gated_at, gate_result) "
        "VALUES ('elig','desc','2026-07-02T00:00:00+00:00','eligible')"
    )
    conn.commit()
    urls = {r["url"] for r in db.get_jobs_by_stage(conn, "pending_score")}
    assert "elig" in urls


def test_ineligible_gate_job_never_pending_score(tmp_path):
    """Hard rejections (location/seniority/policy) stay unscored. Key invariant."""
    conn = _mk(tmp_path)
    conn.execute(
        "INSERT INTO jobs (url, full_description, gated_at, gate_result) "
        "VALUES ('inelig','desc','2026-07-02T00:00:00+00:00','ineligible')"
    )
    conn.commit()
    urls = {r["url"] for r in db.get_jobs_by_stage(conn, "pending_score")}
    assert "inelig" not in urls


def test_unknown_but_already_scored_not_pending(tmp_path):
    conn = _mk(tmp_path)
    conn.execute(
        "INSERT INTO jobs (url, full_description, gated_at, gate_result, fit_score) "
        "VALUES ('done','desc','2026-07-02T00:00:00+00:00','unknown',9)"
    )
    conn.commit()
    urls = {r["url"] for r in db.get_jobs_by_stage(conn, "pending_score")}
    assert "done" not in urls


def test_unknown_stale_gate_not_pending(tmp_path):
    """gated_at < detail_scraped_at (gate ran on thin desc) -> re-gate first."""
    conn = _mk(tmp_path)
    conn.execute(
        "INSERT INTO jobs (url, full_description, gated_at, gate_result, detail_scraped_at) "
        "VALUES ('stale','desc','2026-07-01T00:00:00+00:00','unknown','2026-07-02T00:00:00+00:00')"
    )
    conn.commit()
    urls = {r["url"] for r in db.get_jobs_by_stage(conn, "pending_score")}
    assert "stale" not in urls


def test_pipeline_count_mirror_matches_predicate(tmp_path):
    """Lockstep proof: pipeline _PENDING_SQL['score'] COUNT == number of rows
    get_jobs_by_stage('pending_score') returns, on a mixed-gate fixture."""
    conn = _mk(tmp_path)
    conn.executescript(
        """
        -- unknown eligible-shaped rows: pending_score
        INSERT INTO jobs (url, full_description, gated_at, gate_result)
            VALUES ('unk1','d','2026-07-02T00:00:00+00:00','unknown');
        INSERT INTO jobs (url, full_description, gated_at, gate_result)
            VALUES ('unk2','d','2026-07-02T00:00:00+00:00','unknown');
        -- eligible: pending_score
        INSERT INTO jobs (url, full_description, gated_at, gate_result)
            VALUES ('elig','d','2026-07-02T00:00:00+00:00','eligible');
        -- ineligible: NOT
        INSERT INTO jobs (url, full_description, gated_at, gate_result)
            VALUES ('bad','d','2026-07-02T00:00:00+00:00','ineligible');
        -- ungated legacy: NOT
        INSERT INTO jobs (url, full_description) VALUES ('legacy','d');
        -- unknown, no description: NOT
        INSERT INTO jobs (url, gated_at, gate_result)
            VALUES ('nodesc','2026-07-02T00:00:00+00:00','unknown');
        -- unknown, already scored: NOT
        INSERT INTO jobs (url, full_description, gated_at, gate_result, fit_score)
            VALUES ('scored','d','2026-07-02T00:00:00+00:00','unknown',7);
        -- unknown, stale gate: NOT
        INSERT INTO jobs (url, full_description, gated_at, gate_result, detail_scraped_at)
            VALUES ('stale','d','2026-07-01T00:00:00+00:00','unknown','2026-07-02T00:00:00+00:00');
        """
    )
    conn.commit()

    stage_rows = {r["url"] for r in db.get_jobs_by_stage(conn, "pending_score")}
    assert stage_rows == {"unk1", "unk2", "elig"}

    count = conn.execute(_PENDING_SQL["score"]).fetchone()[0]
    assert count == len(stage_rows), (
        f"pipeline count mirror ({count}) diverged from "
        f"get_jobs_by_stage predicate ({len(stage_rows)})"
    )
