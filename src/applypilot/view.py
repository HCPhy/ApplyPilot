"""ApplyPilot dashboard rendering and browser launch helpers."""

from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from rich.console import Console

from applypilot.config import APP_DIR
from applypilot.database import get_connection, init_db

console = Console()

DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_WAIT_SECONDS = 5.0

DASHBOARD_COLORS = {
    "RemoteOK": "#10b981",
    "WelcomeToTheJungle": "#f59e0b",
    "Job Bank Canada": "#3b82f6",
    "CareerJet Canada": "#8b5cf6",
    "Hacker News Jobs": "#ff6600",
    "BuiltIn Remote": "#ec4899",
    "TD Bank": "#00a651",
    "CIBC": "#c41f3e",
    "RBC": "#003168",
    "indeed": "#2164f3",
    "linkedin": "#0a66c2",
    "Dice": "#eb1c26",
    "Glassdoor": "#0caa41",
}


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


def _dashboard_connection() -> sqlite3.Connection:
    try:
        return init_db()
    except sqlite3.OperationalError as e:
        conn = get_connection()
        console.print(
            f"[yellow]Could not migrate dashboard DB schema ({e}); "
            "falling back to discovery-time ordering.[/yellow]"
        )
        return conn


def _is_db_applied(applied_at: str | None, apply_status: str | None) -> bool:
    return bool(applied_at) or (apply_status or "").strip() == "applied"


def _score_distribution(conn: sqlite3.Connection) -> dict[int, int]:
    score_dist: dict[int, int] = {}
    rows = conn.execute(
        "SELECT fit_score, COUNT(*) FROM jobs "
        "WHERE fit_score > 0 "
        "GROUP BY fit_score ORDER BY fit_score DESC"
    ).fetchall()
    for row in rows:
        score_dist[int(row[0])] = int(row[1])
    return score_dist


