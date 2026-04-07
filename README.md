<!-- logo here -->

> **⚠️ ApplyPilot** is the original open-source project, created by [Pickle-Pixel](https://github.com/Pickle-Pixel) and first published on GitHub on **February 17, 2026**. We are **not affiliated** with applypilot.app, useapplypilot.com, or any other product using the "ApplyPilot" name. These sites are **not associated with this project** and may misrepresent what they offer. If you're looking for the autonomous, open-source job application agent — you're in the right place.

# ApplyPilot

**Applied to 1,000 jobs in 2 days. Fully autonomous. Open source.**

[![PyPI version](https://img.shields.io/pypi/v/applypilot?color=blue)](https://pypi.org/project/applypilot/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/Pickle-Pixel/ApplyPilot?style=social)](https://github.com/Pickle-Pixel/ApplyPilot)
[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/S6S01UL5IO)




https://github.com/user-attachments/assets/7ee3417f-43d4-4245-9952-35df1e77f2df


---

## What It Does

ApplyPilot is a 6-stage autonomous job application pipeline. It discovers jobs from official Workday, Greenhouse, and Lever company portals, scores them against your resume with AI, tailors your resume per job, writes cover letters, and **submits applications for you**. It navigates forms, uploads documents, answers screening questions, all hands-free.

Typical flow:

```bash
pip install applypilot
applypilot init          # one-time setup: resume, profile, preferences, API keys
applypilot doctor        # verify your setup — shows what's installed and what's missing
applypilot run           # discover > enrich > score > tailor > cover letters
applypilot run -w 4      # same but parallel (4 threads for discovery/enrichment)
applypilot apply         # autonomous browser-driven submission
applypilot apply -w 3    # parallel apply (3 Chrome instances)
applypilot apply --dry-run  # fill forms without submitting
```

---

## Mental Model

The most important distinction in ApplyPilot is:

- `applypilot run` prepares applications. It does **not** submit anything.
- `applypilot apply` opens the browser and **does** submit applications unless you use `--dry-run`.
- `applypilot apply --dry-run` is the safe review mode: it fills and checks forms, but does not click the final submit button.

If you are new to the project, think of `run` as the research and document-prep pipeline, and `apply` as the browser automation step.

---

## Recommended First Run

If you do not want fully autonomous submission on day one, use this workflow:

```bash
applypilot init
applypilot doctor
applypilot run
applypilot status
applypilot dashboard
applypilot apply --dry-run --url "JOB_URL"
applypilot apply --url "JOB_URL"
```

Recommended safety settings for your first few real applications:

- use `--url` to target one job at a time
- use `--workers 1`
- do **not** use `--continuous`
- do **not** use `--headless`
- keep Chrome visible while it runs

This gives you a practical human-in-the-loop workflow even though the default `apply` mode is autonomous.

---

## Two Paths

### Full Pipeline (recommended)
**Requires:** Python 3.11+, Node.js (for npx), an LLM provider (Gemini, OpenAI, or local), Claude Code CLI, Chrome

Runs all 6 stages, from job discovery to autonomous application submission. This is the full power of ApplyPilot.

### Discovery + Tailoring Only
**Requires:** Python 3.11+, an LLM provider (Gemini, OpenAI, or local)

Runs stages 1-5: discovers jobs, scores them, tailors your resume, generates cover letters. You submit applications manually with the AI-prepared materials.

---

## The Pipeline

| Stage | What Happens |
|-------|-------------|
| **1. Discover** | Scrapes 51 Workday employer portals + 47 Greenhouse company boards + 16 Lever company boards |
| **2. Enrich** | Fetches full job descriptions via JSON-LD, CSS selectors, or AI-powered extraction |
| **3. Score** | AI rates every job 1-10 based on your resume and preferences. Only high-fit jobs proceed |
| **4. Tailor** | AI rewrites your resume per job: reorganizes, emphasizes relevant experience, adds keywords. Never fabricates |
| **5. Cover Letter** | AI generates a targeted cover letter per job |
| **6. Auto-Apply** | Claude Code navigates application forms, fills fields, uploads documents, answers questions, and submits |

Each stage is independent. Run them all or pick what you need.

---

## ApplyPilot vs The Alternatives

| Feature | ApplyPilot | AIHawk | Manual |
|---------|-----------|--------|--------|
| Job discovery | Official ATS only: Workday + Greenhouse + Lever | LinkedIn only | One board at a time |
| AI scoring | 1-10 fit score per job | Basic filtering | Your gut feeling |
| Resume tailoring | Per-job AI rewrite | Template-based | Hours per application |
| Auto-apply | Full form navigation + submission | LinkedIn Easy Apply only | Click, type, repeat |
| Supported sites | 51 Workday portals, 47 Greenhouse boards, 16 Lever boards | LinkedIn | Whatever you open |
| License | AGPL-3.0 | MIT | N/A |

---

## Requirements

| Component | Required For | Details |
|-----------|-------------|---------|
| Python 3.11+ | Everything | Core runtime |
| Node.js 18+ | Auto-apply | Needed for `npx` to run Playwright MCP server |
| LLM provider | Scoring, tailoring, cover letters | Gemini, OpenAI, or a local OpenAI-compatible endpoint |
| Chrome/Chromium | Auto-apply | Auto-detected on most systems |
| Claude Code CLI | Auto-apply | Install from [claude.ai/code](https://claude.ai/code) |

**Gemini API key is free.** Get one at [aistudio.google.com](https://aistudio.google.com). OpenAI and local models (Ollama/llama.cpp) are also supported.

### Optional

| Component | What It Does |
|-----------|-------------|
| CapSolver API key | Solves CAPTCHAs during auto-apply (hCaptcha, reCAPTCHA, Turnstile, FunCaptcha). Without it, CAPTCHA-blocked applications just fail gracefully |

## Configuration

All generated by `applypilot init`:

### `profile.json`
Your personal data in one structured file: contact info, work authorization, compensation, experience, skills, resume facts (preserved during tailoring), and EEO defaults.

This powers:

- scoring
- tailoring
- cover letters
- form auto-fill during auto-apply

It does **not** directly control which jobs are crawled.

### `searches.yaml`
Job search queries, target titles, locations, and crawl settings.

This is the file that drives discovery. If you want to change what gets searched, which roles get queried, or which locations are targeted, edit `searches.yaml`.

### `.env`
API keys and runtime config: `GEMINI_API_KEY`, `LLM_MODEL`, `CAPSOLVER_API_KEY` (optional).

### Package configs (shipped with ApplyPilot)
- `config/employers.yaml` - Workday employer registry (51 preconfigured)
- `config/greenhouse.yaml` - Greenhouse company board registry (47 preconfigured)
- `config/lever.yaml` - Lever company board registry (16 preconfigured)
- `config/sites.yaml` - Blocked sites, base URLs, manual ATS domains (board scraping disabled)
- `config/searches.example.yaml` - Example search configuration

---

## How Targeting Works

Discovery is targeted by `searches.yaml`, not by your resume or profile.

High-level behavior:

- `Workday` searches each configured query across the built-in employer registry
- `Greenhouse` fetches curated company boards once and filters jobs locally against your query list
- `Lever` fetches curated company boards once and filters jobs locally against your query list
- discovered jobs are deduplicated and then filtered by location rules before later stages

In practice:

- edit `searches.yaml` if you want to change crawl targets
- edit `profile.json` if you want to change scoring/tailoring/application behavior

The most important targeting fields are:

- `queries`
- `locations`
- `defaults.hours_old`

If discovery feels too broad, narrow the query list first. If it feels too narrow, add more query variants before touching the later AI stages.

---

## How Stages Work

### Discover
Scrapes 51 verified Workday employer portals (configurable in `employers.yaml`). Fetches 47 curated Greenhouse company boards via the public Job Board API. Fetches 16 curated Lever company boards via the public Lever Postings API. Deduplicates by URL.

### Enrich
Visits each job URL and extracts the full description. 3-tier cascade: JSON-LD structured data, then CSS selector patterns, then AI-powered extraction for unknown layouts.

### Score
AI scores every job 1-10 against your profile. 9-10 = strong match, 7-8 = good, 5-6 = moderate, 1-4 = skip. Only jobs above your threshold proceed to tailoring.

### Tailor
Generates a custom resume per job: reorders experience, emphasizes relevant skills, incorporates keywords from the job description. Your `resume_facts` (companies, projects, metrics) are preserved exactly. The AI reorganizes but never fabricates.

### Cover Letter
Writes a targeted cover letter per job referencing the specific company, role, and how your experience maps to their requirements.

### Auto-Apply
Claude Code launches a Chrome instance, navigates to each application page, detects the form type, fills personal information and work history, uploads the tailored resume and cover letter, answers screening questions with AI, and submits. A live dashboard shows progress in real-time.

The Playwright MCP server is configured automatically at runtime per worker. No manual MCP setup needed.

```bash
# Utility modes (no Chrome/Claude needed)
applypilot apply --mark-applied URL    # manually mark a job as applied
applypilot apply --mark-failed URL     # manually mark a job as failed
applypilot apply --reset-failed        # reset all failed jobs for retry
applypilot apply --gen --url URL       # generate prompt file for manual debugging
```

---

## Human-In-The-Loop

ApplyPilot supports both autonomous and review-first workflows.

- `applypilot run` is always safe from accidental submission because it never submits
- `applypilot apply --dry-run` fills forms and checks the page, but does not click the final submit button
- `applypilot apply` is autonomous and will submit if the form looks valid

Important limitation:

- there is currently no built-in "pause and ask me before final submit" checkpoint in normal `apply` mode

If you want practical human oversight, the best current workflow is:

1. run `applypilot run`
2. review output in `applypilot dashboard`
3. use `applypilot apply --dry-run --url "JOB_URL"`
4. run `applypilot apply --url "JOB_URL"` once you trust the result

---

## Troubleshooting

### `applypilot run` did not submit any applications

That is expected. `run` prepares jobs and documents only. Submission happens in `apply`.

### Every job scored as `0`

In the current implementation, `0` is the failure sentinel for scoring, not a real "bad fit" score. It usually means:

- the LLM request failed
- the model returned an incompatible format
- the model/provider is not compatible with the current request shape

Check a few rows directly:

```bash
sqlite3 ~/.applypilot/applypilot.db \
"select title, fit_score, substr(score_reasoning,1,300) from jobs where fit_score=0 limit 10;"
```

If you see `LLM error: ...`, fix the provider/model issue first, then clear the bad scores and rescore:

```bash
sqlite3 ~/.applypilot/applypilot.db \
"update jobs
 set fit_score = NULL,
     score_reasoning = NULL,
     scored_at = NULL
 where fit_score = 0;"

applypilot run score
```

### OpenAI `429 Too Many Requests`

That usually means one of:

- requests-per-minute limit
- tokens-per-minute limit
- daily token limit
- quota/billing issue on the API project being used

Check:

- your provider/model in `.env`
- your OpenAI usage and limits page
- whether the key belongs to the paid org/project you expect

### Which OpenAI model should I start with?

For scoring, the safest OpenAI defaults are usually:

- `gpt-4.1-mini`
- `gpt-4o-mini`

Use stronger or more expensive models later for tailoring if needed. If you switch models and want to rescore, clear the old `fit_score` values first as shown above.

---

## CLI Reference

```
applypilot init                         # First-time setup wizard
applypilot doctor                       # Verify setup, diagnose missing requirements
applypilot run [stages...]              # Run pipeline stages (or 'all')
applypilot run --workers 4              # Parallel discovery/enrichment
applypilot run --stream                 # Concurrent stages (streaming mode)
applypilot run --min-score 8            # Override score threshold
applypilot run --dry-run                # Preview without executing
applypilot run --validation lenient     # Relax validation (recommended for Gemini free tier)
applypilot run --validation strict      # Strictest validation (retries on any banned word)
applypilot apply                        # Launch auto-apply
applypilot apply --workers 3            # Parallel browser workers
applypilot apply --dry-run              # Fill forms without submitting
applypilot apply --continuous           # Run forever, polling for new jobs
applypilot apply --headless             # Headless browser mode
applypilot apply --url URL              # Apply to a specific job
applypilot status                       # Pipeline statistics
applypilot dashboard                    # Open HTML results dashboard
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, and PR guidelines.

---

## License

ApplyPilot is licensed under the [GNU Affero General Public License v3.0](LICENSE).

You are free to use, modify, and distribute this software. If you deploy a modified version as a service, you must release your source code under the same license.
