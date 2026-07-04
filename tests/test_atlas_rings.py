from applypilot.discovery.atlas import rings


def test_recently_changed_board_is_hot():
    # posted something new in the last poll AND changed recently -> ring 0
    b = {"new_last_poll": 3, "job_count": 40, "last_changed": "2026-07-03T00:00:00Z"}
    assert rings.assign_ring(b, now="2026-07-03T02:00:00Z") == 0


def test_stale_but_active_board_is_warm():
    b = {"new_last_poll": 0, "job_count": 12, "last_changed": "2026-06-20T00:00:00Z"}
    assert rings.assign_ring(b, now="2026-07-03T00:00:00Z") == 1


def test_long_dormant_board_is_cold():
    b = {"new_last_poll": 0, "job_count": 0, "last_changed": "2026-01-01T00:00:00Z"}
    assert rings.assign_ring(b, now="2026-07-03T00:00:00Z") == 2


def test_never_changed_board_defaults_warm_not_cold():
    # freshly validated, never observed a change yet -> warm (give it a chance)
    b = {"new_last_poll": 0, "job_count": 5, "last_changed": None}
    assert rings.assign_ring(b, now="2026-07-03T00:00:00Z") == 1


def test_is_due_respects_ring_cadence():
    # hot board checked 45 min ago is due (cadence 30-60 min -> due at >=30m in v1)
    assert rings.is_due({"ring": 0, "last_checked": "2026-07-03T00:00:00Z"},
                        now="2026-07-03T00:45:00Z") is True
    # warm board checked 2h ago is NOT due (cadence >=6h)
    assert rings.is_due({"ring": 1, "last_checked": "2026-07-03T00:00:00Z"},
                        now="2026-07-03T02:00:00Z") is False
    # never-checked board is always due
    assert rings.is_due({"ring": 0, "last_checked": None}, now="2026-07-03T00:00:00Z") is True


def test_select_due_within_budget_prioritizes_hot():
    boards = [
        {"ats": "greenhouse", "token": "hot1", "ring": 0, "last_checked": None},
        {"ats": "greenhouse", "token": "hot2", "ring": 0, "last_checked": None},
        {"ats": "lever", "token": "warm1", "ring": 1, "last_checked": None},
    ]
    picked = rings.select_due(boards, now="2026-07-03T00:00:00Z", budget=2)
    assert len(picked) == 2
    assert {p["token"] for p in picked} == {"hot1", "hot2"}   # hot before warm under budget


def test_daily_request_estimate_within_politeness():
    # 5000 ring-0/1 boards, once each, is well under per-host budgets.
    est = rings.estimate_daily_requests(hot=1000, warm=4000)
    # hot polled ~24x/day (hourly), warm ~2x/day => ~24000 + 8000
    assert 25000 <= est <= 40000
