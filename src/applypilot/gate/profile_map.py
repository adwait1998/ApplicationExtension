"""Map profile.json + searches.yaml into the gate engine's policy shape.
One definition, imported everywhere the gate runs (imports nothing from
applypilot except stdlib -> no circular imports)."""
from __future__ import annotations


def gate_profile(profile: dict, search_cfg: dict | None = None) -> dict:
    p = profile or {}
    wa = p.get("work_authorization", {}) or {}
    search_cfg = search_cfg or {}
    return {
        "geo": {
            "remote_ok": True,
            "remote_scope": "US",
            # compiled per-profile geo arrives in Phase 4; v1 seeds the operator's targets
            "onsite_regions": p.get("geo_onsite_regions") or ["us-ca", "us-wa-seattle", "us-ny-nyc"],
        },
        "seniority": {
            "accept_bands": p.get("accept_bands") or ["mid", "senior"],
            "ic_only": bool(p.get("ic_only", True)),
        },
        "needs_sponsorship": bool(wa.get("require_sponsorship", False)),
        "workday_accounts": search_cfg.get("workday_accounts", []) or [],
    }
