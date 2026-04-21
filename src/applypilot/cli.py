"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from typing import Optional

import typer
from rich.logging import RichHandler
from rich.table import Table

from applypilot import __version__
from applypilot.ui import console

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        RichHandler(
            console=console,
            show_time=True,
            show_level=True,
            show_path=False,
            omit_repeated_times=False,
        )
    ],
    force=True,
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def rescore(
    min_score: int = typer.Option(
        7,
        "--min-score",
        help="Only rescore jobs whose current fit_score is strictly greater than this value.",
    ),
    limit: int = typer.Option(
        0,
        "--limit",
        "-l",
        help="Maximum jobs to rescore. Use 0 for all matching jobs.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show how many jobs would be rescored without calling the LLM.",
    ),
) -> None:
    """Refine existing fit scores above a threshold with the configured LLM."""
    _bootstrap()

    from applypilot.config import check_tier
    from applypilot.database import get_connection
    from applypilot.scoring.scorer import run_scoring

    check_tier(2, "AI rescoring")

    if min_score < 0 or min_score > 10:
        console.print("[red]--min-score must be between 0 and 10.[/red]")
        raise typer.Exit(code=1)
    if limit < 0:
        console.print("[red]--limit must be 0 or greater.[/red]")
        raise typer.Exit(code=1)

    conn = get_connection()
    total = conn.execute(
        """
        SELECT COUNT(*) FROM jobs
        WHERE full_description IS NOT NULL
          AND fit_score IS NOT NULL
          AND fit_score > ?
        """,
        (min_score,),
    ).fetchone()[0]
    selected = min(total, limit) if limit > 0 else total

    console.print(
        f"[bold]Rescore candidates:[/bold] {selected} job(s) "
        f"with current score > {min_score}"
    )
    if limit > 0 and total > limit:
        console.print(f"[dim]{total} match the threshold; --limit keeps this run to {limit}.[/dim]")

    if dry_run:
        return
    if selected == 0:
        console.print("[yellow]No matching jobs to rescore.[/yellow]")
        return

    result = run_scoring(limit=limit, rescore_min_score=min_score)

    table = Table(title="Rescore Complete", show_header=True, header_style="bold cyan")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Scored", str(result["scored"]))
    table.add_row("Errors", str(result["errors"]))
    table.add_row("Elapsed", f"{result['elapsed']:.1f}s")
    console.print(table)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    agent: str = typer.Option("claude", "--agent", help="Apply runtime: claude, openai, or deterministic."),
    model: str = typer.Option(
        "haiku",
        "--model",
        "-m",
        help="Agent model name. Defaults to haiku for Claude and computer-use-preview for OpenAI.",
    ),
    effort: str = typer.Option("low", "--effort", help="Claude reasoning effort: low, medium, high, or max."),
    max_budget_usd: Optional[float] = typer.Option(None, "--max-budget-usd", help="Hard per-job Claude budget cap in USD."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    use_base_resume: bool = typer.Option(
        False,
        "--use-base-resume",
        help="Apply using your master resume instead of requiring tailored resumes.",
    ),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import (
        check_tier,
        get_chrome_path,
        PROFILE_PATH as _profile_path,
        RESUME_PATH as _resume_path,
        RESUME_PDF_PATH as _resume_pdf_path,
    )
    from applypilot.database import get_connection
    import os

    agent = agent.lower().strip()
    valid_agents = {"claude", "openai", "deterministic"}
    if agent not in valid_agents:
        console.print(
            f"[red]Invalid --agent value:[/red] '{agent}'. "
            f"Choose from: {', '.join(sorted(valid_agents))}"
        )
        raise typer.Exit(code=1)

    if agent == "openai" and model == "haiku":
        model = "computer-use-preview"
    if agent == "openai" and workers != 1:
        console.print("[yellow]--agent openai currently supports one worker; using --workers 1.[/yellow]")
        workers = 1
    if agent == "openai" and max_budget_usd is not None:
        console.print("[yellow]--max-budget-usd is only enforced by Claude Code; ignoring it for --agent openai.[/yellow]")
    if agent == "deterministic" and max_budget_usd is not None:
        console.print("[yellow]--max-budget-usd does not apply to --agent deterministic.[/yellow]")
    if agent == "deterministic" and model != "haiku":
        console.print("[yellow]--model is ignored for --agent deterministic.[/yellow]")

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: apply runtime requirements
    if agent == "claude":
        check_tier(3, "auto-apply")
    elif agent == "openai":
        check_tier(2, "OpenAI auto-apply")
        if not os.environ.get("OPENAI_API_KEY"):
            console.print("[red]OPENAI_API_KEY is required for --agent openai.[/red]")
            raise typer.Exit(code=1)
        from applypilot.apply.openai_runner import check_openai_computer_use_access
        ok, reason = check_openai_computer_use_access(model)
        if not ok:
            console.print(f"[red]{reason}[/red]")
            console.print(
                "[dim]Use Claude for apply for now, or switch to an OpenAI API org "
                "with computer-use-preview access.[/dim]"
            )
            raise typer.Exit(code=1)
        try:
            get_chrome_path()
        except FileNotFoundError:
            console.print("[red]Chrome/Chromium is required for --agent openai.[/red]")
            raise typer.Exit(code=1)
    else:
        try:
            get_chrome_path()
        except FileNotFoundError:
            console.print("[red]Chrome/Chromium is required for --agent deterministic.[/red]")
            raise typer.Exit(code=1)

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Resume assets / queue exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        if use_base_resume:
            if not _resume_pdf_path.exists():
                console.print(
                    "[red]Base resume PDF not found.[/red]\n"
                    "Run [bold]applypilot init[/bold] or place your PDF at "
                    f"[bold]{_resume_pdf_path}[/bold]."
                )
                raise typer.Exit(code=1)
            if not _resume_path.exists():
                console.print(
                    "[yellow]Base resume text not found.[/yellow]\n"
                    "Continuing with the PDF only may reduce autofill quality. "
                    f"Expected text file at [bold]{_resume_path}[/bold]."
                )
            ready = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE fit_score >= ? AND applied_at IS NULL",
                (min_score,),
            ).fetchone()[0]
        else:
            ready = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
            ).fetchone()[0]
        if ready == 0:
            if use_base_resume:
                console.print(
                    "[red]No scored jobs ready.[/red]\n"
                    f"Run [bold]applypilot run score[/bold] first, or lower [bold]--min-score[/bold] below {min_score}."
                )
            else:
                console.print(
                    "[red]No tailored resumes ready.[/red]\n"
                    "Run [bold]applypilot run score tailor[/bold] first to prepare applications, "
                    "or use [bold]--use-base-resume[/bold]."
                )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_openai_prompt, gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        if agent == "openai":
            prompt_file = gen_openai_prompt(
                target,
                min_score=min_score,
                use_base_resume=use_base_resume,
            )
        else:
            prompt_file = gen_prompt(
                target,
                min_score=min_score,
                model=model,
                use_base_resume=use_base_resume,
            )
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        if agent == "claude":
            mcp_path = _profile_path.parent / ".mcp-apply-0.json"
            console.print(f"\n[bold]Run manually:[/bold]")
            console.print(
                f"  claude --model {model} -p "
                f"--mcp-config {mcp_path} "
                f"--permission-mode bypassPermissions < {prompt_file}"
            )
        elif agent == "openai":
            console.print("[dim]OpenAI computer-use prompts run through applypilot apply --agent openai.[/dim]")
        else:
            console.print("[dim]Deterministic apply runs directly in applypilot; there is no agent prompt file to run manually.[/dim]")
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Agent:    {agent}")
    console.print(f"  Model:    {model}")
    if agent == "claude":
        console.print(f"  Effort:   {effort}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    console.print(f"  Resume:   {'base' if use_base_resume else 'tailored'}")
    if max_budget_usd is not None:
        console.print(f"  Budget:   ${max_budget_usd:.2f}/job")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        effort=effort,
        max_budget_usd=max_budget_usd,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
        use_base_resume=use_base_resume,
        agent=agent,
    )


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard(
    max_jobs: int = typer.Option(
        0,
        "--max-jobs",
        help="Maximum job cards to embed in the HTML. Use 0 for all jobs.",
    ),
    no_open: bool = typer.Option(
        False,
        "--no-open",
        help="Generate the dashboard without opening a browser.",
    ),
) -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    if max_jobs < 0:
        console.print("[red]--max-jobs must be 0 or greater.[/red]")
        raise typer.Exit(code=1)

    open_dashboard(max_jobs=max_jobs, open_browser=not no_open)


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    results.append(("Discovery sources", ok_mark, "Official ATS only: Workday, Greenhouse, Lever"))

    # --- Tier 2 checks ---
    import os
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    if has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        "Set GEMINI_API_KEY in ~/.applypilot/.env (run 'applypilot init')"))

    # --- Tier 3 checks ---
    # Claude Code CLI
    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for apply --agent claude)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → OpenAI apply needs OPENAI_API_KEY + Chrome; Claude apply needs Claude Code CLI + Chrome + Node.js[/dim]")
    elif tier == 2:
        console.print("[dim]  → apply --agent openai works with OPENAI_API_KEY + Chrome; apply --agent claude needs Claude Code CLI + Node.js[/dim]")

    console.print()


if __name__ == "__main__":
    app()