def load_dashboard_data(max_jobs: int = 0) -> dict:
    """Load dashboard data from SQLite for either snapshot or server mode."""
    conn = _dashboard_connection()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    has_posted_at = "posted_at" in columns
    posted_select = "jobs.posted_at AS posted_at" if has_posted_at else "NULL AS posted_at"
    newest_expr = "COALESCE(jobs.posted_at, jobs.discovered_at)" if has_posted_at else "jobs.discovered_at"
    limit_clause = " LIMIT ?" if max_jobs > 0 else ""
    limit_params = (max_jobs,) if max_jobs > 0 else ()

    total = int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
    ready = int(
        conn.execute(
            "SELECT COUNT(*) FROM jobs "
            "WHERE full_description IS NOT NULL AND application_url IS NOT NULL"
        ).fetchone()[0]
    )
    scored = int(conn.execute("SELECT COUNT(*) FROM jobs WHERE fit_score > 0").fetchone()[0])
    high_fit = int(conn.execute("SELECT COUNT(*) FROM jobs WHERE fit_score >= 7").fetchone()[0])
    applied = int(
        conn.execute(
            "SELECT COUNT(*) FROM jobs "
            "WHERE applied_at IS NOT NULL OR apply_status = 'applied'"
        ).fetchone()[0]
    )
    saved = int(
        conn.execute(
            "SELECT COUNT(*) FROM jobs "
            "WHERE saved_at IS NOT NULL "
            "AND applied_at IS NULL "
            "AND COALESCE(apply_status, '') != 'applied'"
        ).fetchone()[0]
    )
    active = total - applied

    score_dist = _score_distribution(conn)

    site_stats = conn.execute(
        """
        SELECT site,
               COUNT(*) AS total,
               SUM(CASE WHEN fit_score >= 7 THEN 1 ELSE 0 END) AS high_fit,
               SUM(CASE WHEN fit_score BETWEEN 5 AND 6 THEN 1 ELSE 0 END) AS mid_fit,
               SUM(CASE WHEN fit_score BETWEEN 1 AND 4 THEN 1 ELSE 0 END) AS low_fit,
               SUM(CASE WHEN fit_score IS NULL THEN 1 ELSE 0 END) AS unscored,
               SUM(CASE WHEN saved_at IS NOT NULL
                         AND applied_at IS NULL
                         AND COALESCE(apply_status, '') != 'applied'
                        THEN 1 ELSE 0 END) AS saved,
               SUM(CASE WHEN applied_at IS NOT NULL OR apply_status = 'applied' THEN 1 ELSE 0 END) AS applied,
               SUM(CASE WHEN apply_status = 'in_progress' THEN 1 ELSE 0 END) AS in_progress,
               ROUND(AVG(CASE WHEN fit_score > 0 THEN fit_score END), 1) AS avg_score
        FROM jobs
        GROUP BY site
        ORDER BY high_fit DESC, total DESC
        """
    ).fetchall()

    jobs = conn.execute(
        f"""
        WITH latest_seen AS (
            SELECT COALESCE(site, '') AS site_key,
                   COALESCE(strategy, '') AS strategy_key,
                   MAX(last_seen_at) AS latest_seen_at
            FROM jobs
            WHERE last_seen_at IS NOT NULL
            GROUP BY COALESCE(site, ''), COALESCE(strategy, '')
        )
        SELECT jobs.url, jobs.title, jobs.salary, jobs.description,
               jobs.location, jobs.site, jobs.strategy,
               substr(COALESCE(jobs.full_description, jobs.description, ''), 1, 180) AS desc_preview,
               jobs.application_url, jobs.detail_error,
               jobs.fit_score, jobs.score_reasoning, jobs.apply_status,
               jobs.applied_at, jobs.apply_error, jobs.saved_at,
               {posted_select}, discovered_at, last_seen_at,
               latest_seen.latest_seen_at AS latest_seen_at
        FROM jobs
        LEFT JOIN latest_seen
          ON latest_seen.site_key = COALESCE(jobs.site, '')
         AND latest_seen.strategy_key = COALESCE(jobs.strategy, '')
        WHERE fit_score IS NULL OR fit_score > 0
        ORDER BY {newest_expr} DESC, fit_score DESC, site, title
        {limit_clause}
        """,
        limit_params,
    ).fetchall()

    loaded_jobs = len(jobs)
    load_note = ""
    if max_jobs > 0 and loaded_jobs < total:
        load_note = f" Loaded freshest {loaded_jobs} of {total} jobs."

    job_items = []
    for row in jobs:
        score = row["fit_score"] or 0
        apply_status = (row["apply_status"] or "").strip()
        is_applied = _is_db_applied(row["applied_at"], apply_status)
        is_in_progress = apply_status == "in_progress"
        latest_seen_at = row["latest_seen_at"]
        last_seen_at = row["last_seen_at"]
        is_stale = bool(
            latest_seen_at
            and last_seen_at != latest_seen_at
            and not is_applied
            and not is_in_progress
        )
        date_label, date_source, sort_ts, date_iso = _date_meta(row["posted_at"], row["discovered_at"])

        reasoning_raw = row["score_reasoning"] or ""
        reasoning_lines = reasoning_raw.split("\n")
        keywords = reasoning_lines[0][:120] if reasoning_lines else ""
        reasoning = reasoning_lines[1][:200] if len(reasoning_lines) > 1 else ""

        search_blob = " ".join(
            [
                row["title"] or "",
                row["site"] or "",
                row["location"] or "",
                keywords,
                reasoning,
            ]
        ).lower()

        site = row["site"] or ""
        job_items.append(
            {
                "url": row["url"] or "",
                "title": row["title"] or "Untitled",
                "salary": row["salary"] or "",
                "location": row["location"] or "",
                "site": site,
                "siteColor": DASHBOARD_COLORS.get(site, "#6b7280"),
                "applyUrl": row["application_url"] or "",
                "applyStatus": apply_status,
                "applyError": (row["apply_error"] or "")[:120],
                "dbApplied": is_applied,
                "savedAt": row["saved_at"] or "",
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
                "descPreview": row["desc_preview"] or "",
                "search": search_blob,
            }
        )

    return {
        "total": total,
        "ready": ready,
        "scored": scored,
        "high_fit": high_fit,
        "applied": applied,
        "saved": saved,
        "active": active,
        "score_dist": score_dist,
        "site_stats": site_stats,
        "jobs": job_items,
        "loaded_jobs": loaded_jobs,
        "load_note": load_note,
    }


def mark_job_saved(url: str, saved: bool = True) -> str | None:
    """Persist or clear the Todo marker for one job."""
    conn = _dashboard_connection()
    saved_at = datetime.now(timezone.utc).isoformat() if saved else None
    conn.execute(
        "UPDATE jobs SET saved_at = ? WHERE url = ?",
        (saved_at, url),
    )
    conn.commit()
    return saved_at


