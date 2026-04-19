"""Lever Postings API discovery for curated company boards.

Fetches public Lever-hosted job listings via:
  GET https://api.lever.co/v0/postings/{site}?mode=json

Lever exposes structured filters like team and location, but not the free-text
role search ApplyPilot is built around. This module therefore fetches each
configured company board and filters jobs locally against the user's queries,
location preferences, recency window, and excluded title patterns.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html import unescape

import yaml
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn

from applypilot import config
from applypilot.config import CONFIG_DIR
from applypilot.database import get_connection, init_db, prune_stale_jobs
from applypilot.discovery.dates import normalize_posted_at
from applypilot.discovery.location_filters import location_ok, normalize_location_preferences
from applypilot.discovery.query_match import matches_query
from applypilot.discovery.workday import strip_html
from applypilot.ui import console

log = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.lever.co/v0/postings"


def load_boards() -> dict:
    """Load curated Lever boards from config/lever.yaml."""
    path = CONFIG_DIR / "lever.yaml"
    if not path.exists():
        log.warning("lever.yaml not found at %s", path)
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("boards", {})


_opener = None


def setup_proxy(proxy_str: str | None) -> None:
    """Configure a global urllib opener with optional proxy support."""
    global _opener
    if not proxy_str:
        _opener = urllib.request.build_opener()
        return

    parts = proxy_str.split(":")
    if len(parts) == 4:
        host, port, user, passwd = parts
        proxy_url = f"http://{user}:{passwd}@{host}:{port}"
    elif len(parts) == 2:
        proxy_url = f"http://{parts[0]}:{parts[1]}"
    else:
        log.warning(
            "Proxy format not recognized: %s (expected host:port:user:pass or host:port)",
            proxy_str,
        )
        _opener = urllib.request.build_opener()
        return

    proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    _opener = urllib.request.build_opener(proxy_handler)


def _urlopen(req, timeout=30):
    """Open a URL using the configured opener."""
    if _opener:
        return _opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _load_location_filter(search_cfg: dict | None = None) -> tuple[list[str], list[str]]:
    """Load location accept/reject rules from either normalized or nested config."""
    if search_cfg is None:
        search_cfg = config.load_search_config()

    return normalize_location_preferences(search_cfg)


def _location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    """Check if a job location passes the user's location filter."""
    return location_ok(location, accept, reject)


def _extract_queries(search_cfg: dict) -> list[str]:
    """Extract the effective query list for Lever local matching."""
    queries_cfg = search_cfg.get("queries", [])
    max_tier = search_cfg.get("lever_max_tier", search_cfg.get("greenhouse_max_tier", 3))

    queries: list[str] = []
    fallback: list[str] = []
    for item in queries_cfg:
        if isinstance(item, str):
            value = item.strip()
            if value:
                fallback.append(value)
                queries.append(value)
            continue
        if not isinstance(item, dict):
            continue
        query = str(item.get("query", "")).strip()
        if not query:
            continue
        fallback.append(query)
        if item.get("tier", 99) <= max_tier:
            queries.append(query)

    return queries or fallback


def _normalize_tokens(text: str) -> list[str]:
    """Tokenize a query/title into simple lowercase words."""
    return [t for t in re.findall(r"[a-z0-9+#]+", text.lower()) if t]


def _matches_query(text: str, query: str) -> bool:
    """Return True if a job text matches a configured query."""
    return matches_query(text, query)


def _title_allowed(title: str, queries: list[str], exclude_titles: list[str]) -> bool:
    """Check include/exclude title rules against a Lever job title."""
    title_lower = title.lower()
    if any(bad.lower() in title_lower for bad in exclude_titles):
        return False
    if not queries:
        return True
    return any(_matches_query(title, query) for query in queries)


def _posted_recently(posted_at, hours_old: int) -> bool:
    """Check if a Lever timestamp falls within the allowed window."""
    if posted_at in (None, "") or hours_old <= 0:
        return True

    try:
        if isinstance(posted_at, (int, float)):
            ts = float(posted_at)
            if ts > 10_000_000_000:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            normalized = str(posted_at).replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc) - timedelta(hours=hours_old)
    except (TypeError, ValueError, OSError):
        return True


