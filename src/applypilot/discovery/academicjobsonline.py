"""AcademicJobsOnline discovery for curated academic discipline pages."""

from __future__ import annotations

import logging
import re
import urllib.parse
from datetime import date
from html import unescape

from bs4 import BeautifulSoup
from rich.progress import Progress

from applypilot.discovery.public_boards import fetch_text, load_boards as load_registry, run_board_discovery

log = logging.getLogger(__name__)

STRATEGY = "academicjobsonline"
BASE_URL = "https://academicjobsonline.org"

_JOB_LINK_RE = re.compile(r"/ajo/jobs/(\d+)$")
_DEADLINE_RE = re.compile(r"\bdeadline\s+(\d{4})/(\d{2})/(\d{2})", re.IGNORECASE)
_DEADLINE_FRAGMENT_RE = re.compile(
    r"\bdeadline\s+\d{4}/\d{2}/\d{2}(?:\s+\d{1,2}:\d{2}\s*(?:AM|PM))?\*?",
    re.IGNORECASE,
)
_CLOSED_MARKERS = (
    "filled",
    "withdrawn",
    "closed",
    "cancelled",
    "canceled",
    "offers accepted",
)


def load_boards() -> dict:
    """Load curated AcademicJobsOnline discipline pages from config/academicjobsonline.yaml."""
    return load_registry("academicjobsonline.yaml")


def _clean_text(value: str | None) -> str:
    text = " ".join(unescape(value or "").replace("\xa0", " ").split())
    return text.replace(" ,", ",")


def _absolute_url(href: str | None) -> str:
    if not href:
        return ""
    if href.startswith("mailto:"):
        return href
    return urllib.parse.urljoin(BASE_URL, href)


def _deadline_date(meta_text: str) -> date | None:
    match = _DEADLINE_RE.search(meta_text)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _is_available(meta_text: str, *, today: date | None = None) -> bool:
    """Return False for explicitly closed or expired AcademicJobsOnline postings."""
    lowered = meta_text.lower()
    if any(marker in lowered for marker in _CLOSED_MARKERS):
        return False

    deadline = _deadline_date(meta_text)
    if deadline and deadline < (today or date.today()):
        return False
    return True


def _strip_wrapping_parens(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("(") and stripped.endswith(")"):
        return stripped[1:-1].strip()
    return stripped


def _extract_location(meta_text: str) -> str:
    """Extract the compact location from an AJO metadata suffix."""
    text = _strip_wrapping_parens(_clean_text(meta_text))
    if not text:
        return ""

    text = _DEADLINE_FRAGMENT_RE.sub("", text)
    parts: list[str] = []
    for raw_part in text.split(","):
        part = raw_part.strip(" -*")
        lowered = part.lower()
        if not part:
            continue
        if lowered in _CLOSED_MARKERS:
            continue
        if lowered.startswith(("posted ", "listed ", "accepting applications")):
            continue
        parts.append(part)

    return ", ".join(parts)


def _institution_text(group) -> str:
    heading = group.select_one("h3")
    if not heading:
        return ""
    return _clean_text(heading.get_text(" ", strip=True))


def _application_url(item, detail_href: str) -> str:
    for link in item.find_all("a", href=True):
        href = str(link.get("href") or "")
        if href == detail_href:
            continue
        text = _clean_text(link.get_text(" ", strip=True)).lower()
        if "apply" in text or href.startswith(("http://", "https://", "mailto:")):
            return _absolute_url(href)
    return _absolute_url(f"{detail_href.rstrip('/')}/apply")


def parse_academicjobsonline_jobs(
    html: str,
    board: dict,
    *,
    today: date | None = None,
) -> list[dict]:
    """Parse an AcademicJobsOnline listing page into ApplyPilot job records."""
    soup = BeautifulSoup(html, "html.parser")
    source_name = str(board.get("name") or "AcademicJobsOnline").strip()
    jobs: list[dict] = []
    seen_urls: set[str] = set()

    for group in soup.select("div.clr"):
        institution = _institution_text(group)
        for item in group.select("ol.ldt > li"):
            detail_link = item.find("a", href=_JOB_LINK_RE)
            if not detail_link:
                continue

            detail_href = str(detail_link.get("href") or "")
            url = _absolute_url(detail_href)
            if not url or url in seen_urls:
                continue

            job_id_match = _JOB_LINK_RE.search(detail_href)
            title_span = item.find("span", id=re.compile(r"^j\d+$"))
            raw_title = title_span.get_text(" ", strip=True) if title_span else detail_link.get_text(" ", strip=True)
            title = _clean_text(raw_title)
            if not title:
                continue

            meta_span = item.find("span", class_="purplesml")
            meta_text = _clean_text(meta_span.get_text(" ", strip=True) if meta_span else "")
            if not _is_available(meta_text, today=today):
                continue

            location = _extract_location(meta_text)
            description_parts = [
                f"Institution: {institution}" if institution else "",
                f"AcademicJobsOnline ID: {job_id_match.group(1)}" if job_id_match else "",
                f"Listing metadata: {meta_text}" if meta_text else "",
            ]
            description = "\n".join(part for part in description_parts if part)
            display_title = f"{institution} - {title}" if institution else title

            seen_urls.add(url)
            jobs.append(
                {
                    "url": url,
                    "application_url": _application_url(item, detail_href) or url,
                    "title": display_title,
                    "salary": None,
                    "location": location,
                    "description": description[:500] if description else None,
                    "full_description": None,
                    "updated_at": None,
                    "site": source_name,
                    "strategy": STRATEGY,
                }
            )

    return jobs


def academicjobsonline_list_jobs(board_key: str, board: dict, timeout: int = 45) -> list[dict]:
    """Fetch one AcademicJobsOnline discipline page."""
    url = str(board.get("url") or "").strip()
    if not url:
        raise ValueError(f"AcademicJobsOnline board {board_key} requires url")

    html = fetch_text(url, timeout=timeout)
    return parse_academicjobsonline_jobs(html, board)


def run_academicjobsonline_discovery(
    boards: dict | None = None,
    workers: int = 1,
    progress: Progress | None = None,
    task_id: int | None = None,
) -> dict:
    """Fetch curated AcademicJobsOnline boards and store matching postings."""
    return run_board_discovery(
        provider_key="academicjobsonline",
        provider_label="AcademicJobsOnline",
        strategy=STRATEGY,
        boards=boards,
        load_default_boards=load_boards,
        fetch_jobs=academicjobsonline_list_jobs,
        max_tier_key="academicjobsonline_max_tier",
        timeout_key="academicjobsonline_timeout_seconds",
        default_timeout=45,
        workers=workers,
        progress=progress,
        task_id=task_id,
        progress_style="[bright_cyan]",
    )
