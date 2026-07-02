from __future__ import annotations

from datetime import datetime, timezone
import base64

import pytest
import yaml

from applypilot.discovery import ats_discovery as D


def test_extract_board_from_url_known_ats_hosts():
    cases = [
        ("https://boards.greenhouse.io/figma/jobs/123", ("greenhouse", "figma")),
        ("https://job-boards.greenhouse.io/openai/jobs/123", ("greenhouse", "openai")),
        ("https://boards-api.greenhouse.io/v1/boards/stripe/jobs", ("greenhouse", "stripe")),
        ("https://jobs.lever.co/plaid/abc", ("lever", "plaid")),
        ("https://api.lever.co/v0/postings/spotify?mode=json", ("lever", "spotify")),
        ("https://jobs.ashbyhq.com/linear/abc", ("ashby", "linear")),
        ("https://api.ashbyhq.com/posting-api/job-board/ramp", ("ashby", "ramp")),
    ]

    for url, expected in cases:
        candidate = D.extract_board_from_url(url)
        assert candidate is not None
        assert (candidate.ats, candidate.token) == expected


def test_normalize_search_href_decodes_bing_redirect():
    target = "https://jobs.ashbyhq.com/deepgram/abc"
    encoded = "a1" + base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")

    assert D._normalize_search_href(f"/ck/a?u={encoded}") == target


def test_load_ats_registry_merges_user_overlay(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    app_dir = tmp_path / "app"
    package_dir.mkdir()
    app_dir.mkdir()
    (package_dir / "ats_companies.yaml").write_text(
        yaml.safe_dump({"greenhouse": ["figma"], "lever": ["plaid"], "ashby": []}),
        encoding="utf-8",
    )
    user_path = app_dir / "ats_companies.yaml"
    user_path.write_text(
        yaml.safe_dump({"greenhouse": ["figma", "stripe"], "ashby": ["linear"]}),
        encoding="utf-8",
    )

    monkeypatch.setattr(D.config, "CONFIG_DIR", package_dir)
    monkeypatch.setattr(D, "USER_REGISTRY_PATH", user_path)

    registry = D.load_ats_registry()

    assert registry["greenhouse"] == ["figma", "stripe"]
    assert registry["lever"] == ["plaid"]
    assert registry["ashby"] == ["linear"]


def test_validate_jobs_requires_matching_titles_and_locations(monkeypatch):
    monkeypatch.setattr(D, "load_location_filter", lambda: (["united states", "remote"], ["canada"]))
    candidate = D.BoardCandidate("greenhouse", "acme", "test")
    jobs = [
        {
            "title": "Senior Product Designer",
            "location": "Remote, United States",
            "posted_at": datetime.now(timezone.utc).isoformat(),
        },
        {
            "title": "Product Manager",
            "location": "Remote, United States",
            "posted_at": datetime.now(timezone.utc).isoformat(),
        },
        {
            "title": "UX Designer",
            "location": "Toronto, Canada",
            "posted_at": datetime.now(timezone.utc).isoformat(),
        },
    ]

    board = D._validate_jobs(
        candidate,
        jobs,
        title_include=["product designer", "ux designer"],
        title_exclude=["product manager"],
        hours_old=24,
        min_matching_jobs=1,
    )

    assert board is not None
    assert board.matching_jobs == 1
    assert board.fresh_matching_jobs == 1
    assert board.sample_titles == ("Senior Product Designer",)


def test_validate_jobs_rejects_board_below_threshold(monkeypatch):
    monkeypatch.setattr(D, "load_location_filter", lambda: (["remote"], []))
    candidate = D.BoardCandidate("greenhouse", "acme", "test")

    board = D._validate_jobs(
        candidate,
        [{"title": "Product Manager", "location": "Remote", "posted_at": None}],
        title_include=["product designer"],
        title_exclude=["product manager"],
        hours_old=24,
        min_matching_jobs=1,
    )

    assert board is None
