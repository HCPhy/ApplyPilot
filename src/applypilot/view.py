"""ApplyPilot HTML Dashboard Generator.

Generates a self-contained HTML dashboard with:
  - Summary stats (total, enriched, scored, high-fit)
  - Score distribution bar chart
  - Firm-level filtering and breakdown
  - Filterable job cards sorted by newest posting/discovery time
  - Client-side search, firm subset selection, and remembered viewed jobs
"""

from __future__ import annotations

import json
import sqlite3
import webbrowser
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from rich.console import Console

from applypilot.config import APP_DIR
from applypilot.database import get_connection, init_db

console = Console()


def _parse_dashboard_datetime(value: str | None) -> datetime | None:
    """Parse an ISO timestamp stored in the jobs table."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _date_meta(posted_at: str | None, discovered_at: str | None) -> tuple[str, str, int, str]:
    """Return display label, source label, sortable timestamp, and ISO value."""
    source = "Posted/updated" if posted_at else "Discovered"
    parsed = _parse_dashboard_datetime(posted_at) or _parse_dashboard_datetime(discovered_at)
    if parsed is None:
        return "Unknown date", source, 0, ""

    label = f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"
    return label, source, int(parsed.timestamp()), parsed.isoformat()


def generate_dashboard(output_path: str | None = None) -> str:
    """Generate an HTML dashboard of all jobs with fit scores.

    Args:
        output_path: Where to write the HTML file. Defaults to ~/.applypilot/dashboard.html.

    Returns:
        Absolute path to the generated HTML file.
    """
    out = Path(output_path) if output_path else APP_DIR / "dashboard.html"

    try:
        conn = init_db()
    except sqlite3.OperationalError as e:
        conn = get_connection()
        console.print(f"[yellow]Could not migrate dashboard DB schema ({e}); falling back to discovery-time ordering.[/yellow]")

    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    has_posted_at = "posted_at" in columns
    posted_select = "posted_at" if has_posted_at else "NULL AS posted_at"
    newest_expr = "COALESCE(posted_at, discovered_at)" if has_posted_at else "discovered_at"

    # Stats
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    ready = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND application_url IS NOT NULL"
    ).fetchone()[0]
    scored = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score > 0"
    ).fetchone()[0]
    high_fit = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score >= 7"
    ).fetchone()[0]
    applied = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL OR apply_status = 'applied'"
    ).fetchone()[0]

    # Score distribution
    score_dist: dict[int, int] = {}
    if scored:
        rows = conn.execute(
            "SELECT fit_score, COUNT(*) FROM jobs "
            "WHERE fit_score > 0 "
            "GROUP BY fit_score ORDER BY fit_score DESC"
        ).fetchall()
        for r in rows:
            score_dist[r[0]] = r[1]

    # Site stats
    site_stats = conn.execute("""
        SELECT site,
               COUNT(*) as total,
               SUM(CASE WHEN fit_score >= 7 THEN 1 ELSE 0 END) as high_fit,
               SUM(CASE WHEN fit_score BETWEEN 5 AND 6 THEN 1 ELSE 0 END) as mid_fit,
               SUM(CASE WHEN fit_score BETWEEN 1 AND 4 THEN 1 ELSE 0 END) as low_fit,
               SUM(CASE WHEN fit_score IS NULL THEN 1 ELSE 0 END) as unscored,
               SUM(CASE WHEN applied_at IS NOT NULL OR apply_status = 'applied' THEN 1 ELSE 0 END) as applied,
               SUM(CASE WHEN apply_status = 'in_progress' THEN 1 ELSE 0 END) as in_progress,
               ROUND(AVG(CASE WHEN fit_score > 0 THEN fit_score END), 1) as avg_score
        FROM jobs GROUP BY site ORDER BY high_fit DESC, total DESC
    """).fetchall()

    # Dashboard triage shows unscored jobs too, but excludes fit_score=0 because
    # that is a scoring failure sentinel rather than a real score.
    jobs = conn.execute(f"""
        SELECT url, title, salary, description, location, site, strategy,
               substr(COALESCE(full_description, description, ''), 1, 300) AS desc_preview,
               application_url, detail_error,
               fit_score, score_reasoning, apply_status, applied_at, apply_error,
               {posted_select}, discovered_at, last_seen_at,
               (
                   SELECT MAX(j2.last_seen_at)
                   FROM jobs j2
                   WHERE COALESCE(j2.site, '') = COALESCE(jobs.site, '')
                     AND COALESCE(j2.strategy, '') = COALESCE(jobs.strategy, '')
                     AND j2.last_seen_at IS NOT NULL
               ) AS latest_seen_at
        FROM jobs
        WHERE fit_score IS NULL OR fit_score > 0
        ORDER BY {newest_expr} DESC, fit_score DESC, site, title
    """).fetchall()

    # Color map per site
    colors = {
        "RemoteOK": "#10b981", "WelcomeToTheJungle": "#f59e0b",
        "Job Bank Canada": "#3b82f6", "CareerJet Canada": "#8b5cf6",
        "Hacker News Jobs": "#ff6600", "BuiltIn Remote": "#ec4899",
        "TD Bank": "#00a651", "CIBC": "#c41f3e", "RBC": "#003168",
        "indeed": "#2164f3", "linkedin": "#0a66c2",
        "Dice": "#eb1c26", "Glassdoor": "#0caa41",
    }

    # Score distribution bar chart
    score_bars = ""
    max_count = max(score_dist.values()) if score_dist else 1
    for s in range(10, 0, -1):
        count = score_dist.get(s, 0)
        pct = (count / max_count * 100) if max_count else 0
        score_color = "#10b981" if s >= 7 else ("#f59e0b" if s >= 5 else "#ef4444")
        score_bars += f"""
        <div class="score-row">
          <span class="score-label">{s}</span>
          <div class="score-bar-track">
            <div class="score-bar-fill" style="width:{pct}%;background:{score_color}"></div>
          </div>
          <span class="score-count">{count}</span>
        </div>"""

    # Firm filter rows
    firm_rows = """
        <button type="button" class="site-row firm-row active" data-site-filter="" data-clear-firms>
          <div class="site-name">All Firms</div>
          <div class="site-nums">Clear selected firms</div>
        </button>"""

    for s in site_stats:
        site = s["site"] or "?"
        color = colors.get(site, "#6b7280")
        avg = s["avg_score"] or 0
        firm_rows += f"""
        <label class="site-row firm-row firm-checkbox-row" data-site-filter="{escape(site)}">
          <input type="checkbox" class="firm-checkbox" value="{escape(site)}" data-firm-checkbox style="accent-color:{color}">
          <span class="firm-row-copy">
            <span class="site-name" style="color:{color}">{escape(site)}</span>
            <span class="site-nums">{s['total']} jobs &middot; {s['high_fit']} strong fit &middot; {s['applied']} applied &middot; avg score {avg}</span>
            <span class="bar-track">
              <span class="bar-fill" style="width:{s['high_fit']/max(s['total'],1)*100}%;background:{color}"></span>
              <span class="bar-fill" style="width:{s['mid_fit']/max(s['total'],1)*100}%;background:{color}66"></span>
            </span>
          </span>
        </label>"""

    # Job cards are rendered client-side in 100-job pages so large crawls do not
    # create thousands of DOM nodes on initial load.
    job_items = []
    for j in jobs:
        score = j["fit_score"] or 0
        apply_status = (j["apply_status"] or "").strip()
        is_applied = bool(j["applied_at"]) or apply_status == "applied"
        is_in_progress = apply_status == "in_progress"
        latest_seen_at = j["latest_seen_at"]
        last_seen_at = j["last_seen_at"]
        is_stale = bool(
            latest_seen_at
            and last_seen_at != latest_seen_at
            and not is_applied
            and not is_in_progress
        )
        date_label, date_source, sort_ts, date_iso = _date_meta(j["posted_at"], j["discovered_at"])

        # Parse keywords and reasoning from score_reasoning
        reasoning_raw = j["score_reasoning"] or ""
        reasoning_lines = reasoning_raw.split("\n")
        keywords = reasoning_lines[0][:120] if reasoning_lines else ""
        reasoning = reasoning_lines[1][:200] if len(reasoning_lines) > 1 else ""

        search_blob = " ".join([
            j["title"] or "",
            j["site"] or "",
            j["location"] or "",
            keywords,
            reasoning,
        ]).lower()

        job_items.append({
            "url": j["url"] or "",
            "title": j["title"] or "Untitled",
            "salary": j["salary"] or "",
            "location": j["location"] or "",
            "site": j["site"] or "",
            "siteColor": colors.get(j["site"] or "", "#6b7280"),
            "applyUrl": j["application_url"] or "",
            "applyStatus": apply_status,
            "applyError": (j["apply_error"] or "")[:120],
            "dbApplied": is_applied,
            "isStale": is_stale,
            "lastSeenAt": last_seen_at or "",
            "latestSeenAt": latest_seen_at or "",
            "score": score,
            "scoreLabel": str(score) if score else "?",
            "dateLabel": date_label,
            "dateSource": date_source,
            "dateIso": date_iso,
            "sortTs": sort_ts,
            "keywords": keywords,
            "reasoning": reasoning,
            "descPreview": j["desc_preview"] or "",
            "search": search_blob,
        })

    jobs_json = (
        json.dumps(job_items, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )

    job_sections = """
    <section class="jobs-section">
      <div class="list-heading">
        <h2>Jobs, Newest First</h2>
        <p>Uses ATS posted/updated dates when available, then falls back to discovery time. Cards render 100 per page.</p>
      </div>
      <div class="pagination-controls" aria-label="Job pagination">
        <button type="button" class="page-btn" onclick="changePage(-1)" data-prev-page>Previous</button>
        <span id="page-status" class="page-status"></span>
        <button type="button" class="page-btn" onclick="changePage(1)" data-next-page>Next</button>
      </div>
      <div id="job-grid" class="job-grid"></div>
      <div class="pagination-controls pagination-bottom" aria-label="Job pagination">
        <button type="button" class="page-btn" onclick="changePage(-1)" data-prev-page>Previous</button>
        <span id="page-status-bottom" class="page-status"></span>
        <button type="button" class="page-btn" onclick="changePage(1)" data-next-page>Next</button>
      </div>
    </section>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ApplyPilot Dashboard</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background: #0f172a; color: #e2e8f0; padding: 2rem; }}

  h1 {{ font-size: 1.8rem; font-weight: 700; margin-bottom: 0.5rem; }}
  .subtitle {{ color: #94a3b8; margin-bottom: 2rem; }}

  /* Summary cards */
  .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 1rem; margin-bottom: 2.5rem; }}
  .stat-card {{ background: #1e293b; border-radius: 12px; padding: 1.25rem; }}
  .stat-num {{ font-size: 2rem; font-weight: 700; }}
  .stat-label {{ color: #94a3b8; font-size: 0.85rem; margin-top: 0.25rem; }}
  .stat-ok .stat-num {{ color: #10b981; }}
  .stat-scored .stat-num {{ color: #60a5fa; }}
  .stat-high .stat-num {{ color: #f59e0b; }}
  .stat-total .stat-num {{ color: #e2e8f0; }}
  .stat-applied .stat-num {{ color: #34d399; }}

  /* Filters */
  .filters {{ background: #1e293b; border-radius: 12px; padding: 1.25rem; margin-bottom: 2rem; display: flex; gap: 1rem; flex-wrap: wrap; align-items: center; }}
  .filter-label {{ color: #94a3b8; font-size: 0.85rem; font-weight: 600; }}
  .filter-btn {{ background: #334155; border: none; color: #94a3b8; padding: 0.4rem 0.8rem; border-radius: 6px; cursor: pointer; font-size: 0.8rem; transition: all 0.15s; }}
  .filter-btn:hover {{ background: #475569; color: #e2e8f0; }}
  .filter-btn.active {{ background: #60a5fa; color: #0f172a; font-weight: 600; }}
  .search-input {{ background: #334155; border: 1px solid #475569; color: #e2e8f0; padding: 0.4rem 0.8rem; border-radius: 6px; font-size: 0.8rem; width: 200px; }}
  .search-input::placeholder {{ color: #64748b; }}
  .sort-select {{ background: #334155; border: 1px solid #475569; color: #e2e8f0; padding: 0.4rem 0.8rem; border-radius: 6px; font-size: 0.8rem; min-width: 220px; }}
  .firm-filter-summary {{ color: #94a3b8; font-size: 0.8rem; min-width: 7.5rem; }}

  /* Score distribution */
  .score-section {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 2.5rem; align-items: stretch; }}
  .score-dist, .sites-section {{ background: #1e293b; border-radius: 12px; padding: 1.5rem; height: 420px; display: flex; flex-direction: column; }}
  .score-dist h3 {{ font-size: 1rem; margin-bottom: 1rem; color: #94a3b8; }}
  .score-row {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.4rem; }}
  .score-label {{ width: 1.5rem; text-align: right; font-size: 0.85rem; font-weight: 600; }}
  .score-bar-track {{ flex: 1; height: 14px; background: #334155; border-radius: 4px; overflow: hidden; }}
  .score-bar-fill {{ height: 100%; border-radius: 4px; transition: width 0.3s; }}
  .score-count {{ width: 2.5rem; font-size: 0.8rem; color: #94a3b8; }}

  /* Site bars */
  .sites-section h3 {{ font-size: 1rem; margin-bottom: 1rem; color: #94a3b8; }}
  .firm-list {{ flex: 1; min-height: 0; overflow-y: auto; padding-right: 0.35rem; overscroll-behavior: contain; }}
  .firm-list::-webkit-scrollbar {{ width: 10px; }}
  .firm-list::-webkit-scrollbar-track {{ background: #172033; border-radius: 999px; }}
  .firm-list::-webkit-scrollbar-thumb {{ background: #334155; border-radius: 999px; }}
  .firm-list::-webkit-scrollbar-thumb:hover {{ background: #475569; }}
  .site-row {{ display: block; margin-bottom: 0.8rem; width: 100%; text-align: left; background: #0f172a; border: 1px solid #334155; border-radius: 10px; padding: 0.75rem; cursor: pointer; }}
  .site-row:hover {{ border-color: #60a5fa55; background: #111c34; }}
  .site-row.active {{ border-color: #60a5fa; box-shadow: inset 0 0 0 1px #60a5fa33; }}
  .firm-checkbox-row {{ display: flex; align-items: flex-start; gap: 0.7rem; }}
  .firm-checkbox {{ flex-shrink: 0; width: 1rem; height: 1rem; margin-top: 0.12rem; }}
  .firm-row-copy {{ display: block; flex: 1; min-width: 0; }}
  .site-name {{ display: block; font-weight: 600; font-size: 0.9rem; }}
  .site-nums {{ display: block; color: #94a3b8; font-size: 0.75rem; margin: 0.15rem 0; }}
  .bar-track {{ height: 8px; background: #334155; border-radius: 4px; display: flex; overflow: hidden; }}
  .bar-fill {{ height: 100%; transition: width 0.3s; }}

  /* Score group headers */
  .score-header {{ font-size: 1.2rem; font-weight: 600; margin: 2.5rem 0 1rem; padding-bottom: 0.5rem; border-bottom: 3px solid; display: flex; align-items: center; gap: 0.75rem; }}
  .score-badge {{ display: inline-flex; align-items: center; justify-content: center; width: 2rem; height: 2rem; border-radius: 8px; color: #0f172a; font-weight: 700; font-size: 1rem; }}

  /* Job grid */
  .jobs-section {{ margin-top: 1rem; }}
  .list-heading {{ display: flex; justify-content: space-between; align-items: end; gap: 1rem; margin: 0 0 1rem; }}
  .list-heading h2 {{ font-size: 1.2rem; font-weight: 700; }}
  .list-heading p {{ color: #94a3b8; font-size: 0.8rem; max-width: 520px; text-align: right; }}
  .pagination-controls {{ display: flex; align-items: center; justify-content: center; gap: 0.75rem; margin: 0 0 1rem; }}
  .pagination-bottom {{ margin: 1.5rem 0 0; }}
  .page-btn {{ background: #334155; border: 1px solid #475569; color: #e2e8f0; padding: 0.4rem 0.85rem; border-radius: 7px; cursor: pointer; font-size: 0.8rem; font-weight: 600; }}
  .page-btn:hover:not(:disabled) {{ background: #475569; }}
  .page-btn:disabled {{ cursor: not-allowed; opacity: 0.45; }}
  .page-status {{ color: #94a3b8; font-size: 0.82rem; min-width: 190px; text-align: center; }}
  .job-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 1rem; }}

  .job-card {{ background: #1e293b; border-radius: 10px; padding: 1rem; border-left: 3px solid #334155; transition: all 0.15s; content-visibility: auto; contain-intrinsic-size: 260px; }}
  .job-card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 12px #00000044; }}
  .job-card.is-viewed {{ box-shadow: inset 0 0 0 1px #60a5fa33; }}
  .job-card.is-applied {{ background: linear-gradient(180deg, #132a23 0%, #1e293b 22%); }}
  .job-card[data-score="9"], .job-card[data-score="10"] {{ border-left-color: #10b981; }}
  .job-card[data-score="8"] {{ border-left-color: #34d399; }}
  .job-card[data-score="7"] {{ border-left-color: #60a5fa; }}
  .job-card[data-score="6"] {{ border-left-color: #f59e0b; }}
  .job-card[data-score="5"] {{ border-left-color: #f59e0b88; }}
  .job-card[data-score="4"], .job-card[data-score="3"], .job-card[data-score="2"], .job-card[data-score="1"] {{ border-left-color: #ef4444; }}
  .job-card[data-score="0"] {{ border-left-color: #64748b; }}

  .card-header {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; }}
  .score-pill {{ display: inline-flex; align-items: center; justify-content: center; min-width: 1.6rem; height: 1.6rem; border-radius: 6px; color: #0f172a; font-weight: 700; font-size: 0.8rem; flex-shrink: 0; }}

  .job-title {{ color: #e2e8f0; text-decoration: none; font-weight: 600; font-size: 0.95rem; }}
  .job-title:hover {{ color: #60a5fa; }}
  .status-row {{ display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.45rem; }}
  .status-chip {{ font-size: 0.7rem; padding: 0.14rem 0.5rem; border-radius: 999px; font-weight: 600; letter-spacing: 0.01em; }}
  .status-chip.applied {{ background: #064e3b; color: #6ee7b7; }}
  .status-chip.in-progress {{ background: #172554; color: #93c5fd; }}
  .status-chip.failed {{ background: #451a03; color: #fdba74; }}
  .status-chip.manual {{ background: #3f3f46; color: #d4d4d8; }}
  .status-chip.viewed {{ background: #1e3a5f; color: #93c5fd; }}
  .status-chip.stale {{ background: #422006; color: #fcd34d; }}

  .meta-row {{ display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.4rem; }}
  .meta-tag {{ font-size: 0.72rem; padding: 0.15rem 0.5rem; border-radius: 4px; background: #334155; color: #94a3b8; }}
  .meta-tag.salary {{ background: #064e3b; color: #6ee7b7; }}
  .meta-tag.location {{ background: #1e3a5f; color: #93c5fd; }}
  .meta-tag.date {{ background: #312e81; color: #c4b5fd; }}

  .keywords-row {{ font-size: 0.75rem; color: #10b981; margin-bottom: 0.3rem; line-height: 1.4; }}
  .reasoning-row {{ font-size: 0.75rem; color: #94a3b8; margin-bottom: 0.5rem; font-style: italic; line-height: 1.4; }}

  .desc-preview {{ font-size: 0.8rem; color: #64748b; line-height: 1.5; margin-bottom: 0.75rem; max-height: 3.6em; overflow: hidden; }}

  .card-footer {{ display: flex; justify-content: flex-end; }}
  .apply-link {{ font-size: 0.8rem; color: #60a5fa; text-decoration: none; padding: 0.3rem 0.8rem; border: 1px solid #60a5fa33; border-radius: 6px; font-weight: 500; }}
  .apply-link:hover {{ background: #60a5fa22; }}

  .modal-backdrop {{ position: fixed; inset: 0; z-index: 1000; display: flex; align-items: center; justify-content: center; padding: 1.5rem; background: #020617cc; backdrop-filter: blur(6px); }}
  .modal-card {{ width: min(420px, 100%); border: 1px solid #334155; border-radius: 16px; background: linear-gradient(180deg, #1e293b 0%, #0f172a 100%); box-shadow: 0 24px 80px #00000088; padding: 1.4rem; }}
  .modal-card h3 {{ margin: 0 0 0.5rem; color: #f8fafc; font-size: 1.05rem; }}
  .modal-card p {{ margin: 0; color: #cbd5e1; line-height: 1.5; font-size: 0.9rem; }}
  .modal-actions {{ display: flex; justify-content: flex-end; gap: 0.75rem; margin-top: 1.25rem; }}
  .modal-btn {{ cursor: pointer; border: 1px solid #334155; border-radius: 8px; padding: 0.55rem 0.9rem; font-weight: 700; font-size: 0.85rem; }}
  .modal-btn.secondary {{ background: #0f172a; color: #cbd5e1; }}
  .modal-btn.secondary:hover {{ background: #1e293b; }}
  .modal-btn.primary {{ background: #10b981; border-color: #10b981; color: #052e2b; }}
  .modal-btn.primary:hover {{ background: #34d399; }}

  .hidden {{ display: none !important; }}
  .job-count {{ color: #94a3b8; font-size: 0.85rem; margin-bottom: 1rem; }}

  @media (max-width: 768px) {{
    .summary {{ grid-template-columns: repeat(2, 1fr); }}
    .score-section {{ grid-template-columns: 1fr; }}
    .score-dist, .sites-section {{ height: auto; }}
    .firm-list {{ max-height: 320px; }}
    .list-heading {{ display: block; }}
    .list-heading p {{ text-align: left; margin-top: 0.35rem; }}
    .pagination-controls {{ justify-content: space-between; }}
    .page-status {{ min-width: 0; }}
    .job-grid {{ grid-template-columns: 1fr; }}
    body {{ padding: 1rem; }}
  }}
</style>
</head>
<body>

<h1>ApplyPilot Dashboard</h1>
<p class="subtitle">{total} jobs &middot; {scored} scored &middot; {high_fit} strong matches (7+)</p>

<div class="summary">
  <div class="stat-card stat-total"><div class="stat-num">{total}</div><div class="stat-label">Total Jobs</div></div>
  <div class="stat-card stat-ok"><div class="stat-num">{ready}</div><div class="stat-label">Ready (desc + URL)</div></div>
  <div class="stat-card stat-scored"><div class="stat-num">{scored}</div><div class="stat-label">Scored by LLM</div></div>
  <div class="stat-card stat-high"><div class="stat-num">{high_fit}</div><div class="stat-label">Strong Fit (7+)</div></div>
  <div class="stat-card stat-applied"><div id="applied-stat" class="stat-num">{applied}</div><div class="stat-label">Applied / Marked Applied</div></div>
</div>

<div class="filters">
  <span class="filter-label">Score:</span>
  <button class="filter-btn active" onclick="filterScore(0, this)">All jobs</button>
  <button class="filter-btn" onclick="filterScore(1, this)">Scored only</button>
  <button class="filter-btn" onclick="filterScore(5, this)">5+ Moderate</button>
  <button class="filter-btn" onclick="filterScore(7, this)">7+ Strong</button>
  <button class="filter-btn" onclick="filterScore(8, this)">8+ Excellent</button>
  <button class="filter-btn" onclick="filterScore(9, this)">9+ Perfect</button>
  <button id="stale-toggle" class="filter-btn" onclick="toggleStale()">Show stale</button>
  <span class="filter-label" style="margin-left:1rem">Sort:</span>
  <select id="sort-order" class="sort-select" onchange="setSortOrder(this.value)">
    <option value="newest">Newest first</option>
    <option value="score">Highest score</option>
    <option value="firm">Firm A-Z</option>
  </select>
  <span class="filter-label" style="margin-left:1rem">Firms:</span>
  <button class="filter-btn" onclick="clearFirmFilter()">Clear firms</button>
  <span id="firm-filter-summary" class="firm-filter-summary">All firms</span>
  <span class="filter-label" style="margin-left:1rem">Search:</span>
  <input type="text" class="search-input" placeholder="Filter by title, firm, location..." oninput="filterText(this.value)">
</div>

<div class="score-section">
  <div class="score-dist">
    <h3>Score Distribution</h3>
    {score_bars}
  </div>
  <div class="sites-section">
    <h3>Firms</h3>
    <div class="firm-list">
      {firm_rows}
    </div>
  </div>
</div>

<div id="job-count" class="job-count"></div>

{job_sections}

<div id="apply-confirm-modal" class="modal-backdrop hidden" role="dialog" aria-modal="true" aria-labelledby="apply-confirm-title">
  <div class="modal-card">
    <h3 id="apply-confirm-title">Mark this job as applied?</h3>
    <p id="apply-confirm-text">The apply page opened in a new tab. If you finished submitting there, mark it as applied on this dashboard.</p>
    <div class="modal-actions">
      <button type="button" class="modal-btn secondary" data-apply-confirm-no>No, keep unchanged</button>
      <button type="button" class="modal-btn primary" data-apply-confirm-yes>Yes, mark applied</button>
    </div>
  </div>
</div>

<script>
const JOBS = {jobs_json};
const PAGE_SIZE = 100;
let minScore = 0;
let searchText = '';
let selectedFirms = [];
let sortOrder = 'newest';
let showStale = false;
let currentPage = 1;
let currentJobs = JOBS.slice();
const DB_APPLIED_COUNT = {applied};
const viewedJobs = new Set();
const manualAppliedJobs = new Set();
let pendingApplyUrl = '';
const VIEWED_STORAGE_KEY = 'applypilot.dashboard.viewedJobs.v1';
const MANUAL_APPLIED_STORAGE_KEY = 'applypilot.dashboard.manualApplied.v1';
const FILTER_STORAGE_KEY = 'applypilot.dashboard.filters.v2';

function loadViewedJobs() {{
  try {{
    const raw = localStorage.getItem(VIEWED_STORAGE_KEY);
    if (!raw) return;
    JSON.parse(raw).forEach(url => viewedJobs.add(url));
  }} catch (err) {{
    console.warn('Could not load viewed jobs', err);
  }}
}}

function saveViewedJobs() {{
  try {{
    localStorage.setItem(VIEWED_STORAGE_KEY, JSON.stringify(Array.from(viewedJobs)));
  }} catch (err) {{
    console.warn('Could not save viewed jobs', err);
  }}
}}

function loadManualAppliedJobs() {{
  try {{
    const raw = localStorage.getItem(MANUAL_APPLIED_STORAGE_KEY);
    if (!raw) return;
    JSON.parse(raw).forEach(url => manualAppliedJobs.add(url));
  }} catch (err) {{
    console.warn('Could not load manually applied jobs', err);
  }}
}}

function saveManualAppliedJobs() {{
  try {{
    localStorage.setItem(MANUAL_APPLIED_STORAGE_KEY, JSON.stringify(Array.from(manualAppliedJobs)));
  }} catch (err) {{
    console.warn('Could not save manually applied jobs', err);
  }}
}}

function loadFilters() {{
  try {{
    const raw = localStorage.getItem(FILTER_STORAGE_KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);
    const savedScore = Number(saved.minScore);
    minScore = savedScore >= 0 ? savedScore : 0;
    searchText = saved.searchText || '';
    if (Array.isArray(saved.selectedFirms)) {{
      selectedFirms = normalizeFirmList(saved.selectedFirms);
    }} else if (saved.selectedFirm) {{
      selectedFirms = normalizeFirmList([saved.selectedFirm]);
    }} else {{
      selectedFirms = [];
    }}
    sortOrder = saved.sortOrder || 'newest';
    showStale = Boolean(saved.showStale);
  }} catch (err) {{
    console.warn('Could not load dashboard filters', err);
  }}
}}

function saveFilters() {{
  try {{
    localStorage.setItem(FILTER_STORAGE_KEY, JSON.stringify({{ minScore, searchText, selectedFirms, sortOrder, showStale }}));
  }} catch (err) {{
    console.warn('Could not save dashboard filters', err);
  }}
}}

function normalizeFirmList(firms) {{
  const seen = new Set();
  const normalized = [];
  firms.forEach(firm => {{
    const value = String(firm || '').trim();
    if (!value || seen.has(value)) return;
    seen.add(value);
    normalized.push(value);
  }});
  return normalized;
}}

function markViewed(url) {{
  if (!url) return;
  viewedJobs.add(url);
  saveViewedJobs();
  syncViewedState();
}}

function syncViewedState() {{
  document.querySelectorAll('.job-card').forEach(card => {{
    const url = card.dataset.url || '';
    const isViewed = viewedJobs.has(url);
    card.classList.toggle('is-viewed', isViewed);
    const badge = card.querySelector('[data-viewed-badge]');
    if (badge) badge.classList.toggle('hidden', !isViewed);
  }});
}}

function markManualApplied(url) {{
  if (!url) return;
  manualAppliedJobs.add(url);
  saveManualAppliedJobs();
  syncManualAppliedState();
}}

function updateManualAppliedSummary() {{
  const manualOnly = JOBS.filter(job => manualAppliedJobs.has(job.url) && !job.dbApplied).length;
  const stat = document.getElementById('applied-stat');
  if (stat) stat.textContent = DB_APPLIED_COUNT + manualOnly;
}}

function syncManualAppliedState() {{
  document.querySelectorAll('.job-card').forEach(card => {{
    const url = card.dataset.url || '';
    const dbApplied = card.dataset.dbApplied === 'true';
    const manualApplied = manualAppliedJobs.has(url);
    card.classList.toggle('is-applied', dbApplied || manualApplied);
    const badge = card.querySelector('[data-manual-applied-badge]');
    if (badge) badge.classList.toggle('hidden', !manualApplied || dbApplied);
    const link = card.querySelector('.apply-link');
    if (link && manualApplied && !dbApplied) link.textContent = 'Open Apply URL';
  }});
  updateManualAppliedSummary();
}}

function getApplyConfirmModal() {{
  return document.getElementById('apply-confirm-modal');
}}

function showApplyConfirm(url, title) {{
  const modal = getApplyConfirmModal();
  if (!modal) return;
  pendingApplyUrl = url || '';
  const text = document.getElementById('apply-confirm-text');
  if (text) {{
    const safeTitle = title || 'this job';
    text.textContent = 'The apply page for "' + safeTitle + '" opened in a new tab. If you finished submitting there, mark it as applied on this dashboard.';
  }}
  modal.classList.remove('hidden');
  const yes = modal.querySelector('[data-apply-confirm-yes]');
  if (yes) yes.focus();
}}

function hideApplyConfirm() {{
  const modal = getApplyConfirmModal();
  if (modal) modal.classList.add('hidden');
  pendingApplyUrl = '';
}}

function initApplyConfirmModal() {{
  const modal = getApplyConfirmModal();
  if (!modal) return;
  const yes = modal.querySelector('[data-apply-confirm-yes]');
  const no = modal.querySelector('[data-apply-confirm-no]');
  if (yes) yes.addEventListener('click', () => {{
    markManualApplied(pendingApplyUrl);
    hideApplyConfirm();
  }});
  if (no) no.addEventListener('click', hideApplyConfirm);
  modal.addEventListener('click', event => {{
    if (event.target === modal) hideApplyConfirm();
  }});
  document.addEventListener('keydown', event => {{
    if (event.key === 'Escape' && !modal.classList.contains('hidden')) hideApplyConfirm();
  }});
}}

function syncFirmControls() {{
  const selected = new Set(selectedFirms);
  document.querySelectorAll('[data-firm-checkbox]').forEach(checkbox => {{
    checkbox.checked = selected.has(checkbox.value);
  }});
  document.querySelectorAll('.firm-row').forEach(row => {{
    const firm = row.dataset.siteFilter || '';
    row.classList.toggle('active', firm ? selected.has(firm) : selected.size === 0);
  }});
  const summary = document.getElementById('firm-filter-summary');
  if (summary) {{
    if (!selectedFirms.length) {{
      summary.textContent = 'All firms';
    }} else if (selectedFirms.length === 1) {{
      summary.textContent = selectedFirms[0];
    }} else {{
      summary.textContent = `${{selectedFirms.length}} firms selected`;
    }}
  }}
}}

function filterScore(min, button = null) {{
  minScore = min;
  currentPage = 1;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  if (button) button.classList.add('active');
  saveFilters();
  applyFilters();
}}

function filterText(text) {{
  searchText = text.toLowerCase();
  currentPage = 1;
  saveFilters();
  applyFilters();
}}

function setFirmChecked(site, checked) {{
  const firm = site || '';
  if (!firm) {{
    selectedFirms = [];
  }} else if (checked) {{
    selectedFirms = normalizeFirmList([...selectedFirms, firm]);
  }} else {{
    selectedFirms = selectedFirms.filter(value => value !== firm);
  }}
  currentPage = 1;
  saveFilters();
  applyFilters();
}}

function clearFirmFilter() {{
  selectedFirms = [];
  currentPage = 1;
  saveFilters();
  applyFilters();
}}

function setSortOrder(order) {{
  sortOrder = order || 'newest';
  currentPage = 1;
  saveFilters();
  applyFilters();
}}

function syncStaleToggle() {{
  const button = document.getElementById('stale-toggle');
  if (!button) return;
  button.classList.toggle('active', showStale);
  button.textContent = showStale ? 'Hide stale' : 'Show stale';
}}

function toggleStale() {{
  showStale = !showStale;
  currentPage = 1;
  saveFilters();
  applyFilters();
}}

function changePage(delta) {{
  const pageCount = Math.max(1, Math.ceil(currentJobs.length / PAGE_SIZE));
  currentPage = Math.min(Math.max(1, currentPage + delta), pageCount);
  renderCurrentPage();
  document.getElementById('job-count')?.scrollIntoView({{ block: 'nearest' }});
}}

function sortJobList(jobs) {{
  return jobs.slice().sort((a, b) => {{
    const scoreA = Number(a.score) || 0;
    const scoreB = Number(b.score) || 0;
    const timeA = Number(a.sortTs) || 0;
    const timeB = Number(b.sortTs) || 0;
    const firmA = (a.site || '').toLowerCase();
    const firmB = (b.site || '').toLowerCase();
    const titleA = (a.title || '').toLowerCase();
    const titleB = (b.title || '').toLowerCase();
    if (sortOrder === 'score') {{
      return (scoreB - scoreA) || (timeB - timeA) || titleA.localeCompare(titleB);
    }}
    if (sortOrder === 'firm') {{
      return firmA.localeCompare(firmB) || (timeB - timeA) || (scoreB - scoreA) || titleA.localeCompare(titleB);
    }}
    return (timeB - timeA) || (scoreB - scoreA) || firmA.localeCompare(firmB) || titleA.localeCompare(titleB);
  }});
}}

function escapeHtml(value) {{
  return String(value ?? '').replace(/[&<>"']/g, ch => ({{
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }}[ch]));
}}

function scoreColor(score) {{
  if (score >= 7) return '#10b981';
  if (score >= 5) return '#f59e0b';
  if (score > 0) return '#ef4444';
  return '#64748b';
}}

function renderStatusChips(job, dbApplied, manualApplied, isViewed) {{
  const chips = [];
  if (job.isStale) {{
    const title = job.lastSeenAt
      ? `Last seen ${{escapeHtml(job.lastSeenAt)}}; latest crawl ${{escapeHtml(job.latestSeenAt || '')}}`
      : `Not seen in latest ${{escapeHtml(job.site || 'firm')}} crawl`;
    chips.push(`<span class="status-chip stale" title="${{title}}">Stale</span>`);
  }}
  if (dbApplied) {{
    chips.push('<span class="status-chip applied">Applied by agent</span>');
  }} else if (job.applyStatus === 'in_progress') {{
    chips.push('<span class="status-chip in-progress">Applying now</span>');
  }} else if (job.applyStatus === 'failed') {{
    chips.push(`<span class="status-chip failed" title="${{escapeHtml(job.applyError || '')}}">Apply failed</span>`);
  }} else if (job.applyStatus === 'manual') {{
    chips.push('<span class="status-chip manual">Manual ATS</span>');
  }}
  if (manualApplied && !dbApplied) {{
    chips.push('<span class="status-chip applied" data-manual-applied-badge>Marked applied</span>');
  }}
  if (isViewed) {{
    chips.push('<span class="status-chip viewed" data-viewed-badge>Viewed</span>');
  }}
  return chips.join('');
}}

function renderJobCard(job) {{
  const score = Number(job.score) || 0;
  const dbApplied = Boolean(job.dbApplied);
  const manualApplied = manualAppliedJobs.has(job.url);
  const isApplied = dbApplied || manualApplied;
  const isViewed = viewedJobs.has(job.url);
  const siteColor = job.siteColor || '#6b7280';
  const statusHtml = renderStatusChips(job, dbApplied, manualApplied, isViewed);
  const salaryHtml = job.salary ? `<span class="meta-tag salary">${{escapeHtml(job.salary)}}</span>` : '';
  const locationHtml = job.location ? `<span class="meta-tag location">${{escapeHtml(job.location.slice(0, 40))}}</span>` : '';
  const keywordsHtml = job.keywords ? `<div class="keywords-row">${{escapeHtml(job.keywords)}}</div>` : '';
  const reasoningHtml = job.reasoning ? `<div class="reasoning-row">${{escapeHtml(job.reasoning)}}</div>` : '';
  const descHtml = job.descPreview ? `<p class="desc-preview">${{escapeHtml(job.descPreview)}}...</p>` : '';
  const applyHtml = job.applyUrl
    ? `<a href="${{escapeHtml(job.applyUrl)}}" class="apply-link" target="_blank" rel="noreferrer">${{isApplied ? 'Open Apply URL' : 'Apply'}}</a>`
    : '';

  return `
    <div class="job-card${{isApplied ? ' is-applied' : ''}}${{isViewed ? ' is-viewed' : ''}}"
      data-score="${{score}}"
      data-url="${{escapeHtml(job.url)}}"
      data-db-applied="${{dbApplied ? 'true' : 'false'}}">
      <div class="card-header">
        <span class="score-pill" style="background:${{scoreColor(score)}}">${{escapeHtml(job.scoreLabel || '?')}}</span>
        <a href="${{escapeHtml(job.url)}}" class="job-title" target="_blank" rel="noreferrer">${{escapeHtml(job.title || 'Untitled')}}</a>
      </div>
      <div class="status-row">${{statusHtml}}</div>
      <div class="meta-row">
        <span class="meta-tag site-tag" style="background:${{siteColor}}33;color:${{siteColor}}">${{escapeHtml(job.site || '?')}}</span>
        <span class="meta-tag date" title="${{escapeHtml(job.dateSource || '')}}: ${{escapeHtml(job.dateIso || '')}}">${{escapeHtml(job.dateSource || 'Discovered')}} ${{escapeHtml(job.dateLabel || 'Unknown date')}}</span>
        ${{salaryHtml}}
        ${{locationHtml}}
      </div>
      ${{keywordsHtml}}
      ${{reasoningHtml}}
      ${{descHtml}}
      <div class="card-footer">${{applyHtml}}</div>
    </div>`;
}}

function updatePaginationStatus(pageCount, start, end) {{
  const label = currentJobs.length
    ? `Page ${{currentPage}} of ${{pageCount}} · jobs ${{start + 1}}-${{end}}`
    : 'No matching jobs';
  document.querySelectorAll('.page-status').forEach(el => {{ el.textContent = label; }});
  document.querySelectorAll('[data-prev-page]').forEach(btn => {{ btn.disabled = currentPage <= 1; }});
  document.querySelectorAll('[data-next-page]').forEach(btn => {{ btn.disabled = currentPage >= pageCount; }});
}}

function renderCurrentPage() {{
  const grid = document.getElementById('job-grid');
  if (!grid) return;
  const pageCount = Math.max(1, Math.ceil(currentJobs.length / PAGE_SIZE));
  currentPage = Math.min(Math.max(1, currentPage), pageCount);
  const start = (currentPage - 1) * PAGE_SIZE;
  const pageJobs = currentJobs.slice(start, start + PAGE_SIZE);
  grid.innerHTML = pageJobs.map(renderJobCard).join('');
  updatePaginationStatus(pageCount, start, start + pageJobs.length);
  syncViewedState();
  syncManualAppliedState();
}}

function applyFilters() {{
  const selected = new Set(selectedFirms);
  const scopedJobs = JOBS.filter(job => {{
    const firm = job.site || '';
    const firmMatch = selected.size === 0 || selected.has(firm);
    const staleMatch = showStale || !job.isStale;
    return firmMatch && staleMatch;
  }});
  currentJobs = sortJobList(scopedJobs.filter(job => {{
    const score = Number(job.score) || 0;
    const text = job.search || '';
    const scoreMatch = minScore <= 0 || score >= minScore;
    const textMatch = !searchText || text.includes(searchText);
    return scoreMatch && textMatch;
  }}));
  const firmLabel = selected.size === 0
    ? 'all firms'
    : selectedFirms.length === 1
      ? selectedFirms[0]
      : `${{selectedFirms.length}} firms`;
  const staleLabel = showStale ? 'including stale' : 'current only';
  document.getElementById('job-count').textContent = `Showing ${{currentJobs.length}} of ${{scopedJobs.length}} jobs · ${{firmLabel}} · ${{staleLabel}} · 100 per page`;
  renderCurrentPage();
  syncFirmControls();
  syncStaleToggle();
}}

function initFirmRows() {{
  document.querySelectorAll('[data-clear-firms]').forEach(button => {{
    button.addEventListener('click', clearFirmFilter);
  }});
  document.querySelectorAll('[data-firm-checkbox]').forEach(checkbox => {{
    checkbox.addEventListener('change', () => setFirmChecked(checkbox.value, checkbox.checked));
  }});
}}

function initViewedLinks() {{
  const grid = document.getElementById('job-grid');
  if (!grid) return;
  grid.addEventListener('click', event => {{
    const link = event.target.closest('a');
    if (!link) return;
    const card = link.closest('.job-card');
    if (!card) return;
    const jobUrl = card.dataset.url || '';
    if (link.classList.contains('apply-link')) {{
      event.preventDefault();
      markViewed(jobUrl);
      window.open(link.href, '_blank', 'noopener,noreferrer');
      const title = card.querySelector('.job-title')?.textContent?.trim() || 'this job';
      showApplyConfirm(jobUrl, title);
      return;
    }}
    if (link.classList.contains('job-title')) {{
      markViewed(jobUrl);
    }}
  }});
}}

function initControls() {{
  loadViewedJobs();
  loadManualAppliedJobs();
  loadFilters();
  document.querySelector('.search-input').value = searchText;
  const sortSelect = document.getElementById('sort-order');
  if (sortSelect) sortSelect.value = sortOrder;
  syncStaleToggle();
  document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));
  const scoreBtnMap = {{ 0: 'All jobs', 1: 'Scored only', 5: '5+ Moderate', 7: '7+ Strong', 8: '8+ Excellent', 9: '9+ Perfect' }};
  const wantedLabel = scoreBtnMap[minScore] || 'All jobs';
  document.querySelectorAll('.filter-btn').forEach(btn => {{
    if (btn.textContent.trim() === wantedLabel) btn.classList.add('active');
  }});
  syncStaleToggle();
  initFirmRows();
  initViewedLinks();
  initApplyConfirmModal();
  syncViewedState();
  syncManualAppliedState();
}}

initControls();
applyFilters();
</script>

</body>
</html>"""

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    abs_path = str(out.resolve())
    console.print(f"[green]Dashboard written to {abs_path}[/green]")
    return abs_path


def open_dashboard(output_path: str | None = None) -> None:
    """Generate the dashboard and open it in the default browser.

    Args:
        output_path: Where to write the HTML file. Defaults to ~/.applypilot/dashboard.html.
    """
    path = generate_dashboard(output_path)
    console.print("[dim]Opening in browser...[/dim]")
    webbrowser.open(f"file:///{path}")
