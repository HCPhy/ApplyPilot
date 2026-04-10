"""Greenhouse Job Board API discovery for curated company boards.

Fetches public job listings from Greenhouse boards via:
  GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true

Greenhouse does not support remote search text filters like Workday, so this
module fetches each configured board once, then filters jobs locally against
the user's ApplyPilot search queries, location preferences, recency window,
and excluded title patterns.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html import unescape

import yaml
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn

from applypilot import config
from applypilot.config import CONFIG_DIR
from applypilot.database import get_connection, init_db
from applypilot.discovery.location_filters import location_ok, normalize_location_preferences
from applypilot.discovery.query_match import matches_query
from applypilot.discovery.workday import strip_html
from applypilot.ui import console

log = logging.getLogger(__name__)


def load_boards() -> dict:
    """Load curated Greenhouse boards from config/greenhouse.yaml."""
    path = CONFIG_DIR / "greenhouse.yaml"
    if not path.exists():
        log.warning("greenhouse.yaml not found at %s", path)
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
    """Extract the effective query list for Greenhouse local matching."""
    queries_cfg = search_cfg.get("queries", [])
    max_tier = search_cfg.get("greenhouse_max_tier", 3)

    queries: list[str] = []
    fallback: list[str] = []
    for item in queries_cfg:
        if isinstance(item, str):
            fallback.append(item.strip())
            queries.append(item.strip())
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
    """Check include/exclude title rules against a Greenhouse job title."""
    title_lower = title.lower()
    if any(bad.lower() in title_lower for bad in exclude_titles):
        return False
    if not queries:
        return True
    return any(_matches_query(title, query) for query in queries)


def _updated_recently(updated_at: str | None, hours_old: int) -> bool:
    """Check if a job update timestamp falls within the allowed window."""
    if not updated_at or hours_old <= 0:
        return True
    try:
        normalized = updated_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc) - timedelta(hours=hours_old)
    except ValueError:
        return True


def greenhouse_list_jobs(board_token: str) -> dict:
    """Fetch all published jobs for a Greenhouse board."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

    with _urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _parse_job(board: dict, raw_job: dict) -> dict:
    """Convert a raw Greenhouse API job into ApplyPilot's job shape."""
    content_html = raw_job.get("content") or ""
    full_description = strip_html(unescape(content_html))
    location = (raw_job.get("location") or {}).get("name", "")
    absolute_url = raw_job.get("absolute_url", "")

    return {
        "url": absolute_url,
        "application_url": absolute_url,
        "title": raw_job.get("title", ""),
        "location": location,
        "description": full_description[:500] if full_description else None,
        "full_description": full_description if full_description else None,
        "updated_at": raw_job.get("updated_at"),
        "site": board.get("name", "Greenhouse"),
        "strategy": "greenhouse_api",
    }


def _store_results(conn: sqlite3.Connection, jobs: list[dict]) -> tuple[int, int]:
    """Store Greenhouse-discovered jobs in the DB with full descriptions."""
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        full_description = job.get("full_description")
        detail_scraped_at = now if full_description else None

        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, "
                "discovered_at, full_description, application_url, detail_scraped_at, detail_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    url,
                    job.get("title"),
                    None,
                    job.get("description"),
                    job.get("location"),
                    job.get("site"),
                    job.get("strategy", "greenhouse_api"),
                    now,
                    full_description,
                    job.get("application_url") or url,
                    detail_scraped_at,
                    None,
                ),
            )
            new += 1
        except sqlite3.IntegrityError:
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
) -> dict:
    """Fetch and locally filter a single Greenhouse board."""
    try:
        payload = greenhouse_list_jobs(board["board_token"])
    except Exception as e:
        log.error("%s: Greenhouse API error: %s", board.get("name", board_key), e)
        return {"board": board.get("name", board_key), "found": 0, "new": 0, "existing": 0, "error": str(e)}

    raw_jobs = payload.get("jobs", [])
    matched: list[dict] = []

    for raw_job in raw_jobs:
        title = str(raw_job.get("title", ""))
        location = (raw_job.get("location") or {}).get("name", "")
        updated_at = raw_job.get("updated_at")

        if not _updated_recently(updated_at, hours_old):
            continue
        if not _title_allowed(title, queries, exclude_titles):
            continue
        if not _location_ok(location, accept_locs, reject_locs):
            continue

        matched.append(_parse_job(board, raw_job))

    conn = get_connection()
    new, existing = _store_results(conn, matched)
    return {
        "board": board.get("name", board_key),
        "found": len(matched),
        "new": new,
        "existing": existing,
    }


def run_greenhouse_discovery(
    boards: dict | None = None,
    workers: int = 1,
    progress: Progress | None = None,
    task_id: int | None = None,
) -> dict:
    """Fetch curated Greenhouse boards and store matching jobs."""
    if boards is None:
        boards = load_boards()

    if not boards:
        log.warning("No Greenhouse boards configured. Create config/greenhouse.yaml.")
        return {"found": 0, "new": 0, "existing": 0, "boards": 0, "errors": 0}

    init_db()
    search_cfg = config.load_search_config()
    queries = _extract_queries(search_cfg)
    accept_locs, reject_locs = _load_location_filter(search_cfg)
    exclude_titles = search_cfg.get("exclude_titles", [])
    hours_old = int(search_cfg.get("defaults", {}).get("hours_old", 72))

    proxy = search_cfg.get("proxy")
    if proxy:
        setup_proxy(proxy)

    board_items = list(boards.items())
    total_new = 0
    total_existing = 0
    total_found = 0
    errors = 0
    t0 = time.time()

    log.info(
        "Greenhouse crawl: %d boards, %d effective queries (workers=%d)",
        len(board_items), len(queries), workers,
    )

    owns_progress = progress is None
    if owns_progress:
        progress = Progress(
            TextColumn("[green]{task.description}"),
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
                            "Greenhouse progress: %d/%d boards (%d new, %d dupes, %d errors) [%.0fs]",
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
            )
            total_new += result["new"]
            total_existing += result["existing"]
            total_found += result["found"]
            if "error" in result:
                errors += 1
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
                    "Greenhouse progress: %d/%d boards (%d new, %d dupes, %d errors) [%.0fs]",
                    i, len(board_items), total_new, total_existing, errors, elapsed,
                )

    if owns_progress:
        with progress:
            task_id = progress.add_task(
                "Greenhouse crawl",
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
    log.info(
        "Greenhouse crawl done: %d found, %d new, %d existing across %d boards in %.0fs",
        total_found, total_new, total_existing, len(board_items), elapsed,
    )

    return {
        "found": total_found,
        "new": total_new,
        "existing": total_existing,
        "boards": len(board_items),
        "errors": errors,
    }
