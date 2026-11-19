# Getting Started

This guide explains how to install `pytest-state-guard`, run it against your test suite, interpret diagnostic output, and temporarily deactivate it when necessary.

---

## Requirements

Before installing, ensure your environment meets the package requirements declared in `pyproject.toml`:

- **Python:** `>= 3.11`
- **Pytest:** `>= 9.1.1` (direct runtime dependency)

---

## Installation

Install `pytest-state-guard` into your testing environment from PyPI:

```bash
pip install pytest-state-guard
```

### Automatic Plugin Discovery

State Guard registers itself via pytest's standard `pytest11` entry point:

```toml
[project.entry-points.pytest11]
state_guard = "pytest_state_guard.plugin"
```

Once installed, State Guard is automatically discovered by pytest on every test run. No edits to `conftest.py`, no command-line flags, and no fixture imports are required.

Run your test suite normally:

```bash
pytest
```

---

## Minimal Usage Example

### Demonstrating a Task Lifetime Violation

Consider a test that submits background work to a `ThreadPoolExecutor` and finishes its assertions while the background worker is still running:

```python
# test_leak_demo.py
import threading
from concurrent.futures import ThreadPoolExecutor

started = threading.Event()
release_worker = threading.Event()

def background_task():
    started.set()
    # Simulate work extending past the test boundary
    release_worker.wait(timeout=2.0)

def test_unjoined_worker():
    executor = ThreadPoolExecutor(max_workers=1)
    executor.submit(background_task)

    # Synchronize to confirm the worker callable has started execution
    assert started.wait(timeout=1.0)

    # Call-phase assertions pass, but the worker is not joined
    assert True
```

Run pytest on this test:

```bash
pytest test_leak_demo.py
```

### Representative Terminal Output

State Guard allows the call phase to pass its assertions, but flags the unjoined task during test teardown under `=== ERRORS ===`:

```text
============================= test session starts =============================
platform ... -- Python 3.x, pytest-9.x, pluggy-1.x
plugins: state-guard-0.1.1
collected 1 item

test_leak_demo.py::test_unjoined_worker PASSED                           [100%]
test_leak_demo.py::test_unjoined_worker ERROR                            [100%]

=================================== ERRORS ====================================
__________________ ERROR at teardown of test_unjoined_worker __________________

- State Guard Teardown Violation: TEST_CALL [test_leak_demo.py::test_unjoined_worker] -
Owner ID: owner:test:test_leak_demo.py::test_unjoined_worker:<uuid>
Owner Finalized Sequence: 7 (created at seq 2)

  * Finding: finding:owner:test:test_leak_demo.py::test_unjoined_worker:<uuid>:task:<task_uuid>
    Classification: CALLABLE_NOT_EXITED
    Task ID:        task:<task_uuid>
    Evidence:       Task 'task:<task_uuid>' callable began execution at seq 4 before finalization, but has not committed an exit event.
    Sequences:      3, 4, 7

=================== State Guard Audit: VIOLATIONS DETECTED ====================
Owner: TEST_CALL [test_leak_demo.py::test_unjoined_worker]
  - [CALLABLE_NOT_EXITED] Task 'task:<task_uuid>': Task 'task:<task_uuid>' callable began execution at seq 4 before finalization, but has not committed an exit event.
    Sequences: 3, 4, 7

=========================== short test summary info ===========================
ERROR test_leak_demo.py::test_unjoined_worker
========================= 1 passed, 1 error in 0.80s ==========================
```

The test run fails with exit code `1` (`pytest.ExitCode.TESTS_FAILED`), alerting you to the leaking thread without masking test assertion results.

---

## Fixing the Violation

To resolve the violation, ensure that all submitted work completes before the owner scope finalizes. The standard pattern is wrapping the executor in a context manager or explicitly waiting for the `Future`:

```python
def test_unjoined_worker_fixed():
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: 42)
        assert future.result() == 42
    # Context manager exit invokes executor.shutdown(wait=True),
    # joining the worker thread before test teardown completes.
```

---

## Deactivating State Guard

If you need to temporarily deactivate State Guard for a specific test run without uninstalling the package, pass pytest's standard plugin deactivation flag:

```bash
pytest -p no:state_guard
```

When deactivated, State Guard's hooks and `ThreadPoolExecutor.submit` monkeypatch are not installed, and tests run with standard pytest behavior.

---

## Next Steps

- [Test & Fixture Usage Guide](user-guide/test-and-fixture-usage.md): Learn how task ownership works and how to structure concurrent tests safely.
- [Classifications & Severities Reference](user-guide/classifications.md): Explore all ten frozen diagnostic classifications and what they mean.
- [Troubleshooting Guide](user-guide/troubleshooting.md): Step-by-step diagnostic workflows for resolving concurrency violations.
- Return to [Home](index.md).
