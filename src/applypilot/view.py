"""ApplyPilot HTML Dashboard Generator.

Generates a self-contained HTML dashboard with:
  - Summary stats (total, enriched, scored, high-fit)
  - Score distribution bar chart
  - Firm-level filtering and breakdown
  - Filterable job cards grouped by score
  - Client-side search, firm selection, and remembered viewed jobs
"""

from __future__ import annotations

import os
import webbrowser
from html import escape
from pathlib import Path

from rich.console import Console

from applypilot.config import APP_DIR, DB_PATH
from applypilot.database import get_connection

console = Console()


def generate_dashboard(output_path: str | None = None) -> str:
    """Generate an HTML dashboard of all jobs with fit scores.

    Args:
        output_path: Where to write the HTML file. Defaults to ~/.applypilot/dashboard.html.

    Returns:
        Absolute path to the generated HTML file.
    """
    out = Path(output_path) if output_path else APP_DIR / "dashboard.html"

    conn = get_connection()

    # Stats
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    ready = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND application_url IS NOT NULL"
    ).fetchone()[0]
    scored = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
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
            "WHERE fit_score IS NOT NULL "
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
               SUM(CASE WHEN fit_score < 5 AND fit_score IS NOT NULL THEN 1 ELSE 0 END) as low_fit,
               SUM(CASE WHEN fit_score IS NULL THEN 1 ELSE 0 END) as unscored,
               SUM(CASE WHEN applied_at IS NOT NULL OR apply_status = 'applied' THEN 1 ELSE 0 END) as applied,
               SUM(CASE WHEN apply_status = 'in_progress' THEN 1 ELSE 0 END) as in_progress,
               ROUND(AVG(fit_score), 1) as avg_score
        FROM jobs GROUP BY site ORDER BY high_fit DESC, total DESC
    """).fetchall()

    # All scored jobs (5+), ordered by score desc
    jobs = conn.execute("""
        SELECT url, title, salary, description, location, site, strategy,
               full_description, application_url, detail_error,
               fit_score, score_reasoning, apply_status, applied_at, apply_error
        FROM jobs
        WHERE fit_score >= 5
        ORDER BY fit_score DESC, site, title
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

    # Firm filter options + stats rows
    firm_options = ""
    firm_rows = """
        <button type="button" class="site-row firm-row active" data-site-filter="">
          <div class="site-name">All Firms</div>
          <div class="site-nums">Show every scored job currently on the dashboard</div>
        </button>"""

    firms_alpha = sorted(site_stats, key=lambda row: (row["site"] or "").lower())
    for s in firms_alpha:
        site = s["site"] or "?"
        firm_options += f'<option value="{escape(site)}">{escape(site)} ({s["total"]})</option>'

    for s in site_stats:
        site = s["site"] or "?"
        color = colors.get(site, "#6b7280")
        avg = s["avg_score"] or 0
        firm_rows += f"""
        <button type="button" class="site-row firm-row" data-site-filter="{escape(site)}">
          <div class="site-name" style="color:{color}">{escape(site)}</div>
          <div class="site-nums">{s['total']} jobs &middot; {s['high_fit']} strong fit &middot; {s['applied']} applied &middot; avg score {avg}</div>
          <div class="bar-track">
            <div class="bar-fill" style="width:{s['high_fit']/max(s['total'],1)*100}%;background:{color}"></div>
            <div class="bar-fill" style="width:{s['mid_fit']/max(s['total'],1)*100}%;background:{color}66"></div>
          </div>
        </button>"""

    # Job cards grouped by score
    job_sections = ""
    current_score = None
    for j in jobs:
        score = j["fit_score"] or 0
        if score != current_score:
            if current_score is not None:
                job_sections += "</div>"
            score_color = "#10b981" if score >= 7 else "#f59e0b"
            score_label = {
                10: "Perfect Match", 9: "Excellent Fit", 8: "Strong Fit",
                7: "Good Fit", 6: "Moderate+", 5: "Moderate",
            }.get(score, f"Score {score}")
            count_at_score = score_dist.get(score, 0)
            job_sections += f"""
            <h2 class="score-header" style="border-color:{score_color}">
              <span class="score-badge" style="background:{score_color}">{score}</span>
              {score_label} ({count_at_score} jobs)
            </h2>
            <div class="job-grid">"""
            current_score = score

        title = escape(j["title"] or "Untitled")
        url = escape(j["url"] or "")
        salary = escape(j["salary"] or "")
        location = escape(j["location"] or "")
        site = escape(j["site"] or "")
        site_color = colors.get(j["site"] or "", "#6b7280")
        apply_url = escape(j["application_url"] or "")
        apply_status = (j["apply_status"] or "").strip()
        apply_error = escape((j["apply_error"] or "")[:120])
        is_applied = bool(j["applied_at"]) or apply_status == "applied"

        # Parse keywords and reasoning from score_reasoning
        reasoning_raw = j["score_reasoning"] or ""
        reasoning_lines = reasoning_raw.split("\n")
        keywords = reasoning_lines[0][:120] if reasoning_lines else ""
        reasoning = reasoning_lines[1][:200] if len(reasoning_lines) > 1 else ""

        desc_preview = escape(j["full_description"] or "")[:300]
        full_desc_html = escape(j["full_description"] or "").replace("\n", "<br>")
        desc_len = len(j["full_description"] or "")

        meta_parts = []
        meta_parts.append(
            f'<span class="meta-tag site-tag" style="background:{site_color}33;color:{site_color}">{site}</span>'
        )
        if salary:
            meta_parts.append(f'<span class="meta-tag salary">{salary}</span>')
        if location:
            meta_parts.append(f'<span class="meta-tag location">{location[:40]}</span>')
        meta_html = " ".join(meta_parts)

        state_parts = []
        if is_applied:
            state_parts.append('<span class="status-chip applied">Applied by agent</span>')
        elif apply_status == "in_progress":
            state_parts.append('<span class="status-chip in-progress">Applying now</span>')
        elif apply_status == "failed":
            tooltip = f' title="{apply_error}"' if apply_error else ""
            state_parts.append(f'<span class="status-chip failed"{tooltip}>Apply failed</span>')
        elif apply_status == "manual":
            state_parts.append('<span class="status-chip manual">Manual ATS</span>')
        state_parts.append('<span class="status-chip viewed hidden" data-viewed-badge>Viewed</span>')
        state_html = "".join(state_parts)

        apply_html = ""
        if apply_url:
            apply_label = "Open Apply URL" if is_applied else "Apply"
            apply_html = f'<a href="{apply_url}" class="apply-link" target="_blank" rel="noreferrer">{apply_label}</a>'

        job_sections += f"""
        <div class="job-card{' is-applied' if is_applied else ''}" data-score="{score}" data-site="{escape(j['site'] or '')}" data-location="{location.lower()}" data-url="{url}" data-apply-status="{escape(apply_status)}">
          <div class="card-header">
            <span class="score-pill" style="background:{'#10b981' if score >= 7 else '#f59e0b'}">{score}</span>
            <a href="{url}" class="job-title" target="_blank" rel="noreferrer">{title}</a>
          </div>
          <div class="status-row">{state_html}</div>
          <div class="meta-row">{meta_html}</div>
          {f'<div class="keywords-row">{escape(keywords)}</div>' if keywords else ''}
          {f'<div class="reasoning-row">{escape(reasoning)}</div>' if reasoning else ''}
          <p class="desc-preview">{desc_preview}...</p>
          {"<details class='full-desc-details'><summary class='expand-btn'>Full Description (" + f'{desc_len:,}' + " chars)</summary><div class='full-desc'>" + full_desc_html + "</div></details>" if j["full_description"] else ""}
          <div class="card-footer">{apply_html}</div>
        </div>"""

    if current_score is not None:
        job_sections += "</div>"

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
  .firm-select {{ background: #334155; border: 1px solid #475569; color: #e2e8f0; padding: 0.4rem 0.8rem; border-radius: 6px; font-size: 0.8rem; min-width: 220px; }}

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
  .site-row {{ margin-bottom: 0.8rem; width: 100%; text-align: left; background: #0f172a; border: 1px solid #334155; border-radius: 10px; padding: 0.75rem; cursor: pointer; }}
  .site-row:hover {{ border-color: #60a5fa55; background: #111c34; }}
  .site-row.active {{ border-color: #60a5fa; box-shadow: inset 0 0 0 1px #60a5fa33; }}
  .site-name {{ font-weight: 600; font-size: 0.9rem; }}
  .site-nums {{ color: #94a3b8; font-size: 0.75rem; margin: 0.15rem 0; }}
  .bar-track {{ height: 8px; background: #334155; border-radius: 4px; display: flex; overflow: hidden; }}
  .bar-fill {{ height: 100%; transition: width 0.3s; }}

  /* Score group headers */
  .score-header {{ font-size: 1.2rem; font-weight: 600; margin: 2.5rem 0 1rem; padding-bottom: 0.5rem; border-bottom: 3px solid; display: flex; align-items: center; gap: 0.75rem; }}
  .score-badge {{ display: inline-flex; align-items: center; justify-content: center; width: 2rem; height: 2rem; border-radius: 8px; color: #0f172a; font-weight: 700; font-size: 1rem; }}

  /* Job grid */
  .job-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 1rem; }}

  .job-card {{ background: #1e293b; border-radius: 10px; padding: 1rem; border-left: 3px solid #334155; transition: all 0.15s; }}
  .job-card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 12px #00000044; }}
  .job-card.is-viewed {{ box-shadow: inset 0 0 0 1px #60a5fa33; }}
  .job-card.is-applied {{ background: linear-gradient(180deg, #132a23 0%, #1e293b 22%); }}
  .job-card[data-score="9"], .job-card[data-score="10"] {{ border-left-color: #10b981; }}
  .job-card[data-score="8"] {{ border-left-color: #34d399; }}
  .job-card[data-score="7"] {{ border-left-color: #60a5fa; }}
  .job-card[data-score="6"] {{ border-left-color: #f59e0b; }}
  .job-card[data-score="5"] {{ border-left-color: #f59e0b88; }}

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

  .meta-row {{ display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.4rem; }}
  .meta-tag {{ font-size: 0.72rem; padding: 0.15rem 0.5rem; border-radius: 4px; background: #334155; color: #94a3b8; }}
  .meta-tag.salary {{ background: #064e3b; color: #6ee7b7; }}
  .meta-tag.location {{ background: #1e3a5f; color: #93c5fd; }}

  .keywords-row {{ font-size: 0.75rem; color: #10b981; margin-bottom: 0.3rem; line-height: 1.4; }}
  .reasoning-row {{ font-size: 0.75rem; color: #94a3b8; margin-bottom: 0.5rem; font-style: italic; line-height: 1.4; }}

  .desc-preview {{ font-size: 0.8rem; color: #64748b; line-height: 1.5; margin-bottom: 0.75rem; max-height: 3.6em; overflow: hidden; }}

  .card-footer {{ display: flex; justify-content: flex-end; }}
  .apply-link {{ font-size: 0.8rem; color: #60a5fa; text-decoration: none; padding: 0.3rem 0.8rem; border: 1px solid #60a5fa33; border-radius: 6px; font-weight: 500; }}
  .apply-link:hover {{ background: #60a5fa22; }}

  /* Expandable full description */
  .full-desc-details {{ margin-bottom: 0.75rem; }}
  .expand-btn {{ font-size: 0.8rem; color: #60a5fa; cursor: pointer; list-style: none; padding: 0.3rem 0; }}
  .expand-btn::-webkit-details-marker {{ display: none; }}
  .expand-btn:hover {{ color: #93c5fd; }}
  .full-desc {{ font-size: 0.8rem; color: #cbd5e1; line-height: 1.6; margin-top: 0.5rem; padding: 0.75rem; background: #0f172a; border-radius: 8px; max-height: 400px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; }}

  .hidden {{ display: none !important; }}
  .job-count {{ color: #94a3b8; font-size: 0.85rem; margin-bottom: 1rem; }}

  @media (max-width: 768px) {{
    .summary {{ grid-template-columns: repeat(2, 1fr); }}
    .score-section {{ grid-template-columns: 1fr; }}
    .score-dist, .sites-section {{ height: auto; }}
    .firm-list {{ max-height: 320px; }}
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
  <div class="stat-card stat-applied"><div class="stat-num">{applied}</div><div class="stat-label">Applied by Agent</div></div>
</div>

<div class="filters">
  <span class="filter-label">Score:</span>
  <button class="filter-btn active" onclick="filterScore(0, this)">All 5+</button>
  <button class="filter-btn" onclick="filterScore(7, this)">7+ Strong</button>
  <button class="filter-btn" onclick="filterScore(8, this)">8+ Excellent</button>
  <button class="filter-btn" onclick="filterScore(9, this)">9+ Perfect</button>
  <span class="filter-label" style="margin-left:1rem">Firm:</span>
  <select id="firm-filter" class="firm-select" onchange="filterFirm(this.value)">
    <option value="">All firms</option>
    {firm_options}
  </select>
  <span class="filter-label" style="margin-left:1rem">Search:</span>
  <input type="text" class="search-input" placeholder="Filter by title, site..." oninput="filterText(this.value)">
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

<script>
let minScore = 0;
let searchText = '';
let selectedFirm = '';
const viewedJobs = new Set();
const VIEWED_STORAGE_KEY = 'applypilot.dashboard.viewedJobs.v1';
const FILTER_STORAGE_KEY = 'applypilot.dashboard.filters.v1';

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

function loadFilters() {{
  try {{
    const raw = localStorage.getItem(FILTER_STORAGE_KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);
    minScore = saved.minScore || 0;
    searchText = saved.searchText || '';
    selectedFirm = saved.selectedFirm || '';
  }} catch (err) {{
    console.warn('Could not load dashboard filters', err);
  }}
}}

function saveFilters() {{
  try {{
    localStorage.setItem(FILTER_STORAGE_KEY, JSON.stringify({{ minScore, searchText, selectedFirm }}));
  }} catch (err) {{
    console.warn('Could not save dashboard filters', err);
  }}
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

function syncFirmControls() {{
  const select = document.getElementById('firm-filter');
  if (select) select.value = selectedFirm;
  document.querySelectorAll('.firm-row').forEach(row => {{
    row.classList.toggle('active', row.dataset.siteFilter === selectedFirm);
  }});
}}

function filterScore(min, button = null) {{
  minScore = min;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  if (button) button.classList.add('active');
  saveFilters();
  applyFilters();
}}

function filterText(text) {{
  searchText = text.toLowerCase();
  saveFilters();
  applyFilters();
}}

function filterFirm(site) {{
  selectedFirm = site || '';
  saveFilters();
  applyFilters();
}}

function applyFilters() {{
  let shown = 0;
  let total = 0;
  let firmTotal = 0;
  document.querySelectorAll('.job-card').forEach(card => {{
    total++;
    const score = parseInt(card.dataset.score) || 0;
    const text = card.textContent.toLowerCase();
    const firm = card.dataset.site || '';
    const scoreMatch = score >= (minScore || 5);
    const textMatch = !searchText || text.includes(searchText);
    const firmMatch = !selectedFirm || firm === selectedFirm;
    if (firmMatch) firmTotal++;
    if (scoreMatch && textMatch && firmMatch) {{
      card.classList.remove('hidden');
      shown++;
    }} else {{
      card.classList.add('hidden');
    }}
  }});
  const scopeTotal = selectedFirm ? firmTotal : total;
  const scopeLabel = selectedFirm ? ` for ${{selectedFirm}}` : '';
  document.getElementById('job-count').textContent = `Showing ${{shown}} of ${{scopeTotal}} jobs${{scopeLabel}}`;

  // Hide empty score groups
  document.querySelectorAll('.score-header').forEach(header => {{
    const grid = header.nextElementSibling;
    if (grid && grid.classList.contains('job-grid')) {{
      const visible = grid.querySelectorAll('.job-card:not(.hidden)').length;
      header.style.display = visible ? '' : 'none';
      grid.style.display = visible ? '' : 'none';
    }}
  }});

  syncFirmControls();
}}

function initFirmRows() {{
  document.querySelectorAll('.firm-row').forEach(row => {{
    row.addEventListener('click', () => filterFirm(row.dataset.siteFilter || ''));
  }});
}}

function initViewedLinks() {{
  document.querySelectorAll('.job-title, .apply-link').forEach(link => {{
    link.addEventListener('click', () => {{
      const card = link.closest('.job-card');
      if (card) markViewed(card.dataset.url);
    }});
  }});
}}

function initControls() {{
  loadViewedJobs();
  loadFilters();
  document.querySelector('.search-input').value = searchText;
  document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));
  const scoreBtnMap = {{ 0: 'All 5+', 7: '7+ Strong', 8: '8+ Excellent', 9: '9+ Perfect' }};
  const wantedLabel = scoreBtnMap[minScore] || 'All 5+';
  document.querySelectorAll('.filter-btn').forEach(btn => {{
    if (btn.textContent.trim() === wantedLabel) btn.classList.add('active');
  }});
  initFirmRows();
  initViewedLinks();
  syncViewedState();
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
