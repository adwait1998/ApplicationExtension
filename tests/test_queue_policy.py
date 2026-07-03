"""Task 12: single queue_policy() — the one SQL predicate for 'may this job
be applied to'. v2 rule: only gated-ELIGIBLE + auto-automatable rows are ever
queue-visible. These tests pin the fragment contract and the exclusion rules;
they also guard against the historical drift where the webui count overstated
what acquire_job actually picked (fit_score-only, no gate, no attempt cap)."""

from applypilot import database as db


def test_queue_policy_returns_fragment_and_params():
    frag, params = db.queue_policy(min_score=8, max_age_hours=24)
    assert "fit_score >= ?" in frag
    assert "gate_result = 'eligible'" in frag   # v2: only gated-eligible rows visible
    assert "automatability = 'auto'" in frag
    assert 8 in params


def test_queue_policy_excludes_ineligible(tmp_path):
    db.init_db(tmp_path / "q.db")
    conn = db.get_connection(tmp_path / "q.db")
    conn.executemany(
        "INSERT INTO jobs (url, fit_score, gate_result, automatability, gated_at, application_url, apply_status, applied_at) VALUES (?,?,?,?,?,?,?,?)",
        [("u1", 9, "eligible", "auto", "t", "https://boards.greenhouse.io/x/jobs/1", None, None),
         ("u2", 9, "ineligible", "manual", "t", "https://linkedin.com/jobs/2", None, None),
         ("u3", 9, "unknown", "auto", "t", "https://boards.greenhouse.io/x/jobs/3", None, None)],
    )
    conn.commit()
    frag, params = db.queue_policy(min_score=8)
    urls = {r["url"] for r in conn.execute(f"SELECT url FROM jobs WHERE {frag}", params).fetchall()}
    assert urls == {"u1"}   # ineligible + unknown excluded; only gated-eligible+auto


def test_queue_policy_attempt_cap_and_applied(tmp_path):
    db.init_db(tmp_path / "q2.db")
    conn = db.get_connection(tmp_path / "q2.db")
    from applypilot import config
    cap = config.DEFAULTS["max_apply_attempts"]
    conn.executemany(
        "INSERT INTO jobs (url, fit_score, gate_result, automatability, gated_at, apply_status, applied_at, apply_attempts) VALUES (?,?,?,?,?,?,?,?)",
        [("ok", 9, "eligible", "auto", "t", None, None, 0),
         ("maxed", 9, "eligible", "auto", "t", "failed", None, cap),   # attempts >= cap -> excluded
         ("done", 9, "eligible", "auto", "t", "applied", "2026-07-01", 0)],  # applied -> excluded
    )
    conn.commit()
    frag, params = db.queue_policy(min_score=8)
    urls = {r["url"] for r in conn.execute(f"SELECT url FROM jobs WHERE {frag}", params).fetchall()}
    assert urls == {"ok"}


def test_no_eligibility_predicate_selects_ineligible():
    # every queue predicate must exclude ineligible/ungated rows.
    frag, params = db.queue_policy(min_score=8)
    assert "gate_result = 'eligible'" in frag and "automatability = 'auto'" in frag
