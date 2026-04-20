"""Recruitee Careers Site API discovery for curated company boards."""

from __future__ import annotations

import logging
import urllib.parse
from html import unescape

from rich.progress import Progress

from applypilot.discovery.public_boards import fetch_json, load_boards as load_registry, run_board_discovery
from applypilot.discovery.workday import strip_html

log = logging.getLogger(__name__)

STRATEGY = "recruitee_api"


def load_boards() -> dict:
    """Load curated Recruitee boards from config/recruitee.yaml."""
    return load_registry("recruitee.yaml")


def _first_text(*values) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _extract_items(payload: dict | list) -> list[dict]:
    """Normalize common Recruitee public response shapes to a list."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("offers", "jobs", "data"):
            items = payload.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return []


def _compose_location(raw_job: dict) -> str:
    """Build a readable Recruitee location string."""
    raw_location = raw_job.get("location")
    if isinstance(raw_location, dict):
        parts = [
            raw_location.get("city"),
            raw_location.get("state") or raw_location.get("region"),
            raw_location.get("country"),
        ]
        location = ", ".join(str(part).strip() for part in parts if str(part or "").strip())
    else:
        location = _first_text(raw_location, raw_job.get("location_name"))

    remote_value = raw_job.get("remote")
    if remote_value and "remote" not in location.lower():
        location = f"{location} (Remote)" if location else "Remote"
    return location


def _description(raw_job: dict) -> str:
    sections = []
    for key in ("description", "requirements", "offer", "benefits"):
        value = _first_text(raw_job.get(key))
        if value:
            sections.append(strip_html(unescape(value)))
    return "\n\n".join(section for section in sections if section).strip()


def _build_url(board: dict, raw_job: dict) -> str:
    explicit = _first_text(raw_job.get("careers_url"), raw_job.get("url"))
    if explicit:
        return explicit

    subdomain = _first_text(board.get("subdomain"), board.get("company"))
    slug = _first_text(raw_job.get("slug"))
    if subdomain and slug:
        return f"https://{subdomain}.recruitee.com/o/{urllib.parse.quote(slug, safe='')}"
    return ""


def _parse_job(board: dict, raw_job: dict) -> dict:
    """Convert a raw Recruitee offer into ApplyPilot's job shape."""
    full_description = _description(raw_job)
    url = _build_url(board, raw_job)
    apply_url = _first_text(raw_job.get("careers_apply_url"), raw_job.get("apply_url"), url)

    return {
        "url": url,
        "application_url": apply_url or url,
        "title": _first_text(raw_job.get("title"), raw_job.get("name")),
        "salary": _first_text(raw_job.get("salary")) or None,
        "location": _compose_location(raw_job),
        "description": full_description[:500] if full_description else None,
        "full_description": full_description if full_description else None,
        "updated_at": raw_job.get("published_at") or raw_job.get("created_at") or raw_job.get("updated_at"),
        "site": board.get("name", "Recruitee"),
        "strategy": STRATEGY,
    }


def recruitee_list_jobs(board_key: str, board: dict, timeout: int = 45) -> list[dict]:
    """Fetch published jobs for a Recruitee careers site."""
    subdomain = str(board.get("subdomain") or board.get("company") or board_key).strip()
    if not subdomain:
        raise ValueError("Recruitee board requires subdomain")

    base_url = str(board.get("base_url") or f"https://{subdomain}.recruitee.com/api/offers/").rstrip("/")
    payload = fetch_json(base_url, timeout=timeout)
    items = _extract_items(payload)
    return [_parse_job(board, raw_job) for raw_job in items]


def run_recruitee_discovery(
    boards: dict | None = None,
    workers: int = 1,
    progress: Progress | None = None,
    task_id: int | None = None,
) -> dict:
    """Fetch curated Recruitee boards and store matching jobs."""
    return run_board_discovery(
        provider_key="recruitee",
        provider_label="Recruitee",
        strategy=STRATEGY,
        boards=boards,
        load_default_boards=load_boards,
        fetch_jobs=recruitee_list_jobs,
        max_tier_key="recruitee_max_tier",
        timeout_key="recruitee_timeout_seconds",
        default_timeout=45,
        workers=workers,
        progress=progress,
        task_id=task_id,
        progress_style="[bright_blue]",
    )
