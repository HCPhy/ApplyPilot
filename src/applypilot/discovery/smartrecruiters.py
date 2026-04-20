"""SmartRecruiters Posting API discovery for curated company boards."""

from __future__ import annotations

import logging
import urllib.parse
from html import unescape

from rich.progress import Progress

from applypilot.discovery.public_boards import fetch_json, load_boards as load_registry, run_board_discovery
from applypilot.discovery.workday import strip_html

log = logging.getLogger(__name__)

STRATEGY = "smartrecruiters_api"
DEFAULT_API_BASE = "https://api.smartrecruiters.com/v1/companies"


def load_boards() -> dict:
    """Load curated SmartRecruiters boards from config/smartrecruiters.yaml."""
    return load_registry("smartrecruiters.yaml")


def _first_text(*values) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _compose_location(raw_job: dict) -> str:
    """Build a readable SmartRecruiters location string."""
    location = raw_job.get("location") or {}
    if not isinstance(location, dict):
        return _first_text(location)

    parts = [location.get("city"), location.get("region"), location.get("country")]
    text = ", ".join(str(part).strip() for part in parts if str(part or "").strip())
    if location.get("remote") and "remote" not in text.lower():
        text = f"{text} (Remote)" if text else "Remote"
    return text


def _format_salary(raw_job: dict) -> str | None:
    compensation = raw_job.get("compensation")
    if not isinstance(compensation, dict):
        return None
    for key in ("salarySummary", "summary", "compensationTierSummary"):
        value = _first_text(compensation.get(key))
        if value:
            return value
    return None


def _parse_list_job(board: dict, raw_job: dict) -> dict:
    """Convert a SmartRecruiters posting list item into ApplyPilot's shape."""
    posting_id = _first_text(raw_job.get("id"), raw_job.get("uuid"))
    company = raw_job.get("company") if isinstance(raw_job.get("company"), dict) else {}
    identifier = _first_text(company.get("identifier"), board.get("company_identifier"))
    fallback_url = f"https://jobs.smartrecruiters.com/{identifier}/{posting_id}" if identifier and posting_id else ""

    return {
        "url": _first_text(raw_job.get("postingUrl"), raw_job.get("jobAdUrl"), fallback_url, raw_job.get("ref")),
        "application_url": _first_text(raw_job.get("applyUrl")),
        "title": _first_text(raw_job.get("name"), raw_job.get("title")),
        "salary": _format_salary(raw_job),
        "location": _compose_location(raw_job),
        "description": None,
        "full_description": None,
        "updated_at": raw_job.get("releasedDate") or raw_job.get("postedDate"),
        "site": board.get("name", "SmartRecruiters"),
        "strategy": STRATEGY,
        "_posting_id": posting_id,
        "_company_identifier": identifier,
    }


def _description_from_detail(detail: dict) -> str:
    """Extract a plain-text description from a SmartRecruiters posting detail."""
    sections = (((detail.get("jobAd") or {}).get("sections")) or {})
    if not isinstance(sections, dict):
        return ""

    text_sections: list[str] = []
    for section in sections.values():
        if not isinstance(section, dict):
            continue
        title = _first_text(section.get("title"))
        text = _first_text(section.get("text"))
        if not text:
            continue
        plain = strip_html(unescape(text))
        text_sections.append(f"{title}\n{plain}" if title else plain)

    return "\n\n".join(text_sections).strip()


def _merge_detail(job: dict, detail: dict) -> dict:
    """Merge SmartRecruiters posting detail into a parsed list job."""
    full_description = _description_from_detail(detail)
    url = _first_text(detail.get("postingUrl"), job.get("url"))
    apply_url = _first_text(detail.get("applyUrl"), job.get("application_url"), url)

    merged = dict(job)
    merged.update({
        "url": url,
        "application_url": apply_url or url,
        "salary": _format_salary(detail) or job.get("salary"),
        "description": full_description[:500] if full_description else job.get("description"),
        "full_description": full_description if full_description else job.get("full_description"),
        "updated_at": detail.get("releasedDate") or job.get("updated_at"),
    })
    return merged


