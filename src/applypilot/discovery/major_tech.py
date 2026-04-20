"""Official careers discovery for major tech firms with custom job systems."""

from __future__ import annotations

import html
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlencode, urljoin

import yaml
from bs4 import BeautifulSoup
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn

from applypilot import config
from applypilot.config import CONFIG_DIR
from applypilot.database import get_connection, init_db, prune_stale_jobs
from applypilot.discovery.location_filters import location_ok, normalize_location_preferences
from applypilot.discovery.public_boards import (
    extract_queries,
    fetch_json,
    fetch_text,
    post_json,
    posted_recently,
    setup_proxy,
    store_results,
    title_allowed,
)
from applypilot.discovery.workday import strip_html
from applypilot.ui import console

log = logging.getLogger(__name__)

UBER_SEARCH_URL = "https://www.uber.com/api/loadSearchJobsResults"
GOOGLE_SEARCH_URL = "https://www.google.com/about/careers/applications/jobs/results/"
AMAZON_SEARCH_URL = "https://www.amazon.jobs/en/search.json"
APPLE_SEARCH_URL = "https://jobs.apple.com/en-us/search"
APPLE_BASE_URL = "https://jobs.apple.com"

DEFAULT_PROVIDERS = {
    "amazon": {
        "name": "Amazon",
        "enabled": True,
    },
    "apple": {
        "name": "Apple",
        "enabled": True,
    },
    "google": {
        "name": "Google",
        "enabled": True,
    },
    "uber": {
        "name": "Uber",
        "enabled": True,
    },
}


def load_providers() -> dict:
    """Load custom major-tech provider configuration."""
    path = CONFIG_DIR / "major_tech.yaml"
    if not path.exists():
        log.warning("major_tech.yaml not found at %s", path)
        return DEFAULT_PROVIDERS.copy()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    providers = data.get("providers", {}) or {}
    return providers or DEFAULT_PROVIDERS.copy()


