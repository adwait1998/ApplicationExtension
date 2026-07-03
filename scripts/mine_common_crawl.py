#!/usr/bin/env python
"""OFFLINE Common Crawl board-token miner for the ApplyPilot Board Atlas.

This is a MAINTAINER script. It is NOT imported by the client, NOT wired into
the `applypilot` CLI, and NOT run on any user's machine or hot path. It runs on
the maintainer's box, roughly quarterly (spec §7.1: the CC batch is an offline
job with zero ATS load), to (re)generate the bundled candidate snapshot that
the client loads via `applypilot.discovery.atlas.miner.mine_from_snapshot`.

What it does
------------
1. Queries the Common Crawl URL index (http://index.commoncrawl.org/) for the
   ATS host patterns we care about:
       boards.greenhouse.io
       job-boards.greenhouse.io
       jobs.lever.co
       jobs.ashbyhq.com
   The CC index exposes one CDX endpoint per crawl (e.g.
   `CC-MAIN-2024-33-index`); each `?url=<host>/*&output=json` request streams
   one JSON object per captured URL.
2. Runs every captured URL through `extract_board_from_url` (the exact same
   host-parsing the client uses — no re-implementation), keeping only well-
   formed (ats, token) pairs.
3. Dedupes on (ats, token) and writes one JSON object per line to
   `src/applypilot/data/atlas_snapshot.jsonl` (or `--out`).

Politeness: Common Crawl's index is a public research dataset; this script uses
an honest User-Agent and a modest per-request pause. It talks ONLY to
index.commoncrawl.org — never to Greenhouse/Lever/Ashby (membership validation
against the ATS itself is the client's `validator.py`, profile-agnostically).

Usage
-----
    python scripts/mine_common_crawl.py \
        --cc-index CC-MAIN-2024-33 \
        --out src/applypilot/data/atlas_snapshot.jsonl

The result should be hand-reviewed before committing — keep the bundled set
small and high-signal (spec §14 YAGNI: do not machine-dump thousands in v1).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

# Allow running from a source checkout without an install.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from applypilot.discovery.ats_discovery import extract_board_from_url  # noqa: E402

CC_INDEX_BASE = "http://index.commoncrawl.org"
HOST_PATTERNS = (
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
)
USER_AGENT = "Mozilla/5.0 (compatible; ApplyPilotBot/1.0; +offline-cc-miner)"
DEFAULT_OUT = _SRC / "applypilot" / "data" / "atlas_snapshot.jsonl"


def _index_url(cc_index: str) -> str:
    """CC crawl id -> its CDX index endpoint (e.g. CC-MAIN-2024-33-index)."""
    cc_index = cc_index.strip()
    if not cc_index.endswith("-index"):
        cc_index = f"{cc_index}-index"
    return f"{CC_INDEX_BASE}/{cc_index}"


def mine_host(client: httpx.Client, cc_index: str, host: str) -> set[tuple[str, str]]:
    """Stream one host pattern from the CC index and extract board tokens."""
    found: set[tuple[str, str]] = set()
    resp = client.get(
        _index_url(cc_index),
        params={"url": f"{host}/*", "output": "json"},
        timeout=60,
    )
    resp.raise_for_status()
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        cand = extract_board_from_url(rec.get("url", ""))
        if cand and cand.token:
            found.add((cand.ats, cand.token))
    return found


def mine(cc_index: str, *, pause: float = 1.0) -> list[tuple[str, str]]:
    """Query every host pattern; return deduped, sorted (ats, token) pairs."""
    seen: set[tuple[str, str]] = set()
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(headers=headers, follow_redirects=True) as client:
        for host in HOST_PATTERNS:
            try:
                seen |= mine_host(client, cc_index, host)
            except httpx.HTTPError as exc:  # keep going if one host errors
                print(f"WARN: {host}: {exc}", file=sys.stderr)
            time.sleep(pause)  # be polite to the CC index
    return sorted(seen)


def write_snapshot(pairs: list[tuple[str, str]], out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"ats": ats, "token": token}) for ats, token in pairs]
    out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline Common Crawl board-token miner (maintainer only).")
    parser.add_argument("--cc-index", required=True,
                        help="Common Crawl crawl id, e.g. CC-MAIN-2024-33 (with or without -index).")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"Output JSONL path (default: {DEFAULT_OUT}).")
    parser.add_argument("--pause", type=float, default=1.0,
                        help="Seconds to pause between host queries (politeness).")
    args = parser.parse_args(argv)

    pairs = mine(args.cc_index, pause=args.pause)
    count = write_snapshot(pairs, args.out)
    print(f"Wrote {count} candidate tokens to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