def mark_job_applied(url: str) -> str:
    """Persist a manual applied confirmation from the dashboard."""
    conn = _dashboard_connection()
    applied_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE jobs
        SET apply_status = 'applied',
            applied_at = ?,
            apply_error = NULL,
            agent_id = NULL,
            saved_at = NULL
        WHERE url = ?
        """,
        (applied_at, url),
    )
    conn.commit()
    return applied_at


def _score_bars_html(score_dist: dict[int, int]) -> str:
    score_bars = ""
    max_count = max(score_dist.values()) if score_dist else 1
    for score in range(10, 0, -1):
        count = score_dist.get(score, 0)
        pct = (count / max_count * 100) if max_count else 0
        score_color = "#10b981" if score >= 7 else ("#f59e0b" if score >= 5 else "#ef4444")
        score_bars += f"""
        <div class="score-row">
          <span class="score-label">{score}</span>
          <div class="score-bar-track">
            <div class="score-bar-fill" style="width:{pct}%;background:{score_color}"></div>
          </div>
          <span class="score-count">{count}</span>
        </div>"""
    return score_bars


def _firm_rows_html(site_stats) -> str:
    firm_rows = """
        <button type="button" class="site-row firm-row active" data-site-filter="" data-clear-firms>
          <div class="site-name">All Firms</div>
          <div class="site-nums">Clear selected firms</div>
        </button>"""

    for stat in site_stats:
        site = stat["site"] or "?"
        color = DASHBOARD_COLORS.get(site, "#6b7280")
        avg = stat["avg_score"] or 0
        firm_rows += f"""
        <label class="site-row firm-row firm-checkbox-row" data-site-filter="{escape(site)}">
          <input type="checkbox" class="firm-checkbox" value="{escape(site)}" data-firm-checkbox style="accent-color:{color}">
          <span class="firm-row-copy">
            <span class="site-name" style="color:{color}">{escape(site)}</span>
            <span class="site-nums">{stat['total']} jobs &middot; {stat['high_fit']} strong fit &middot; {stat['saved']} todo &middot; {stat['applied']} applied &middot; avg score {avg}</span>
            <span class="bar-track">
              <span class="bar-fill" style="width:{stat['high_fit']/max(stat['total'], 1)*100}%;background:{color}"></span>
              <span class="bar-fill" style="width:{stat['mid_fit']/max(stat['total'], 1)*100}%;background:{color}66"></span>
            </span>
          </span>
        </label>"""

    return firm_rows


def render_dashboard_html(data: dict, *, api_enabled: bool = False) -> str:
    """Render the dashboard HTML with either snapshot or API-backed behavior."""
    jobs_json = (
        json.dumps(data["jobs"], ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    score_bars = _score_bars_html(data["score_dist"])
    firm_rows = _firm_rows_html(data["site_stats"])
    subtitle_bits = [
        f"{data['total']} jobs",
        f"{data['scored']} scored",
        f"{data['high_fit']} strong matches (7+)",
    ]
    if api_enabled:
        subtitle_bits.append("interactive mode saves Todo/Applied to SQLite")
    subtitle = " &middot; ".join(subtitle_bits)
    api_enabled_js = "true" if api_enabled else "false"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ApplyPilot Dashboard</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background: #0f172a; color: #e2e8f0; padding: 2rem; }}
  h1 {{ font-size: 1.8rem; font-weight: 700; margin-bottom: 0.5rem; }}
  .subtitle {{ color: #94a3b8; margin-bottom: 1.5rem; }}

  .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(165px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }}
  .stat-card {{ background: #1e293b; border-radius: 12px; padding: 1.25rem; }}
  .stat-num {{ font-size: 2rem; font-weight: 700; }}
  .stat-label {{ color: #94a3b8; font-size: 0.85rem; margin-top: 0.25rem; }}
  .stat-total .stat-num {{ color: #e2e8f0; }}
  .stat-active .stat-num {{ color: #c4b5fd; }}
  .stat-todo .stat-num {{ color: #fbbf24; }}
  .stat-ready .stat-num {{ color: #10b981; }}
  .stat-scored .stat-num {{ color: #60a5fa; }}
  .stat-high .stat-num {{ color: #f59e0b; }}
  .stat-applied .stat-num {{ color: #34d399; }}

  .view-tabs {{ display: flex; flex-wrap: wrap; gap: 0.75rem; margin-bottom: 1rem; }}
  .view-tab {{ background: #172033; border: 1px solid #334155; color: #cbd5e1; padding: 0.7rem 1rem; border-radius: 999px; cursor: pointer; font-size: 0.9rem; font-weight: 600; }}
  .view-tab.active {{ background: #60a5fa; color: #0f172a; border-color: #60a5fa; }}
  .view-tab .tab-count {{ opacity: 0.8; margin-left: 0.3rem; }}

  .filters {{ background: #1e293b; border-radius: 12px; padding: 1.25rem; margin-bottom: 2rem; display: flex; gap: 1rem; flex-wrap: wrap; align-items: center; }}
  .filter-label {{ color: #94a3b8; font-size: 0.85rem; font-weight: 600; }}
  .filter-btn {{ background: #334155; border: none; color: #94a3b8; padding: 0.4rem 0.8rem; border-radius: 6px; cursor: pointer; font-size: 0.8rem; transition: all 0.15s; }}
  .filter-btn:hover {{ background: #475569; color: #e2e8f0; }}
  .filter-btn.active {{ background: #60a5fa; color: #0f172a; font-weight: 600; }}
  .search-input {{ background: #334155; border: 1px solid #475569; color: #e2e8f0; padding: 0.4rem 0.8rem; border-radius: 6px; font-size: 0.8rem; width: 220px; }}
  .search-input::placeholder {{ color: #64748b; }}
  .sort-select {{ background: #334155; border: 1px solid #475569; color: #e2e8f0; padding: 0.4rem 0.8rem; border-radius: 6px; font-size: 0.8rem; min-width: 200px; }}
  .firm-filter-summary {{ color: #94a3b8; font-size: 0.8rem; min-width: 7.5rem; }}
  .filter-hint {{ color: #94a3b8; font-size: 0.78rem; }}

  .score-section {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 2.5rem; align-items: stretch; }}
  .score-dist, .sites-section {{ background: #1e293b; border-radius: 12px; padding: 1.5rem; height: 420px; display: flex; flex-direction: column; }}
  .score-dist h3, .sites-section h3 {{ font-size: 1rem; margin-bottom: 1rem; color: #94a3b8; }}
  .score-row {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.4rem; }}
  .score-label {{ width: 1.5rem; text-align: right; font-size: 0.85rem; font-weight: 600; }}
  .score-bar-track {{ flex: 1; height: 14px; background: #334155; border-radius: 4px; overflow: hidden; }}
  .score-bar-fill {{ height: 100%; border-radius: 4px; transition: width 0.3s; }}
  .score-count {{ width: 2.5rem; font-size: 0.8rem; color: #94a3b8; }}

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

  .jobs-section {{ margin-top: 1rem; }}
  .list-heading {{ display: flex; justify-content: space-between; align-items: end; gap: 1rem; margin: 0 0 1rem; }}
  .list-heading h2 {{ font-size: 1.2rem; font-weight: 700; }}
  .list-heading p {{ color: #94a3b8; font-size: 0.8rem; max-width: 520px; text-align: right; }}
  .job-count {{ color: #94a3b8; font-size: 0.85rem; margin-bottom: 1rem; }}
  .pagination-controls {{ display: flex; align-items: center; justify-content: center; gap: 0.75rem; margin: 0 0 1rem; }}
  .pagination-bottom {{ margin: 1.5rem 0 0; }}
  .page-btn {{ background: #334155; border: 1px solid #475569; color: #e2e8f0; padding: 0.4rem 0.85rem; border-radius: 7px; cursor: pointer; font-size: 0.8rem; font-weight: 600; }}
  .page-btn:hover:not(:disabled) {{ background: #475569; }}
  .page-btn:disabled {{ cursor: not-allowed; opacity: 0.45; }}
  .page-status {{ color: #94a3b8; font-size: 0.82rem; min-width: 190px; text-align: center; }}
  .job-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 1rem; }}

  .job-card {{ background: #1e293b; border-radius: 10px; padding: 1rem; border-left: 3px solid #334155; transition: all 0.15s; content-visibility: auto; contain-intrinsic-size: 280px; }}
  .job-card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 12px #00000044; }}
  .job-card.is-viewed {{ box-shadow: inset 0 0 0 1px #60a5fa33; }}
  .job-card.is-applied {{ background: linear-gradient(180deg, #132a23 0%, #1e293b 22%); }}
  .job-card.is-saved {{ box-shadow: inset 0 0 0 1px #f59e0b33; }}
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
  .status-chip.todo {{ background: #3b2f06; color: #fde68a; }}

  .meta-row {{ display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.4rem; }}
  .meta-tag {{ font-size: 0.72rem; padding: 0.15rem 0.5rem; border-radius: 4px; background: #334155; color: #94a3b8; }}
  .meta-tag.salary {{ background: #064e3b; color: #6ee7b7; }}
  .meta-tag.location {{ background: #1e3a5f; color: #93c5fd; }}
  .meta-tag.date {{ background: #312e81; color: #c4b5fd; }}

  .keywords-row {{ font-size: 0.75rem; color: #10b981; margin-bottom: 0.3rem; line-height: 1.4; }}
  .reasoning-row {{ font-size: 0.75rem; color: #94a3b8; margin-bottom: 0.5rem; font-style: italic; line-height: 1.4; }}
  .desc-preview {{ font-size: 0.8rem; color: #64748b; line-height: 1.5; margin-bottom: 0.75rem; max-height: 3.6em; overflow: hidden; }}

  .card-footer {{ display: flex; justify-content: flex-end; gap: 0.55rem; flex-wrap: wrap; }}
  .apply-link, .card-btn {{ font-size: 0.8rem; text-decoration: none; padding: 0.3rem 0.8rem; border-radius: 6px; font-weight: 500; cursor: pointer; }}
  .apply-link {{ color: #60a5fa; border: 1px solid #60a5fa33; }}
  .apply-link:hover {{ background: #60a5fa22; }}
  .card-btn {{ background: #334155; border: 1px solid #475569; color: #e2e8f0; }}
  .card-btn:hover {{ background: #475569; }}
  .card-btn.todo-active {{ border-color: #f59e0b; color: #fde68a; }}

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
<p class="subtitle">{subtitle}</p>

<div class="summary">
  <div class="stat-card stat-total"><div class="stat-num">{data['total']}</div><div class="stat-label">Total Jobs</div></div>
  <div class="stat-card stat-active"><div id="active-stat" class="stat-num">{data['active']}</div><div class="stat-label">Active Jobs</div></div>
  <div class="stat-card stat-todo"><div id="todo-stat" class="stat-num">{data['saved']}</div><div class="stat-label">Todo Jobs</div></div>
  <div class="stat-card stat-ready"><div class="stat-num">{data['ready']}</div><div class="stat-label">Ready (desc + URL)</div></div>
  <div class="stat-card stat-scored"><div class="stat-num">{data['scored']}</div><div class="stat-label">Scored by LLM</div></div>
  <div class="stat-card stat-high"><div class="stat-num">{data['high_fit']}</div><div class="stat-label">Strong Fit (7+)</div></div>
  <div class="stat-card stat-applied"><div id="applied-stat" class="stat-num">{data['applied']}</div><div class="stat-label">Applied</div></div>
</div>

<div class="view-tabs" aria-label="Dashboard views">
  <button type="button" class="view-tab active" data-bucket-tab="all" onclick="setBucket('all')">All <span id="bucket-count-all" class="tab-count"></span></button>
  <button type="button" class="view-tab" data-bucket-tab="todo" onclick="setBucket('todo')">Todo <span id="bucket-count-todo" class="tab-count"></span></button>
  <button type="button" class="view-tab" data-bucket-tab="applied" onclick="setBucket('applied')">Applied <span id="bucket-count-applied" class="tab-count"></span></button>
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
  <span id="stale-filter-summary" class="filter-hint"></span>
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

<section class="jobs-section">
  <div class="list-heading">
    <div>
      <h2 id="jobs-heading">All Jobs</h2>
    </div>
    <p id="jobs-description">Uses ATS posted/updated dates when available, then falls back to discovery time. Cards render 100 per page.{data['load_note']}</p>
  </div>
  <div class="pagination-controls" aria-label="Job pagination">
    <button type="button" class="page-btn" onclick="changePage(-1)" data-prev-page>Previous</button>
    <span class="page-status"></span>
    <button type="button" class="page-btn" onclick="changePage(1)" data-next-page>Next</button>
  </div>
  <div id="job-grid" class="job-grid"></div>
  <div class="pagination-controls pagination-bottom" aria-label="Job pagination">
    <button type="button" class="page-btn" onclick="changePage(-1)" data-prev-page>Previous</button>
    <span class="page-status"></span>
    <button type="button" class="page-btn" onclick="changePage(1)" data-next-page>Next</button>
  </div>
</section>

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
const API_ENABLED = {api_enabled_js};
const PAGE_SIZE = 100;
let minScore = 0;
let searchText = '';
let selectedFirms = [];
let sortOrder = 'newest';
let showStale = false;
let currentBucket = 'all';
let currentPage = 1;
let currentJobs = JOBS.slice();
let pendingApplyUrl = '';
const VIEWED_STORAGE_KEY = 'applypilot.dashboard.viewedJobs.v1';
const LOCAL_TODO_STORAGE_KEY = 'applypilot.dashboard.todoJobs.v1';
const LOCAL_APPLIED_STORAGE_KEY = 'applypilot.dashboard.manualApplied.v2';
const FILTER_STORAGE_KEY = 'applypilot.dashboard.filters.v3';
const viewedJobs = new Set();
const localTodoJobs = new Set();
const localAppliedJobs = new Set();

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

function loadLocalSet(key, target) {{
  try {{
    const raw = localStorage.getItem(key);
    if (!raw) return;
    JSON.parse(raw).forEach(url => target.add(url));
  }} catch (err) {{
    console.warn('Could not load dashboard set', key, err);
  }}
}}

function saveLocalSet(key, source) {{
  try {{
    localStorage.setItem(key, JSON.stringify(Array.from(source)));
  }} catch (err) {{
    console.warn('Could not save dashboard set', key, err);
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
    selectedFirms = Array.isArray(saved.selectedFirms) ? normalizeFirmList(saved.selectedFirms) : [];
    sortOrder = saved.sortOrder || 'newest';
    showStale = Boolean(saved.showStale);
    currentBucket = ['all', 'todo', 'applied'].includes(saved.currentBucket) ? saved.currentBucket : 'all';
  }} catch (err) {{
    console.warn('Could not load dashboard filters', err);
  }}
}}

function saveFilters() {{
  try {{
    localStorage.setItem(
      FILTER_STORAGE_KEY,
      JSON.stringify({{ minScore, searchText, selectedFirms, sortOrder, showStale, currentBucket }})
    );
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

function findJob(url) {{
  return JOBS.find(job => job.url === url) || null;
}}

function isSavedJob(job) {{
  return API_ENABLED ? Boolean(job.savedAt) : localTodoJobs.has(job.url);
}}

function isAppliedJob(job) {{
  return Boolean(job.dbApplied) || (!API_ENABLED && localAppliedJobs.has(job.url));
}}

function updateOverviewStats() {{
  const activeCount = JOBS.filter(job => !isAppliedJob(job)).length;
  const todoCount = JOBS.filter(job => isSavedJob(job) && !isAppliedJob(job)).length;
  const appliedCount = JOBS.filter(isAppliedJob).length;
  const activeStat = document.getElementById('active-stat');
  const todoStat = document.getElementById('todo-stat');
  const appliedStat = document.getElementById('applied-stat');
  if (activeStat) activeStat.textContent = activeCount;
  if (todoStat) todoStat.textContent = todoCount;
  if (appliedStat) appliedStat.textContent = appliedCount;
  const allTab = document.getElementById('bucket-count-all');
  const todoTab = document.getElementById('bucket-count-todo');
  const appliedTab = document.getElementById('bucket-count-applied');
  if (allTab) allTab.textContent = `(${{activeCount}})`;
  if (todoTab) todoTab.textContent = `(${{todoCount}})`;
  if (appliedTab) appliedTab.textContent = `(${{appliedCount}})`;
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

async function postJson(path, payload) {{
  const response = await fetch(path, {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify(payload),
  }});
  if (!response.ok) {{
    throw new Error(`Request failed: ${{response.status}}`);
  }}
  return await response.json();
}}

async function toggleTodo(url) {{
  const job = findJob(url);
  if (!job || isAppliedJob(job)) return;
  const nextSaved = !isSavedJob(job);
  if (API_ENABLED) {{
    try {{
      const result = await postJson('/api/jobs/todo', {{ url, saved: nextSaved }});
      job.savedAt = result.saved_at || '';
    }} catch (err) {{
      console.warn('Could not update todo state', err);
      return;
    }}
  }} else {{
    if (nextSaved) localTodoJobs.add(url);
    else localTodoJobs.delete(url);
    saveLocalSet(LOCAL_TODO_STORAGE_KEY, localTodoJobs);
  }}
  updateOverviewStats();
  applyFilters();
}}

async function markApplied(url) {{
  const job = findJob(url);
  if (!job) return;
  if (API_ENABLED) {{
    try {{
      const result = await postJson('/api/jobs/applied', {{ url }});
      job.dbApplied = true;
      job.applyStatus = 'applied';
      job.savedAt = '';
      job.appliedAt = result.applied_at || '';
    }} catch (err) {{
      console.warn('Could not persist applied state', err);
      return;
    }}
  }} else {{
    localAppliedJobs.add(url);
    localTodoJobs.delete(url);
    saveLocalSet(LOCAL_APPLIED_STORAGE_KEY, localAppliedJobs);
    saveLocalSet(LOCAL_TODO_STORAGE_KEY, localTodoJobs);
  }}
  updateOverviewStats();
  applyFilters();
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
  if (yes) yes.addEventListener('click', async () => {{
    await markApplied(pendingApplyUrl);
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
  document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));
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
  const appliedBucket = currentBucket === 'applied';
  button.disabled = appliedBucket;
  button.classList.toggle('active', showStale && !appliedBucket);
  button.textContent = appliedBucket ? 'Stale off for applied' : (showStale ? 'Hide stale' : 'Show stale');
  const summary = document.getElementById('stale-filter-summary');
  if (summary) {{
    summary.textContent = appliedBucket
      ? 'Applied jobs ignore stale filtering'
      : (showStale ? 'Including stale postings' : 'Current postings only');
  }}
}}

function toggleStale() {{
  if (currentBucket === 'applied') return;
  showStale = !showStale;
  currentPage = 1;
  saveFilters();
  applyFilters();
}}

function setBucket(bucket) {{
  currentBucket = bucket;
  currentPage = 1;
  saveFilters();
  applyFilters();
}}

function syncBucketTabs() {{
  document.querySelectorAll('[data-bucket-tab]').forEach(tab => {{
    tab.classList.toggle('active', tab.dataset.bucketTab === currentBucket);
  }});
  const heading = document.getElementById('jobs-heading');
  const description = document.getElementById('jobs-description');
  if (heading) {{
    heading.textContent = currentBucket === 'todo'
      ? 'Todo Jobs'
      : currentBucket === 'applied'
        ? 'Applied Jobs'
        : 'All Jobs';
  }}
  if (description) {{
    description.textContent = currentBucket === 'todo'
      ? 'Jobs you explicitly set aside for later. Todo jobs stay in SQLite in interactive mode and in local browser storage in snapshot mode.'
      : currentBucket === 'applied'
        ? 'Jobs already marked as applied. This view ignores stale filtering so your history stays intact.'
        : 'Uses ATS posted/updated dates when available, then falls back to discovery time. Cards render 100 per page.{data["load_note"]}';
  }}
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

function renderStatusChips(job, isViewed) {{
  const chips = [];
  const applied = isAppliedJob(job);
  const saved = isSavedJob(job);
  if (job.isStale && !applied) {{
    const title = job.lastSeenAt
      ? `Last seen ${{escapeHtml(job.lastSeenAt)}}; latest crawl ${{escapeHtml(job.latestSeenAt || '')}}`
      : `Not seen in latest ${{escapeHtml(job.site || 'firm')}} crawl`;
    chips.push(`<span class="status-chip stale" title="${{title}}">Stale</span>`);
  }}
  if (saved && !applied) {{
    chips.push('<span class="status-chip todo">Todo</span>');
  }}
  if (applied) {{
    chips.push('<span class="status-chip applied">Applied</span>');
  }} else if (job.applyStatus === 'in_progress') {{
    chips.push('<span class="status-chip in-progress">Applying now</span>');
  }} else if (job.applyStatus === 'failed') {{
    chips.push(`<span class="status-chip failed" title="${{escapeHtml(job.applyError || '')}}">Apply failed</span>`);
  }} else if (job.applyStatus === 'manual') {{
    chips.push('<span class="status-chip manual">Manual ATS</span>');
  }}
  if (isViewed) {{
    chips.push('<span class="status-chip viewed" data-viewed-badge>Viewed</span>');
  }}
  return chips.join('');
}}

function renderJobCard(job) {{
  const score = Number(job.score) || 0;
  const isApplied = isAppliedJob(job);
  const isSaved = isSavedJob(job);
  const isViewed = viewedJobs.has(job.url);
  const siteColor = job.siteColor || '#6b7280';
  const statusHtml = renderStatusChips(job, isViewed);
  const salaryHtml = job.salary ? `<span class="meta-tag salary">${{escapeHtml(job.salary)}}</span>` : '';
  const locationHtml = job.location ? `<span class="meta-tag location">${{escapeHtml(job.location.slice(0, 40))}}</span>` : '';
  const keywordsHtml = job.keywords ? `<div class="keywords-row">${{escapeHtml(job.keywords)}}</div>` : '';
  const reasoningHtml = job.reasoning ? `<div class="reasoning-row">${{escapeHtml(job.reasoning)}}</div>` : '';
  const descHtml = job.descPreview ? `<p class="desc-preview">${{escapeHtml(job.descPreview)}}...</p>` : '';
  const applyHtml = !isApplied && job.applyUrl
    ? `<a href="${{escapeHtml(job.applyUrl)}}" class="apply-link" target="_blank" rel="noreferrer">Apply</a>`
    : (job.applyUrl ? `<a href="${{escapeHtml(job.applyUrl)}}" class="apply-link" target="_blank" rel="noreferrer">Open Apply URL</a>` : '');
  const todoButton = !isApplied
    ? `<button type="button" class="card-btn${{isSaved ? ' todo-active' : ''}}" data-action="toggle-todo" data-url="${{escapeHtml(job.url)}}">${{isSaved ? 'Remove Todo' : 'Add to Todo'}}</button>`
    : '';

  return `
    <div class="job-card${{isApplied ? ' is-applied' : ''}}${{isSaved ? ' is-saved' : ''}}${{isViewed ? ' is-viewed' : ''}}"
      data-score="${{score}}"
      data-url="${{escapeHtml(job.url)}}">
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
      <div class="card-footer">
        ${{todoButton}}
        ${{applyHtml}}
      </div>
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
}}

function applyFilters() {{
  const selected = new Set(selectedFirms);
  const scopedJobs = JOBS.filter(job => {{
    const firm = job.site || '';
    const firmMatch = selected.size === 0 || selected.has(firm);
    const staleMatch = currentBucket === 'applied' || showStale || !job.isStale;
    return firmMatch && staleMatch;
  }});
  const bucketJobs = scopedJobs.filter(job => {{
    const applied = isAppliedJob(job);
    const saved = isSavedJob(job);
    if (currentBucket === 'todo') return saved && !applied;
    if (currentBucket === 'applied') return applied;
    return !applied;
  }});
  currentJobs = sortJobList(bucketJobs.filter(job => {{
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
  const bucketLabel = currentBucket === 'todo'
    ? 'todo jobs'
    : currentBucket === 'applied'
      ? 'applied jobs'
      : 'active jobs';
  const staleLabel = currentBucket === 'applied'
    ? 'stale filter ignored'
    : (showStale ? 'including stale' : 'current only');
  document.getElementById('job-count').textContent = `Showing ${{currentJobs.length}} of ${{bucketJobs.length}} ${{bucketLabel}} in scope · ${{firmLabel}} · ${{staleLabel}} · 100 per page`;
  renderCurrentPage();
  syncFirmControls();
  syncStaleToggle();
  syncBucketTabs();
}}

function initFirmRows() {{
  document.querySelectorAll('[data-clear-firms]').forEach(button => {{
    button.addEventListener('click', clearFirmFilter);
  }});
  document.querySelectorAll('[data-firm-checkbox]').forEach(checkbox => {{
    checkbox.addEventListener('change', () => setFirmChecked(checkbox.value, checkbox.checked));
  }});
}}

function initGridActions() {{
  const grid = document.getElementById('job-grid');
  if (!grid) return;
  grid.addEventListener('click', async event => {{
    const actionBtn = event.target.closest('[data-action="toggle-todo"]');
    if (actionBtn) {{
      event.preventDefault();
      await toggleTodo(actionBtn.dataset.url || '');
      return;
    }}
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
  loadFilters();
  if (!API_ENABLED) {{
    loadLocalSet(LOCAL_TODO_STORAGE_KEY, localTodoJobs);
    loadLocalSet(LOCAL_APPLIED_STORAGE_KEY, localAppliedJobs);
  }}
  document.querySelector('.search-input').value = searchText;
  const sortSelect = document.getElementById('sort-order');
  if (sortSelect) sortSelect.value = sortOrder;
  document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));
  const scoreBtnMap = {{ 0: 'All jobs', 1: 'Scored only', 5: '5+ Moderate', 7: '7+ Strong', 8: '8+ Excellent', 9: '9+ Perfect' }};
  const wantedLabel = scoreBtnMap[minScore] || 'All jobs';
  document.querySelectorAll('.filter-btn').forEach(btn => {{
    if (btn.textContent.trim() === wantedLabel) btn.classList.add('active');
  }});
  initFirmRows();
  initGridActions();
  initApplyConfirmModal();
  updateOverviewStats();
  syncFirmControls();
  syncStaleToggle();
  syncBucketTabs();
}}

initControls();
applyFilters();
</script>

</body>
</html>"""


