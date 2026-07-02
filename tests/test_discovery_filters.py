from __future__ import annotations

from datetime import datetime, timedelta, timezone

from applypilot.discovery.freshness import iso_or_none, is_recent, parse_posted_at
from applypilot.discovery.jobspy import _clean_url
from applypilot.discovery.title_filter import build_title_filter, title_matches


def test_title_filter_keeps_target_roles_and_rejects_adjacent_noise():
    include, exclude = build_title_filter({
        "title_include": ["product designer", "ux designer", "ux researcher"],
        "exclude_titles": ["product manager", "design manager", "intern"],
    })

    assert title_matches("Senior Product Designer, AI", include, exclude)
    assert title_matches("UX Researcher", include, exclude)
    assert not title_matches("Senior Product Manager, Growth", include, exclude)
    assert not title_matches("Product Design Manager", include, exclude)
    assert not title_matches("Product Design Intern", include, exclude)


def test_short_title_terms_use_word_boundaries():
    include, exclude = build_title_filter({"title_include": ["ui", "ux"]})

    assert title_matches("Senior UI Designer", include, exclude)
    assert title_matches("UX Designer", include, exclude)
    assert not title_matches("Build Systems Designer", include, exclude)


def test_freshness_parses_board_dates_and_filters_old_posts():
    now = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    fresh = now - timedelta(hours=23)
    old = now - timedelta(hours=25)

    assert is_recent(fresh.isoformat(), 24, now=now)
    assert not is_recent(old.isoformat(), 24, now=now)

    lever_ms = int(fresh.timestamp() * 1000)
    assert parse_posted_at(lever_ms) == fresh
    assert iso_or_none(lever_ms) == fresh.isoformat()


def test_jobspy_clean_url_rejects_stringified_nulls():
    assert _clean_url(None) is None
    assert _clean_url("None") is None
    assert _clean_url("nan") is None
    assert _clean_url("javascript:void(0)") is None
    assert _clean_url("https://boards.greenhouse.io/acme/jobs/1") == "https://boards.greenhouse.io/acme/jobs/1"
