# pytest-state-guard

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

A pytest plugin auditing `ThreadPoolExecutor` task lifetimes against fixture and test owner boundaries.

---

## The test passed. The work didn't stop.

Pytest evaluates assertions, executes fixture teardowns, and moves smoothly to the next test in the suite. But if a test function or fixture submitted a callable to a `concurrent.futures.ThreadPoolExecutor` and did not explicitly wait on `Future.result()` or shut down the executor with `wait=True`, the submitted work may remain queued or actively executing past the owner's boundary.

Depending on operating system scheduling, the submitted callable may have completed unnoticed, may still be queued waiting for a worker, or may be actively mutating process-level state while a subsequent test assumes a clean environment.

Consider an illustrative scenario: a test submits background work and exits cleanly in mere milliseconds. Several tests later, an intermittent flake occurs because an orphaned thread from that earlier test was still executing and wrote into shared memory.

`pytest-state-guard` identifies this lifecycle mismatch. It instruments `ThreadPoolExecutor.submit` to capture task provenance, records observed lifecycle transitions into an append-only event ledger with monotonic sequence numbers, and evaluates task lifetimes against owner finalization boundaries using a formal policy engine.

---

## What It Does — and What It Does Not Do

### What State Guard Does
- **Instruments Task Submission:** Intercepts `ThreadPoolExecutor.submit` dynamically during the pytest session to bind task provenance to the active owner context.
- **Enforces Monotonic Ordering:** Commits every observed, tracked submission, start, exit, and finalization event to a thread-safe, append-only in-memory ledger.
- **Classifies Against Boundaries:** Evaluates whether submitted callables completed before their submitter was finalized, remained outstanding, or started execution after owner teardown.
- **Surfaces Failures Without Masking:** Reports violations in test teardowns (`=== ERRORS ===`) and the session summary without overwriting original call assertions (`=== FAILURES ===`) or swallowing upstream exceptions.

### What State Guard Deliberately Does Not Do
- **It does not terminate threads:** State Guard is an auditor, not an execution sandbox. It will not forcibly kill threads, cancel running callables, or shut down executors.
- **It does not isolate memory or prevent races:** It does not intercept memory writes or prevent data races in shared state. It does not detect every concurrency defect.
- **It does not instrument other concurrency primitives:** Raw `threading.Thread`, `multiprocessing`, `asyncio` loops, and third-party thread pools are out of scope.
- **Physical scheduling is non-deterministic; auditing is not:** State Guard does not claim that physical thread scheduling is deterministic. It guarantees that ledger event commitment, boundary snapshots, and policy classifications are strictly deterministic for any observed event sequence.

---

## Task Provenance: The Submitter Owns the Task

In concurrent systems, an executor is merely an execution mechanism—often a shared, long-lived resource configured across multiple tests or fixtures. The entity responsible for the existence and lifetime of a task is the submitter.

State Guard establishes ownership through two primary scopes:

1. **Test Call Owners:** Bound to the test item during its `pytest_runtest_call` phase and finalized by the `pytest_runtest_teardown` hookwrapper after teardown processing (including fixture teardown finalizers).
2. **Fixture Owners:** Bound to fixture invocations during `pytest_fixture_setup` across all fixture scopes (`function`, `class`, `module`, `package`, and `session`), and finalized through `pytest_fixture_post_finalizer` after that fixture's teardown logic has completed. State Guard does not track arbitrary teardown code beyond this hook-defined boundary.

Ownership is propagated via context-local storage (`contextvars.ContextVar`). When a test or fixture calls `executor.submit(fn, *args, **kwargs)`, State Guard captures the active owner ID, issues a UUID for the task, and commits a submission reservation event before delegating to the real executor.

---

## The Event Ledger & Committed Ordering

Physical time is unreliable for concurrent auditing. State Guard relies on monotonic event sequence IDs assigned under an atomic lock. All sequence identifiers ($S_*$) represent **committed ledger sequence numbers**, not physical timestamps or hardware clock readings. State Guard establishes the deterministic order in which observations were committed, not the exact physical instants a thread started or completed.

The ledger records the lifecycle evidence State Guard observes:

