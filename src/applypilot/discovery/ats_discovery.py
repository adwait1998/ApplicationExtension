"""Discover and validate Greenhouse/Lever/Ashby board tokens.

The normal ats_boards crawler is deliberately fast because it only crawls a
known registry. This module expands that registry by mining existing DB URLs
and search-result pages, then validating candidates against each ATS public
JSON API before writing them to the user's ats_companies.yaml overlay.
"""

from __future__ import annotations

import logging
import base64
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import yaml
from bs4 import BeautifulSoup

from applypilot import config
from applypilot.database import get_connection, init_db
from applypilot.discovery.freshness import iso_or_none, is_recent, parse_posted_at
from applypilot.discovery.location_filter import load_location_filter, location_ok
from applypilot.discovery.title_filter import build_title_filter, title_matches

log = logging.getLogger(__name__)

ATS_TYPES = ("greenhouse", "lever", "ashby")
USER_REGISTRY_PATH = config.APP_DIR / "ats_companies.yaml"
_TIMEOUT = 8
_USER_AGENT = "Mozilla/5.0 (compatible; ApplyPilotBot/1.0)"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,80}$")


@dataclass(frozen=True)
class BoardCandidate:
    ats: str
    token: str
    source: str


@dataclass(frozen=True)
class ValidatedBoard:
    ats: str
    token: str
    source: str
    total_jobs: int
    matching_jobs: int
    fresh_matching_jobs: int
    newest_posted_at: str | None
    sample_titles: tuple[str, ...]


def _clean_token(value: str | None) -> str | None:
    if not value:
        return None
    token = unquote(value).strip().strip("/")
    if not token or token.lower() in {"jobs", "job", "embed", "departments", "applications"}:
        return None
    if not _TOKEN_RE.match(token):
        return None
    return token.lower()


def extract_board_from_url(url: str) -> BoardCandidate | None:
    """Extract an ATS board token from a public job-board URL."""
    if not url:
        return None
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None
    host = parsed.netloc.lower()
    parts = [unquote(p) for p in parsed.path.split("/") if p]

    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"} and parts:
        token = _clean_token(parts[0])
        return BoardCandidate("greenhouse", token, url) if token else None

    if host == "boards-api.greenhouse.io" and len(parts) >= 3 and parts[:2] == ["v1", "boards"]:
        token = _clean_token(parts[2])
        return BoardCandidate("greenhouse", token, url) if token else None

    if host == "jobs.lever.co" and parts:
        token = _clean_token(parts[0])
        return BoardCandidate("lever", token, url) if token else None

    if host == "api.lever.co" and len(parts) >= 3 and parts[:2] == ["v0", "postings"]:
        token = _clean_token(parts[2])
        return BoardCandidate("lever", token, url) if token else None

    if host == "jobs.ashbyhq.com" and parts:
        token = _clean_token(parts[0])
        return BoardCandidate("ashby", token, url) if token else None

    if host == "api.ashbyhq.com" and len(parts) >= 3 and parts[:2] == ["posting-api", "job-board"]:
        token = _clean_token(parts[2])
        return BoardCandidate("ashby", token, url) if token else None

    return None


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_ats_registry() -> dict:
    """Merge package defaults with the user's discovered ATS registry."""
    base = _load_yaml(config.CONFIG_DIR / "ats_companies.yaml")
    user = _load_yaml(USER_REGISTRY_PATH)
    merged = dict(base)
    for ats in ATS_TYPES:
        seen: set[str] = set()
        values: list[str] = []
        for token in list(base.get(ats, []) or []) + list(user.get(ats, []) or []):
            cleaned = _clean_token(str(token))
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                values.append(cleaned)
        merged[ats] = values
    for key, value in user.items():
        if key not in ATS_TYPES:
            merged[key] = value
    return merged


