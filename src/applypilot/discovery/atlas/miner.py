"""Profile-agnostic candidate-token harvest for the Board Atlas.

Client-side sources (cheap, no ATS load):
  - mine_from_db: reuse extract_board_from_url over the jobs table.
  - mine_from_snapshot: read the bundled JSONL produced OFFLINE by
    scripts/mine_common_crawl.py (spec §7.1 — CC mining is an offline batch,
    re-run quarterly; the client only loads its output).
mine_candidates() upserts everything as status='candidate'; Task 4's validator
promotes survivors to 'active'."""
from __future__ import annotations

import json
from pathlib import Path

from applypilot.discovery.ats_discovery import extract_board_from_url
from applypilot.discovery.atlas import ATS_TYPES
from applypilot.discovery.atlas import boards_repo as repo

SNAPSHOT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "atlas_snapshot.jsonl"


def mine_from_db(conn) -> set[tuple[str, str]]:
    """Harvest (ats, token) pairs from stored job URLs via extract_board_from_url."""
    rows = conn.execute(
        "SELECT url, application_url FROM jobs "
        "WHERE url LIKE '%greenhouse%' OR application_url LIKE '%greenhouse%' "
        "OR url LIKE '%lever.co%' OR application_url LIKE '%lever.co%' "
        "OR url LIKE '%ashbyhq.com%' OR application_url LIKE '%ashbyhq.com%'"
    ).fetchall()
    out: set[tuple[str, str]] = set()
    for row in rows:
        for value in (row["url"], row["application_url"]):
            cand = extract_board_from_url(value or "")
            if cand and cand.ats in ATS_TYPES and cand.token:
                out.add((cand.ats, cand.token))
    return out


def mine_from_snapshot(path: Path | None = None) -> dict[tuple[str, str], str | None]:
    """Load the bundled CC snapshot. Returns {(ats, token): company_name|None}."""
    path = path or SNAPSHOT_PATH
    out: dict[tuple[str, str], str | None] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ats, token = str(rec.get("ats", "")).lower(), str(rec.get("token", "")).lower()
        if ats not in ATS_TYPES or not token:
            continue
        out[(ats, token)] = rec.get("company_name")
    return out


def mine_candidates(conn, *, include_db: bool = True, include_snapshot: bool = True) -> int:
    """Upsert all mined candidates as status='candidate'. Returns newly-added count."""
    added = 0
    if include_db:
        for ats, token in mine_from_db(conn):
            if repo.upsert_board(conn, ats, token, source="db_mining"):
                added += 1
    if include_snapshot:
        for (ats, token), name in mine_from_snapshot().items():
            if repo.upsert_board(conn, ats, token, company_name=name, source="cc_snapshot"):
                added += 1
    return added
