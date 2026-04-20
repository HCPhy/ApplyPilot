"""Shared helpers for public employer ATS JSON boards."""

from __future__ import annotations

import gzip
import json
import logging
import sqlite3
import time
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import yaml
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn

from applypilot import config
from applypilot.config import CONFIG_DIR
from applypilot.database import get_connection, init_db, prune_stale_jobs
from applypilot.discovery.dates import normalize_posted_at
from applypilot.discovery.location_filters import location_ok, normalize_location_preferences
from applypilot.discovery.query_match import matches_query
from applypilot.ui import console

log = logging.getLogger(__name__)

FetchJobs = Callable[[str, dict, int], list[dict]]
EnrichMatchedJobs = Callable[[dict, list[dict], int], list[dict]]

_opener = None


def load_boards(filename: str) -> dict:
    """Load a board registry from applypilot/config."""
    path = CONFIG_DIR / filename
    if not path.exists():
        log.warning("%s not found at %s", filename, path)
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("boards", {}) or {}


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


def _add_default_headers(req: urllib.request.Request, headers: dict | None = None) -> None:
    """Add common request headers for public careers endpoints."""
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    for key, value in (headers or {}).items():
        req.add_header(key, value)


def fetch_json(url: str, timeout: int = 30, headers: dict | None = None) -> dict | list:
    """Fetch a JSON URL with a browser-ish user agent."""
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    _add_default_headers(req, headers)

    with _urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def post_json(
    url: str,
    payload: dict,
    timeout: int = 30,
    headers: dict | None = None,
) -> dict | list:
    """POST JSON to a public careers endpoint with browser-ish headers."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    _add_default_headers(req, headers)

    with _urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_text(url: str, timeout: int = 30, headers: dict | None = None) -> str:
    """Fetch a text URL with a browser-ish user agent."""
    req = urllib.request.Request(url)
    req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    _add_default_headers(req, headers)

    with _urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
        if encoding == "gzip" or raw.startswith(b"\x1f\x8b"):
            raw = gzip.decompress(raw)
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def _extract_query_items(queries_cfg, max_tier: int) -> list[str]:
    queries: list[str] = []
    fallback: list[str] = []
    for item in queries_cfg or []:
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


def extract_queries(search_cfg: dict, max_tier_key: str) -> list[str]:
    """Extract effective local-match queries for a public board crawler."""
    max_tier = int(search_cfg.get(max_tier_key, search_cfg.get("greenhouse_max_tier", 3)))
    return _extract_query_items(search_cfg.get("queries", []), max_tier)


def source_default_queries(
    provider_key: str,
    board: dict,
    search_cfg: dict,
    max_tier_key: str,
) -> list[str]:
    """Extract optional source-level fallback queries for specialized boards."""
    use_defaults = search_cfg.get(
        f"{provider_key}_use_default_queries",
        search_cfg.get("use_source_default_queries", True),
    )
    if not use_defaults:
        return []

    max_tier = int(search_cfg.get(max_tier_key, search_cfg.get("greenhouse_max_tier", 3)))
    return _extract_query_items(board.get("default_queries", []), max_tier)


def title_allowed(title: str, queries: list[str], exclude_titles: list[str]) -> bool:
    """Apply include/exclude title rules to a public board job title."""
    title_lower = title.lower()
    if any(bad.lower() in title_lower for bad in exclude_titles):
        return False
    if not queries:
        return True
    return any(matches_query(title, query) for query in queries)


def posted_recently(posted_at, hours_old: int) -> bool:
    """Check if a job timestamp falls within the allowed window."""
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


def store_results(
    conn: sqlite3.Connection,
    jobs: list[dict],
    strategy: str,
    seen_at: str | None = None,
) -> tuple[int, int]:
    """Store public-board jobs in the DB with full descriptions when available."""
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
        detail_error = job.get("detail_error")
        detail_scraped_at = now if full_description or detail_error else None
        posted_at = normalize_posted_at(job.get("updated_at"), now=now_dt)

        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, "
                "discovered_at, posted_at, last_seen_at, full_description, application_url, "
                "detail_scraped_at, detail_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    url,
                    job.get("title"),
                    job.get("salary"),
                    job.get("description"),
                    job.get("location"),
                    job.get("site"),
                    job.get("strategy", strategy),
                    now,
                    posted_at,
                    seen_at,
                    full_description,
                    job.get("application_url") or url,
                    detail_scraped_at,
                    detail_error,
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


def run_board_discovery(
    *,
    provider_key: str,
    provider_label: str,
    strategy: str,
    boards: dict | None,
    load_default_boards: Callable[[], dict],
    fetch_jobs: FetchJobs,
    max_tier_key: str,
    timeout_key: str,
    default_timeout: int,
    workers: int = 1,
    progress: Progress | None = None,
    task_id: int | None = None,
    enrich_matched_jobs: EnrichMatchedJobs | None = None,
    progress_style: str = "[cyan]",
) -> dict:
    """Fetch configured public ATS boards, filter locally, store, and prune stale rows."""
    if boards is None:
        boards = load_default_boards()

    if not boards:
        log.warning("No %s boards configured. Create config/%s.yaml.", provider_label, provider_key)
        return {"found": 0, "new": 0, "existing": 0, "boards": 0, "errors": 0}

    init_db()
    search_cfg = config.load_search_config()
    queries = extract_queries(search_cfg, max_tier_key)
    accept_locs, reject_locs = normalize_location_preferences(search_cfg)
    exclude_titles = search_cfg.get("exclude_titles", [])
    hours_old = int(search_cfg.get("defaults", {}).get("hours_old", 72))
    request_timeout = int(search_cfg.get(timeout_key, default_timeout))
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
        "%s crawl: %d boards, %d effective queries (workers=%d)",
        provider_label, len(board_items), len(queries), workers,
    )

    owns_progress = progress is None
    if owns_progress:
        progress = Progress(
            TextColumn(f"{progress_style}{{task.description}}"),
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

    def _process_one(board_key: str, board: dict) -> dict:
        try:
            raw_jobs = fetch_jobs(board_key, board, request_timeout)
        except Exception as e:
            log.error("%s: %s API error: %s", board.get("name", board_key), provider_label, e)
            return {
                "board": board.get("name", board_key),
                "board_key": board_key,
                "found": 0,
                "new": 0,
                "existing": 0,
                "error": str(e),
            }

        matched: list[dict] = []
        board_queries = source_default_queries(provider_key, board, search_cfg, max_tier_key)
        effective_queries = list(dict.fromkeys([*queries, *board_queries]))
        for job in raw_jobs:
            title = str(job.get("title") or "")
            location = str(job.get("location") or "")
            posted_at = job.get("updated_at")

            if not posted_recently(posted_at, hours_old):
                continue
            if not title_allowed(title, effective_queries, exclude_titles):
                continue
            if not location_ok(location, accept_locs, reject_locs):
                continue

            matched.append(job)

        if enrich_matched_jobs and matched:
            matched = enrich_matched_jobs(board, matched, request_timeout)

        conn = get_connection()
        new, existing = store_results(conn, matched, strategy=strategy, seen_at=seen_at)
        return {
            "board": board.get("name", board_key),
            "board_key": board_key,
            "found": len(matched),
            "new": new,
            "existing": existing,
        }

    def _run_boards() -> None:
        nonlocal total_new, total_existing, total_found, errors, progress, task_id
        assert progress is not None
        assert task_id is not None
        if workers > 1 and len(board_items) > 1:
            with ThreadPoolExecutor(max_workers=min(workers, len(board_items))) as pool:
                futures = {
                    pool.submit(_process_one, board_key, board): board_key
                    for board_key, board in board_items
                }
                for i, future in enumerate(as_completed(futures), 1):
                    result = future.result()
                    _record_result(i, result)
            return

        for i, (board_key, board) in enumerate(board_items, 1):
            result = _process_one(board_key, board)
            _record_result(i, result)

    def _record_result(i: int, result: dict) -> None:
        nonlocal total_new, total_existing, total_found, errors
        assert progress is not None
        assert task_id is not None
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
                "%s progress: %d/%d boards (%d new, %d dupes, %d errors) [%.0fs]",
                provider_label, i, len(board_items), total_new, total_existing, errors, elapsed,
            )

    if owns_progress:
        with progress:
            task_id = progress.add_task(
                f"{provider_label} crawl",
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
                strategies=(strategy,),
                seen_at=seen_at,
            )
        if stale_removed:
            log.info(
                "%s stale cleanup: removed %d old jobs across %d successful boards",
                provider_label, stale_removed, len(cleanup_keys),
            )
        if error_board_keys:
            log.info("%s stale cleanup: skipped %d boards with crawl errors", provider_label, len(error_board_keys))

    log.info(
        "%s crawl done: %d found, %d new, %d existing across %d boards in %.0fs",
        provider_label, total_found, total_new, total_existing, len(board_items), elapsed,
    )

    return {
        "found": total_found,
        "new": total_new,
        "existing": total_existing,
        "boards": len(board_items),
        "errors": errors,
        "stale_removed": stale_removed,
    }
