# Troubleshooting Concurrency Violations

This guide provides actionable diagnostic workflows for investigating and resolving concurrency violations reported by `pytest-state-guard`.

---

## Troubleshooting Workflows by Classification

### Scenario 1: `CALLABLE_NOT_EXITED` or `EXIT_COMMITTED_AFTER_FINALIZATION`

#### Observable Symptom
Pytest reports `ERROR at teardown of <test_name>` with:
```text
Classification: CALLABLE_NOT_EXITED
Evidence:       Task 'task:<uuid>' callable began execution at seq 4 before finalization, but has not committed an exit event.
```
or:
```text
Classification: EXIT_COMMITTED_AFTER_FINALIZATION
Evidence:       Task 'task:<uuid>' callable began at seq 4 before finalization, but exit was committed at seq 8 after finalization at seq 6.
```

#### What the Evidence Establishes
- The worker thread began executing the wrapped user callable before the owner reached its finalization boundary ($S_{\text{start}} < S_F$).
- Either the callable had not finished when the owner finalized (`CALLABLE_NOT_EXITED`), or its exit event committed to the ledger after finalization concluded (`EXIT_COMMITTED_AFTER_FINALIZATION`).

#### What the Evidence Does Not Establish
- It does not prove that the task was intentionally left running indefinitely; the worker may have finished merely milliseconds after teardown.
- It does not mean the executor is broken; the executor functioned normally, but the test failed to synchronize with its worker before returning.

#### Recommended Investigation
1. Search the test function or fixture for calls to `executor.submit()`.
2. Check whether the returned `Future` was awaited using `.result()`, or whether `executor.shutdown(wait=True)` was called.
3. Check for infinite loops or blocking I/O calls (network, file locks, un-triggered events) inside the submitted worker callable.

#### Corrective Pattern
Ensure the test explicitly joins or awaits the submitted callable within the test body:

```python
# Before (Violates boundary):
def test_async_fetch():
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fetch_data)
    # Exits without awaiting future; fetch_data continues running across teardown

# After (Clean):
def test_async_fetch():
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fetch_data)
        data = future.result(timeout=5.0)
        assert data is not None
```

---

### Scenario 2: `OUTSTANDING_TASK`

#### Observable Symptom
State Guard reports a teardown error with:
```text
Classification: OUTSTANDING_TASK
Evidence:       Task 'task:<uuid>' was accepted for execution before finalization at seq 3 but callable has not started.
```

#### What the Evidence Establishes
- `ThreadPoolExecutor.submit()` returned successfully before finalization ($S_{\text{ret}} < S_F$).
- No worker thread had begun executing the callable at the time owner finalization occurred ($S_{\text{start}} = \emptyset$).
- The work item remained queued inside the executor's internal queue.

#### What the Evidence Does Not Establish
- It does not establish that the task will never run; the task *will* run as soon as a worker becomes available, which may occur during a subsequent test.
- It does not mean the thread pool is hung; worker threads may simply be busy with earlier tasks.

#### Recommended Investigation
1. Check if the executor's `max_workers` is saturated with long-running tasks, causing newly submitted tasks to sit in the queue.
2. If the task was intended to be cancelled, check whether `future.cancel()` was called and committed before the owner finalized.

#### Corrective Pattern
If the task is no longer needed, cancel it before finalization:

```python
def test_queued_task():
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(expensive_work)
    
    # If tearing down before execution begins, cancel explicitly
    if not future.done():
        future.cancel()
    
    # Note: If future.cancel() returns True, the task is classified as
    # CANCELLED_BEFORE_START (CLEAN), provided it committed before finalization.
```

---

### Scenario 3: `POST_LIFETIME_SUBMISSION`

#### Observable Symptom
State Guard reports:
```text
Classification: POST_LIFETIME_SUBMISSION
Evidence:       Task 'task:<uuid>' was reserved at seq 9 after owner 'owner:test:<test_name>' was finalized at seq 6.
```

#### What the Evidence Establishes
- A task was submitted to an instrumented `ThreadPoolExecutor` using an `OwnerContext` that had already finalized ($S_{\text{res}} > S_F$).

#### What the Evidence Does Not Establish
- It does not mean the test function itself called `submit()` after returning; submissions typically originate from a detached background worker or callback that captured the test's `ContextVar`.

#### Recommended Investigation
1. Look for background threads started outside State Guard's control (e.g., using `threading.Thread` directly) that retain a reference to a test-scoped executor or context.
2. Check asynchronous event loops or callbacks scheduled during the test that fire after the test function finishes.

#### Corrective Pattern
Join detached background producers before test exit:

