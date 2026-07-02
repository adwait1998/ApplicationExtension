"""Shared title targeting for discovery modules."""

from __future__ import annotations

import re
from collections.abc import Iterable

from applypilot import config


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Iterable):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _contains_term(text: str, term: str) -> bool:
    haystack = text.lower()
    needle = term.strip().lower()
    if not needle:
        return False

    if re.fullmatch(r"[a-z0-9+#.]+", needle):
        return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None
    return needle in haystack


def build_title_filter(
    search_cfg: dict | None = None,
    *,
    local_cfg: dict | None = None,
    search: dict | None = None,
) -> tuple[list[str], list[str]]:
    """Return include/exclude title filters from search and source config.

    Include terms are intentionally not inferred from broad query strings. If
    the user wants fetch-time targeting, they should set `title_include` (or a
    source-specific `title_keywords`) to precise role phrases.
    """
    if search_cfg is None:
        search_cfg = config.load_search_config() or {}
    local_cfg = local_cfg or {}
    search = search or {}

    include = (
        _as_list(search.get("title_include"))
        or _as_list(search_cfg.get("title_include"))
        or _as_list(local_cfg.get("title_include"))
        or _as_list(local_cfg.get("title_keywords"))
    )

    exclude = []
    for cfg in (local_cfg, search_cfg, search):
        exclude.extend(_as_list(cfg.get("title_exclude")))
        exclude.extend(_as_list(cfg.get("exclude_titles")))

    # Preserve order while removing duplicates case-insensitively.
    seen: set[str] = set()
    deduped_exclude: list[str] = []
    for term in exclude:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            deduped_exclude.append(term)

    return include, deduped_exclude


def title_matches(title: str | None, include: list[str], exclude: list[str]) -> bool:
    """Check a posting title against include/exclude filters."""
    if not title:
        return False

    if any(_contains_term(title, term) for term in exclude):
        return False

    if not include:
        return True

    return any(_contains_term(title, term) for term in include)
