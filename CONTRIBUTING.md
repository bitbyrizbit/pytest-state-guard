# Contributing to pytest-state-guard

Thank you for your interest in contributing to `pytest-state-guard`.

`pytest-state-guard` is a specialized, deterministic concurrency auditor for pytest. It instruments `concurrent.futures.ThreadPoolExecutor` task lifetimes and evaluates them against fixture setup and test call boundaries.

---

## Architectural Scope & Discipline

State Guard v1 is engineered with strict, frozen operational boundaries:
- **Supported Executor:** Strictly `concurrent.futures.ThreadPoolExecutor`. Custom thread implementations (`threading.Thread`), multiprocessing, and asyncio are out of scope.
- **Process Scope:** Single-process testing. Multi-process aggregation (`pytest-xdist`) is not provided.
- **Audit, Not Intervention:** State Guard observes and reports; it does not kill threads, terminate tasks, or force executor shutdowns.
- **Frozen Classifications:** The ten policy classifications and their severities are frozen. Feature requests or pull requests proposing scope expansion beyond these boundaries require prior architectural review before implementation.

---

## Development Setup

### 1. Prerequisites
- Python `>= 3.11`
- `pip` or modern Python package manager (e.g. `uv`)

### 2. Clone and Setup Environment
Clone the repository and set up a virtual environment:

```bash
git clone https://github.com/bitbyrizbit/pytest-state-guard.git
cd pytest-state-guard

# Create and activate a virtual environment
python -m venv .venv
# On Linux/macOS:
source .venv/bin/activate
# On Windows (PowerShell):
.venv\Scripts\Activate.ps1
```

### 3. Install in Editable Mode
Install the package and its test dependencies:

```bash
pip install -e .
```

---

## Running Tests

State Guard maintains a comprehensive test suite across unit, lifecycle integration, and adversarial concurrency scenarios.

Run the complete test suite:

```bash
pytest
```

Run specific test suites:

```bash
# Unit tests (ledger, policy engine, models)
pytest tests/unit/

# Integration tests (fixture scopes, test call hooks, packaging)
pytest tests/integration/

# Adversarial concurrency tests (races, submission failures)
pytest tests/adversarial/
```

Before submitting a pull request, ensure all tests pass cleanly in your local environment.

---

## Building Documentation

Documentation is built using MkDocs with the Material theme.

1. Install documentation dependencies from `requirements-docs.txt`:
   ```bash
   pip install -r requirements-docs.txt
   ```
2. Build documentation in strict mode (which fails on any broken links or missing snippets):
   ```bash
   mkdocs build --strict
   ```
3. Preview documentation locally:
   ```bash
   mkdocs serve
   ```

---

## Pull Request Guidelines

1. **Target Branch:** All contributions must target the default branch (`feat/state-guard-v1`).
2. **Test Coverage:** Any bug fix or behavioral change must include corresponding unit, integration, or adversarial tests demonstrating the fix.
3. **Core Distinctions:** When modifying or testing policy behavior, preserve the core distinctions:
   - An observed physical event, committed ledger record, and policy finding are distinct concepts.
   - Callable execution and Future settlement are separate lifecycle dimensions.
   - Fixture teardown execution is not an owner scope.
4. **Documentation:** Update documentation if your contribution impacts user-facing behavior. Verify that `mkdocs build --strict` succeeds without warnings.
