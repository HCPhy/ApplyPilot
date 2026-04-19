"""Avature careers-site discovery for curated official company portals.

This module supports public Avature-hosted search pages such as Siemens:
  GET https://jobs.siemens.com/en_US/externaljobs/SearchJobs

Unlike Workday, Avature does not expose a simple public JSON search API here.
We crawl the server-rendered search result pages, filter listings locally
against the user's queries and location preferences, then fetch matching job
detail pages to extract the full description, apply URL, and posted date.
"""

from __future__ import annotations

import http.cookiejar
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
from applypilot.discovery.query_match import matches_query, query_to_search_text
from applypilot.discovery.workday import strip_html
from applypilot.ui import console

log = logging.getLogger(__name__)


def load_boards() -> dict:
    """Load curated Avature boards from config/avature.yaml."""
    path = CONFIG_DIR / "avature.yaml"
    if not path.exists():
        log.warning("avature.yaml not found at %s", path)
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("boards", {})


_proxy_url: str | None = None


def setup_proxy(proxy_str: str | None) -> None:
    """Configure proxy settings for future urllib openers."""
    global _proxy_url
    if not proxy_str:
        _proxy_url = None
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
        _proxy_url = None
        return

    _proxy_url = proxy_url


def _build_opener(with_cookies: bool = False) -> urllib.request.OpenerDirector:
    """Build a urllib opener with the configured proxy and optional cookies."""
    handlers: list = []
    if _proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": _proxy_url, "https": _proxy_url}))
    if with_cookies:
        handlers.append(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    return urllib.request.build_opener(*handlers)


def _urlopen(req, timeout=30, opener: urllib.request.OpenerDirector | None = None):
    """Open a URL using the provided or configured opener."""
    if opener is not None:
        return opener.open(req, timeout=timeout)
    if _proxy_url:
        return _build_opener().open(req, timeout=timeout)
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
    """Extract the effective query list for Avature local matching."""
    queries_cfg = search_cfg.get("queries", [])
    max_tier = search_cfg.get("avature_max_tier", search_cfg.get("greenhouse_max_tier", 3))

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
    """Check include/exclude title rules against an Avature job title."""
    title_lower = title.lower()
    if any(bad.lower() in title_lower for bad in exclude_titles):
        return False
    if not queries:
        return True
    return any(_matches_query(title, query) for query in queries)


def _posted_recently(posted_since: str | None, hours_old: int) -> bool:
    """Check if an Avature posted date falls within the allowed window."""
    if not posted_since or hours_old <= 0:
        return True
    try:
        dt = datetime.strptime(posted_since.strip(), "%d-%b-%Y").replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc) - timedelta(hours=hours_old)
    except ValueError:
        return True


_RESULT_BLOCK_RE = re.compile(r"<article class=\"article article--result\b.*?</article>", re.S)
_NEXT_LINK_RE = re.compile(
    r"<li class=\"list-controls__pagination__item paginationNextLink\">.*?<a href=\"([^\"]+)\"",
    re.S,
)
_TITLE_LINK_RE = re.compile(
    r"<h3[^>]*>\s*<a class=\"link\" href=\"([^\"]+/JobDetail/\d+)\"[^>]*>(.*?)</a>",
    re.S,
)
_SPAN_CLASS_RE = {
    "city": re.compile(r"<span class=\"list-item-jobCity\">(.*?)</span>", re.S),
    "state": re.compile(r"<span class=\"list-item-jobState\">(.*?)</span>", re.S),
    "country": re.compile(r"<span class=\"list-item-jobCountry\">(.*?)</span>", re.S),
    "family": re.compile(r"<span class=\"list-item-family\">(.*?)</span>", re.S),
    "job_id": re.compile(r"<span class=\"list-item-jobId\">(.*?)</span>", re.S),
}
_DETAIL_TITLE_RE = re.compile(
    r"<h3 class=\"section__header__text__title[^\"]*\">(.*?)</h3>",
    re.S,
)
_DETAIL_FIELD_RE = re.compile(
    r"<div class=\"article__content__view__field[^\"]*\">\s*"
    r"<div class=\"article__content__view__field__label\"[^>]*>\s*(.*?)\s*</div>\s*"
    r"<div class=\"article__content__view__field__value\">\s*(.*?)\s*</div>",
    re.S,
)
_DETAIL_BODY_RE = re.compile(
    r"<div class=\"article__content__view__field tf_replaceFieldVideoTokens\">.*?"
    r"<div class=\"article__content__view__field__value\">\s*(.*?)\s*</div>",
    re.S,
)
_APPLY_LINK_RE = re.compile(
    r"<a class=\"button button--hero\" href=\"([^\"]+/ApplicationMethods\?folderId=\d+)\"",
    re.S,
)


