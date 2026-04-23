from applypilot.apply import launcher
from applypilot.database import close_connection, init_db


def test_parse_claude_result_detects_credit_balance_error():
    assert launcher._parse_claude_apply_result("Credit balance is too low") == "failed:claude_credit_balance_low"


def test_parse_claude_result_extracts_failed_reason():
    assert launcher._parse_claude_apply_result("RESULT:FAILED:sso_required") == "failed:sso_required"


def test_agent_infra_failure_is_not_job_specific():
    assert launcher._is_agent_infra_failure("failed:claude_credit_balance_low")
    assert launcher._is_agent_infra_failure("failed:workday_missing_password")
    assert launcher._is_agent_infra_failure("failed:workday_dry_run_needs_account")
    assert not launcher._is_agent_infra_failure("failed:sso_required")


def test_targeted_acquire_allows_untouched_job_with_null_apply_status(tmp_path, monkeypatch):
    db_path = tmp_path / "applypilot.sqlite"
    conn = init_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO jobs (
                url, title, site, application_url, fit_score,
                full_description, discovered_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "https://example.com/job/1",
                "Example Engineer",
                "ExampleCo",
                "https://example.com/apply/1",
                8,
                "Build useful systems.",
                "2026-04-20T00:00:00+00:00",
                "2026-04-20T00:00:00+00:00",
            ),
        )
        conn.commit()

        monkeypatch.setattr(launcher, "get_connection", lambda: conn)

        job = launcher.acquire_job(
            target_url="https://example.com/job/1",
            min_score=7,
            worker_id=3,
            use_base_resume=True,
        )

        assert job is not None
        assert job["url"] == "https://example.com/job/1"
        row = conn.execute(
            "SELECT apply_status, agent_id FROM jobs WHERE url = ?",
            ("https://example.com/job/1",),
        ).fetchone()
        assert tuple(row) == ("in_progress", "worker-3")
    finally:
        close_connection(db_path)