def lever_list_jobs(board: dict, page_size: int = 100, timeout: int = 60) -> list[dict]:
    """Fetch all published jobs for a Lever board."""
    site = board["site"]
    api_base = board.get("api_base", DEFAULT_API_BASE).rstrip("/")
    skip = 0
    max_pages = 25
    all_jobs: list[dict] = []

    while True:
        params = urllib.parse.urlencode({"mode": "json", "limit": page_size, "skip": skip})
        url = f"{api_base}/{urllib.parse.quote(site, safe='')}?{params}"
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

        with _urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())

        if not isinstance(payload, list):
            raise ValueError(f"Unexpected Lever response for {site}: expected list, got {type(payload).__name__}")

        all_jobs.extend(payload)
        if len(payload) < page_size:
            break

        skip += page_size
        if skip >= page_size * max_pages:
            log.info("%s: capped Lever crawl at %d pages", board.get("name", site), max_pages)
            break

    return all_jobs


def _compose_location(raw_job: dict) -> str:
    """Build a location string that preserves remote/hybrid hints."""
    categories = raw_job.get("categories") or {}
    location = str(categories.get("location") or "").strip()
    all_locations = [str(item).strip() for item in categories.get("allLocations") or [] if str(item).strip()]

    if not location and all_locations:
        location = ", ".join(dict.fromkeys(all_locations))

    workplace_type = str(raw_job.get("workplaceType") or "").strip().lower()
    label_map = {"remote": "Remote", "hybrid": "Hybrid", "on-site": "On-site"}
    label = label_map.get(workplace_type)
    if label and label.lower() not in location.lower():
        location = f"{location} ({label})" if location else label

    return location


def _format_salary(raw_job: dict) -> str | None:
    """Format Lever salary metadata into a compact string if available."""
    salary_desc = str(raw_job.get("salaryDescriptionPlain") or "").strip()
    salary_range = raw_job.get("salaryRange") or {}
    minimum = salary_range.get("min")
    maximum = salary_range.get("max")
    currency = str(salary_range.get("currency") or "").strip().upper()
    interval = str(salary_range.get("interval") or "").strip().lower()

    if minimum is None and maximum is None:
        return salary_desc or None

    interval_label = {
        "hourly": "/hr",
        "daily": "/day",
        "weekly": "/wk",
        "monthly": "/mo",
        "yearly": "/yr",
        "year": "/yr",
    }.get(interval, f"/{interval}" if interval else "")

    def _fmt_number(value) -> str:
        try:
            num = float(value)
            return f"{num:,.0f}"
        except (TypeError, ValueError):
            return str(value)

    if minimum is not None and maximum is not None:
        salary = f"{_fmt_number(minimum)}-{_fmt_number(maximum)}"
    else:
        single = minimum if minimum is not None else maximum
        salary = _fmt_number(single)

    prefix = f"{currency} " if currency else ""
    formatted = f"{prefix}{salary}{interval_label}".strip()
    return salary_desc or formatted or None


def _extract_lists_text(raw_job: dict) -> str:
    """Convert Lever list sections into plain text."""
    sections: list[str] = []
    for item in raw_job.get("lists") or []:
        if not isinstance(item, dict):
            continue
        heading = str(item.get("text") or "").strip()
        content = strip_html(unescape(str(item.get("content") or ""))).strip()
        if heading and content:
            sections.append(f"{heading}\n{content}")
        elif content:
            sections.append(content)
    return "\n\n".join(sections)


def _compose_description(raw_job: dict) -> str:
    """Build a full plain-text description from Lever posting fields."""
    sections: list[str] = []
    for key in ("descriptionPlain", "descriptionBodyPlain"):
        value = str(raw_job.get(key) or "").strip()
        if value:
            sections.append(value)

    lists_text = _extract_lists_text(raw_job)
    if lists_text:
        sections.append(lists_text)

    additional = str(raw_job.get("additionalPlain") or "").strip()
    if additional:
        sections.append(additional)

    return "\n\n".join(section for section in sections if section).strip()


