"""Shared location filter helpers for official ATS discovery."""

from __future__ import annotations

import re

US_WIDE_SENTINEL = "__US_ANYWHERE__"

_REMOTE_MARKERS = (
    "remote",
    "anywhere",
    "work from home",
    "wfh",
    "distributed",
    "virtual",
)

_US_WIDE_TERMS = (
    "anywhere in the u.s.",
    "anywhere in the us",
    "united states",
    "united states of america",
    "usa",
    "u.s.",
    "u.s.a.",
    "us only",
    "usa only",
    "united states only",
)

_US_COUNTRY_MARKERS = (
    "united states",
    "united states of america",
    "usa",
    "u.s.",
    "u.s.a.",
    " us ",
    " us,",
    " us-",
    " us.",
    " us/",
)

_NON_US_MARKERS = (
    "canada",
    "toronto",
    "ottawa",
    "montreal",
    "vancouver",
    "singapore",
    "uk",
    "united kingdom",
    "england",
    "london",
    "ireland",
    "dublin",
    "scotland",
    "wales",
    "europe",
    "emea",
    "apac",
    "latam",
    "india",
    "bengaluru",
    "bangalore",
    "mumbai",
    "noida",
    "kolkata",
    "gurugram",
    "hyderabad",
    "chennai",
    "pune",
    "thailand",
    "spain",
    "mexico",
    "poland",
    "warsaw",
    "kraków",
    "krakow",
    "germany",
    "france",
    "australia",
    "sydney",
    "netherlands",
    "sweden",
    "switzerland",
    "taiwan",
    "korea",
    "hong kong",
    "china",
    "prc",
    "israel",
    "brazil",
    "argentina",
    "turkey",
    "uae",
    "emirates",
    "dubai",
    "abu dhabi",
    "south africa",
    "new zealand",
    "norway",
    "oslo",
    "belgium",
    "brussels",
    "hungary",
    "budapest",
    "romania",
    "bucharest",
    "philippines",
    "manila",
    "makati",
    "quezon city",
    "vietnam",
    "ho chi minh",
    "malaysia",
    "penang",
    "putrajaya",
    "japan",
    "tokyo",
    "costa rica",
    "bogota",
    "colombia",
    "cairo",
)

_NON_US_CODES = {
    "can",
    "gbr",
    "ind",
    "pol",
    "de",
    "irl",
    "col",
    "srb",
    "hun",
    "phl",
    "ph",
    "vnm",
    "uae",
    "prc",
    "aus",
}

_US_STATE_NAMES = (
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "district of columbia",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west virginia",
    "wisconsin",
    "wyoming",
    "puerto rico",
)

_US_STATE_CODES = (
    "al",
    "ak",
    "az",
    "ar",
    "ca",
    "co",
    "ct",
    "dc",
    "de",
    "fl",
    "ga",
    "hi",
    "ia",
    "id",
    "il",
    "in",
    "ks",
    "ky",
    "la",
    "ma",
    "md",
    "me",
    "mi",
    "mn",
    "mo",
    "ms",
    "mt",
    "nc",
    "nd",
    "ne",
    "nh",
    "nj",
    "nm",
    "nv",
    "ny",
    "oh",
    "ok",
    "or",
    "pa",
    "pr",
    "ri",
    "sc",
    "sd",
    "tn",
    "tx",
    "ut",
    "va",
    "vt",
    "wa",
    "wi",
    "wv",
    "wy",
)

_US_CITY_HINTS = (
    "new york",
    "boston",
    "chicago",
    "san francisco",
    "los angeles",
    "austin",
    "tampa",
    "phoenix",
    "reston",
    "irving",
    "hillsboro",
    "jersey city",
    "los gatos",
    "milpitas",
    "newaygo",
    "round rock",
    "o'fallon",
    "ofallon",
)

_MULTI_LOCATION_RE = re.compile(r"^\d+\s+locations?$")
_STATE_NAME_RE = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(v) for v in _US_STATE_NAMES))
_STATE_CODE_AFTER_COMMA_RE = re.compile(r",\s*(?:%s)\b" % "|".join(code.upper() for code in _US_STATE_CODES), re.IGNORECASE)
_US_PREFIX_STATE_RE = re.compile(
    r"\b(?:US|USA|U\.S\.|U\.S\.A\.)\b[\s,./_-]+(?:%s)\b" % "|".join(code.upper() for code in _US_STATE_CODES),
    re.IGNORECASE,
)


def _normalize_text(value: str) -> str:
    return " ".join(str(value).strip().split())


def _tokenize_code_text(value: str) -> list[str]:
    return [token for token in re.split(r"[^a-z0-9]+", value.lower()) if token]


def _is_us_wide_term(value: str) -> bool:
    normalized = _normalize_text(value).lower()
    return any(term in normalized for term in _US_WIDE_TERMS)


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _is_placeholder_location(normalized: str) -> bool:
    lowered = normalized.lower()
    if _MULTI_LOCATION_RE.fullmatch(lowered):
        return True
    return lowered in {
        "multiple locations",
        "various locations",
        "several locations",
        "location flexible",
        "locations",
    }


def _has_non_us_marker(normalized: str) -> bool:
    lowered = normalized.lower()
    if any(marker in lowered for marker in _NON_US_MARKERS):
        return True
    tokens = set(_tokenize_code_text(lowered))
    return bool(tokens & _NON_US_CODES)


def _has_us_country_marker(normalized: str) -> bool:
    lowered = normalized.lower()
    if lowered.startswith("us "):
        return True
    return any(marker in lowered for marker in _US_COUNTRY_MARKERS)


def _has_us_state_marker(normalized: str) -> bool:
    if _STATE_NAME_RE.search(normalized.lower()):
        return True
    if _STATE_CODE_AFTER_COMMA_RE.search(normalized):
        return True
    if _US_PREFIX_STATE_RE.search(normalized):
        return True
    return False


def _has_us_city_hint(normalized: str) -> bool:
    lowered = normalized.lower()
    return any(hint in lowered for hint in _US_CITY_HINTS)


def _looks_us_based(location: str) -> bool:
    """Return True only when the location string has positive US evidence."""
    normalized = _normalize_text(location)
    if not normalized:
        return False
    if _is_placeholder_location(normalized):
        return False
    if _has_non_us_marker(normalized):
        return False
    if _has_us_country_marker(normalized):
        return True
    if _has_us_state_marker(normalized):
        return True
    if _has_us_city_hint(normalized):
        return True
    return False


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
        return US_WIDE_SENTINEL not in accept

    normalized = _normalize_text(location)
    lowered = normalized.lower()

    for pattern in reject:
        if pattern.lower() in lowered:
            return False

    if US_WIDE_SENTINEL in accept:
        return _looks_us_based(normalized)

    if any(marker in lowered for marker in _REMOTE_MARKERS):
        return True

    if not accept:
        return True

    return any(pattern.lower() in lowered for pattern in accept)
