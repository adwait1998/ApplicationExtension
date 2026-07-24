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
    is_greenhouse_expired_redirect,
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
# Greenhouse soft-302 — the board-index redirect body markers miss
# ---------------------------------------------------------------------------

_GH_JOB = "https://job-boards.greenhouse.io/twilio/jobs/7985610"
# The board index is a normal 200 with NEITHER an expired phrase nor a live form
# marker — exactly what fooled the pre-check into "unknown" (or a live guess).
_BOARD_BODY = "<html><body><div class='job-search'>Search filters</div></body></html>"


def test_classify_liveness_greenhouse_soft302_error_true_is_expired():
    # Expired GH req: /jobs/<id> 302s to the board root carrying ?error=true.
    assert (
        classify_liveness(
            200,
            _BOARD_BODY,
            url=_GH_JOB,
            final_url="https://job-boards.greenhouse.io/twilio?error=true",
        )
        == EXPIRED
    )


def test_classify_liveness_greenhouse_soft302_board_root_is_expired():
    # Same expiry, no error param: the link simply left /jobs/<id> for the board.
    assert (
        classify_liveness(
            200,
            _BOARD_BODY,
            url=_GH_JOB,
            final_url="https://job-boards.greenhouse.io/twilio",
        )
        == EXPIRED
    )


def test_classify_liveness_greenhouse_live_job_page_unchanged():
    # A live req keeps its /jobs/<id> URL — never falsely expired. Body markers
    # still decide live vs unknown as before.
    live_body = '<form><input type="file"><button>Submit application</button></form>'
    assert classify_liveness(200, live_body, url=_GH_JOB, final_url=_GH_JOB) == LIVE
    assert classify_liveness(200, _BOARD_BODY, url=_GH_JOB, final_url=_GH_JOB) == UNKNOWN


def test_classify_liveness_non_greenhouse_redirect_unchanged():
    # A non-Greenhouse link that redirects is out of scope: fall back to the
    # body-marker verdict (here: unknown), never a URL-shape 'expired'.
    assert (
        classify_liveness(
            200,
            _BOARD_BODY,
            url="https://careers.example.com/apply/123",
            final_url="https://careers.example.com/home",
        )
        == UNKNOWN
    )


def test_classify_liveness_vanity_ghjid_not_false_expired():
    # Vanity careers host carrying ?gh_jid=<id> that did NOT redirect is live-shaped:
    # must not be flagged expired just because the landing host isn't greenhouse.io.
    vanity = "https://careers.example.com/job?gh_jid=123"
    live_body = "<button>Apply for this job</button>"
    assert classify_liveness(200, live_body, url=vanity, final_url=vanity) == LIVE


def test_classify_liveness_no_urls_is_backcompat():
    # The pure 2-arg call (no url/final_url) behaves exactly as before.
    assert classify_liveness(200, _BOARD_BODY) == UNKNOWN
    assert classify_liveness(404, "") == EXPIRED


@pytest.mark.parametrize(
    "intended,landed,expected",
    [
        # error=true on a greenhouse landing host -> expired
        (_GH_JOB, "https://job-boards.greenhouse.io/twilio?error=true", True),
        # left /jobs/<id> for the board root -> expired
        (_GH_JOB, "https://job-boards.greenhouse.io/twilio", True),
        # still on /jobs/<id> -> live
        (_GH_JOB, _GH_JOB, False),
        # older boards.greenhouse.io host, same signature -> expired
        ("https://boards.greenhouse.io/acme/jobs/42",
         "https://boards.greenhouse.io/acme?error=true", True),
        # vanity gh_jid embed, no redirect -> not expired
        ("https://careers.example.com/job?gh_jid=123",
         "https://careers.example.com/job?gh_jid=123", False),
        # non-greenhouse intent that redirects -> out of scope, not expired
        ("https://careers.example.com/apply/1", "https://careers.example.com/home", False),
        # error=true but NON-greenhouse landing host -> not trusted
        ("https://careers.example.com/job?gh_jid=9", "https://evil.example.com/x?error=true", False),
        # missing landed url -> not expired (conservative)
        (_GH_JOB, None, False),
        (None, "https://job-boards.greenhouse.io/twilio?error=true", False),
    ],
)
def test_is_greenhouse_expired_redirect(intended, landed, expected):
    assert is_greenhouse_expired_redirect(intended, landed) is expected


# ---------------------------------------------------------------------------
# check_queue with a 3-tuple fetcher (status, body, final_url)
# ---------------------------------------------------------------------------


def test_check_queue_parks_greenhouse_soft302(db):
    # A fetch_fn returning the newer (status, body, final_url) triple lets the
    # pruner catch a GH soft-302 the body alone can't — and the 2-tuple fetcher
    # in the other tests still works (back-compat).
    db.execute(
        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
        ("jgh", "https://job-boards.greenhouse.io/twilio/jobs/7985610", 9,
         None, None, None, "2026-06-01"),
    )
    db.commit()

    def fetch3(url):
        if "greenhouse.io" in url:
            return 200, _BOARD_BODY, "https://job-boards.greenhouse.io/twilio?error=true"
        if url.endswith("/live"):
            return 200, "<button>Submit application</button>", url
        if url.endswith("/dead"):
            return 200, "The job you're looking for is no longer open.", url
        return 403, "bot wall", url

    result = check_queue(db, min_score=7, fetch_fn=fetch3)
    status, error = db.execute(
        "SELECT apply_status, apply_error FROM jobs WHERE url='jgh'"
    ).fetchone()
    assert status == "expired"
    assert error.startswith("link_expired_precheck")
    # the GH row is counted among expired alongside the /dead body-marker row.
    assert result.expired == 2


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