def _extract_text(match: re.Match[str] | None) -> str:
    """Convert a regex group to plain text."""
    if not match:
        return ""
    return strip_html(unescape(match.group(1))).strip()


def _compose_location(city: str, state: str, country: str) -> str:
    """Build a readable location string from Avature listing fields."""
    parts = [part.strip() for part in (city, state, country) if part and part.strip()]
    return ", ".join(parts)


def _parse_listing_page(board: dict, html: str) -> list[dict]:
    """Parse one Avature search page into lightweight job cards."""
    jobs: list[dict] = []
    seen_urls: set[str] = set()

    for block in _RESULT_BLOCK_RE.findall(html):
        title_match = _TITLE_LINK_RE.search(block)
        if not title_match:
            continue

        detail_url = urllib.parse.urljoin(board["search_url"], unescape(title_match.group(1)))
        if detail_url in seen_urls:
            continue
        seen_urls.add(detail_url)

        title = strip_html(unescape(title_match.group(2))).strip()
        city = _extract_text(_SPAN_CLASS_RE["city"].search(block))
        state = _extract_text(_SPAN_CLASS_RE["state"].search(block))
        country = _extract_text(_SPAN_CLASS_RE["country"].search(block))
        family = _extract_text(_SPAN_CLASS_RE["family"].search(block))
        job_id = _extract_text(_SPAN_CLASS_RE["job_id"].search(block)).replace("Job ID:", "").strip()

        jobs.append(
            {
                "url": detail_url,
                "application_url": detail_url,
                "title": title,
                "location": _compose_location(city, state, country),
                "job_id": job_id,
                "family": family,
                "site": board.get("name", "Avature"),
                "strategy": "avature_html",
            }
        )

    return jobs


def _extract_next_url(current_url: str, html: str) -> str | None:
    """Extract the next pagination URL from an Avature search page."""
    match = _NEXT_LINK_RE.search(html)
    if not match:
        return None
    return urllib.parse.urljoin(current_url, unescape(match.group(1)))


