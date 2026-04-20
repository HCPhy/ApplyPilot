"""Oracle Cloud Recruiting Candidate Experience discovery."""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

import yaml
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn

from applypilot import config
from applypilot.config import CONFIG_DIR
from applypilot.database import get_connection, init_db, prune_stale_jobs
from applypilot.discovery.location_filters import location_ok, normalize_location_preferences
from applypilot.discovery.public_boards import (
    extract_queries,
    fetch_json,
    posted_recently,
    setup_proxy,
    source_default_queries,
    store_results,
    title_allowed,
)
from applypilot.discovery.query_match import query_to_search_text
from applypilot.discovery.workday import strip_html
from applypilot.ui import console

log = logging.getLogger(__name__)

STRATEGY = "oracle_cloud"
DEFAULT_PAGE_SIZE = 25
DEFAULT_MAX_PAGES = 4


def load_boards() -> dict:
    """Load curated Oracle Cloud Recruiting boards from config/oracle.yaml."""
    path = CONFIG_DIR / "oracle.yaml"
    if not path.exists():
        log.warning("oracle.yaml not found at %s", path)
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("boards", {}) or {}


def _api_base(board: dict) -> str:
    return str(board["api_base_url"]).rstrip("/")


def _site_number(board: dict) -> str:
    return str(board["site_number"]).strip()


def _site_path(board: dict) -> str:
    return str(board.get("site_path") or _site_number(board)).strip("/")


def _ui_base(board: dict) -> str:
    return str(board.get("ui_base_url") or _api_base(board)).rstrip("/")


def _job_url(board: dict, job_id: str) -> str:
    public_base = str(board.get("public_role_base_url") or "").rstrip("/")
    if public_base:
        return f"{public_base}/{quote(job_id, safe='')}"
    return f"{_ui_base(board)}/hcmUI/CandidateExperience/en/sites/{_site_path(board)}/job/{quote(job_id, safe='')}"


def _apply_url(board: dict, job_id: str) -> str:
    return f"{_ui_base(board)}/hcmUI/CandidateExperience/en/sites/{_site_path(board)}/job/{quote(job_id, safe='')}/apply/email"


