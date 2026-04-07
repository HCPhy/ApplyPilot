"""Shared location filter helpers for official ATS discovery."""

from __future__ import annotations

US_WIDE_SENTINEL = "__US_ANYWHERE__"

_REMOTE_MARKERS = (
    "remote",
    "anywhere",
    "work from home",
    "wfh",
    "distributed",
)

_US_WIDE_TERMS = (
    "anywhere in the u.s.",
    "anywhere in the us",
    "united states",
    "usa",
    "u.s.",
    "us only",
    "usa only",
    "united states only",
)

_NON_US_MARKERS = (
    "canada",
    "singapore",
    "uk",
    "united kingdom",
    "japan",
    "thailand",
    "spain",
    "india",
    "mexico",
    "ireland",
    "poland",
    "germany",
    "france",
    "australia",
    "netherlands",
    "sweden",
    "switzerland",
    "taiwan",
    "korea",
    "hong kong",
    "china",
    "israel",
    "brazil",
    "argentina",
    "turkey",
    "uae",
    "emirates",
    "south africa",
    "new zealand",
    "europe",
    "emea",
    "apac",
    "latam",
)


def _is_us_wide_term(value: str) -> bool:
    normalized = " ".join(value.lower().split())
    return any(term in normalized for term in _US_WIDE_TERMS)


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def normalize_location_preferences(search_cfg: dict) -> tuple[list[str], list[str]]:
    """Normalize location filters from either explicit patterns or wizard output."""
    nested = search_cfg.get("location", {}) if isinstance(search_cfg.get("location"), dict) else {}

    raw_accept = search_cfg.get("location_accept")
    raw_reject = search_cfg.get("location_reject_non_remote")

    if raw_accept is None:
        raw_accept = nested.get("accept_patterns", [])
    if raw_reject is None:
        raw_reject = nested.get("reject_patterns", [])

    accept: list[str] = []
    reject: list[str] = []

    for value in raw_accept or []:
        text = str(value).strip()
        if not text:
            continue
        if _is_us_wide_term(text):
            _append_unique(accept, US_WIDE_SENTINEL)
        else:
            _append_unique(accept, text)

    for value in raw_reject or []:
        text = str(value).strip()
        if text:
            _append_unique(reject, text)

    if not accept:
        for item in search_cfg.get("locations", []):
            if not isinstance(item, dict):
                continue
            location = str(item.get("location", "")).strip()
            if not location:
                continue
            if _is_us_wide_term(location):
                _append_unique(accept, US_WIDE_SENTINEL)
            else:
                _append_unique(accept, location)
            if item.get("remote"):
                _append_unique(accept, "Remote")

    return accept, reject


def location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    """Check whether a location string matches the user's effective preferences."""
    if not location:
        return True

    loc = location.lower()

    for pattern in reject:
        if pattern.lower() in loc:
            return False

    if US_WIDE_SENTINEL in accept:
        if any(marker in loc for marker in _NON_US_MARKERS):
            return False
        return True

    if any(marker in loc for marker in _REMOTE_MARKERS):
        return True

    if not accept:
        return True

    return any(pattern.lower() in loc for pattern in accept)
