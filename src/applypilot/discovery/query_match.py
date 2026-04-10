"""Shared query matching helpers for discovery crawlers."""

from __future__ import annotations

import fnmatch
import re

_TOKEN_ALIASES = {
    "internship": "intern",
    "internships": "intern",
    "co-op": "intern",
    "coop": "intern",
    "coops": "intern",
}


def _normalize_token(token: str) -> str:
    """Normalize a single token for fuzzy query matching."""
    return _TOKEN_ALIASES.get(token, token)


def _normalize_tokens(text: str) -> list[str]:
    """Tokenize free text into normalized lowercase words."""
    cleaned = text.lower().replace("&", " and ")
    return [_normalize_token(tok) for tok in re.findall(r"[a-z0-9+#]+", cleaned) if tok]


def _normalize_glob_text(text: str) -> str:
    """Normalize text into a space-separated form that works with glob matching."""
    return " ".join(_normalize_tokens(text))


def _normalize_glob_query(query: str) -> str:
    """Normalize glob patterns while preserving wildcard characters."""
    lowered = query.lower().replace("&", " and ")
    placeholder_star = "__APPLYPILOT_STAR__"
    placeholder_qmark = "__APPLYPILOT_QMARK__"
    lowered = lowered.replace("*", placeholder_star).replace("?", placeholder_qmark)
    lowered = re.sub(r"[^a-z0-9+#_]+", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    lowered = lowered.replace(placeholder_star.lower(), "*").replace(placeholder_qmark.lower(), "?")
    return lowered


def query_to_search_text(query: str) -> str:
    """Convert a user query into a safe plain-text search string for ATS APIs."""
    search = query.strip()
    if search.startswith("re:"):
        search = search[3:].strip()
    search = search.replace("*", " ").replace("?", " ")
    search = re.sub(r"[^A-Za-z0-9+#]+", " ", search)
    search = re.sub(r"\s+", " ", search).strip()
    return search or query.strip()


def matches_query(text: str, query: str) -> bool:
    """Return True if a title/text matches a configured query."""
    candidate = (text or "").strip()
    pattern = (query or "").strip()
    if not candidate or not pattern:
        return False

    if pattern.startswith("re:"):
        regex = pattern[3:].strip()
        if not regex:
            return False
        try:
            return re.search(regex, candidate, flags=re.IGNORECASE) is not None
        except re.error:
            return False

    if "*" in pattern or "?" in pattern:
        normalized_text = _normalize_glob_text(candidate)
        normalized_pattern = _normalize_glob_query(pattern)
        if normalized_pattern and fnmatch.fnmatch(normalized_text, normalized_pattern):
            return True

    candidate_lower = candidate.lower()
    pattern_lower = pattern.lower()
    if pattern_lower in candidate_lower:
        return True

    query_tokens = _normalize_tokens(pattern)
    if not query_tokens:
        return False

    text_tokens = set(_normalize_tokens(candidate))
    return all(tok in text_tokens for tok in query_tokens)
