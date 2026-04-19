"""Date normalization helpers for ATS discovery."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


def _coerce_now(now: datetime | None = None) -> datetime:
    """Return a timezone-aware UTC reference time."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _isoformat_utc(dt: datetime) -> str:
    """Normalize a datetime to UTC ISO format."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def normalize_posted_at(value, now: datetime | None = None) -> str | None:
    """Convert common ATS date shapes to an ISO UTC timestamp.

    This intentionally accepts approximate relative dates from Workday
    ("Posted 3 Days Ago") because they are still useful for newest-first
    ranking. Unknown formats return None so callers can fall back to
    discovered_at.
    """
    if value in (None, ""):
        return None

    reference = _coerce_now(now)

    if isinstance(value, (int, float)):
        try:
            ts = float(value)
            if ts > 10_000_000_000:
                ts /= 1000.0
            return _isoformat_utc(datetime.fromtimestamp(ts, tz=timezone.utc))
        except (OSError, OverflowError, ValueError):
            return None

    raw = str(value).strip()
    if not raw:
        return None

    normalized = raw.replace("Z", "+00:00")
    try:
        return _isoformat_utc(datetime.fromisoformat(normalized))
    except ValueError:
        pass

    for fmt in ("%d-%b-%Y", "%d %b %Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return _isoformat_utc(parsed)
        except ValueError:
            pass

    lowered = raw.lower()
    lowered = re.sub(r"^posted\s+", "", lowered).strip()
    lowered = re.sub(r"^on\s+", "", lowered).strip()

    if lowered in {"today", "just posted"}:
        return _isoformat_utc(reference)
    if lowered == "yesterday":
        return _isoformat_utc(reference - timedelta(days=1))

    relative = re.search(r"(\d+)\+?\s+(minute|hour|day|week|month|year)s?\s+ago", lowered)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        if unit == "minute":
            delta = timedelta(minutes=amount)
        elif unit == "hour":
            delta = timedelta(hours=amount)
        elif unit == "day":
            delta = timedelta(days=amount)
        elif unit == "week":
            delta = timedelta(weeks=amount)
        elif unit == "month":
            delta = timedelta(days=30 * amount)
        else:
            delta = timedelta(days=365 * amount)
        return _isoformat_utc(reference - delta)

    return None