def _parse_job(board: dict, raw_job: dict) -> dict:
    """Convert a raw Lever API job into ApplyPilot's job shape."""
    full_description = _compose_description(raw_job)
    location = _compose_location(raw_job)
    hosted_url = str(raw_job.get("hostedUrl") or "").strip()
    apply_url = str(raw_job.get("applyUrl") or hosted_url).strip()
    canonical_url = hosted_url or apply_url
    posted_at = raw_job.get("updatedAt") or raw_job.get("createdAt")

    return {
        "url": canonical_url,
        "application_url": apply_url or canonical_url,
        "title": str(raw_job.get("text") or "").strip(),
        "location": location,
        "salary": _format_salary(raw_job),
        "description": full_description[:500] if full_description else None,
        "full_description": full_description if full_description else None,
        "updated_at": posted_at,
        "site": board.get("name", "Lever"),
        "strategy": "lever_api",
    }


def _store_results(conn: sqlite3.Connection, jobs: list[dict], seen_at: str | None = None) -> tuple[int, int]:
    """Store Lever-discovered jobs in the DB with full descriptions."""
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    seen_at = seen_at or now
    new = 0
    existing = 0

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        full_description = job.get("full_description")
        detail_scraped_at = now if full_description else None
        posted_at = normalize_posted_at(job.get("updated_at"), now=now_dt)

        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, "
                "discovered_at, posted_at, last_seen_at, full_description, application_url, detail_scraped_at, detail_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    url,
                    job.get("title"),
                    job.get("salary"),
                    job.get("description"),
                    job.get("location"),
                    job.get("site"),
                    job.get("strategy", "lever_api"),
                    now,
                    posted_at,
                    seen_at,
                    full_description,
                    job.get("application_url") or url,
                    detail_scraped_at,
                    None,
                ),
            )
            new += 1
        except sqlite3.IntegrityError:
            conn.execute(
                "UPDATE jobs SET posted_at = COALESCE(posted_at, ?), last_seen_at = ? WHERE url = ?",
                (posted_at, seen_at, url),
            )
            existing += 1

    conn.commit()
    return new, existing


def _process_one(
    board_key: str,
    board: dict,
    queries: list[str],
    accept_locs: list[str],
    reject_locs: list[str],
    exclude_titles: list[str],
    hours_old: int,
    request_timeout: int,
    seen_at: str,
) -> dict:
    """Fetch and locally filter a single Lever board."""
    try:
        raw_jobs = lever_list_jobs(board, timeout=request_timeout)
    except Exception as e:
        log.error("%s: Lever API error: %s", board.get("name", board_key), e)
        return {"board": board.get("name", board_key), "board_key": board_key,
                "found": 0, "new": 0, "existing": 0, "error": str(e)}

    matched: list[dict] = []

    for raw_job in raw_jobs:
        title = str(raw_job.get("text") or "")
        location = _compose_location(raw_job)
        posted_at = raw_job.get("updatedAt") or raw_job.get("createdAt")

        if not _posted_recently(posted_at, hours_old):
            continue
        if not _title_allowed(title, queries, exclude_titles):
            continue
        if not _location_ok(location, accept_locs, reject_locs):
            continue

        matched.append(_parse_job(board, raw_job))

    conn = get_connection()
    new, existing = _store_results(conn, matched, seen_at=seen_at)
    return {
        "board": board.get("name", board_key),
        "board_key": board_key,
        "found": len(matched),
        "new": new,
        "existing": existing,
    }


