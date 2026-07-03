from applypilot.apply.submit_broker import SubmitBroker


def test_no_ticket_means_no_submit(tmp_path):
    b = SubmitBroker(tmp_path / "ticket.json")
    assert b.ticket_open("greenhouse:chime:1") is False


def test_issue_then_open(tmp_path):
    b = SubmitBroker(tmp_path / "ticket.json")
    b.issue("greenhouse:chime:1")
    assert b.ticket_open("greenhouse:chime:1") is True
    assert b.ticket_open("greenhouse:other:2") is False   # scoped to identity


def test_consume_is_one_shot(tmp_path):
    b = SubmitBroker(tmp_path / "ticket.json")
    b.issue("greenhouse:chime:1")
    assert b.consume("greenhouse:chime:1") is True
    assert b.ticket_open("greenhouse:chime:1") is False   # consumed
    assert b.consume("greenhouse:chime:1") is False       # second submit blocked


def test_ttl_expiry(tmp_path):
    b = SubmitBroker(tmp_path / "ticket.json", ttl_s=0)
    b.issue("greenhouse:chime:1")
    assert b.ticket_open("greenhouse:chime:1") is False   # already expired


def test_dry_run_broker_never_opens(tmp_path):
    b = SubmitBroker(tmp_path / "ticket.json", dry_run=True)
    b.issue("greenhouse:chime:1")
    assert b.ticket_open("greenhouse:chime:1") is False   # dry-run: structurally no submit
    assert b.consume("greenhouse:chime:1") is False


def test_survives_separate_process_read(tmp_path):
    # the stream MCP server is a separate process; a broker constructed on the
    # same file must see the issued ticket
    p = tmp_path / "ticket.json"
    SubmitBroker(p).issue("greenhouse:chime:1")
    assert SubmitBroker(p).ticket_open("greenhouse:chime:1") is True


def test_issue_overwrites_prior_ticket(tmp_path):
    # a fresh issue for a new job replaces a stale ticket for a different identity
    p = tmp_path / "ticket.json"
    b = SubmitBroker(p)
    b.issue("greenhouse:chime:1")
    b.issue("greenhouse:acme:2")
    assert b.ticket_open("greenhouse:chime:1") is False   # old identity no longer valid
    assert b.ticket_open("greenhouse:acme:2") is True


def test_corrupt_ticket_file_fails_closed(tmp_path):
    p = tmp_path / "ticket.json"
    p.write_text("{ not valid json")
    b = SubmitBroker(p)
    assert b.ticket_open("greenhouse:chime:1") is False   # unreadable -> no submit
