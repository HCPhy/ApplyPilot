"""Ashby Job Postings API discovery for curated company boards."""

from __future__ import annotations

import logging
import urllib.parse
from html import unescape

from rich.progress import Progress

from applypilot.discovery.public_boards import fetch_json, load_boards as load_registry, run_board_discovery
from applypilot.discovery.workday import strip_html

log = logging.getLogger(__name__)

STRATEGY = "ashby_api"
DEFAULT_API_BASE = "https://api.ashbyhq.com/posting-api/job-board"


def load_boards() -> dict:
    """Load curated Ashby boards from config/ashby.yaml."""
    return load_registry("ashby.yaml")


def _first_text(*values) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _dedupe_join(values: list[str]) -> str:
    cleaned = [value.strip() for value in values if value and value.strip()]
    return "; ".join(dict.fromkeys(cleaned))


def _compose_location(raw_job: dict) -> str:
    """Build a readable Ashby location string."""
    locations = []
    primary = _first_text(raw_job.get("location"))
    if primary:
        locations.append(primary)

    for item in raw_job.get("secondaryLocations") or []:
        if not isinstance(item, dict):
            continue
        secondary = _first_text(item.get("location"))
        if secondary:
            locations.append(secondary)

    workplace_type = _first_text(raw_job.get("workplaceType"))
    if raw_job.get("isRemote") and "remote" not in " ".join(locations).lower():
        locations.append("Remote")
    elif workplace_type and workplace_type.lower() not in " ".join(locations).lower():
        label = {"onsite": "On-site", "on site": "On-site"}.get(workplace_type.lower(), workplace_type)
        locations.append(label)

    return _dedupe_join(locations)


def _format_salary(raw_job: dict) -> str | None:
    """Extract the most useful Ashby compensation summary."""
    compensation = raw_job.get("compensation")
    if not isinstance(compensation, dict):
        return None

    for key in ("compensationTierSummary", "scrapeableCompensationSalarySummary"):
        value = _first_text(compensation.get(key))
        if value:
            return value

    component_summaries: list[str] = []
    for component in compensation.get("summaryComponents") or []:
        if isinstance(component, dict):
            summary = _first_text(component.get("summary"))
            if summary:
                component_summaries.append(summary)

    if component_summaries:
        return " | ".join(dict.fromkeys(component_summaries))
    return None


def _description(raw_job: dict) -> str:
    plain = _first_text(raw_job.get("descriptionPlain"))
    if plain:
        return plain
    html = _first_text(raw_job.get("descriptionHtml"))
    return strip_html(unescape(html)) if html else ""


def _parse_job(board: dict, raw_job: dict) -> dict:
    """Convert a raw Ashby job posting into ApplyPilot's job shape."""
    full_description = _description(raw_job)
    job_url = _first_text(raw_job.get("jobUrl"), raw_job.get("url"), raw_job.get("applyUrl"))
    apply_url = _first_text(raw_job.get("applyUrl"), job_url)

    return {
        "url": job_url,
        "application_url": apply_url or job_url,
        "title": _first_text(raw_job.get("title")),
        "salary": _format_salary(raw_job),
        "location": _compose_location(raw_job),
        "description": full_description[:500] if full_description else None,
        "full_description": full_description if full_description else None,
        "updated_at": raw_job.get("publishedAt"),
        "site": board.get("name", "Ashby"),
        "strategy": STRATEGY,
    }


def ashby_list_jobs(board_key: str, board: dict, timeout: int = 60) -> list[dict]:
    """Fetch all listed jobs for an Ashby board."""
    board_name = str(board.get("job_board_name") or board.get("board_name") or board_key).strip()
    if not board_name:
        raise ValueError("Ashby board requires job_board_name")

    api_base = str(board.get("api_base") or DEFAULT_API_BASE).rstrip("/")
    params = urllib.parse.urlencode({"includeCompensation": "true"})
    url = f"{api_base}/{urllib.parse.quote(board_name, safe='')}?{params}"
    payload = fetch_json(url, timeout=timeout)
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected Ashby response for {board_name}: expected object")

    jobs = payload.get("jobs") or []
    if not isinstance(jobs, list):
        raise ValueError(f"Unexpected Ashby jobs response for {board_name}: expected list")

    return [_parse_job(board, raw_job) for raw_job in jobs
            if isinstance(raw_job, dict) and raw_job.get("isListed", True)]


def run_ashby_discovery(
    boards: dict | None = None,
    workers: int = 1,
    progress: Progress | None = None,
    task_id: int | None = None,
) -> dict:
    """Fetch curated Ashby boards and store matching jobs."""
    return run_board_discovery(
        provider_key="ashby",
        provider_label="Ashby",
        strategy=STRATEGY,
        boards=boards,
        load_default_boards=load_boards,
        fetch_jobs=ashby_list_jobs,
        max_tier_key="ashby_max_tier",
        timeout_key="ashby_timeout_seconds",
        default_timeout=60,
        workers=workers,
        progress=progress,
        task_id=task_id,
        progress_style="[bright_magenta]",
    )
