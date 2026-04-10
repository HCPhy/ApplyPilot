# ApplyPilot (Fork)

> This repository started as a fork of the original [Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot).
> It now differs substantially from upstream in discovery scope, safety defaults, dashboard behavior, and recommended workflows.
> If you want the upstream project or the published PyPI package behavior, use upstream directly.
>
> This repo is not affiliated with `applypilot.app`, `useapplypilot.com`, or any other product using the "ApplyPilot" name.

This fork is an opinionated job-search and application pipeline focused on:

- official employer ATS portals only
- curated company registries instead of generic job-board scraping
- safer, review-first application workflows
- a firm-centric dashboard for triage
- practical control over LLM cost

The current pipeline is:

`discover -> enrich -> score -> tailor -> cover -> pdf -> apply`

The most important mental model is:

- `applypilot run` prepares jobs and documents
- `applypilot apply` opens the browser and can submit applications
- `applypilot apply --dry-run` is the safe review mode

## What This Fork Changes

Compared with upstream, this fork currently does all of the following:

- Discovery is limited to official ATS sources: Workday, Greenhouse, Lever, and Avature.
- Generic board crawling like LinkedIn, Indeed, ZipRecruiter, and similar sources is intentionally out of scope.
- The dashboard is firm-centric: you can filter by company, see apply state, and the UI remembers which jobs you clicked.
- Auto-apply can use your base resume with `--use-base-resume`, so tailoring is optional when cost matters.
- Relocation preference is explicit in the profile instead of being hardcoded into the apply prompt.
- Discovery and filtering have been tuned toward curated U.S.-focused searches rather than broad board volume.

If you are maintaining this fork long-term, the README should describe this repo as its own product and stop assuming upstream behavior.

## Install This Fork

Do not rely on:

```bash
pip install applypilot
```

if you want this exact fork. That may install the upstream package instead of the code in this repository.

Use a source install:

```bash
git clone <your-fork-url>
cd ApplyPilot
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
applypilot init
applypilot doctor
```

## Requirements

| Component | Needed For | Notes |
|---|---|---|
| Python 3.11+ | Everything | Core runtime |
| LLM provider | `score`, `tailor`, `cover` | Gemini, OpenAI, or a local OpenAI-compatible endpoint |
| Chrome or Chromium | `apply` | Browser automation target |
| Node.js 18+ | `apply` | Needed so `npx` can launch Playwright MCP |
| Claude Code CLI | `apply` | Auto-apply agent runtime |
| CapSolver API key | Optional | Only for CAPTCHA-heavy application flows |

## Recommended Workflows

### Cheapest Practical Workflow

Use this if you want to minimize token spend:

```bash
applypilot init
applypilot doctor
applypilot run discover enrich score
applypilot dashboard
applypilot apply --use-base-resume --dry-run --limit 1
```

Then, if the dry run looks good:

```bash
applypilot apply --use-base-resume --limit 1
```

### Tailored Resume Workflow

Use this if you want per-job resume customization:

```bash
applypilot run discover enrich score tailor cover pdf
applypilot dashboard
applypilot apply --dry-run --limit 1
```

### Safe First Run

If you are new to this repo, this is the safest path:

```bash
applypilot init
applypilot doctor
applypilot run discover enrich score
applypilot dashboard
applypilot apply --dry-run --url "JOB_URL"
applypilot apply --url "JOB_URL"
```

Recommended safety habits:

- use `--url` to target one job at a time
- keep `--workers 1` while testing
- do not start with `--continuous`
- keep Chrome visible
- prefer `--dry-run` before real submission

## Discovery Perimeter

This fork intentionally discovers jobs only from official employer systems:

- Workday
- Greenhouse
- Lever
- Avature

The exact company universe is defined by curated registries in:

- `src/applypilot/config/employers.yaml`
- `src/applypilot/config/greenhouse.yaml`
- `src/applypilot/config/lever.yaml`
- `src/applypilot/config/avature.yaml`

That means:

