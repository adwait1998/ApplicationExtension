"""$0 tests for the expired-link pruner (freshness.py)."""

from __future__ import annotations

import sqlite3

import pytest

from applypilot.freshness import (
    EXPIRED,
    LIVE,
    UNKNOWN,
    check_queue,
    classify_liveness,
    select_queue_urls,
)

# ---------------------------------------------------------------------------
# classify_liveness — pure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (404, "", EXPIRED),
        (410, "anything", EXPIRED),
        (None, None, UNKNOWN),  # network error
        (403, "access denied", UNKNOWN),  # bot wall — never park
        (500, "oops", UNKNOWN),
        (429, "slow down", UNKNOWN),
        # Greenhouse closed posting
        (200, "<h1>The job you're looking for is no longer open.</h1>", EXPIRED),
        # Lever closed posting
        (200, "This job is no longer accepting applications", EXPIRED),
        # Ashby
        (200, "<div>Job not found</div>", EXPIRED),
        # Live Greenhouse form
        (200, '<form><input type="file"><button>Submit application</button></form>', LIVE),
        # Live form that ALSO contains a generic "no longer" footer → live wins
        (
            200,
            '<button>Submit application</button> <footer>positions no longer accepting applications appear here</footer>',
            LIVE,
        ),
        # JS shell with neither marker → don't guess
        (200, "<div id='root'></div>", UNKNOWN),
        (200, "", UNKNOWN),
    ],
)
def test_classify_liveness(status, body, expected):
    assert classify_liveness(status, body) == expected


# ---------------------------------------------------------------------------
# check_queue — DB integration with injected fetcher
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE jobs (
            url TEXT PRIMARY KEY, application_url TEXT, fit_score INTEGER,
            apply_status TEXT, apply_error TEXT, applied_at TEXT, discovered_at TEXT
        )
        """
    )
    rows = [
        ("j1", "https://x.test/live", 9, None, None, None, "2026-06-01"),
        ("j2", "https://x.test/dead", 8, None, None, None, "2026-05-01"),
        ("j3", "https://x.test/unknown", 8, "failed", None, None, "2026-05-01"),
        ("j4", "https://x.test/already-applied", 9, "applied", None, "2026-05-20", "2026-05-01"),
        ("j5", None, 9, None, None, None, "2026-05-01"),  # no URL → not selected
        ("j6", "https://x.test/lowscore", 3, None, None, None, "2026-05-01"),
    ]
    conn.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn


def fake_fetch(url):
    if url.endswith("/live"):
        return 200, "<button>Submit application</button>"
    if url.endswith("/dead"):
        return 200, "The job you're looking for is no longer open."
    return 403, "bot wall"


def test_select_queue_urls_respects_apply_gate(db):
    urls = {u for u, _ in select_queue_urls(db, min_score=7)}
    assert urls == {"j1", "j2", "j3"}  # applied / no-url / low-score excluded


def test_check_queue_parks_only_confident_expired(db):
    result = check_queue(db, min_score=7, fetch_fn=fake_fetch)
    assert (result.checked, result.expired, result.live, result.unknown) == (3, 1, 1, 1)
    status, error = db.execute("SELECT apply_status, apply_error FROM jobs WHERE url='j2'").fetchone()
    assert status == "expired"
    assert error.startswith("link_expired_precheck")
    # live + unknown rows untouched
    assert db.execute("SELECT apply_status FROM jobs WHERE url='j1'").fetchone()[0] is None
    assert db.execute("SELECT apply_status FROM jobs WHERE url='j3'").fetchone()[0] == "failed"


def test_check_queue_dry_run_writes_nothing(db):
    result = check_queue(db, min_score=7, dry_run=True, fetch_fn=fake_fetch)
    assert result.expired == 1
    assert db.execute("SELECT apply_status FROM jobs WHERE url='j2'").fetchone()[0] is None


def test_check_queue_is_reversible(db):
    check_queue(db, min_score=7, fetch_fn=fake_fetch)
    db.execute(
        "UPDATE jobs SET apply_status=NULL, apply_error=NULL WHERE apply_error LIKE 'link_expired_precheck%'"
    )
    assert db.execute("SELECT apply_status FROM jobs WHERE url='j2'").fetchone()[0] is None


def test_check_queue_empty_queue():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE jobs (url TEXT, application_url TEXT, fit_score INTEGER, apply_status TEXT, apply_error TEXT, applied_at TEXT, discovered_at TEXT)")
    result = check_queue(conn, fetch_fn=fake_fetch)
    assert result.checked == 0
