from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from applypilot import database
from applypilot.discovery import theirstack


def test_theirstack_payload_uses_queries_locations_and_freshness():
    cfg = {
        "country": "USA",
        "tiers": [1],
        "queries": [
            {"query": "software engineer", "tier": 1},
            {"query": "product manager", "tier": 2},
        ],
        "locations": [
            {"location": "San Francisco, CA", "remote": False},
            {"location": "Remote", "remote": True},
        ],
        "defaults": {"hours_old": 24},
    }

    payloads = theirstack._payloads_for_run(cfg, {}, limit=25, hours_old=24)

    assert len(payloads) == 2
    local, remote = payloads
    assert local["job_title_or"] == ["software engineer"]
    assert local["job_country_code_or"] == ["US"]
    assert local["job_location_pattern_or"] == ["San Francisco"]
    assert local["remote"] is False
    assert local["posted_at_max_age_days"] == 1
    assert remote["remote"] is True


def test_run_theirstack_discovery_filters_and_stores_jobs(tmp_path, monkeypatch):
    db_path = tmp_path / "applypilot.db"
    database.close_connection(db_path)
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(theirstack.config, "load_search_config", lambda: {
        "country": "USA",
        "queries": [{"query": "software engineer", "tier": 1}],
        "locations": [{"location": "Remote", "remote": True}],
        "defaults": {"hours_old": 24, "results_per_site": 2},
        "title_include": ["software engineer"],
        "exclude_titles": ["product manager"],
        "theirstack": {"max_results": 2, "limit": 2},
    })
    monkeypatch.setenv("THEIRSTACK_API_KEY", "test-token")
    today = datetime.now(timezone.utc).date().isoformat()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["remote"] is True
        assert payload["limit"] == 2
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "job_title": "Software Engineer",
                        "company": "Acme",
                        "final_url": "https://jobs.acme.test/1",
                        "description": "Build reliable backend systems.",
                        "location": "United States",
                        "remote": True,
                            "date_posted": today,
                        "salary_string": "$100,000 - $120,000",
                    },
                    {
                        "job_title": "Product Manager",
                        "company": "Acme",
                        "final_url": "https://jobs.acme.test/2",
                        "description": "Roadmaps.",
                        "location": "United States",
                        "remote": True,
                            "date_posted": today,
                    },
                ],
                "metadata": {"total_results": None},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    result = theirstack.run_theirstack_discovery(client=client)

    assert result["status"] == "ok"
    assert result["total"] == 2
    assert result["filtered"] == 1
    assert result["new"] == 1

    conn = database.get_connection(db_path)
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["url"] == "https://jobs.acme.test/1"
    assert row["application_url"] == "https://jobs.acme.test/1"
    assert row["site"] == "Acme (TheirStack)"
    assert row["strategy"] == "theirstack_api"
    assert row["full_description"] == "Build reliable backend systems."
    assert row["detail_scraped_at"] is not None
