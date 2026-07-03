import json

from applypilot import database as db
from applypilot.discovery.atlas import boards_repo as repo
from applypilot.discovery.atlas import miner


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


def test_mine_from_db_uses_extract_board_from_url(tmp_path):
    conn = _conn(tmp_path)
    conn.executemany(
        "INSERT INTO jobs (url, application_url, discovered_at) VALUES (?,?,datetime('now'))",
        [
            ("https://boards.greenhouse.io/newco/jobs/1", "https://boards.greenhouse.io/newco/jobs/1"),
            ("https://jobs.lever.co/leverco/abc", "https://jobs.lever.co/leverco/abc"),
            ("https://example.com/careers/x", "https://example.com/careers/x"),  # not an ATS
        ],
    )
    conn.commit()
    cands = miner.mine_from_db(conn)
    assert ("greenhouse", "newco") in cands
    assert ("lever", "leverco") in cands
    assert all(a in ("greenhouse", "lever", "ashby") for a, _ in cands)
    assert len(cands) == 2


def test_mine_from_snapshot_reads_jsonl(tmp_path):
    snap = tmp_path / "snap.jsonl"
    snap.write_text(
        json.dumps({"ats": "ashby", "token": "snapco", "company_name": "SnapCo"}) + "\n"
        + json.dumps({"ats": "greenhouse", "token": "gco"}) + "\n"
        + "\n"                                       # blank line tolerated
        + json.dumps({"ats": "notanats", "token": "x"}) + "\n",  # dropped
        encoding="utf-8",
    )
    cands = miner.mine_from_snapshot(snap)
    assert ("ashby", "snapco") in cands
    assert ("greenhouse", "gco") in cands
    assert ("notanats", "x") not in cands
    assert cands[("ashby", "snapco")] == "SnapCo"    # company_name carried when present


def test_mine_candidates_upserts_as_candidate_status(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO jobs (url, application_url, discovered_at) "
                 "VALUES ('https://boards.greenhouse.io/dbco/jobs/9','https://boards.greenhouse.io/dbco/jobs/9',datetime('now'))")
    conn.commit()
    snap = tmp_path / "snap.jsonl"
    snap.write_text(json.dumps({"ats": "lever", "token": "snco"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(miner, "SNAPSHOT_PATH", snap)
    added = miner.mine_candidates(conn)
    assert added == 2
    rows = {(r["ats"], r["token"]): r for r in repo.get_all(conn)}
    assert rows[("greenhouse", "dbco")]["source"] == "db_mining"
    assert rows[("lever", "snco")]["source"] == "cc_snapshot"
    assert all(r["status"] == "candidate" for r in rows.values())   # unvalidated until Task 4
