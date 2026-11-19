# Development Guide

This guide provides setup and workflow instructions for contributors developing, testing, and building `pytest-state-guard`.

---

## Prerequisites

Before beginning development, verify that your local environment satisfies the runtime requirements declared in `pyproject.toml`:

- **Python:** `>= 3.11`
- **Pytest:** `>= 9.1.1` (installed via editable package install)
- **Git**

---

## Setting Up a Local Development Environment

### 1. Clone the Repository
Clone `pytest-state-guard` from GitHub:

```bash
git clone https://github.com/bitbyrizbit/pytest-state-guard.git
cd pytest-state-guard
```

### 2. Create and Activate a Virtual Environment
Create an isolated virtual environment:

```bash
# Using standard venv:
python -m venv .venv

# On Linux/macOS:
source .venv/bin/activate

# On Windows (PowerShell):
.venv\Scripts\Activate.ps1

# On Windows (cmd.exe):
.venv\Scripts\activate.bat
```

### 3. Install in Editable Mode
Install the package in editable mode so changes in `src/` are immediately reflected:

```bash
pip install -e .
```

---

## Running the Test Suite

State Guard uses standard `pytest` for all test execution.

### Complete Test Suite
Run the full test suite across unit, lifecycle integration, and adversarial tests:

```bash
pytest
```

### Focused Test Execution
Run specific sub-suites targeting individual architectural components:

```bash
# Run unit tests (models, ledger, policy engine, owner lifecycle)
pytest tests/unit/

# Run integration tests (fixture scopes, test item ownership, reporting)
pytest tests/integration/

# Run adversarial tests (thread races, callback races, submission failures)
pytest tests/adversarial/
```

### Running with Verbose Output
To inspect individual test names and outcomes:

```bash
pytest -v
```

---

## Building Documentation

Documentation is authored in Markdown and built using MkDocs with the Material for MkDocs theme.

### 1. Install Documentation Dependencies
Install the pinned documentation dependencies:

```bash
pip install -r requirements-docs.txt
```

### 2. Build Documentation (Strict Mode)
Build the static site locally. Strict mode ensures that all links resolve and snippet transclusions succeed without warnings:

```bash
mkdocs build --strict
```

### 3. Local Live Preview
Start a local development server with live reloading:

```bash
mkdocs serve
```
Preview the site in your browser at `http://127.0.0.1:8000/`.

---

## Building Distribution Packages

To build standalone source distributions (`.tar.gz`) and binary wheels (`.whl`) using standard Python packaging tools:

1. Install `build`:
   ```bash
   pip install build
   ```
2. Build the distribution assets:
   ```bash
   python -m build
   ```
Generated artifacts are output to the `dist/` directory:
- `dist/pytest_state_guard-<version>-py3-none-any.whl`
- `dist/pytest_state_guard-<version>.tar.gz`

---

## Repository Structure

```text
pytest-state-guard/
├── .github/workflows/         # CI and release GitHub Actions workflows
├── docs/                      # Documentation source files (Markdown)
│   ├── architecture/          # Architecture specifications
│   └── user-guide/            # User-facing guides and references
├── src/pytest_state_guard/    # Package runtime implementation
│   ├── executor.py            # ThreadPoolExecutor submission monkeypatch
│   ├── ledger.py              # Serialized append-only event ledger
│   ├── models.py              # Domain records, enums, dataclasses
│   ├── owners.py              # ContextVar ownership management
│   ├── plugin.py              # Pytest plugin hooks and reporting
│   └── policy.py              # Pure deterministic policy engine
├── tests/                     # Test suite
│   ├── adversarial/           # Concurrency races and failure paths
│   ├── integration/           # Pytest hook and scope integration tests
│   └── unit/                  # Isolated ledger and policy unit tests
├── CHANGELOG.md               # Authoritative project release history
├── CONTRIBUTING.md            # Contributor guidelines
├── LICENSE                    # MIT License
├── mkdocs.yml                 # Documentation site configuration
├── pyproject.toml             # Package metadata and entry points
├── README.md                  # Project landing page
└── requirements-docs.txt      # Pinned documentation dependencies
```
