# Source Code Reading Guide

This guide is meant to help you understand ApplyPilot without reading the repo in random order.

## Start With The Mental Model

ApplyPilot is a command-line pipeline with six main stages:

1. `discover` - find jobs from boards and employer sites
2. `enrich` - fetch full descriptions and apply links
3. `score` - score jobs against the user's profile
4. `tailor` - generate a tailored resume per job
5. `cover` - generate a cover letter per job
6. `apply` - drive the browser to submit applications

The easiest way to read the code is to follow the same order.

## Best Reading Order

### 1. Entry Points

Read these first to understand how the app starts and what commands exist:

- `README.md`
- `src/applypilot/cli.py`
- `src/applypilot/__main__.py`

What to look for:

- Which commands users can run
- Which options are available
- Where each command hands off control

### 2. Shared Foundations

Next, read the files that define where data lives and how state is stored:

- `src/applypilot/config.py`
- `src/applypilot/database.py`
- `src/applypilot/llm.py`

What to look for:

- `~/.applypilot` paths and generated files
- the difference between `searches.yaml` and `profile.json`
- SQLite schema and job lifecycle fields
- How the LLM provider is chosen

Useful mental split:

- `searches.yaml` controls discovery targets
- `profile.json` controls scoring, tailoring, and application behavior

## 3. Pipeline Control

Read:

- `src/applypilot/pipeline.py`

This is the best "map of the system" file in the repo.

What to look for:

- Stage order
- Which modules implement each stage
- Which stages are LLM-based and which are not

## 4. Discovery And Enrichment

Once you know the pipeline shape, read the crawling code:

- `src/applypilot/discovery/workday.py`
- `src/applypilot/discovery/greenhouse.py`
- `src/applypilot/discovery/lever.py`
- `src/applypilot/enrichment/detail.py`

Recommended order:

1. `workday.py` for direct Workday scraping
2. `greenhouse.py` and `lever.py` for direct company-board APIs
3. `detail.py` for full-description enrichment

What to look for:

- Input search config
- Which parts of `init` affect crawling and which do not
- How jobs are normalized before storage
- When LLM calls are avoided and when they are used as fallback

## 5. Scoring And Document Generation

Then read the AI-heavy stage files:

- `src/applypilot/scoring/scorer.py`
- `src/applypilot/scoring/tailor.py`
- `src/applypilot/scoring/validator.py`
- `src/applypilot/scoring/cover_letter.py`
- `src/applypilot/scoring/pdf.py`

What to look for:

- Prompt construction
- Retry and validation logic
- How generated text becomes saved output files

If you are worried about hallucinations or bad automation decisions, `tailor.py` and `validator.py` are especially important.

## 6. Auto-Apply

Read these last, after you already understand the data flow:

- `src/applypilot/apply/launcher.py`
- `src/applypilot/apply/prompt.py`
- `src/applypilot/apply/chrome.py`
- `src/applypilot/apply/dashboard.py`

Recommended order:

1. `launcher.py` to see the orchestration loop
2. `prompt.py` to see what the browser agent is told to do
3. `chrome.py` to see browser/session management
4. `dashboard.py` if you want the live progress view

What to look for:

- How a job is selected from the DB
- How Chrome and Claude are launched
- Whether the agent is autonomous or human-in-the-loop
- Which failure states are permanent

## 7. Setup And UX

These are helpful once the core flow makes sense:

- `src/applypilot/wizard/init.py`
- `src/applypilot/view.py`

They explain:

- How first-run setup writes `profile.json`, `searches.yaml`, and `.env`
- How status and dashboard output are presented

## A Good Way To Read The Code

Do three passes instead of trying to understand everything at once.

### Pass 1: Follow One Command

Trace one command end-to-end:

- `applypilot run discover`
- or `applypilot apply --dry-run --url ...`

Only answer:

- Where does execution start?
- Which function gets called next?
- What files or DB rows get written?

### Pass 2: Track Data

Now follow the main job record through the database:

- discovered
- enriched
- scored
- tailored
- cover letter generated
- applied or failed

The `jobs` table in `database.py` is the source of truth for this.

### Pass 3: Inspect Risky Areas

Once the happy path is clear, focus on the parts with the most risk:

- LLM prompts and validators
- login handling
- CAPTCHA logic
- final submit behavior
- retries and permanent failure classification

## Questions To Keep Asking

For each file, ask:

1. What are the inputs?
2. What are the outputs?
3. What state changes happen?
4. What code calls this file?
5. What code does this file call next?

That usually gives you enough structure to understand the module without reading every line deeply.

## If You Only Read Five Files

If you want the shortest useful reading list, read:

1. `src/applypilot/cli.py`
2. `src/applypilot/pipeline.py`
3. `src/applypilot/database.py`
4. `src/applypilot/enrichment/detail.py`
5. `src/applypilot/apply/launcher.py`

Those five files give you the command surface, the pipeline map, the shared state model, the enrichment behavior, and the auto-apply control loop.

## Suggested First Exercise

A practical first exercise is:

1. Read `cli.py`
2. Read `pipeline.py`
3. Open `database.py`
4. Trace what happens during `applypilot run discover`
5. Then trace what happens during `applypilot apply --dry-run`

That gives you both the preparation side and the browser automation side without needing to understand every helper immediately.
