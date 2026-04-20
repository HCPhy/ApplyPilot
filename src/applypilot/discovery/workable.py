"""Workable public account API discovery for curated company boards."""

from __future__ import annotations

import logging
import urllib.parse
from html import unescape

from rich.progress import Progress

from applypilot.discovery.public_boards import fetch_json, load_boards as load_registry, run_board_discovery
from applypilot.discovery.workday import strip_html

log = logging.getLogger(__name__)

STRATEGY = "workable_api"
DEFAULT_API_BASE = "https://www.workable.com/api/accounts"


def load_boards() -> dict:
    """Load curated Workable boards from config/workable.yaml."""
    return load_registry("workable.yaml")


def _first_text(*values) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _extract_items(payload: dict | list) -> list[dict]:
    """Normalize public Workable response shapes to a list."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("jobs", "results", "data"):
            items = payload.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return []


def _compose_location(raw_job: dict) -> str:
    """Build a readable Workable location string."""
    raw_location = raw_job.get("location")
    workplace_label = ""

    if isinstance(raw_location, dict):
        location = _first_text(raw_location.get("location_str"))
        if not location:
            parts = [
                raw_location.get("city"),
                raw_location.get("region") or raw_location.get("region_code"),
                raw_location.get("country") or raw_location.get("country_code"),
            ]
            location = ", ".join(str(part).strip() for part in parts if str(part or "").strip())
        workplace = _first_text(raw_location.get("workplace_type"))
        if workplace:
            workplace_label = workplace.replace("_", " ").title()
        if raw_location.get("telecommuting") and "remote" not in location.lower():
            workplace_label = "Remote"
    else:
        location = _first_text(raw_location)

    if workplace_label and workplace_label.lower() not in location.lower():
        return f"{location} ({workplace_label})" if location else workplace_label
    return location


def _format_salary(raw_job: dict) -> str | None:
    salary = raw_job.get("salary")
    if not isinstance(salary, dict):
        return _first_text(salary) or None

    minimum = salary.get("salary_from") or salary.get("min")
    maximum = salary.get("salary_to") or salary.get("max")
    currency = _first_text(salary.get("salary_currency"), salary.get("currency")).upper()
    if minimum is None and maximum is None:
        return None

    def _fmt(value) -> str:
        try:
            return f"{float(value):,.0f}"
        except (TypeError, ValueError):
            return str(value)

    if minimum is not None and maximum is not None:
        value = f"{_fmt(minimum)}-{_fmt(maximum)}"
    else:
        value = _fmt(minimum if minimum is not None else maximum)
    return f"{currency} {value}".strip()


def _description(raw_job: dict) -> str:
    sections = []
    for key in ("description", "requirements", "benefits"):
        value = _first_text(raw_job.get(key))
        if value:
            sections.append(strip_html(unescape(value)))
    return "\n\n".join(section for section in sections if section).strip()


def _parse_job(board: dict, raw_job: dict) -> dict:
    """Convert a raw Workable public job into ApplyPilot's job shape."""
    full_description = _description(raw_job)
    url = _first_text(raw_job.get("url"), raw_job.get("shortlink"))
    apply_url = _first_text(raw_job.get("application_url"), raw_job.get("apply_url"), url)

    return {
        "url": url,
        "application_url": apply_url or url,
        "title": _first_text(raw_job.get("title"), raw_job.get("full_title")),
        "salary": _format_salary(raw_job),
        "location": _compose_location(raw_job),
        "description": full_description[:500] if full_description else None,
        "full_description": full_description if full_description else None,
        "updated_at": raw_job.get("published_at") or raw_job.get("created_at") or raw_job.get("updated_at"),
        "site": board.get("name", "Workable"),
        "strategy": STRATEGY,
    }


def workable_list_jobs(board_key: str, board: dict, timeout: int = 45) -> list[dict]:
    """Fetch all public jobs for a Workable account."""
    subdomain = str(board.get("subdomain") or board.get("account") or board_key).strip()
    if not subdomain:
        raise ValueError("Workable board requires subdomain")

    api_base = str(board.get("api_base") or DEFAULT_API_BASE).rstrip("/")
    params = urllib.parse.urlencode({"details": "true"})
    url = f"{api_base}/{urllib.parse.quote(subdomain, safe='')}?{params}"
    payload = fetch_json(url, timeout=timeout)
    items = _extract_items(payload)
    return [_parse_job(board, raw_job) for raw_job in items]


def run_workable_discovery(
    boards: dict | None = None,
    workers: int = 1,
    progress: Progress | None = None,
    task_id: int | None = None,
) -> dict:
    """Fetch curated Workable boards and store matching jobs."""
    return run_board_discovery(
        provider_key="workable",
        provider_label="Workable",
        strategy=STRATEGY,
        boards=boards,
        load_default_boards=load_boards,
        fetch_jobs=workable_list_jobs,
        max_tier_key="workable_max_tier",
        timeout_key="workable_timeout_seconds",
        default_timeout=45,
        workers=workers,
        progress=progress,
        task_id=task_id,
        progress_style="[bright_green]",
    )
