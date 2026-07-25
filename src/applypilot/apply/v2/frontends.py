"""Front-end registry (spec §6.3): ats -> pure parse_observation fn. The single
place that maps an ATS dialect to its observation->IR parser. Adding an ATS is a
one-line registry entry + a frontend module (invariant 13)."""
from __future__ import annotations

from applypilot.apply.v2 import frontend_greenhouse, frontend_ashby, frontend_lever

_REGISTRY = {
    "greenhouse": frontend_greenhouse.parse_observation,
    "ashby": frontend_ashby.parse_observation,
    "lever": frontend_lever.parse_observation,
}


def parser_for(ats: str | None):
    """The parse_observation fn for `ats`, or None if v2 has no front-end for it
    (caller falls open to legacy — invariant 2)."""
    return _REGISTRY.get((ats or "").lower())