def run_lever_discovery(
    boards: dict | None = None,
    workers: int = 1,
    progress: Progress | None = None,
    task_id: int | None = None,
) -> dict:
    """Fetch curated Lever boards and store matching jobs."""
    if boards is None:
        boards = load_boards()

    if not boards:
        log.warning("No Lever boards configured. Create config/lever.yaml.")
        return {"found": 0, "new": 0, "existing": 0, "boards": 0, "errors": 0}

    init_db()
    search_cfg = config.load_search_config()
    queries = _extract_queries(search_cfg)
    accept_locs, reject_locs = _load_location_filter(search_cfg)
    exclude_titles = search_cfg.get("exclude_titles", [])
    hours_old = int(search_cfg.get("defaults", {}).get("hours_old", 72))
    request_timeout = int(search_cfg.get("lever_timeout_seconds", 60))
    prune_stale = search_cfg.get("prune_stale_jobs", True)
    seen_at = datetime.now(timezone.utc).isoformat()

    proxy = search_cfg.get("proxy")
    if proxy:
        setup_proxy(proxy)

    board_items = list(boards.items())
    total_new = 0
    total_existing = 0
    total_found = 0
    errors = 0
    successful_board_keys: set[str] = set()
    error_board_keys: set[str] = set()
    t0 = time.time()

    log.info(
        "Lever crawl: %d boards, %d effective queries (workers=%d)",
        len(board_items), len(queries), workers,
    )

    owns_progress = progress is None
    if owns_progress:
        progress = Progress(
            TextColumn("[magenta]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            MofNCompleteColumn(),
            TextColumn("[dim]board:[/dim] {task.fields[target]}"),
            TextColumn("[dim]new:[/dim] {task.fields[new]}"),
            TextColumn("[dim]dupes:[/dim] {task.fields[dupes]}"),
            TextColumn("[dim]err:[/dim] {task.fields[errors]}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )

    def _run_boards() -> None:
        nonlocal total_new, total_existing, total_found, errors, progress, task_id
        assert progress is not None
        assert task_id is not None
        if workers > 1 and len(board_items) > 1:
            with ThreadPoolExecutor(max_workers=min(workers, len(board_items))) as pool:
                futures = {
                    pool.submit(
                        _process_one,
                        board_key,
                        board,
                        queries,
                        accept_locs,
                        reject_locs,
                        exclude_titles,
                        hours_old,
                        request_timeout,
                        seen_at,
                    ): board_key
                    for board_key, board in board_items
                }
                for i, future in enumerate(as_completed(futures), 1):
                    result = future.result()
                    total_new += result["new"]
                    total_existing += result["existing"]
                    total_found += result["found"]
                    if "error" in result:
                        errors += 1
                        error_board_keys.add(result["board_key"])
                    else:
                        successful_board_keys.add(result["board_key"])
                    progress.update(
                        task_id,
                        advance=1,
                        target=result["board"],
                        new=total_new,
                        dupes=total_existing,
                        errors=errors,
                    )
                    if i % 10 == 0 or i == len(board_items):
                        elapsed = time.time() - t0
                        log.info(
                            "Lever progress: %d/%d boards (%d new, %d dupes, %d errors) [%.0fs]",
                            i, len(board_items), total_new, total_existing, errors, elapsed,
                        )
            return

        for i, (board_key, board) in enumerate(board_items, 1):
            result = _process_one(
                board_key,
                board,
                queries,
                accept_locs,
                reject_locs,
                exclude_titles,
                hours_old,
                request_timeout,
                seen_at,
            )
            total_new += result["new"]
            total_existing += result["existing"]
            total_found += result["found"]
            if "error" in result:
                errors += 1
                error_board_keys.add(result["board_key"])
            else:
                successful_board_keys.add(result["board_key"])
            progress.update(
                task_id,
                advance=1,
                target=result["board"],
                new=total_new,
                dupes=total_existing,
                errors=errors,
            )
            if i % 10 == 0 or i == len(board_items):
                elapsed = time.time() - t0
                log.info(
                    "Lever progress: %d/%d boards (%d new, %d dupes, %d errors) [%.0fs]",
                    i, len(board_items), total_new, total_existing, errors, elapsed,
                )

    if owns_progress:
        with progress:
            task_id = progress.add_task(
                "Lever crawl",
                total=len(board_items),
                target="-",
                new=0,
                dupes=0,
                errors=0,
            )
            _run_boards()
    else:
        assert progress is not None
        assert task_id is not None
        progress.update(task_id, total=len(board_items), completed=0, target="-", new=0, dupes=0, errors=0)
        _run_boards()
    elapsed = time.time() - t0
    stale_removed = 0
    if prune_stale:
        conn = get_connection()
        cleanup_keys = sorted(successful_board_keys - error_board_keys)
        for key in cleanup_keys:
            site = boards[key].get("name", key)
            stale_removed += prune_stale_jobs(
                conn,
                site=site,
                strategies=("lever_api",),
                seen_at=seen_at,
            )
        if stale_removed:
            log.info("Lever stale cleanup: removed %d old jobs across %d successful boards",
                     stale_removed, len(cleanup_keys))
        if error_board_keys:
            log.info("Lever stale cleanup: skipped %d boards with crawl errors", len(error_board_keys))

    log.info(
        "Lever crawl done: %d found, %d new, %d existing across %d boards in %.0fs",
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
