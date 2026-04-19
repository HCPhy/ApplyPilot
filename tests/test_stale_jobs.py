from pathlib import Path
from tempfile import TemporaryDirectory

from applypilot.database import close_connection, current_job_clause, init_db, prune_stale_jobs


def _insert_job(
    conn,
    url: str,
    *,
    site: str = "Acme",
    strategy: str = "workday_api",
    last_seen_at: str | None = "old-run",
    applied_at: str | None = None,
    apply_status: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO jobs (url, title, site, strategy, last_seen_at, applied_at, apply_status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (url, url, site, strategy, last_seen_at, applied_at, apply_status),
    )


def test_prune_stale_jobs_is_scoped_and_preserves_application_history():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "applypilot.db"
        conn = init_db(db_path)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "last_seen_at" in columns

        _insert_job(conn, "current", last_seen_at="current-run")
        _insert_job(conn, "stale")
        _insert_job(conn, "missing-marker", last_seen_at=None)
        _insert_job(conn, "other-site", site="OtherCo")
        _insert_job(conn, "other-strategy", strategy="greenhouse_api")
        _insert_job(conn, "applied-stale", applied_at="2026-04-11T00:00:00+00:00")
        _insert_job(conn, "in-progress-stale", apply_status="in_progress")
        conn.commit()

        removed = prune_stale_jobs(
            conn,
            site="Acme",
            strategies=("workday_api",),
            seen_at="current-run",
        )

        urls = {row[0] for row in conn.execute("SELECT url FROM jobs").fetchall()}
        assert removed == 2
        assert "current" in urls
        assert "stale" not in urls
        assert "missing-marker" not in urls
        assert "other-site" in urls
        assert "other-strategy" in urls
        assert "applied-stale" in urls
        assert "in-progress-stale" in urls

        close_connection(db_path)


def test_current_job_clause_filters_rows_older_than_latest_seen():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "applypilot.db"
        conn = init_db(db_path)

        _insert_job(conn, "current", last_seen_at="2026-04-18T00:00:00+00:00")
        _insert_job(conn, "stale", last_seen_at="2026-04-17T00:00:00+00:00")
        _insert_job(conn, "missing-marker", last_seen_at=None)
        _insert_job(conn, "other-site", site="OtherCo", last_seen_at="2026-04-17T00:00:00+00:00")
        _insert_job(conn, "other-strategy", strategy="greenhouse_api", last_seen_at="2026-04-17T00:00:00+00:00")
        conn.commit()

        urls = {
            row[0]
            for row in conn.execute(
                f"SELECT url FROM jobs WHERE {current_job_clause('jobs')}"
            ).fetchall()
        }

        assert "current" in urls
        assert "stale" not in urls
        assert "missing-marker" not in urls
        assert "other-site" in urls
        assert "other-strategy" in urls

        close_connection(db_path)


if __name__ == "__main__":
    test_prune_stale_jobs_is_scoped_and_preserves_application_history()
    test_current_job_clause_filters_rows_older_than_latest_seen()
    print("ok")
