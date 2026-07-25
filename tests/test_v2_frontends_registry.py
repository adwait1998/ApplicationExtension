"""Task 7: the front-end registry (spec §6.3) — ats -> pure parse_observation fn.

The single place that maps an ATS dialect to its observation->IR parser. Adding
an ATS is a one-line registry entry + a frontend module (invariant 13). Unknown
ATSes return None so the caller falls open to legacy (invariant 2)."""
from applypilot.apply.v2 import frontends
from applypilot.apply.v2 import frontend_greenhouse, frontend_ashby, frontend_lever


def test_registry_routes_each_ats():
    assert frontends.parser_for("greenhouse") is frontend_greenhouse.parse_observation
    assert frontends.parser_for("ashby") is frontend_ashby.parse_observation
    assert frontends.parser_for("lever") is frontend_lever.parse_observation


def test_registry_unknown_ats_returns_none():
    assert frontends.parser_for("workday") is None
    assert frontends.parser_for("unsupported") is None


def test_registry_case_insensitive_and_none_safe():
    # Detection strings are lower-cased upstream, but the registry must not choke
    # on mixed case or a None ats (falls open -> None).
    assert frontends.parser_for("Greenhouse") is frontend_greenhouse.parse_observation
    assert frontends.parser_for("ASHBY") is frontend_ashby.parse_observation
    assert frontends.parser_for(None) is None
    assert frontends.parser_for("") is None
