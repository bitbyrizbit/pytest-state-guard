# pytest-state-guard

**Deterministic `ThreadPoolExecutor` task lifetime auditor for pytest.**

---

## Overview

In concurrent Python test suites, tests often pass their assertions while background work continues executing silently in the process. When a test function or fixture submits a callable to a `concurrent.futures.ThreadPoolExecutor` without waiting for `Future.result()` or shutting down the executor with `wait=True`, the submitted work can remain active or queued across test and fixture teardown boundaries.

These unjoined tasks cause subtle, non-deterministic bugs:
- **Intermittent test pollution:** A background worker from an earlier test writes to shared memory or mocks while a subsequent test assumes an isolated environment.
- **Teardown races:** Tests exit in milliseconds while background threads attempt to interact with cleaned-up fixtures or closed database connections.
- **Hidden regressions:** Concurrency leaks go undetected in CI because standard pytest passes so long as call-phase assertions do not fail.

`pytest-state-guard` detects this lifecycle mismatch. It dynamically instruments `ThreadPoolExecutor.submit` to capture task provenance, records observed lifecycle transitions into an append-only event ledger under a monotonic sequence counter, and evaluates task lifetimes against owner finalization boundaries using a deterministic policy engine.

---

## Core Principles

### 1. The Submitter Owns the Task
Executors are often long-lived, shared resources configured across multiple fixtures or tests. An executor is merely an execution mechanism; the entity responsible for a task's existence and bounded lifetime is the **submitter**.

State Guard tracks three distinct lifecycle owner scopes:
- **Test Call Owners (`TEST_CALL`):** Bound to the test item during its `pytest_runtest_call` execution and finalized in `pytest_runtest_teardown`. Tasks submitted during the test body belong to this owner.
- **Fixture Setup Owners (`FIXTURE_SETUP`):** Bound to fixture invocations during `pytest_fixture_setup` across all fixture scopes (`function`, `class`, `module`, `package`, `session`), and finalized through `pytest_fixture_post_finalizer` after fixture teardown logic completes.
- **Fixture Teardown Owners (`FIXTURE_TEARDOWN`):** Bound to fixture finalizer and post-yield execution intercepted via the `FixtureDef.finish` dispatcher, and finalized upon teardown conclusion.

> **Important Lifecycle Boundary:** Fixture teardown and finalizer execution do not belong to the completed fixture-setup owner; they are audited under their own dedicated `FIXTURE_TEARDOWN` owner. State Guard verifies that teardown dispatch is not bypassed by non-delegating plugins (SCEN-10).

### 2. Auditing, Not Execution Intervention
State Guard is a passive diagnostic auditor, not an execution sandbox or active manager:
- **No thread termination:** State Guard will never kill threads, interrupt running callables, or terminate operating system threads.
- **No executor management:** State Guard does not alter executor pools, force shutdown, or cancel user tasks.
- **No memory isolation:** State Guard does not intercept memory reads/writes or prevent data races.
- **Non-masking diagnostics:** Concurrency violations are surfaced as teardown errors without overwriting original call assertion failures or hiding application exceptions.

---

## Supported Scope & Limitations

State Guard v1 is engineered with strict, frozen operational boundaries:

| Dimension | Supported in Scope | Out of Scope |
| :--- | :--- | :--- |
| **Executor Type** | `concurrent.futures.ThreadPoolExecutor` | Raw `threading.Thread`, `multiprocessing`, `asyncio`, custom thread pools |
| **Process Model** | Single pytest process (per-process in-memory ledger) | Multi-process aggregation or cross-process correlation (`pytest-xdist`) |
| **Intervention** | Read-only observation & policy classification | Thread interruption, cancellation, resource cleanup |
| **Scheduling** | Monotonic event sequence order under ledger lock | Operating system physical thread scheduling guarantees |

---

## Next Steps

To install State Guard and begin auditing your test suite, proceed to the [Getting Started](getting-started.md) guide.