def _existing_tokens() -> set[tuple[str, str]]:
    registry = load_ats_registry()
    existing: set[tuple[str, str]] = set()
    for ats in ATS_TYPES:
        for token in registry.get(ats, []) or []:
            cleaned = _clean_token(str(token))
            if cleaned:
                existing.add((ats, cleaned))
    return existing


def _dedupe_candidates(candidates: Iterable[BoardCandidate]) -> set[BoardCandidate]:
    by_key: dict[tuple[str, str], BoardCandidate] = {}
    for candidate in candidates:
        key = (candidate.ats, candidate.token)
        current = by_key.get(key)
        if current is None or (current.source.startswith("seed:generic") and not candidate.source.startswith("seed:generic")):
            by_key[key] = candidate
    return set(by_key.values())


def _candidates_from_db() -> set[BoardCandidate]:
    init_db()
    conn = get_connection()
    rows = conn.execute(
        "SELECT url, application_url FROM jobs "
        "WHERE url LIKE '%greenhouse%' OR application_url LIKE '%greenhouse%' "
        "OR url LIKE '%lever.co%' OR application_url LIKE '%lever.co%' "
        "OR url LIKE '%ashbyhq.com%' OR application_url LIKE '%ashbyhq.com%'"
    ).fetchall()
    candidates: set[BoardCandidate] = set()
    for row in rows:
        for value in (row["url"], row["application_url"]):
            candidate = extract_board_from_url(value or "")
            if candidate:
                candidates.add(candidate)
    return candidates


def _normalize_search_href(href: str) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith("/l/"):
        query = parse_qs(urlparse(href).query)
        target = query.get("uddg", [None])[0]
        return unquote(target) if target else None
    parsed = urlparse(href)
    if href.startswith("/ck/") or parsed.netloc.lower().endswith("bing.com") and parsed.path.startswith("/ck/"):
        query = parse_qs(parsed.query)
        target = query.get("u", [None])[0]
        if target:
            # Bing commonly prefixes URL-safe base64 redirects with "a1".
            encoded = target[2:] if target.startswith("a1") else target
            try:
                padded = encoded + "=" * (-len(encoded) % 4)
                return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
            except Exception:
                return unquote(target)
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return None


def _fetch_search_urls(query: str, limit: int) -> list[str]:
    """Best-effort web search scrape for public ATS URLs."""
    urls: list[str] = []
    seen: set[str] = set()
    endpoints = [
        ("https://duckduckgo.com/html/", {"q": query}),
        ("https://www.bing.com/search", {"q": query}),
    ]
    with httpx.Client(
        timeout=_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html"},
    ) as client:
        for url, params in endpoints:
            try:
                resp = client.get(url, params=params)
                resp.raise_for_status()
            except Exception as exc:
                log.debug("Search provider failed for %s: %s", query, exc)
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = _normalize_search_href(a.get("href", ""))
                if not href:
                    continue
                lower = href.lower()
                if not any(host in lower for host in (
                    "boards.greenhouse.io",
                    "job-boards.greenhouse.io",
                    "jobs.lever.co",
                    "jobs.ashbyhq.com",
                )):
                    continue
                if href not in seen:
                    seen.add(href)
                    urls.append(href)
                if len(urls) >= limit:
                    return urls
    return urls


def _search_queries(raw_queries: list[str]) -> list[str]:
    ats_sites = [
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "jobs.lever.co",
        "jobs.ashbyhq.com",
    ]
    queries: list[str] = []
    for role in raw_queries:
        role = role.strip()
        if not role:
            continue
        for site in ats_sites:
            queries.append(f'site:{site} "{role}"')
            queries.append(f"site:{site} {role}")
    return queries


def _candidates_from_search(raw_queries: list[str], limit: int) -> set[BoardCandidate]:
    if limit <= 0:
        return set()
    candidates: set[BoardCandidate] = set()
    per_query_limit = max(10, min(50, limit))
    for query in _search_queries(raw_queries):
        for url in _fetch_search_urls(query, per_query_limit):
            candidate = extract_board_from_url(url)
            if candidate:
                candidates.add(candidate)
        if len(candidates) >= limit:
            break
        time.sleep(0.2)
    return candidates


