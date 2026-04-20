# Contributing to ApplyPilot

Thank you for your interest in contributing to ApplyPilot. This guide covers everything you need to get started.

## Development Setup

### Prerequisites

- Python 3.11 or higher
- Git

### Clone and Install

```bash
git clone https://github.com/Pickle-Pixel/ApplyPilot.git
cd ApplyPilot
pip install -e ".[dev]"
playwright install chromium
```

This installs ApplyPilot in editable mode with all development dependencies (pytest, ruff, etc.) and downloads the Chromium browser binary for Playwright.

### Verify Installation

```bash
applypilot --version
pytest tests/ -v
ruff check src/
```

## How to Contribute

### Adding New Workday Employers

Workday employer portals are configured in `config/employers.yaml`. To add a new employer:

1. Find the company's Workday career portal URL (usually `https://company.wd5.myworkdaysite.com/`)
2. Identify the Workday instance number (wd1, wd3, wd5, etc.) and the tenant ID
3. Add an entry to `config/employers.yaml`:

```yaml
- name: "Company Name"
  tenant: "company_tenant_id"
  instance: "wd5"
  url: "https://company.wd5.myworkdaysite.com/en-US/recruiting"
```

4. Test discovery: `applypilot discover --employer "Company Name"`
5. Submit a PR with the new entry

### Adding New Official Employer Sources

This repo now targets official employer/application portals only. Prefer adding firms to:

1. `config/employers.yaml` for Workday
2. `config/greenhouse.yaml` for Greenhouse
3. `config/lever.yaml` for Lever
4. `config/avature.yaml` for Avature
5. `config/ashby.yaml` for Ashby
6. `config/recruitee.yaml` for Recruitee
7. `config/workable.yaml` for Workable
8. `config/smartrecruiters.yaml` for SmartRecruiters
9. `config/major_tech.yaml` plus a firm-specific adapter for custom systems like Amazon, Apple, Google, or Uber
10. `config/academicjobsonline.yaml` for AcademicJobsOnline discipline/application pages

Please do not add third-party job boards or aggregators such as LinkedIn, Indeed, ZipRecruiter, Dice, Otta, or similar sites.

### Bug Fixes and Features

1. Check existing [issues](https://github.com/Pickle-Pixel/ApplyPilot/issues) to avoid duplicating work
2. For new features, open an issue first to discuss the approach
3. Fork the repo and create a feature branch from `main`
4. Write your code with type hints and docstrings
5. Add tests for new functionality
6. Update the CHANGELOG.md under an `[Unreleased]` section
7. Submit a PR

## Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run a specific test file
pytest tests/test_scoring.py -v

# Run with coverage
pytest tests/ --cov=src/applypilot --cov-report=term-missing
```

## Linting and Code Style

ApplyPilot uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting.

```bash
# Check for issues
ruff check src/

# Auto-fix what can be fixed
ruff check src/ --fix

# Format code
ruff format src/
```

### Code Style Guidelines

- **Type hints**: All function signatures must have type annotations
- **Docstrings**: All public functions and classes must have docstrings (Google style)
- **Naming**: snake_case for functions and variables, PascalCase for classes
- **Imports**: Sorted by Ruff (isort-compatible)
- **Line length**: 100 characters maximum

## PR Guidelines

- **One feature per PR.** Keep changes focused and reviewable.
- **Include tests.** New features need test coverage. Bug fixes need a regression test.
- **Update CHANGELOG.md.** Add your changes under `[Unreleased]`.
- **Write a clear PR description.** Explain what changed and why.
- **Keep commits clean.** Squash fixup commits before requesting review.
- **CI must pass.** All linting and tests must be green.

## Project Structure

```
ApplyPilot/
├── src/applypilot/       # Main package
│   ├── __init__.py
│   ├── cli.py            # CLI entry points
│   ├── discover/         # Stage 1: job discovery scrapers
│   ├── enrich/           # Stage 2: description extraction
│   ├── score/            # Stage 3: AI scoring
│   ├── tailor/           # Stage 4: resume tailoring
│   ├── cover/            # Stage 5: cover letter generation
│   ├── apply/            # Stage 6: browser automation
│   └── utils/            # Shared utilities
├── config/               # Default configuration files
├── tests/                # Test suite
├── docs/                 # Documentation
└── pyproject.toml        # Package configuration
```

## License

By contributing to ApplyPilot, you agree that your contributions will be licensed under the [GNU Affero General Public License v3.0](LICENSE).
