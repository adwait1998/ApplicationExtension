"""One-time seed of the boards table from the curated ats_companies.yaml
registry (the 56 tokens). Idempotent — safe to re-run; upsert_board never
duplicates. Membership is profile-agnostic, so ALL registry tokens import
regardless of what they post."""
from __future__ import annotations

from applypilot.discovery.ats_discovery import load_ats_registry
from applypilot.discovery.atlas import ATS_TYPES
from applypilot.discovery.atlas import boards_repo as repo


def import_registry(conn) -> int:
    """Upsert every ats token from the merged registry into boards.
    Returns the count of NEWLY-inserted rows."""
    registry = load_ats_registry()
    added = 0
    for ats in ATS_TYPES:
        for token in registry.get(ats, []) or []:
            if repo.upsert_board(conn, ats, str(token), source="yaml_import"):
                added += 1
    return added
