from applypilot.apply.browser_stream import should_block_request


def test_blocks_post_to_ats_without_ticket():
    assert should_block_request("POST", "https://boards.greenhouse.io/chime/jobs/1", ticket_open=False, dry_run=False) is True


def test_allows_post_with_open_ticket():
    assert should_block_request("POST", "https://boards.greenhouse.io/chime/jobs/1", ticket_open=True, dry_run=False) is False


def test_dry_run_blocks_even_with_ticket():
    assert should_block_request("POST", "https://jobs.lever.co/x/y/apply", ticket_open=True, dry_run=True) is True


def test_allows_get_always():
    assert should_block_request("GET", "https://boards.greenhouse.io/chime/jobs/1", ticket_open=False, dry_run=False) is False


def test_ignores_non_ats_hosts_live():
    assert should_block_request("POST", "https://analytics.example.com/track", ticket_open=False, dry_run=False) is False


def test_ashby_graphql_submit_blocked():
    assert should_block_request("POST", "https://jobs.ashbyhq.com/api/non-user-graphql", ticket_open=False, dry_run=False) is True


def test_dry_run_fails_closed_on_unknown_host():
    # vanity/embedded/unknown ATS submit (the Twilio incident class) must be blocked in dry-run
    assert should_block_request("POST", "https://careers.airbnb.com/positions/apply", ticket_open=True, dry_run=True) is True
    assert should_block_request("POST", "https://apply.someats.io/submit", ticket_open=True, dry_run=True) is True


def test_dry_run_allows_safe_analytics_host():
    assert should_block_request("POST", "https://www.google-analytics.com/collect", ticket_open=True, dry_run=True) is False


def test_workday_mutation_blocked_without_ticket():
    assert should_block_request("POST", "https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/x/submit", ticket_open=False, dry_run=False) is True
