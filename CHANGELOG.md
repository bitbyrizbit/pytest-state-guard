# Changelog

All notable changes to pytest-state-guard will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Fixture Teardown Lifecycle Tracking (Stage 15B):**
  - Added `OwnerType.FIXTURE_TEARDOWN` for distinct ownership tracking during fixture finalizer and post-yield execution.
  - Implemented dynamic `FixtureDef.finish` dispatcher (`_state_guard_fixture_finish`) with structured 5-phase lifecycle: entry registration, instance resolution and depth tracking, owner activation, teardown execution, and deterministic cleanup.
  - Added safe `ContextVar` context restoration across nested and sequential fixture finish invocations.
  - Added detection of non-delegating plugin replacements (SCEN-10) via `bypassed_fixtures` registry, promoting session exit status to `pytest.ExitCode.TESTS_FAILED` during session finish audit.
  - Added deterministic, aggregated incomplete audit diagnostics in session finish reporting.
  - Introduced core fixture teardown test suite (34 tests in `tests/integration/test_fixture_teardown.py`) covering standard scopes, cyclic dependencies, fault injections, and subprocess isolation.
  - Introduced adversarial fixture teardown test suite (35 tests in `tests/integration/test_fixture_teardown_adversarial.py`) validating dispatcher races, ContextVar isolation, premature sentinel consumption, and multi-definition counting.
  - Integrated documentation favicon assets across standard and high-DPI resolutions.

### Fixed
- Fixed undefined `SessionState` type annotation on `_evaluate_and_cleanup_session_finish` in `src/pytest_state_guard/plugin.py`, correcting runtime reflection (`typing.get_type_hints()`).
- Reconciled architecture and user guide documentation to accurately reflect `OwnerType.FIXTURE_TEARDOWN` and `TEST_CALL` finalization in `pytest_runtest_teardown`.

## [0.1.1] - 2026-11-18

### Changed
- Bumped package version to `0.1.1` in `src/pytest_state_guard/__init__.py` (commit `b3af47c`).
- Updated representative terminal output in `README.md` to display `plugins: state-guard-0.1.1` (commit `b3af47c`).
- Enhanced documentation landing page (`README.md`) with PyPI badges, quickstart installation guide, OS-scheduling caveats in usage examples, link to the multi-OS cross-platform CI verification matrix, and modern `python -m build` instructions (commit `adaf3c0`).

## [0.1.0] - 2026-11-17

### Added
- Initial public release of `pytest-state-guard`.
- Deterministic concurrency auditing for `concurrent.futures.ThreadPoolExecutor` task lifetimes.
- Fixture setup (`FIXTURE_SETUP`) and test call (`TEST_CALL`) ownership tracking via context variables and pytest hook wrappers.
- Central serialized in-memory `Ledger` capturing append-only lifecycle event records under non-reentrant locking.
- Pure policy engine evaluating committed evidence against owner boundaries across 10 frozen classifications.
- Zero-configuration automatic pytest plugin registration via `pytest11` entry point.
- Plugin opt-out support via standard pytest flag `-p no:state_guard`.
- Comprehensive test suite covering unit, lifecycle integration, and adversarial concurrency scenarios.
- Multi-OS CI pipeline covering Ubuntu, Windows, and macOS across Python 3.11, 3.12, 3.13, and 3.14.
