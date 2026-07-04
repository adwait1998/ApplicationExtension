# tests/test_v2_mapping_cache.py
from applypilot import database as db
from applypilot.apply.v2 import mapping_cache as mc


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


def test_put_then_get_active_binding(tmp_path):
    conn = _conn(tmp_path)
    mc.put(conn, "greenhouse", "fp1", binding="profile.personal.email",
           widget_driver="text", locator_tier="label")
    row = mc.get(conn, "greenhouse", "fp1")
    assert row["binding"] == "profile.personal.email"
    assert row["widget_driver"] == "text" and row["locator_tier"] == "label"
    assert row["active"] == 1 and row["fail_streak"] == 0 and row["version"] == 1


def test_put_never_stores_literal_value(tmp_path):
    # Contract: only bindings. A literal-looking binding is still just a string,
    # but the repo API has no 'value' param at all — enforced structurally.
    conn = _conn(tmp_path)
    mc.put(conn, "greenhouse", "fp2", binding="answer:qfpXYZ", widget_driver="textarea")
    assert "value" not in mc.get(conn, "greenhouse", "fp2")   # column does not exist


def test_hit_increments_and_stays_active(tmp_path):
    conn = _conn(tmp_path)
    mc.put(conn, "greenhouse", "fp1", binding="profile.personal.phone", widget_driver="phone_intl")
    mc.record_success(conn, "greenhouse", "fp1")
    mc.record_success(conn, "greenhouse", "fp1")
    row = mc.get(conn, "greenhouse", "fp1")
    assert row["hits"] == 2 and row["fail_streak"] == 0 and row["active"] == 1


def test_demote_after_two_verified_failures_never_deletes(tmp_path):
    conn = _conn(tmp_path)
    mc.put(conn, "greenhouse", "fp1", binding="profile.personal.city", widget_driver="typeahead_location")
    mc.record_failure(conn, "greenhouse", "fp1")
    assert mc.get(conn, "greenhouse", "fp1")["active"] == 1     # one failure: still active
    mc.record_failure(conn, "greenhouse", "fp1")
    row = mc.get(conn, "greenhouse", "fp1")
    assert row["active"] == 0 and row["fail_streak"] == 2       # demoted, NOT deleted
    # get_active returns None for a demoted mapping (resolver re-resolves)
    assert mc.get_active(conn, "greenhouse", "fp1") is None
    assert mc.get(conn, "greenhouse", "fp1") is not None        # row survives (audit)


def test_reput_bumps_version_and_reactivates(tmp_path):
    conn = _conn(tmp_path)
    mc.put(conn, "greenhouse", "fp1", binding="profile.a", widget_driver="text")
    mc.record_failure(conn, "greenhouse", "fp1"); mc.record_failure(conn, "greenhouse", "fp1")
    assert mc.get(conn, "greenhouse", "fp1")["active"] == 0
    mc.put(conn, "greenhouse", "fp1", binding="profile.b", widget_driver="text")  # re-resolved
    row = mc.get(conn, "greenhouse", "fp1")
    assert row["binding"] == "profile.b" and row["version"] == 2
    assert row["active"] == 1 and row["fail_streak"] == 0       # revived on re-resolution


def test_submit_endpoint_harvest_upsert(tmp_path):
    conn = _conn(tmp_path)
    mc.record_submit_endpoint(conn, "greenhouse", "acme", "POST",
                              "boards.greenhouse.io/acme/applications")
    mc.record_submit_endpoint(conn, "greenhouse", "acme", "POST",
                              "boards.greenhouse.io/acme/applications")
    eps = mc.get_submit_endpoints(conn, "greenhouse", "acme")
    assert len(eps) == 1 and eps[0]["seen_count"] == 2         # upsert, count bumps


def test_hit_rate_measurable(tmp_path):
    # invariant 10 / risk "fingerprint hit-rate overstated": hit rate is queryable.
    conn = _conn(tmp_path)
    mc.put(conn, "greenhouse", "fp1", binding="profile.a", widget_driver="text")
    mc.record_success(conn, "greenhouse", "fp1")
    stats = mc.stats(conn, "greenhouse")
    assert stats["active_mappings"] == 1 and stats["total_hits"] == 1