def _salary_from_detail(detail: dict, full_description: str) -> str | None:
    for item in detail.get("requisitionFlexFields") or []:
        prompt = str(item.get("Prompt") or "")
        value = str(item.get("Value") or "").strip()
        if value and re.search(r"base pay|salary|compensation", prompt, flags=re.IGNORECASE):
            return value

    patterns = (
        r"salary range[^$]*?(\$[\d,]+(?:\.\d+)?\s*[-–]\s*\$[\d,]+(?:\.\d+)?)",
        r"base salary[^$]*?(\$[\d,]+(?:\.\d+)?\s*[-–]\s*\$[\d,]+(?:\.\d+)?)",
        r"base pay[^$]*?(\$[\d,]+(?:\.\d+)?\s*[-–]\s*\$[\d,]+(?:\.\d+)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, full_description or "", flags=re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return None


def parse_oracle_list_job(raw_job: dict, board: dict) -> dict:
    """Parse one Oracle requisition search item into ApplyPilot's job shape."""
    job_id = str(raw_job.get("Id") or "").strip()
    if not job_id:
        return {}

    short_description = strip_html(str(raw_job.get("ShortDescriptionStr") or ""))
    return {
        "url": _job_url(board, job_id),
        "title": raw_job.get("Title") or "",
        "salary": None,
        "description": short_description,
        "location": raw_job.get("PrimaryLocation") or "",
        "site": board.get("name", "Oracle Cloud Careers"),
        "strategy": STRATEGY,
        "updated_at": raw_job.get("PostedDate"),
        "full_description": short_description,
        "application_url": _apply_url(board, job_id),
        "oracle_id": job_id,
    }


def merge_oracle_detail(board: dict, job: dict, detail: dict) -> dict:
    """Merge an Oracle requisition detail payload into a parsed list job."""
    parts = [
        detail.get("ExternalDescriptionStr"),
        detail.get("ExternalResponsibilitiesStr"),
        detail.get("ExternalQualificationsStr"),
        detail.get("OrganizationDescriptionStr"),
        detail.get("CorporateDescriptionStr"),
    ]
    full_description = "\n\n".join(part for part in (strip_html(str(p or "")) for p in parts) if part)
    if full_description:
        job["full_description"] = full_description
        job["description"] = full_description[:500]
    job["salary"] = _salary_from_detail(detail, full_description) or job.get("salary")
    job["updated_at"] = detail.get("ExternalPostedStartDate") or job.get("updated_at")
    if detail.get("PrimaryLocation"):
        job["location"] = detail["PrimaryLocation"]
    job["application_url"] = _apply_url(board, str(job.get("oracle_id") or detail.get("Id") or ""))
    return job


def oracle_search_jobs(
    board: dict,
    query: str,
    offset: int = 0,
    limit: int = DEFAULT_PAGE_SIZE,
    timeout: int = 45,
) -> tuple[list[dict], int]:
    """Fetch one Oracle Cloud Recruiting search page."""
    finder = f"findReqs;siteNumber={_site_number(board)},keyword={query_to_search_text(query)}"
    params = {
        "onlyData": "true",
        "expand": "all",
        "limit": str(limit),
        "offset": str(offset),
        "finder": finder,
    }
    url = f"{_api_base(board)}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?{urlencode(params, safe=';,=')}"
    data = fetch_json(url, timeout=timeout)
    if not isinstance(data, dict):
        raise ValueError("Oracle search returned an unexpected response shape")
    search_items = data.get("items") or []
    if not search_items:
        return [], 0
    item = search_items[0]
    if not isinstance(item, dict):
        raise ValueError("Oracle search item returned an unexpected response shape")
    raw_jobs = item.get("requisitionList") or []
    if not isinstance(raw_jobs, list):
        raise ValueError("Oracle search returned an unexpected requisitionList shape")
    return raw_jobs, int(item.get("TotalJobsCount") or len(raw_jobs))


def oracle_detail(board: dict, job_id: str, timeout: int = 45) -> dict:
    """Fetch one Oracle Cloud Recruiting requisition detail payload."""
    finder = f"ById;Id={job_id},siteNumber={_site_number(board)}"
    params = {
        "expand": "all",
        "onlyData": "true",
        "finder": finder,
    }
    url = (
        f"{_api_base(board)}/hcmRestApi/resources/latest/"
        f"recruitingCEJobRequisitionDetails?{urlencode(params, safe=';,=')}"
    )
    data = fetch_json(url, timeout=timeout)
    if not isinstance(data, dict):
        raise ValueError("Oracle detail returned an unexpected response shape")
    items = data.get("items") or []
    if not items:
        return {}
    detail = items[0]
    if not isinstance(detail, dict):
        raise ValueError("Oracle detail item returned an unexpected response shape")
    return detail


def _filter_jobs(jobs: list[dict], queries: list[str], search_cfg: dict) -> list[dict]:
    accept_locs, reject_locs = normalize_location_preferences(search_cfg)
    exclude_titles = search_cfg.get("exclude_titles", [])
    hours_old = int(search_cfg.get("defaults", {}).get("hours_old", 72))

    filtered: dict[str, dict] = {}
    for job in jobs:
        title = str(job.get("title") or "")
        location = str(job.get("location") or "")
        if not posted_recently(job.get("updated_at"), hours_old):
            continue
        if not title_allowed(title, queries, exclude_titles):
            continue
        if not location_ok(location, accept_locs, reject_locs):
            continue
        filtered[job["url"]] = job
    return list(filtered.values())


def _enrich_jobs(board: dict, jobs: list[dict], timeout: int) -> list[dict]:
    enriched: list[dict] = []
    for job in jobs:
        job_id = str(job.get("oracle_id") or "").strip()
        if not job_id:
            enriched.append(job)
            continue
        try:
            detail = oracle_detail(board, job_id, timeout=timeout)
            if detail:
                job = merge_oracle_detail(board, job, detail)
        except Exception as exc:
            job["detail_error"] = str(exc)
        enriched.append(job)
    return enriched


def _crawl_board(board_key: str, board: dict, search_cfg: dict, timeout: int) -> dict:
    queries = extract_queries(search_cfg, "oracle_max_tier")
    default_queries = source_default_queries("oracle", board, search_cfg, "oracle_max_tier")
    effective_queries = list(dict.fromkeys([*queries, *default_queries]))
    max_pages = int(search_cfg.get("oracle_max_pages", DEFAULT_MAX_PAGES))
    page_size = int(search_cfg.get("oracle_page_size", DEFAULT_PAGE_SIZE))

    jobs_by_url: dict[str, dict] = {}
    pages = 0
    errors = 0

    for query in effective_queries:
        for page in range(max_pages):
            offset = page * page_size
            try:
                raw_jobs, total = oracle_search_jobs(board, query, offset=offset, limit=page_size, timeout=timeout)
            except Exception as exc:
                errors += 1
                log.error("%s: Oracle search failed for query %r offset %d: %s", board.get("name", board_key), query, offset, exc)
                break

            pages += 1
            for raw_job in raw_jobs:
                job = parse_oracle_list_job(raw_job, board)
                if job.get("url"):
                    jobs_by_url[job["url"]] = job

            if not raw_jobs or len(raw_jobs) < page_size or offset + page_size >= total:
                break

    matched = _filter_jobs(list(jobs_by_url.values()), effective_queries, search_cfg)
    if matched:
        matched = _enrich_jobs(board, matched, timeout)

    conn = get_connection()
    seen_at = search_cfg["_seen_at"]
    new, existing = store_results(conn, matched, strategy=STRATEGY, seen_at=seen_at)

    return {
        "board": board.get("name", board_key),
        "board_key": board_key,
        "found": len(matched),
        "new": new,
        "existing": existing,
        "pages": pages,
        "errors": errors,
    }


def run_oracle_discovery(
    boards: dict | None = None,
    workers: int = 1,
    progress: Progress | None = None,
    task_id: int | None = None,
) -> dict:
    """Run Oracle Cloud Recruiting discovery across configured boards."""
    if boards is None:
        boards = load_boards()

    if not boards:
        log.warning("No Oracle Cloud boards configured. Create config/oracle.yaml.")
        return {"found": 0, "new": 0, "existing": 0, "boards": 0, "errors": 0}

    init_db()
    search_cfg = config.load_search_config()
    search_cfg["_seen_at"] = datetime.now(timezone.utc).isoformat()
    timeout = int(search_cfg.get("oracle_timeout_seconds", 45))
    prune_stale = search_cfg.get("prune_stale_jobs", True)

    proxy = search_cfg.get("proxy")
    if proxy:
        setup_proxy(proxy)

    board_items = list(boards.items())
    total_new = 0
    total_existing = 0
    total_found = 0
    errors = 0
    pages = 0
    successful_board_keys: set[str] = set()
    error_board_keys: set[str] = set()
    t0 = time.time()

    owns_progress = progress is None
    if owns_progress:
        progress = Progress(
            TextColumn("[bright_blue]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            MofNCompleteColumn(),
            TextColumn("[dim]board:[/dim] {task.fields[target]}"),
            TextColumn("[dim]pages:[/dim] {task.fields[pages]}"),
            TextColumn("[dim]new:[/dim] {task.fields[new]}"),
            TextColumn("[dim]dupes:[/dim] {task.fields[dupes]}"),
            TextColumn("[dim]err:[/dim] {task.fields[errors]}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )

    def _record_result(i: int, result: dict) -> None:
        nonlocal total_new, total_existing, total_found, errors, pages
        assert progress is not None
        assert task_id is not None
        total_new += result["new"]
        total_existing += result["existing"]
        total_found += result["found"]
        pages += result["pages"]
        if result["errors"]:
            errors += result["errors"]
            error_board_keys.add(result["board_key"])
        else:
            successful_board_keys.add(result["board_key"])
        progress.update(
            task_id,
            advance=1,
            target=result["board"],
            pages=pages,
            new=total_new,
            dupes=total_existing,
            errors=errors,
        )
        if i % 10 == 0 or i == len(board_items):
            elapsed = time.time() - t0
            log.info(
                "Oracle progress: %d/%d boards (%d new, %d dupes, %d errors) [%.0fs]",
                i, len(board_items), total_new, total_existing, errors, elapsed,
            )

    def _run_boards() -> None:
        if workers > 1 and len(board_items) > 1:
            with ThreadPoolExecutor(max_workers=min(workers, len(board_items))) as pool:
                futures = {
                    pool.submit(_crawl_board, board_key, board, dict(search_cfg), timeout): board_key
                    for board_key, board in board_items
                }
                for i, future in enumerate(as_completed(futures), 1):
                    _record_result(i, future.result())
            return

        for i, (board_key, board) in enumerate(board_items, 1):
            _record_result(i, _crawl_board(board_key, board, dict(search_cfg), timeout))

    if owns_progress:
        with progress:
            task_id = progress.add_task(
                "Oracle Cloud crawl",
                total=len(board_items),
                target="-",
                pages=0,
                new=0,
                dupes=0,
                errors=0,
            )
            _run_boards()
    else:
        assert progress is not None
        assert task_id is not None
        progress.update(task_id, total=len(board_items), completed=0, target="-", pages=0, new=0, dupes=0, errors=0)
        _run_boards()

    stale_removed = 0
    if prune_stale:
        conn = get_connection()
        cleanup_keys = sorted(successful_board_keys - error_board_keys)
        seen_at = search_cfg["_seen_at"]
        for key in cleanup_keys:
            site = boards[key].get("name", key)
            stale_removed += prune_stale_jobs(conn, site=site, strategies=(STRATEGY,), seen_at=seen_at)
        if stale_removed:
            log.info("Oracle stale cleanup: removed %d old jobs across %d successful boards", stale_removed, len(cleanup_keys))
        if error_board_keys:
            log.info("Oracle stale cleanup: skipped %d boards with crawl errors", len(error_board_keys))

    elapsed = time.time() - t0
    log.info(
        "Oracle crawl done: %d found, %d new, %d existing across %d boards in %.0fs",
        total_found, total_new, total_existing, len(board_items), elapsed,
    )
    return {
        "found": total_found,
        "new": total_new,
        "existing": total_existing,
        "boards": len(board_items),
        "errors": errors,
        "stale_removed": stale_removed,
    }