def _candidates_from_seed_config() -> set[BoardCandidate]:
    cfg = _load_yaml(config.CONFIG_DIR / "ats_seed_companies.yaml")
    candidates: set[BoardCandidate] = set()
    for token in cfg.get("generic", []) or []:
        cleaned = _clean_token(str(token))
        if not cleaned:
            continue
        for ats in ATS_TYPES:
            candidates.add(BoardCandidate(ats, cleaned, "seed:generic"))
    for ats in ATS_TYPES:
        for token in cfg.get(ats, []) or []:
            cleaned = _clean_token(str(token))
            if cleaned:
                candidates.add(BoardCandidate(ats, cleaned, f"seed:{ats}"))
    return candidates


def _posted_dt(value) -> datetime | None:
    dt = parse_posted_at(value)
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _validate_jobs(
    candidate: BoardCandidate,
    raw_jobs: Iterable[dict],
    *,
    title_include: list[str],
    title_exclude: list[str],
    hours_old: int | None,
    min_matching_jobs: int,
) -> ValidatedBoard | None:
    accept, reject = load_location_filter()
    total = 0
    matching = 0
    fresh = 0
    newest: datetime | None = None
    titles: list[str] = []

    for job in raw_jobs:
        total += 1
        title = str(job.get("title") or "")
        posted = job.get("posted_at")
        posted_dt = _posted_dt(posted)
        if posted_dt and (newest is None or posted_dt > newest):
            newest = posted_dt
        if not title_matches(title, title_include, title_exclude):
            continue
        if not location_ok(str(job.get("location") or ""), accept, reject):
            continue
        matching += 1
        if len(titles) < 5:
            titles.append(title)
        if is_recent(posted, hours_old, keep_unknown=True):
            fresh += 1

    if total <= 0 or matching < min_matching_jobs:
        return None
    return ValidatedBoard(
        ats=candidate.ats,
        token=candidate.token,
        source=candidate.source,
        total_jobs=total,
        matching_jobs=matching,
        fresh_matching_jobs=fresh,
        newest_posted_at=iso_or_none(newest),
        sample_titles=tuple(titles),
    )