- **Owner Finalization Boundary ($S_F$):** The sequence ID committed at the owner's teardown boundary (in `pytest_runtest_teardown` for test items, or `pytest_fixture_post_finalizer` for fixtures).
- **Submission Window ($S_{inv} \to S_{ret}$ / $S_{raise}$):** $S_{inv}$ is the submission-reservation sequence ID committed before delegating to the real executor; $S_{ret}$ or $S_{raise}$ is committed when `submit()` returns a `Future` or raises an exception.
- **Callable Lifecycle ($S_{start} \to S_{exit}$):** Committed when the wrapper begins executing the user callable and when it exits (by return or exception).
- **Future Observations ($S_{done}$ / $S_{cancel}$):** Committed when done-callbacks or cancellation were observed (noting that callback observation is not the exact physical settlement instant).

Policy evaluation compares observed evidence against the committed finalization boundary $S_F$.

---

## Policy Classifications & Severity

Every task tracked by State Guard is classified according to its committed sequence history:

| Classification | Severity | Semantic Description |
|---|---|---|
| `NO_OUTSTANDING_TASKS` | `CLEAN` | No task remains outstanding at the owner finalization boundary (tasks may have completed or been cancelled prior to finalization). |
| `COMPLETED_BEFORE_FINALIZATION` | `CLEAN` | Callable started and exited before owner finalization ($S_{exit} < S_F$). |
| `CANCELLED_BEFORE_START` | `CLEAN` | Future cancellation committed before finalization and before callable started. |
| `OUTSTANDING_TASK` | `VIOLATION` | Submission returned before finalization, but callable never started ($S_{ret} < S_F$). |
| `CALLABLE_NOT_EXITED` | `VIOLATION` | Callable started before finalization but did not exit before finalization. |
| `EXECUTION_AFTER_OWNER_FINALIZATION` | `VIOLATION` | Callable began execution after owner finalization ($S_{start} > S_F$). |
| `EXIT_COMMITTED_AFTER_FINALIZATION` | `VIOLATION` | Callable started before finalization but exited after finalization ($S_{exit} > S_F$). |
| `POST_LIFETIME_SUBMISSION` | `VIOLATION` | Task submitted after owner was already finalized ($S_{inv} > S_F$). |
| `SUBMISSION_UNRESOLVED` | `UNCERTAIN` | `submit()` did not return or raise before finalization. |
| `SUBMISSION_FAILED_UNVERIFIED` | `UNCERTAIN` | `submit()` raised before finalization; work item enqueue state unverified. |

### Preserving Boundary Evidence
- **Uncertainty is Advisory:** Submissions that were in flight when an owner finalized remain `SUBMISSION_UNRESOLVED` even if `submit()` subsequently returns. Uncertain findings are advisory: they are rendered in an advisory section of the terminal summary and do not automatically cause test failures.
- **Finding Identity Stability:** If an unresolved task subsequently begins execution after finalization, its classification evolves to `EXECUTION_AFTER_OWNER_FINALIZATION` while retaining its stable finding identifier.

---

## Reporting Architecture

### 1. Per-Test Teardown Errors
Findings originating from test functions or function-scoped fixtures are attributed directly to that test item:
- **Test Passed:** Marked failed in teardown, appearing under `=== ERRORS ===` as `ERROR at teardown of <nodeid>`.
- **Test Failed:** If the test call itself failed (for example, an assertion failure), the call failure remains visible under `=== FAILURES ===` with its complete traceback, while concurrency violations appear under `=== ERRORS ===`.
- **Teardown Already Failed:** If the test teardown already failed (for example, an exception in a fixture finalizer), State Guard preserves the original teardown error representation and chains its violation details using a chained terminal representation (`ChainedTerminalRepr`) without overwriting the fixture error.

### 2. Session Audit Summary
Class, module, package, and session-scoped fixtures cannot be attributed to an arbitrary single test. Their findings are audited at session finish and rendered in the terminal summary by `pytest_terminal_summary`.

### 3. Exit Code Promotion
If all test assertions passed but State Guard detected concurrency violations, the session exit code is promoted to `pytest.ExitCode.TESTS_FAILED` (`1`). Pre-existing nonzero exit codes (such as assertion failures or interrupts) are preserved.

---

## Usage Example

### An Illustrative Leaky Test
The following test submits work to a `ThreadPoolExecutor` and uses synchronization to guarantee that the worker callable has started before the test call finishes. In the illustrated execution, this demonstrates a worker remaining active across the owner finalization boundary.