def smartrecruiters_list_jobs(board_key: str, board: dict, timeout: int = 60) -> list[dict]:
    """Fetch active public postings for a SmartRecruiters company."""
    company_identifier = str(board.get("company_identifier") or board.get("identifier") or board_key).strip()
    if not company_identifier:
        raise ValueError("SmartRecruiters board requires company_identifier")

    api_base = str(board.get("api_base") or DEFAULT_API_BASE).rstrip("/")
    page_size = int(board.get("page_size", 100))
    max_pages = int(board.get("max_pages", 25))
    offset = 0
    jobs: list[dict] = []

    while True:
        params = urllib.parse.urlencode({
            "limit": page_size,
            "offset": offset,
            "destination": "PUBLIC",
        })
        url = f"{api_base}/{urllib.parse.quote(company_identifier, safe='')}/postings?{params}"
        payload = fetch_json(url, timeout=timeout)
        if not isinstance(payload, dict):
            raise ValueError(f"Unexpected SmartRecruiters response for {company_identifier}: expected object")

        content = payload.get("content") or payload.get("jobs") or []
        if not isinstance(content, list):
            raise ValueError(f"Unexpected SmartRecruiters postings response for {company_identifier}: expected list")

        jobs.extend(_parse_list_job(board, raw_job) for raw_job in content if isinstance(raw_job, dict))

        total_found = int(payload.get("totalFound") or len(jobs))
        if len(content) < page_size or len(jobs) >= total_found:
            break
        offset += page_size
        if offset >= page_size * max_pages:
            log.info("%s: capped SmartRecruiters crawl at %d pages", board.get("name", board_key), max_pages)
            break

    return jobs


def _detail_url(board: dict, job: dict) -> str:
    posting_id = _first_text(job.get("_posting_id"))
    company_identifier = str(
        board.get("company_identifier") or board.get("identifier") or job.get("_company_identifier") or ""
    ).strip()
    api_base = str(board.get("api_base") or DEFAULT_API_BASE).rstrip("/")
    company_path = urllib.parse.quote(company_identifier, safe="")
    posting_path = urllib.parse.quote(posting_id, safe="")
    return f"{api_base}/{company_path}/postings/{posting_path}"


def _enrich_matched_jobs(board: dict, jobs: list[dict], timeout: int = 60) -> list[dict]:
    """Fetch posting details for already-matched SmartRecruiters jobs."""
    enriched: list[dict] = []
    for job in jobs:
        posting_id = _first_text(job.get("_posting_id"))
        if not posting_id:
            enriched.append(job)
            continue
        try:
            detail = fetch_json(_detail_url(board, job), timeout=timeout)
            if isinstance(detail, dict):
                enriched.append(_merge_detail(job, detail))
            else:
                enriched.append(job)
        except Exception as e:
            log.warning(
                "%s: SmartRecruiters detail error for %s: %s",
                board.get("name", "SmartRecruiters"),
                posting_id,
                e,
            )
            fallback = dict(job)
            fallback["detail_error"] = str(e)
            enriched.append(fallback)
    return enriched


def run_smartrecruiters_discovery(
    boards: dict | None = None,
    workers: int = 1,
    progress: Progress | None = None,
    task_id: int | None = None,
) -> dict:
    """Fetch curated SmartRecruiters boards and store matching jobs."""
    return run_board_discovery(
        provider_key="smartrecruiters",
        provider_label="SmartRecruiters",
        strategy=STRATEGY,
        boards=boards,
        load_default_boards=load_boards,
        fetch_jobs=smartrecruiters_list_jobs,
        max_tier_key="smartrecruiters_max_tier",
        timeout_key="smartrecruiters_timeout_seconds",
        default_timeout=60,
        workers=workers,
        progress=progress,
        task_id=task_id,
        enrich_matched_jobs=_enrich_matched_jobs,
        progress_style="[bright_yellow]",
    )