```python
def test_background_producer():
    executor = ThreadPoolExecutor(max_workers=2)
    stop_signal = threading.Event()

    def producer():
        while not stop_signal.is_set():
            executor.submit(work_item)
            time.sleep(0.01)

    t = threading.Thread(target=producer)
    t.start()

    # Teardown: Stop producer and join it before letting test exit
    stop_signal.set()
    t.join(timeout=2.0)
    executor.shutdown(wait=True)
```

---

### Scenario 4: Work Submitted from Fixture Setup vs. Fixture Teardown

#### The Scenario
A test passes cleanly, but background work submitted during fixture teardown is unjoined or leaks across test boundaries.

#### Why State Guard Behaves This Way
- **Fixture Setup Ownership:** Tasks submitted while the fixture's setup phase is active (before `yield`) are attributed to that fixture's `FIXTURE_SETUP` owner.
- **Fixture Teardown Ownership:** The code executing *after* `yield` in a generator fixture (or registered via `request.addfinalizer`) executes outside the setup owner context. Instead, State Guard activates a separate, dedicated `OwnerType.FIXTURE_TEARDOWN` owner via its `FixtureDef.finish` dispatcher.
- **Teardown Lifecycle Audit:** Tasks submitted during teardown are bound to that teardown owner and evaluated against the teardown finalization boundary ($S_F$). If background work is dispatched during teardown and not joined before the teardown block concludes, State Guard flags the task as a teardown concurrency violation (e.g., `CALLABLE_NOT_EXITED` or `OUTSTANDING_TASK`).
- **Non-Delegating Plugin Replacements:** If an external plugin replaces `FixtureDef.finish` with a wrapper that fails to delegate, State Guard cannot instrument teardown submissions directly. Instead, State Guard detects this bypass during `pytest_fixture_post_finalizer` (SCEN-10) and promotes the session exit code to `TESTS_FAILED` with an incomplete audit warning.

#### Safe Pattern for Fixture Authors
Never dispatch unjoined background work in fixture teardown. If teardown needs to perform asynchronous cleanup or thread pool shutdown, wait for all work to complete before the teardown block finishes:

```python
import pytest
from concurrent.futures import ThreadPoolExecutor

@pytest.fixture
def managed_service():
    executor = ThreadPoolExecutor(max_workers=2)
    yield executor
    
    # Correct Teardown: Join all workers before returning
    executor.shutdown(wait=True)
```

---

### Scenario 5: `SUBMISSION_UNRESOLVED` or `SUBMISSION_FAILED_UNVERIFIED`

#### Observable Symptom
State Guard renders an advisory section in the session audit summary:
```text
=================== State Guard Audit: UNCERTAIN FINDINGS ===================
Owner: TEST_CALL [test_demo.py::test_contention]
  - [SUBMISSION_UNRESOLVED] Task 'task:<uuid>': Task submission was unresolved at owner finalization at seq 6.
```

#### What the Evidence Establishes
- For `SUBMISSION_UNRESOLVED`: The submitting thread was actively inside `executor.submit()` when the owner finalized.
- For `SUBMISSION_FAILED_UNVERIFIED`: `executor.submit()` raised an exception (e.g. `RuntimeError: can't start new thread`). In CPython, work items are enqueued *before* thread count adjustment, meaning existing idle workers might still execute the work item.

#### Corrective Pattern
1. Avoid submitting tasks at the very instant a test function returns.
2. If `submit()` raises due to thread limits, lower the test suite's thread consumption or configure executor limits appropriately.

---

## General Troubleshooting FAQ

### Why did my test pass assertions but fail the overall run?
Pytest evaluates assertions during the call phase. State Guard audits concurrency during test teardown. If assertions pass but a worker thread is left running across teardown, State Guard marks the teardown phase as `ERROR`, promoting the exit code to `1` to protect the rest of the test suite from pollution.

### Why not use `time.sleep()` to fix violations?
Arbitrary sleeps (`time.sleep(0.5)`) are inherently flaky. Under high CPU load in CI runners or across different operating systems, a thread might take longer to schedule than the sleep duration, causing intermittent CI failures. Always use deterministic synchronization primitives (`threading.Event`, `Future.result(timeout=...)`, or `executor.shutdown(wait=True)`).

### Does a healthy executor mean there are no outstanding tasks?
No. An executor remaining healthy and open across a session simply means the thread pool is active. A healthy shared executor can hold unjoined tasks from finished tests. State Guard attributes tasks to the **submitter**, enforcing that each test cleans up the work it created.

---

## Related Documentation

- [Test & Fixture Usage Guide](test-and-fixture-usage.md): How to structure concurrent tests correctly.
- [Classifications & Severities Reference](classifications.md): Complete reference for all ten classifications.