*(Note: The bounded timeout on `release.wait(timeout=2.0)` ensures that worker threads safely terminate without hanging the test process, but it also means that under arbitrary operating system scheduling—such as thread suspension—this specific outcome is not guaranteed. This example deliberately demonstrates an unjoined background worker to illustrate State Guard's detection; it is an anti-pattern for production test suites.)*

```python
import threading
from concurrent.futures import ThreadPoolExecutor

started = threading.Event()
release = threading.Event()

def worker():
    started.set()
    # Bounded wait allows the thread to safely terminate if release is never set:
    release.wait(timeout=2.0)

def test_background_worker():
    executor = ThreadPoolExecutor(max_workers=1)
    executor.submit(worker)

    # Synchronize: guarantee the worker callable has started execution
    assert started.wait(timeout=1.0), "Worker failed to start"

    # The test passes assertions, but the worker is still active:
    assert True
```

### Pytest Output (Illustrative)
Because physical thread scheduling is non-deterministic, the sample output below represents one possible illustrative execution. If scheduling delays the test thread until the worker timeout expires, an alternate classification (such as `COMPLETED_BEFORE_FINALIZATION`) could be observed instead. Total process duration may reflect background workers concluding before process exit.

```text
==================================== ERRORS ====================================
_______________________ ERROR at teardown of test_background_worker _______________________
---------------- State Guard Teardown Violation: TEST_CALL [test_demo.py::test_background_worker] ----------------
Owner ID: 3b9a1c4e-6e8d-4f1a-b2c3-9d8e7f6a5b4c
Owner Finalized Sequence: 42 (created at seq 38)

  * Finding: f-3b9a1c4e-001
    Classification: CALLABLE_NOT_EXITED
    Task ID:        t-8a7b6c5d-1122-3344
    Evidence:       Callable started at seq 40 but did not exit before owner finalization (seq 42).
    Sequences:      40, 42

=========================== short test summary info ============================
ERROR test_demo.py::test_background_worker - State Guard Teardown Violation: TEST_CALL
============================== 1 error in 0.12s ===============================
```

### The Fix
To prevent task leakage, ensure executors are joined or futures are waited on within the owner's scope:

```python
def test_background_worker_fixed():
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: 42)
        assert future.result() == 42
    # Context manager exit shuts down the executor and joins worker threads
```

---

## Installation & Plugin Discovery

During development and prior to PyPI release, install State Guard from your local repository root or from a pre-built wheel:

```bash
# Install from local repository root:
pip install .

# Or install from a previously built wheel:
pip install dist/pytest_state_guard-0.1.0-py3-none-any.whl
```

Once installed, State Guard is automatically discovered by pytest via its standard `pytest11` entry point (`state_guard = "pytest_state_guard.plugin"`).

### Disabling the Plugin
To temporarily deactivate State Guard for a specific test run without uninstalling it:

```bash
pytest -p no:state_guard
```

---

## Compatibility & Verification

- **Python Requirements:** `>= 3.11`
  - *Verified Environments:* Python 3.11.9, Python 3.14.0
- **Pytest Requirements:** `>= 9.1.1`
  - *Verified Environments:* pytest 9.1.1, pytest 9.2.0
- **Pluggy Requirements:** `>= 1.6.0`
  - *Verified Environments:* pluggy 1.6.0, pluggy 1.7.0

---

## Development & Test Suite

To run the complete test suite across adversarial, unit, and lifecycle integration tests:

**PowerShell:**
```powershell
$env:PYTHONPATH = "src"
pytest
```

**Command Prompt:**
```cmd
set PYTHONPATH=src
pytest
```

To build a standalone distribution wheel:

```bash
pip wheel . --no-deps -w dist/
```

---

## Scope & Limitations

- **ThreadPoolExecutor Exclusivity:** Only `concurrent.futures.ThreadPoolExecutor` is instrumented. Custom worker implementations relying on `threading.Thread` directly are uninstrumented.
- **Process Boundaries:** Each pytest process maintains its own independent in-memory ledger. Cross-process ledger aggregation and distributed ownership correlation (such as across `pytest-xdist` worker processes) are not provided.
- **Audit, Not Intervention:** State Guard reports concurrency boundary violations; it does not alter thread execution, terminate running threads, or enforce shutdown.

---

## License

This project is licensed under the [MIT License](LICENSE).

Copyright (c) 2026 bitbyrizbit
