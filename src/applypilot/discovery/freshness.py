"""Freshness helpers for discovery modules."""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta, timezone


_EMPTY_MARKERS = {"", "nan", "nat", "none", "null"}


def parse_posted_at(value) -> datetime | None:
    """Best-effort conversion of board-specific posted dates to UTC datetimes."""
    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min)
    elif isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        # Lever uses Unix milliseconds.
        seconds = float(value) / 1000 if abs(float(value)) > 10_000_000_000 else float(value)
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    else:
        raw = str(value).strip()
        if raw.lower() in _EMPTY_MARKERS:
            return None

        if re.fullmatch(r"\d+(\.\d+)?", raw):
            return parse_posted_at(float(raw))

        normalized = raw.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y"):
                try:
                    dt = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_recent(
    value,
    hours_old: int | None,
    *,
    now: datetime | None = None,
    keep_unknown: bool = True,
) -> bool:
    """Return True when value is within the configured freshness window."""
    if not hours_old or hours_old <= 0:
        return True

    posted_at = parse_posted_at(value)
    if posted_at is None:
        return keep_unknown

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return posted_at >= now.astimezone(timezone.utc) - timedelta(hours=hours_old)


def iso_or_none(value) -> str | None:
    """Return an ISO UTC timestamp for a posted date, if parseable."""
    dt = parse_posted_at(value)
    return dt.isoformat() if dt else None