def _low_int(value, default: int = 0) -> int:
    if isinstance(value, dict):
        value = value.get("low", default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _plain_fragment(value) -> str:
    if isinstance(value, list):
        if len(value) > 1 and value[1]:
            return str(value[1])
        return ""
    return str(value or "")


def _clean_markdown(text: str) -> str:
    """Convert the small Markdown subset used by Uber descriptions to plain text."""
    text = html.unescape(text or "").replace("\xa0", " ")
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clean_html_parts(*parts: str) -> str:
    cleaned = [strip_html(part) for part in parts if part]
    return "\n\n".join(part for part in cleaned if part).strip()


def _date_string_to_iso(value: str | None) -> str | None:
    """Normalize public careers date strings to ISO timestamps."""
    if not value:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def _node_text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def _extract_salary(text: str) -> str | None:
    text = text or ""
    patterns = (
        r"Rate of Pay:\s*([^\n]+)",
        r"base salary range is\s*([^.\n]+)",
        r"base salary range for this full-time position is\s*([^.\n]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return None


def _format_uber_location(raw_location: dict | None) -> str:
    if not isinstance(raw_location, dict):
        return ""
    city = raw_location.get("city")
    region = raw_location.get("region")
    country = raw_location.get("countryName") or raw_location.get("country")
    parts = [part for part in (city, region, country) if part]
    return ", ".join(parts)


def _format_google_location(raw_location: list | tuple | None) -> str:
    if not raw_location:
        return ""
    if raw_location[0]:
        return str(raw_location[0])
    city = raw_location[2] if len(raw_location) > 2 else None
    region = raw_location[4] if len(raw_location) > 4 else None
    country = raw_location[5] if len(raw_location) > 5 else None
    return ", ".join(str(part) for part in (city, region, country) if part)


def _format_amazon_location(raw_job: dict) -> str:
    location = raw_job.get("normalized_location") or raw_job.get("location")
    if location:
        return str(location)
    parts = [raw_job.get("city"), raw_job.get("state"), raw_job.get("country_code")]
    return ", ".join(str(part) for part in parts if part)


def _format_apple_location(raw_location: str) -> str:
    location = re.sub(r"\s+", " ", raw_location or "").strip()
    if not location:
        return "United States"
    if not re.search(r"\b(US|USA|United States)\b", location, flags=re.IGNORECASE):
        location = f"{location}, United States"
    return location


def _google_timestamp(value) -> str | None:
    if not isinstance(value, list) or not value:
        return None
    try:
        seconds = float(value[0])
        nanos = float(value[1] or 0) if len(value) > 1 else 0.0
        return datetime.fromtimestamp(seconds + nanos / 1_000_000_000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _best_google_timestamp(raw_job: list) -> str | None:
    timestamps = []
    for idx in (12, 13, 14):
        ts = _google_timestamp(raw_job[idx] if len(raw_job) > idx else None)
        if ts:
            timestamps.append(ts)
    return max(timestamps) if timestamps else None


def parse_amazon_job(raw_job: dict, site: str = "Amazon") -> dict:
    """Parse one Amazon Jobs search result into ApplyPilot's job shape."""
    job_path = raw_job.get("job_path")
    job_id = raw_job.get("id_icims") or raw_job.get("id")
    if not job_path and not job_id:
        return {}

    detail_url = urljoin("https://www.amazon.jobs", job_path or f"/en/jobs/{job_id}/")
    full_description = _clean_html_parts(
        raw_job.get("description"),
        raw_job.get("basic_qualifications"),
        raw_job.get("preferred_qualifications"),
    )

    return {
        "url": detail_url,
        "title": raw_job.get("title") or "",
        "salary": _extract_salary(full_description),
        "description": full_description or raw_job.get("description_short") or "",
        "location": _format_amazon_location(raw_job),
        "site": site,
        "strategy": "amazon_jobs",
        "updated_at": _date_string_to_iso(raw_job.get("posted_date")),
        "full_description": full_description,
        "application_url": raw_job.get("url_next_step") or detail_url,
    }


def amazon_search_jobs(
    query: str,
    offset: int = 0,
    limit: int = 20,
    location: str = "United States",
    timeout: int = 45,
) -> tuple[list[dict], int]:
    """Fetch one page of Amazon Jobs search results."""
    params = {
        "base_query": query,
        "loc_query": location,
        "offset": offset,
        "result_limit": limit,
        "sort": "relevant",
    }
    response = fetch_json(f"{AMAZON_SEARCH_URL}?{urlencode(params)}", timeout=timeout)
    if not isinstance(response, dict):
        raise ValueError("Amazon Jobs search returned an unexpected response shape")
    jobs = response.get("jobs") or []
    if not isinstance(jobs, list):
        raise ValueError("Amazon Jobs search returned an unexpected jobs shape")
    return jobs, _low_int(response.get("hits"))


def parse_apple_search_results(html_text: str, site: str = "Apple") -> tuple[list[dict], bool]:
    """Parse Apple Careers server-rendered search cards."""
    soup = BeautifulSoup(html_text, "html.parser")
    jobs: list[dict] = []

    for item in soup.select("li.rc-accordion-item"):
        title_anchor = item.select_one("h3 a[href*='/details/']")
        if not title_anchor:
            continue
        title = _node_text(title_anchor)
        href = title_anchor.get("href") or ""
        if not title or not href:
            continue

        detail_url = urljoin(APPLE_BASE_URL, href)
        role_match = re.search(r"/details/([^/]+)/", href)
        role_number = role_match.group(1) if role_match else ""
        team = _node_text(item.select_one(".team-name"))
        posted = _node_text(item.select_one(".job-posted-date"))

        location_node = item.select_one(".job-title-location")
        location_parts = []
        if location_node:
            for span in location_node.find_all("span"):
                classes = span.get("class") or []
                text = _node_text(span)
                if text and "a11y" not in classes and text.lower() != "location":
                    location_parts.append(text)
        location = _format_apple_location(" ".join(location_parts))

        summary = _node_text(item.select_one("div[id*='job-summary'] p"))
        description_parts = [part for part in (summary, team, f"Role Number: {role_number}" if role_number else "") if part]
        full_description = "\n\n".join(description_parts)

        jobs.append(
            {
                "url": detail_url,
                "title": title,
                "salary": _extract_salary(full_description),
                "description": full_description,
                "location": location,
                "site": site,
                "strategy": "apple_careers",
                "updated_at": _date_string_to_iso(posted),
                "full_description": full_description,
                "application_url": detail_url,
            }
        )

    has_next = bool(soup.select_one("link[rel='next']"))
    return jobs, has_next


def apple_search_jobs(
    query: str,
    page: int = 1,
    timeout: int = 45,
) -> tuple[list[dict], bool]:
    """Fetch and parse one Apple Careers search-result page."""
    params = {
        "search": query,
        "location": "united-states-USA",
    }
    if page > 1:
        params["page"] = page
    html_text = fetch_text(f"{APPLE_SEARCH_URL}?{urlencode(params)}", timeout=timeout)
    return parse_apple_search_results(html_text)


def parse_uber_job(raw_job: dict, site: str = "Uber") -> dict:
    """Parse one Uber careers RPC result into ApplyPilot's job shape."""
    job_id = raw_job.get("id")
    if not job_id:
        return {}
    description = _clean_markdown(str(raw_job.get("description") or ""))
    locations = raw_job.get("allLocations") if isinstance(raw_job.get("allLocations"), list) else []
    location_text = "; ".join(
        location for location in (_format_uber_location(loc) for loc in locations) if location
    )
    if not location_text:
        location_text = _format_uber_location(raw_job.get("location"))

    detail_url = f"https://www.uber.com/global/en/careers/list/{job_id}/"
    return {
        "url": detail_url,
        "title": raw_job.get("title") or "",
        "salary": _extract_salary(description),
        "description": description,
        "location": location_text,
        "site": site,
        "strategy": "uber_careers",
        "updated_at": raw_job.get("updatedDate") or raw_job.get("creationDate"),
        "full_description": description,
        "application_url": f"https://www.uber.com/careers/apply/interstitial/{job_id}",
    }


def uber_search_jobs(
    query: str,
    page: int = 0,
    limit: int = 20,
    timeout: int = 45,
) -> tuple[list[dict], int]:
    """Fetch one page of Uber careers search results."""
    payload = {
        "limit": limit,
        "page": page,
        "params": {
            "query": query,
            "department": [],
            "lineOfBusinessName": [],
            "location": [],
            "programAndPlatform": [],
            "team": [],
        },
    }
    response = post_json(
        UBER_SEARCH_URL,
        payload,
        timeout=timeout,
        headers={"x-csrf-token": "x"},
    )
    if not isinstance(response, dict) or response.get("status") != "success":
        error = response.get("data") if isinstance(response, dict) else response
        raise ValueError(f"Uber careers search failed: {error}")
    data = response.get("data") or {}
    results = data.get("results") or []
    if not isinstance(results, list):
        raise ValueError("Uber careers search returned an unexpected results shape")
    return results, _low_int(data.get("totalResults"))


def _extract_js_literal(source: str, start: int) -> str:
    i = start
    while i < len(source) and source[i].isspace():
        i += 1
    if i >= len(source) or source[i] not in "[{":
        raise ValueError("Expected a JavaScript array/object literal")

    opener = source[i]
    closer = "]" if opener == "[" else "}"
    depth = 0
    in_string = False
    escaped = False

    for j in range(i, len(source)):
        ch = source[j]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return source[i:j + 1]

    raise ValueError("Unterminated JavaScript literal")


def extract_google_search_data(html_text: str) -> list:
    """Extract the Google Careers ds:1 data array from a search page."""
    marker = "AF_initDataCallback({key: 'ds:1'"
    idx = html_text.find(marker)
    if idx < 0:
        raise ValueError("Google Careers ds:1 data block not found")
    start = html_text.find("data:", idx)
    if start < 0:
        raise ValueError("Google Careers ds:1 data literal not found")
    raw_literal = _extract_js_literal(html_text, start + len("data:"))
    return json.loads(raw_literal)


def parse_google_job(raw_job: list, site: str = "Google") -> dict:
    """Parse one Google Careers job array into ApplyPilot's job shape."""
    if not isinstance(raw_job, list) or len(raw_job) < 11:
        return {}
    job_id = str(raw_job[0] or "").strip()
    if not job_id:
        return {}

    responsibilities = _plain_fragment(raw_job[3] if len(raw_job) > 3 else None)
    qualifications = _plain_fragment(raw_job[4] if len(raw_job) > 4 else None)
    description_html = _plain_fragment(raw_job[10] if len(raw_job) > 10 else None)
    notes = _plain_fragment(raw_job[18] if len(raw_job) > 18 else None)
    minimums = _plain_fragment(raw_job[19] if len(raw_job) > 19 else None)
    full_description = _clean_html_parts(description_html, responsibilities, qualifications, notes, minimums)

    raw_locations = raw_job[9] if len(raw_job) > 9 and isinstance(raw_job[9], list) else []
    location_text = "; ".join(
        location for location in (_format_google_location(loc) for loc in raw_locations) if location
    )
    detail_url = f"{GOOGLE_SEARCH_URL}{job_id}/"
    application_url = str(raw_job[2] or detail_url)

    return {
        "url": detail_url,
        "title": raw_job[1] or "",
        "salary": _extract_salary(full_description),
        "description": full_description,
        "location": location_text,
        "site": site,
        "strategy": "google_careers",
        "updated_at": _best_google_timestamp(raw_job),
        "full_description": full_description,
        "application_url": application_url,
    }


def google_search_jobs(
    query: str,
    page: int = 1,
    location: str | None = None,
    timeout: int = 45,
) -> tuple[list[dict], int, int]:
    """Fetch and parse one Google Careers search-result page."""
    params = {"q": query, "page": page}
    if location:
        params["location"] = location
    url = f"{GOOGLE_SEARCH_URL}?{urlencode(params)}"
    html_text = fetch_text(url, timeout=timeout)
    data = extract_google_search_data(html_text)
    raw_jobs = data[0] if data and isinstance(data[0], list) else []
    total = int(data[2]) if len(data) > 2 and isinstance(data[2], int) else len(raw_jobs)
    page_size = int(data[3]) if len(data) > 3 and isinstance(data[3], int) else len(raw_jobs)
    return raw_jobs, total, page_size


def _configured_locations(search_cfg: dict) -> list[str | None]:
    custom = search_cfg.get("major_tech_locations") or search_cfg.get("google_locations")
    if custom:
        if isinstance(custom, str):
            return [custom]
        if isinstance(custom, list):
            return [str(item.get("location") if isinstance(item, dict) else item)
                    for item in custom if item]

    terms: list[str] = []
    for item in search_cfg.get("locations", []):
        if isinstance(item, dict) and item.get("remote"):
            continue
        value = item.get("location") if isinstance(item, dict) else item
        if value and str(value) not in terms:
            terms.append(str(value))

    accept_patterns = search_cfg.get("location", {}).get("accept_patterns", [])
    if any(str(pattern).lower() in {"united states", "usa", "us"} for pattern in accept_patterns):
        if "United States" not in terms:
            terms.append("United States")

    return terms[:3] or [None]


def _effective_queries(search_cfg: dict) -> list[str]:
    cfg = dict(search_cfg)
    cfg.setdefault("major_tech_max_tier", 2)
    return extract_queries(cfg, "major_tech_max_tier")


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


def _crawl_amazon(provider: dict, queries: list[str], search_cfg: dict) -> dict:
    site = provider.get("name", "Amazon")
    timeout = int(search_cfg.get("amazon_timeout_seconds", search_cfg.get("major_tech_timeout_seconds", 45)))
    max_pages = int(search_cfg.get("amazon_max_pages", search_cfg.get("major_tech_max_pages", 4)))
    page_size = int(search_cfg.get("amazon_page_size", search_cfg.get("major_tech_page_size", 20)))
    locations = provider.get("locations") or search_cfg.get("amazon_locations") or ["United States"]
    if isinstance(locations, str):
        locations = [locations]
    jobs_by_url: dict[str, dict] = {}
    pages = 0
    errors = 0

    for query in queries:
        for location in locations:
            for page in range(max_pages):
                offset = page * page_size
                try:
                    raw_jobs, total = amazon_search_jobs(
                        query,
                        offset=offset,
                        limit=page_size,
                        location=str(location),
                        timeout=timeout,
                    )
                except Exception as exc:
                    errors += 1
                    log.error(
                        "%s: Amazon search failed for query %r location %r offset %d: %s",
                        site, query, location, offset, exc,
                    )
                    break
                pages += 1
                for raw_job in raw_jobs:
                    job = parse_amazon_job(raw_job, site=site)
                    if job.get("url"):
                        jobs_by_url[job["url"]] = job
                if not raw_jobs or len(raw_jobs) < page_size or offset + page_size >= total:
                    break

    return {
        "site": site,
        "strategy": "amazon_jobs",
        "jobs": _filter_jobs(list(jobs_by_url.values()), queries, search_cfg),
        "pages": pages,
        "errors": errors,
    }


def _crawl_apple(provider: dict, queries: list[str], search_cfg: dict) -> dict:
    site = provider.get("name", "Apple")
    timeout = int(search_cfg.get("apple_timeout_seconds", search_cfg.get("major_tech_timeout_seconds", 45)))
    max_pages = int(search_cfg.get("apple_max_pages", search_cfg.get("major_tech_max_pages", 4)))
    jobs_by_url: dict[str, dict] = {}
    pages = 0
    errors = 0

    for query in queries:
        for page in range(1, max_pages + 1):
            try:
                raw_jobs, has_next = apple_search_jobs(query, page=page, timeout=timeout)
            except Exception as exc:
                errors += 1
                log.error("%s: Apple search failed for query %r page %d: %s", site, query, page, exc)
                break
            pages += 1
            for raw_job in raw_jobs:
                raw_job["site"] = site
                jobs_by_url[raw_job["url"]] = raw_job
            if not raw_jobs or not has_next:
                break

    return {
        "site": site,
        "strategy": "apple_careers",
        "jobs": _filter_jobs(list(jobs_by_url.values()), queries, search_cfg),
        "pages": pages,
        "errors": errors,
    }


def _crawl_uber(provider: dict, queries: list[str], search_cfg: dict) -> dict:
    site = provider.get("name", "Uber")
    timeout = int(search_cfg.get("uber_timeout_seconds", search_cfg.get("major_tech_timeout_seconds", 45)))
    max_pages = int(search_cfg.get("uber_max_pages", search_cfg.get("major_tech_max_pages", 4)))
    page_size = int(search_cfg.get("uber_page_size", search_cfg.get("major_tech_page_size", 20)))
    jobs_by_url: dict[str, dict] = {}
    pages = 0
    errors = 0

    for query in queries:
        for page in range(max_pages):
            try:
                raw_jobs, total = uber_search_jobs(query, page=page, limit=page_size, timeout=timeout)
            except Exception as exc:
                errors += 1
                log.error("%s: Uber search failed for query %r page %d: %s", site, query, page, exc)
                break
            pages += 1
            for raw_job in raw_jobs:
                job = parse_uber_job(raw_job, site=site)
                if job.get("url"):
                    jobs_by_url[job["url"]] = job
            if not raw_jobs or len(raw_jobs) < page_size or (page + 1) * page_size >= total:
                break

    return {
        "site": site,
        "strategy": "uber_careers",
        "jobs": _filter_jobs(list(jobs_by_url.values()), queries, search_cfg),
        "pages": pages,
        "errors": errors,
    }


def _crawl_google(provider: dict, queries: list[str], search_cfg: dict) -> dict:
    site = provider.get("name", "Google")
    timeout = int(search_cfg.get("google_timeout_seconds", search_cfg.get("major_tech_timeout_seconds", 45)))
    max_pages = int(search_cfg.get("google_max_pages", search_cfg.get("major_tech_max_pages", 4)))
    locations = _configured_locations(search_cfg)
    jobs_by_url: dict[str, dict] = {}
    pages = 0
    errors = 0

    for query in queries:
        for location in locations:
            for page in range(1, max_pages + 1):
                try:
                    raw_jobs, total, page_size = google_search_jobs(
                        query,
                        page=page,
                        location=location,
                        timeout=timeout,
                    )
                except Exception as exc:
                    errors += 1
                    log.error(
                        "%s: Google search failed for query %r location %r page %d: %s",
                        site, query, location, page, exc,
                    )
                    break
                pages += 1
                for raw_job in raw_jobs:
                    job = parse_google_job(raw_job, site=site)
                    if job.get("url"):
                        jobs_by_url[job["url"]] = job
                if not raw_jobs or page * max(page_size, 1) >= total:
                    break

    return {
        "site": site,
        "strategy": "google_careers",
        "jobs": _filter_jobs(list(jobs_by_url.values()), queries, search_cfg),
        "pages": pages,
        "errors": errors,
    }


CRAWLERS = {
    "amazon": _crawl_amazon,
    "apple": _crawl_apple,
    "google": _crawl_google,
    "uber": _crawl_uber,
}


def run_major_tech_discovery(
    providers: dict | None = None,
    workers: int = 1,
    progress: Progress | None = None,
    task_id: int | None = None,
) -> dict:
    """Fetch configured major-tech custom careers systems and store matching jobs."""
    if providers is None:
        providers = load_providers()

    provider_items = [
        (key, provider)
        for key, provider in providers.items()
        if provider.get("enabled", True) and key in CRAWLERS
    ]
    if not provider_items:
        log.warning("No major-tech providers configured. Create config/major_tech.yaml.")
        return {"found": 0, "new": 0, "existing": 0, "providers": 0, "errors": 0}

    init_db()
    search_cfg = config.load_search_config()
    queries = _effective_queries(search_cfg)
    seen_at = datetime.now(timezone.utc).isoformat()
    prune_stale = search_cfg.get("prune_stale_jobs", True)

    proxy = search_cfg.get("proxy")
    if proxy:
        setup_proxy(proxy)

    total_new = 0
    total_existing = 0
    total_found = 0
    total_pages = 0
    errors = 0
    stale_removed = 0
    successful_sites: list[tuple[str, str]] = []
    t0 = time.time()

    owns_progress = progress is None
    if owns_progress:
        progress = Progress(
            TextColumn("[bright_cyan]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            MofNCompleteColumn(),
            TextColumn("[dim]query:[/dim] {task.fields[query]}"),
            TextColumn("[dim]target:[/dim] {task.fields[target]}"),
            TextColumn("[dim]pages:[/dim] {task.fields[pages]}"),
            TextColumn("[dim]new:[/dim] {task.fields[new]}"),
            TextColumn("[dim]dupes:[/dim] {task.fields[dupes]}"),
            TextColumn("[dim]err:[/dim] {task.fields[errors]}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )

    def _process_one(provider_key: str, provider: dict) -> dict:
        crawler = CRAWLERS[provider_key]
        result = crawler(provider, queries, search_cfg)
        conn = get_connection()
        new, existing = store_results(
            conn,
            result["jobs"],
            strategy=result["strategy"],
            seen_at=seen_at,
        )
        result["new"] = new
        result["existing"] = existing
        result["found"] = len(result["jobs"])
        return result

    def _record_result(i: int, result: dict) -> None:
        nonlocal total_new, total_existing, total_found, total_pages, errors
        assert progress is not None
        assert task_id is not None
        total_new += result["new"]
        total_existing += result["existing"]
        total_found += result["found"]
        total_pages += result["pages"]
        errors += result["errors"]
        if result["errors"] == 0:
            successful_sites.append((result["site"], result["strategy"]))
        progress.update(
            task_id,
            advance=1,
            query="-",
            target=result["site"],
            pages=total_pages,
            new=total_new,
            dupes=total_existing,
            errors=errors,
        )
        log.info(
            "Major tech progress: %d/%d providers (%d new, %d dupes, %d errors)",
            i, len(provider_items), total_new, total_existing, errors,
        )

    def _run_providers() -> None:
        if workers > 1 and len(provider_items) > 1:
            with ThreadPoolExecutor(max_workers=min(workers, len(provider_items))) as pool:
                futures = {
                    pool.submit(_process_one, provider_key, provider): provider_key
                    for provider_key, provider in provider_items
                }
                for i, future in enumerate(as_completed(futures), 1):
                    _record_result(i, future.result())
            return

        for i, (provider_key, provider) in enumerate(provider_items, 1):
            _record_result(i, _process_one(provider_key, provider))

    if owns_progress:
        assert progress is not None
        with progress:
            task_id = progress.add_task(
                "Major tech crawl",
                total=len(provider_items),
                query="-",
                target="-",
                pages=0,
                new=0,
                dupes=0,
                errors=0,
            )
            _run_providers()
    else:
        assert progress is not None
        assert task_id is not None
        progress.update(
            task_id,
            total=len(provider_items),
            completed=0,
            query="-",
            target="-",
            pages=0,
            new=0,
            dupes=0,
            errors=0,
        )
        _run_providers()

    if prune_stale:
        conn = get_connection()
        for site, strategy in successful_sites:
            stale_removed += prune_stale_jobs(
                conn,
                site=site,
                strategies=(strategy,),
                seen_at=seen_at,
            )
        if stale_removed:
            log.info("Major tech stale cleanup: removed %d old jobs", stale_removed)
        skipped = len(provider_items) - len(successful_sites)
        if skipped:
            log.info("Major tech stale cleanup: skipped %d providers with crawl errors", skipped)

    elapsed = time.time() - t0
    log.info(
        "Major tech crawl done: %d found, %d new, %d existing across %d providers in %.0fs",
        total_found, total_new, total_existing, len(provider_items), elapsed,
    )

    return {
        "found": total_found,
        "new": total_new,
        "existing": total_existing,
        "providers": len(provider_items),
        "errors": errors,
        "pages": total_pages,
        "stale_removed": stale_removed,
    }