def validate_board(
    candidate: BoardCandidate,
    *,
    title_include: list[str],
    title_exclude: list[str],
    hours_old: int | None = None,
    min_matching_jobs: int = 1,
) -> ValidatedBoard | None:
    """Probe a board token against its public ATS API."""
    try:
        if candidate.ats == "greenhouse":
            url = f"https://boards-api.greenhouse.io/v1/boards/{candidate.token}/jobs?content=false"
            resp = httpx.get(url, timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            data = resp.json()
            jobs = [
                {
                    "title": j.get("title", ""),
                    "location": (j.get("location") or {}).get("name", ""),
                    "posted_at": j.get("first_published") or j.get("updated_at"),
                }
                for j in data.get("jobs", [])
            ]
        elif candidate.ats == "lever":
            url = f"https://api.lever.co/v0/postings/{candidate.token}?mode=json"
            resp = httpx.get(url, timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            data = resp.json()
            jobs = [
                {
                    "title": j.get("text", ""),
                    "location": (j.get("categories") or {}).get("location", ""),
                    "posted_at": j.get("createdAt"),
                }
                for j in data
            ]
        elif candidate.ats == "ashby":
            url = f"https://api.ashbyhq.com/posting-api/job-board/{candidate.token}?includeCompensation=false"
            resp = httpx.get(url, timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            data = resp.json()
            jobs = [
                {
                    "title": j.get("title", ""),
                    "location": j.get("location", ""),
                    "posted_at": j.get("publishedAt"),
                }
                for j in data.get("jobs", [])
            ]
        else:
            return None
    except Exception as exc:
        log.debug("ATS validation failed for %s/%s: %s", candidate.ats, candidate.token, exc)
        return None

    return _validate_jobs(
        candidate,
        jobs,
        title_include=title_include,
        title_exclude=title_exclude,
        hours_old=hours_old,
        min_matching_jobs=min_matching_jobs,
    )


def _default_role_queries(max_tier: int = 2) -> list[str]:
    search_cfg = config.load_search_config() or {}
    queries = []
    for row in search_cfg.get("queries", []) or []:
        if int(row.get("tier", 99)) <= max_tier:
            queries.append(str(row.get("query", "")).strip())
    if queries:
        return queries
    return ["Product Designer", "UX Designer", "Senior Product Designer", "UX Researcher"]


def _write_user_registry(validated: list[ValidatedBoard]) -> int:
    USER_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    current = _load_yaml(USER_REGISTRY_PATH)
    changed = 0
    for ats in ATS_TYPES:
        existing = []
        seen: set[str] = set()
        for token in current.get(ats, []) or []:
            cleaned = _clean_token(str(token))
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                existing.append(cleaned)
        for board in sorted((b for b in validated if b.ats == ats), key=lambda b: b.token):
            if board.token not in seen:
                seen.add(board.token)
                existing.append(board.token)
                changed += 1
        current[ats] = existing

    meta = current.setdefault("discovered_metadata", {})
    now = datetime.now(timezone.utc).isoformat()
    for board in validated:
        meta[f"{board.ats}:{board.token}"] = {
            "last_validated_at": now,
            "source": board.source,
            "total_jobs": board.total_jobs,
            "matching_jobs": board.matching_jobs,
            "fresh_matching_jobs": board.fresh_matching_jobs,
            "newest_posted_at": board.newest_posted_at,
            "sample_titles": list(board.sample_titles),
        }
    USER_REGISTRY_PATH.write_text(yaml.safe_dump(current, sort_keys=False), encoding="utf-8")
    return changed


def discover_ats_boards(
    *,
    queries: list[str] | None = None,
    search_limit: int = 200,
    workers: int = 12,
    hours_old: int | None = None,
    min_matching_jobs: int = 1,
    include_db: bool = True,
    include_seeds: bool = True,
    write: bool = True,
) -> dict:
    """Find, validate, and optionally persist new direct ATS board tokens."""
    search_cfg = config.load_search_config() or {}
    if hours_old is None:
        hours_old = search_cfg.get("defaults", {}).get("hours_old", 24)
    role_queries = queries or _default_role_queries(max_tier=2)
    title_include, title_exclude = build_title_filter(search_cfg)

    candidates: set[BoardCandidate] = set()
    if include_seeds:
        candidates.update(_candidates_from_seed_config())
    if include_db:
        candidates.update(_candidates_from_db())
    candidates.update(_candidates_from_search(role_queries, search_limit))
    candidates = _dedupe_candidates(candidates)

    existing = _existing_tokens()
    candidates = {
        c for c in candidates
        if c.token and c.ats in ATS_TYPES and (c.ats, c.token) not in existing
    }

    validated: list[ValidatedBoard] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(
                validate_board,
                candidate,
                title_include=title_include,
                title_exclude=title_exclude,
                hours_old=hours_old,
                min_matching_jobs=min_matching_jobs,
            ): candidate
            for candidate in candidates
        }
        for fut in as_completed(futures):
            board = fut.result()
            if board:
                validated.append(board)

    validated.sort(
        key=lambda b: (b.fresh_matching_jobs, b.matching_jobs, b.total_jobs, b.token),
        reverse=True,
    )
    added = _write_user_registry(validated) if write and validated else 0
    return {
        "candidates": len(candidates),
        "validated": len(validated),
        "added": added,
        "registry_path": str(USER_REGISTRY_PATH),
        "boards": validated,
    }
