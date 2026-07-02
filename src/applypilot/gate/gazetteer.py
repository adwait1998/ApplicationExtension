"""Bundled geo lexicon. Word-boundary matching only — the 'ca' in
'appliCAtions' bug is banned by construction.

Sourced verbatim from the curated ``location_accept`` /
``location_reject_non_remote`` lists in ``searches.yaml``. The substring-hack
forms in that file (e.g. ``", us"``, ``" apac"``, ``"(latam)"``) are collapsed
to their bare tokens here because :func:`word_match` matches whole words with
regex boundaries, so the padding is unnecessary and would break matching.
"""
from __future__ import annotations

import re

# All 50 states, full name -> USPS code. Full names come from searches.yaml
# location_accept; bare two-letter codes are deliberately NOT gazetteer keys
# (that is exactly the "ca"/appliCAtions class of bug this module bans).
US_STATES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
    "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa", "west virginia": "wv",
    "wisconsin": "wi", "wyoming": "wy",
}

# US metros -> region tag. Names come from searches.yaml location_accept city
# list. CA metros -> us-ca, Seattle -> us-wa-seattle, NYC -> us-ny-nyc, all
# others -> us-<state>. (Order matters at match time: longer phrases like
# "new york city" / "san francisco bay area" must be tried before their
# prefixes — rules.py iterates longest-first.)
US_METROS = {
    # California
    "san francisco": "us-ca",
    "san francisco bay area": "us-ca",
    "san jose": "us-ca",
    "palo alto": "us-ca",
    "mountain view": "us-ca",
    "menlo park": "us-ca",
    "redwood city": "us-ca",
    "sunnyvale": "us-ca",
    "cupertino": "us-ca",
    "berkeley": "us-ca",
    "oakland": "us-ca",
    "santa monica": "us-ca",
    "los angeles": "us-ca",
    "san diego": "us-ca",
    # Seattle metro
    "seattle": "us-wa-seattle",
    # NYC metro
    "new york": "us-ny-nyc",
    "new york city": "us-ny-nyc",
    # Other US metros -> us-<state>
    "boston": "us-ma",
    "austin": "us-tx",
    "dallas": "us-tx",
    "houston": "us-tx",
    "denver": "us-co",
    "chicago": "us-il",
    "atlanta": "us-ga",
    "miami": "us-fl",
    "portland": "us-or",
    "philadelphia": "us-pa",
    "pittsburgh": "us-pa",
    "minneapolis": "us-mn",
    "detroit": "us-mi",
    "phoenix": "us-az",
    "washington dc": "us-dc",
    "washington d.c.": "us-dc",
}

# Distinctive non-US tokens. Countries + cities + region tags from
# searches.yaml location_reject_non_remote (padding/parens stripped — see
# module docstring). "hong kong" appears once here (deduped from the source).
NON_US_MARKERS = {
    # Countries
    "canada", "mexico", "brazil", "argentina", "chile", "colombia", "peru",
    "united kingdom", "ireland", "germany", "france", "spain", "italy",
    "netherlands", "portugal", "switzerland", "sweden", "norway", "denmark",
    "finland", "poland", "czech", "austria", "belgium", "greece", "turkey",
    "russia", "ukraine", "romania", "bulgaria", "india", "singapore",
    "australia", "new zealand", "japan", "china", "south korea", "taiwan",
    "hong kong", "thailand", "vietnam", "philippines", "malaysia", "indonesia",
    "south africa", "egypt", "israel", "uae", "saudi arabia",
    # Cities
    "toronto", "vancouver", "montreal", "montréal", "ottawa", "calgary",
    "edmonton", "mexico city", "são paulo", "sao paulo", "rio de janeiro",
    "buenos aires", "london", "manchester", "edinburgh", "dublin", "paris",
    "berlin", "munich", "frankfurt", "hamburg", "amsterdam", "rotterdam",
    "brussels", "madrid", "barcelona", "lisbon", "rome", "milan", "warsaw",
    "krakow", "prague", "budapest", "vienna", "zurich", "geneva", "stockholm",
    "oslo", "copenhagen", "helsinki", "athens", "istanbul", "moscow", "kiev",
    "kyiv", "tokyo", "osaka", "seoul", "shanghai", "beijing", "bangalore",
    "bengaluru", "mumbai", "delhi", "hyderabad", "chennai", "pune", "sydney",
    "melbourne", "auckland", "tel aviv",
    # Region tags
    "emea", "apac", "latam", "europe", "asia pacific", "asia-pacific",
}

REMOTE_RE = re.compile(r"\bremote\b", re.I)
US_COUNTRY_RE = re.compile(r"\b(united states|u\.?s\.?a?|usa)\b", re.I)


def word_match(needle: str, haystack: str) -> bool:
    """True if `needle` appears in `haystack` as a whole word/phrase."""
    return re.search(rf"\b{re.escape(needle)}\b", haystack, re.I) is not None