def generate_dashboard(output_path: str | None = None, max_jobs: int = 0) -> str:
    """Generate a static HTML snapshot of the dashboard."""
    out = Path(output_path) if output_path else APP_DIR / "dashboard.html"
    html = render_dashboard_html(load_dashboard_data(max_jobs=max_jobs), api_enabled=False)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    abs_path = str(out.resolve())
    console.print(f"[green]Dashboard snapshot written to {abs_path}[/green]")
    return abs_path


def _pick_free_port(host: str = DASHBOARD_HOST) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _wait_for_server(url: str, timeout: float = DASHBOARD_WAIT_SECONDS) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=0.5) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.1)
    return False


def open_dashboard(
    output_path: str | None = None,
    max_jobs: int = 0,
    open_browser: bool = True,
) -> None:
    """Open the dashboard in persistent interactive mode, with snapshot fallback."""
    if not open_browser:
        generate_dashboard(output_path, max_jobs=max_jobs)
        return

    port = _pick_free_port()
    url = f"http://{DASHBOARD_HOST}:{port}"
    cmd = [
        sys.executable,
        "-m",
        "applypilot.dashboard_server",
        "--host",
        DASHBOARD_HOST,
        "--port",
        str(port),
        "--max-jobs",
        str(max_jobs),
    ]

    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if _wait_for_server(url):
            console.print(f"[green]Interactive dashboard ready at {url}[/green]")
            webbrowser.open(url)
            return
        console.print("[yellow]Dashboard server did not become ready in time; opening snapshot instead.[/yellow]")
    except OSError as e:
        console.print(f"[yellow]Could not start interactive dashboard server ({e}); opening snapshot instead.[/yellow]")

    path = generate_dashboard(output_path, max_jobs=max_jobs)
    console.print("[dim]Opening snapshot in browser...[/dim]")
    webbrowser.open(f"file:///{path}")