- discovery quality depends on those registries
- adding a new firm usually means adding the firm to the correct ATS registry
- the README should avoid hardcoding lots of counts, because the registries change often in this fork

## How Configuration Works

### `~/.applypilot/searches.yaml`

This controls discovery:

- queries
- locations
- recency window
- search tiers

If you want to change what gets crawled, start here.

### `~/.applypilot/profile.json`

This controls downstream behavior:

- personal info
- work authorization
- relocation scope
- compensation
- resume facts
- skills boundaries

This affects scoring, tailoring, cover letters, and auto-apply.

### `~/.applypilot/.env`

This controls runtime credentials and model selection, for example:

- `GEMINI_API_KEY`
- `OPENAI_API_KEY`
- `LLM_MODEL`
- `LLM_URL`
- `CAPSOLVER_API_KEY`

## Pipeline Stages

| Stage | What It Does |
|---|---|
| `discover` | Pulls jobs from curated official ATS registries |
| `enrich` | Fetches full descriptions and apply URLs |
| `score` | Assigns an LLM fit score |
| `tailor` | Generates a resume tailored to the job |
| `cover` | Generates a cover letter |
| `pdf` | Converts generated assets to PDF |
| `apply` | Uses Claude Code plus browser automation to submit applications |

Each stage can be run independently.

Examples:

```bash
applypilot run discover enrich
applypilot run score
applypilot run tailor cover
applypilot run all
```

## Dashboard

The dashboard in this fork is meant for firm-by-firm triage, not just a flat list.

It currently supports:

- firm filtering
- score filtering
- text search
- applied, applying, and failed badges from the DB
- remembered clicked jobs in the browser

Open it with:

```bash
applypilot dashboard
```

Important nuance:

- apply state is persistent because it comes from SQLite
- clicked or viewed state is browser-local because it is stored in `localStorage`

## Auto-Apply Notes

`applypilot apply` uses Claude Code plus a Playwright MCP browser toolchain.

Useful modes:

```bash
applypilot apply --dry-run
applypilot apply --use-base-resume
applypilot apply --url "JOB_URL"
applypilot apply --limit 1
applypilot apply --mark-applied "JOB_URL"
applypilot apply --mark-failed "JOB_URL" --fail-reason "manual"
applypilot apply --reset-failed
```

Important limitations:

- normal `apply` mode is autonomous
- there is no built-in "pause before final submit" approval step
- if you want human review, use `--dry-run` or target one URL at a time

## CLI Quick Reference

```bash
applypilot init
applypilot doctor
applypilot run [stages...]
applypilot run --workers 4
applypilot run --stream
applypilot run --validation normal
applypilot apply
applypilot apply --dry-run
applypilot apply --use-base-resume
applypilot apply --url URL
applypilot status
applypilot dashboard
```

## Troubleshooting

### `applypilot run` did not submit any applications

That is expected. Submission happens in `apply`, not in `run`.

### Every job scored as `0`

In this codebase, `0` is usually a scoring failure sentinel, not a real "bad fit" score.

Check a few rows:

```bash
sqlite3 ~/.applypilot/applypilot.db \
"select title, fit_score, substr(score_reasoning,1,300) from jobs where fit_score=0 limit 10;"
```

If you see `LLM error: ...`, fix the provider or model issue first, then clear and rescore.

### `apply` starts but browser tools do not work

Check both:

- `node --version`
- `npx --version`

`npx` existing without a working `node` runtime is not enough.

### Tailoring is too expensive

Skip it and use:

```bash
applypilot apply --use-base-resume
```

### Discovery feels too broad or too narrow

Edit `searches.yaml` first. Do not start by changing the scoring prompt.

## Reading The Code

If you are trying to understand the repo, start with:

- `README.md`
- `src/applypilot/cli.py`
- `src/applypilot/pipeline.py`
- `src/applypilot/database.py`

Then use the longer walkthrough in [guide.md](guide.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

This repository remains licensed under the [GNU Affero General Public License v3.0](LICENSE).