def _with_records_per_page(url: str, records_per_page: int) -> str:
    """Force a consistent Avature page size on listing URLs."""
    parsed = urllib.parse.urlparse(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query["folderRecordsPerPage"] = str(records_per_page)
    normalized = parsed._replace(query=urllib.parse.urlencode(query))
    return urllib.parse.urlunparse(normalized)


def _fetch_html(
    url: str,
    timeout: int,
    *,
    data: dict | list[tuple[str, str]] | bytes | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> str:
    """Fetch a page as decoded HTML, optionally posting form data."""
    encoded_data = None
    if data is not None:
        if isinstance(data, bytes):
            encoded_data = data
        else:
            encoded_data = urllib.parse.urlencode(data, doseq=True).encode("utf-8")

    req = urllib.request.Request(url, data=encoded_data)
    req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    if encoded_data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("Origin", urllib.parse.urlsplit(url).scheme + "://" + urllib.parse.urlsplit(url).netloc)
        req.add_header("Referer", url)
    with _urlopen(req, timeout=timeout, opener=opener) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def avature_list_jobs(board: dict, search_text: str | None = None, timeout: int = 30) -> tuple[list[dict], int]:
    """Fetch all visible listings for a single Avature board/query."""
    max_pages = int(board.get("max_pages", 250))
    records_per_page = int(board.get("records_per_page", 100))
    current_url = _with_records_per_page(board["search_url"], records_per_page)
    pages = 0
    jobs_by_url: dict[str, dict] = {}
    visited: set[tuple[str, str]] = set()
    opener = _build_opener(with_cookies=True)
    next_post_data: dict | None = None

    if search_text:
        search_text = query_to_search_text(search_text)
        next_post_data = {
            "search": search_text,
            "listFilterMode": "true",
            "action": "search",
            "folderOffset": "0",
            "folderRecordsPerPage": str(records_per_page),
        }

    while current_url and pages < max_pages:
        visit_key = ("POST" if next_post_data else "GET", current_url)
        if visit_key in visited:
            break
        visited.add(visit_key)

        html = _fetch_html(current_url, timeout=timeout, data=next_post_data, opener=opener)
        pages += 1
        for job in _parse_listing_page(board, html):
            jobs_by_url.setdefault(job["url"], job)

        current_url = _extract_next_url(current_url, html)
        if current_url:
            current_url = _with_records_per_page(current_url, records_per_page)
        next_post_data = None

    if current_url and pages >= max_pages:
        log.info("%s: capped Avature crawl at %d pages", board.get("name", "Avature"), max_pages)

    return list(jobs_by_url.values()), pages


def _parse_detail_page(job: dict, html: str) -> dict:
    """Extract title, fields, description, and apply URL from a detail page."""
    parsed = dict(job)
    title = _extract_text(_DETAIL_TITLE_RE.search(html))
    if title:
        parsed["title"] = title

    fields: dict[str, str] = {}
    for label_html, value_html in _DETAIL_FIELD_RE.findall(html):
        label = strip_html(unescape(label_html)).strip().lower()
        value = strip_html(unescape(value_html)).strip()
        if label and value:
            fields[label] = value

    description_match = _DETAIL_BODY_RE.search(html)
    full_description = ""
    if description_match:
        full_description = strip_html(unescape(description_match.group(1))).strip()

    apply_match = _APPLY_LINK_RE.search(html)
    apply_url = parsed.get("application_url") or parsed["url"]
    if apply_match:
        apply_url = urllib.parse.urljoin(parsed["url"], unescape(apply_match.group(1)))

    location = fields.get("location(s)") or parsed.get("location") or ""
    posted_since = fields.get("posted since", "")

    parsed.update(
        {
            "title": parsed.get("title", ""),
            "location": location,
            "description": full_description[:500] if full_description else None,
            "full_description": full_description or None,
            "application_url": apply_url,
            "updated_at": posted_since,
            "organization": fields.get("organization", ""),
            "company": fields.get("company", ""),
            "field_of_work": fields.get("field of work", ""),
            "experience_level": fields.get("experience level", ""),
            "employment_type": fields.get("employment type", ""),
            "work_mode": fields.get("work mode", ""),
        }
    )
    return parsed


def _fetch_one_detail(job: dict, timeout: int) -> dict:
    """Fetch and parse a single Avature detail page."""
    parsed = dict(job)
    try:
        html = _fetch_html(job["url"], timeout=timeout)
        parsed = _parse_detail_page(parsed, html)
    except Exception as e:
        parsed["detail_error"] = str(e)
        parsed["full_description"] = None
        parsed["description"] = None
        parsed["application_url"] = parsed.get("application_url") or parsed["url"]
    return parsed


def _store_results(conn: sqlite3.Connection, jobs: list[dict], seen_at: str | None = None) -> tuple[int, int]:
    """Store Avature-discovered jobs in the DB with full descriptions when available."""
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
        detail_error = job.get("detail_error")
        posted_at = normalize_posted_at(job.get("updated_at"), now=now_dt)

        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, "
                "discovered_at, posted_at, last_seen_at, full_description, application_url, detail_scraped_at, detail_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    url,
                    job.get("title"),
                    None,
                    job.get("description"),
                    job.get("location"),
                    job.get("site"),
                    job.get("strategy", "avature_html"),
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


def _process_one(
    board_key: str,
    board: dict,
    query: str | None,
    accept_locs: list[str],
    reject_locs: list[str],
    exclude_titles: list[str],
    hours_old: int,
    request_timeout: int,
    detail_workers: int,
    seen_at: str,
) -> dict:
    """Fetch and locally filter a single Avature board/query."""
    board_name = board.get("name", board_key)
    try:
        listings, pages = avature_list_jobs(board, search_text=query, timeout=request_timeout)
    except Exception as e:
        log.error("%s: Avature listing crawl error: %s", board_name, e)
        return {
            "board": board_name,
            "board_key": board_key,
            "query": query or "-",
            "found": 0,
            "new": 0,
            "existing": 0,
            "pages": 0,
            "error": str(e),
        }

    local_queries = [query] if query else []

    candidates = [
        job
        for job in listings
        if _title_allowed(job.get("title", ""), local_queries, exclude_titles)
        and _location_ok(job.get("location"), accept_locs, reject_locs)
    ]

    detailed: list[dict] = []
    fetch_workers = min(max(1, detail_workers), max(1, len(candidates)))
    if fetch_workers > 1 and len(candidates) > 1:
        with ThreadPoolExecutor(max_workers=fetch_workers) as pool:
            futures = [pool.submit(_fetch_one_detail, job, request_timeout) for job in candidates]
            for future in as_completed(futures):
                detailed.append(future.result())
    else:
        for job in candidates:
            detailed.append(_fetch_one_detail(job, request_timeout))

    matched = [job for job in detailed if _posted_recently(job.get("updated_at"), hours_old)]
    conn = get_connection()
    new, existing = _store_results(conn, matched, seen_at=seen_at)
    return {
        "board": board_name,
        "board_key": board_key,
        "query": query or "-",
        "found": len(matched),
        "new": new,
        "existing": existing,
        "pages": pages,
    }


def run_avature_discovery(
    boards: dict | None = None,
    workers: int = 1,
    progress: Progress | None = None,
    task_id: int | None = None,
) -> dict:
    """Fetch curated Avature boards and store matching jobs."""
    if boards is None:
        boards = load_boards()

    if not boards:
        log.warning("No Avature boards configured. Create config/avature.yaml.")
        return {"found": 0, "new": 0, "existing": 0, "boards": 0, "errors": 0}

    init_db()
    search_cfg = config.load_search_config()
    queries = _extract_queries(search_cfg)
    accept_locs, reject_locs = _load_location_filter(search_cfg)
    exclude_titles = search_cfg.get("exclude_titles", [])
    hours_old = int(search_cfg.get("defaults", {}).get("hours_old", 72))
    request_timeout = int(search_cfg.get("avature_timeout_seconds", 45))
    detail_workers = int(search_cfg.get("avature_detail_workers", min(4, max(1, workers))))
    prune_stale = search_cfg.get("prune_stale_jobs", True)
    seen_at = datetime.now(timezone.utc).isoformat()

    if not queries:
        log.warning("No search queries configured in searches.yaml.")
        return {"found": 0, "new": 0, "existing": 0, "queries": 0, "boards": len(boards), "errors": 0}

    proxy = search_cfg.get("proxy")
    if proxy:
        setup_proxy(proxy)

    board_items = list(boards.items())
    work_items = [(query, board_key, board) for query in queries for board_key, board in board_items]
    total_new = 0
    total_existing = 0
    total_found = 0
    total_pages = 0
    errors = 0
    error_board_keys: set[str] = set()
    t0 = time.time()

    log.info(
        "Avature crawl: %d queries x %d boards (workers=%d)",
        len(queries), len(board_items), workers,
    )

    owns_progress = progress is None
    if owns_progress:
        progress = Progress(
            TextColumn("[blue]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            MofNCompleteColumn(),
            TextColumn("[dim]query:[/dim] {task.fields[query]}"),
            TextColumn("[dim]board:[/dim] {task.fields[target]}"),
            TextColumn("[dim]pages:[/dim] {task.fields[pages]}"),
            TextColumn("[dim]new:[/dim] {task.fields[new]}"),
            TextColumn("[dim]dupes:[/dim] {task.fields[dupes]}"),
            TextColumn("[dim]err:[/dim] {task.fields[errors]}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )

    def _run_queries() -> None:
        nonlocal total_new, total_existing, total_found, total_pages, errors, progress, task_id
        assert progress is not None
        assert task_id is not None
        if workers > 1 and len(work_items) > 1:
            with ThreadPoolExecutor(max_workers=min(workers, len(work_items))) as pool:
                futures = {
                    pool.submit(
                        _process_one,
                        board_key,
                        board,
                        query,
                        accept_locs,
                        reject_locs,
                        exclude_titles,
                        hours_old,
                        request_timeout,
                        detail_workers,
                        seen_at,
                    ): (query, board_key)
                    for query, board_key, board in work_items
                }
                for i, future in enumerate(as_completed(futures), 1):
                    result = future.result()
                    total_new += result["new"]
                    total_existing += result["existing"]
                    total_found += result["found"]
                    total_pages += result.get("pages", 0)
                    if "error" in result:
                        errors += 1
                        error_board_keys.add(result["board_key"])
                    progress.update(
                        task_id,
                        advance=1,
                        query=result["query"],
                        target=result["board"],
                        pages=total_pages,
                        new=total_new,
                        dupes=total_existing,
                        errors=errors,
                    )
                    if i % 10 == 0 or i == len(work_items):
                        elapsed = time.time() - t0
                        log.info(
                            "Avature progress: %d/%d query-board searches (%d pages, %d new, %d dupes, %d errors) [%.0fs]",
                            i, len(work_items), total_pages, total_new, total_existing, errors, elapsed,
                        )
            return

        for i, (query, board_key, board) in enumerate(work_items, 1):
            result = _process_one(
                board_key,
                board,
                query,
                accept_locs,
                reject_locs,
                exclude_titles,
                hours_old,
                request_timeout,
                detail_workers,
                seen_at,
            )
            total_new += result["new"]
            total_existing += result["existing"]
            total_found += result["found"]
            total_pages += result.get("pages", 0)
            if "error" in result:
                errors += 1
                error_board_keys.add(result["board_key"])
            progress.update(
                task_id,
                advance=1,
                query=result["query"],
                target=result["board"],
                pages=total_pages,
                new=total_new,
                dupes=total_existing,
                errors=errors,
            )
            if i % 10 == 0 or i == len(work_items):
                elapsed = time.time() - t0
                log.info(
                    "Avature progress: %d/%d query-board searches (%d pages, %d new, %d dupes, %d errors) [%.0fs]",
                    i, len(work_items), total_pages, total_new, total_existing, errors, elapsed,
                )

    if owns_progress:
        with progress:
            task_id = progress.add_task(
                "Avature crawl",
                total=len(work_items),
                query="-",
                target="-",
                pages=0,
                new=0,
                dupes=0,
                errors=0,
            )
            _run_queries()
    else:
        assert progress is not None
        assert task_id is not None
        progress.update(
            task_id,
            total=len(work_items),
            completed=0,
            query="-",
            target="-",
            pages=0,
            new=0,
            dupes=0,
            errors=0,
        )
        _run_queries()
    elapsed = time.time() - t0
    stale_removed = 0
    if prune_stale:
        conn = get_connection()
        cleanup_keys = sorted(set(boards) - error_board_keys)
        for key in cleanup_keys:
            site = boards[key].get("name", key)
            stale_removed += prune_stale_jobs(
                conn,
                site=site,
                strategies=("avature_html",),
                seen_at=seen_at,
            )
        if stale_removed:
            log.info("Avature stale cleanup: removed %d old jobs across %d successful boards",
                     stale_removed, len(cleanup_keys))
        if error_board_keys:
            log.info("Avature stale cleanup: skipped %d boards with crawl errors", len(error_board_keys))

    log.info(
        "Avature crawl done: %d found, %d new, %d existing across %d queries x %d boards (%d pages) in %.0fs",
        total_found, total_new, total_existing, len(queries), len(board_items), total_pages, elapsed,
    )

    return {
        "found": total_found,
        "new": total_new,
        "existing": total_existing,
        "queries": len(queries),
        "boards": len(board_items),
        "pages": total_pages,
        "errors": errors,
        "stale_removed": stale_removed,
    }
