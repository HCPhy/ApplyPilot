from pathlib import Path

from applypilot import database
from applypilot.database import close_connection, get_connection, init_db
from applypilot.view import (
    load_dashboard_data,
    mark_job_applied,
    mark_job_saved,
    render_dashboard_html,
)


def _insert_job(
    conn,
    url: str,
    *,
    title: str = "Software Engineer",
    site: str = "Acme",
    strategy: str = "workday_api",
    application_url: str = "https://example.com/apply",
    fit_score: int | None = 8,
) -> None:
    conn.execute(
        """
        INSERT INTO jobs (url, title, site, strategy, application_url, fit_score)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (url, title, site, strategy, application_url, fit_score),
    )


def test_init_db_includes_saved_at_column(tmp_path):
    db_path = Path(tmp_path) / "applypilot.db"
    conn = init_db(db_path)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "saved_at" in columns

    close_connection(db_path)


def test_mark_job_saved_and_applied_updates_sqlite_state(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "applypilot.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    conn = init_db(db_path)
    _insert_job(conn, "https://example.com/jobs/1")
    conn.commit()

    saved_at = mark_job_saved("https://example.com/jobs/1", saved=True)
    row = get_connection(db_path).execute(
        "SELECT saved_at, applied_at, apply_status FROM jobs WHERE url = ?",
        ("https://example.com/jobs/1",),
    ).fetchone()
    assert saved_at
    assert row["saved_at"] == saved_at
    assert row["applied_at"] is None
    assert row["apply_status"] is None

    applied_at = mark_job_applied("https://example.com/jobs/1")
    row = get_connection(db_path).execute(
        "SELECT saved_at, applied_at, apply_status FROM jobs WHERE url = ?",
        ("https://example.com/jobs/1",),
    ).fetchone()
    assert applied_at
    assert row["saved_at"] is None
    assert row["applied_at"] == applied_at
    assert row["apply_status"] == "applied"

    close_connection(db_path)


def test_dashboard_data_and_html_include_all_todo_applied_views(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "applypilot.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    conn = init_db(db_path)
    _insert_job(conn, "https://example.com/jobs/open", title="Open Job")
    _insert_job(conn, "https://example.com/jobs/todo", title="Todo Job")
    _insert_job(conn, "https://example.com/jobs/applied", title="Applied Job")
    conn.execute(
        "UPDATE jobs SET saved_at = ? WHERE url = ?",
        ("2026-04-21T00:00:00+00:00", "https://example.com/jobs/todo"),
    )
    conn.execute(
        "UPDATE jobs SET apply_status = 'applied', applied_at = ? WHERE url = ?",
        ("2026-04-21T00:00:00+00:00", "https://example.com/jobs/applied"),
    )
    conn.commit()

    data = load_dashboard_data()
    assert data["total"] == 3
    assert data["saved"] == 1
    assert data["applied"] == 1
    assert any(job["savedAt"] for job in data["jobs"] if job["url"].endswith("/todo"))

    html = render_dashboard_html(data, api_enabled=True)
    assert 'data-bucket-tab="all"' in html
    assert 'data-bucket-tab="todo"' in html
    assert 'data-bucket-tab="applied"' in html
    assert "/api/jobs/todo" in html
    assert "/api/jobs/applied" in html

    close_connection(db_path)
