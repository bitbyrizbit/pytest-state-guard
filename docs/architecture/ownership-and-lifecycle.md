# Ownership & Lifecycle Architecture

This document describes how `pytest-state-guard` models task provenance, binds execution contexts across thread boundaries, and audits task lifecycles against pytest owner scopes.

---

## The Submitter Ownership Model

In concurrent Python testing, an executor (`concurrent.futures.ThreadPoolExecutor`) is merely an execution resource. It is frequently configured as a shared, long-lived object spanning multiple test cases or fixtures. 

The entity accountable for task creation, execution duration, and resource cleanup is the **submitter**. State Guard attaches task ownership to the active pytest lifecycle scope in which `executor.submit()` is invoked.

```mermaid
flowchart TD
    subgraph OwnerScopes["Owner Scopes"]
        TCO["TEST_CALL Owner<br>(pytest_runtest_call)"]
        FSO["FIXTURE_SETUP Owner<br>(pytest_fixture_setup)"]
        FTO["FIXTURE_TEARDOWN Owner<br>(FixtureDef.finish dispatcher)"]
    end

    subgraph ContextVar["ContextVar Propagation"]
        CV["_CURRENT_OWNER ContextVar"]
    end

    subgraph TaskBinding["Task Identity Binding"]
        SUBMIT["executor.submit(fn, *args, **kwargs)"]
        RES["reserve_task()<br>Binds TaskRecord to Active Owner"]
        WRAP["_CallableWrapper(fn, task_id, owner, ledger)"]
    end

    TCO --> CV
    FSO --> CV
    FTO --> CV
    CV --> SUBMIT
    SUBMIT --> RES
    RES --> WRAP
```

---

## Owner Scopes and Lifecycles

State Guard defines three distinct owner types in [models.py](file:///C:/Users/Rud/Desktop/state-guard/src/pytest_state_guard/models.py):

### 1. Test Call Owners (`OwnerType.TEST_CALL`)
- **Activation:** Created and activated via [plugin.py](file:///C:/Users/Rud/Desktop/state-guard/src/pytest_state_guard/plugin.py) inside `pytest_runtest_call`.
- **Scope:** Bounded to the physical execution of the test function. Any task submitted during the test body is bound to this test owner ID (`owner:test:<nodeid>:<uuid>`).
- **Finalization:** In `pytest_runtest_teardown` after the test function and all associated fixture finalizers have finished executing, the test owner transitions irreversibly to `OwnerState.FINALIZED`, committing finalization sequence $S_F$ to the ledger.

### 2. Fixture Setup Owners (`OwnerType.FIXTURE_SETUP`)
- **Activation:** Created and activated inside `pytest_fixture_setup` across all fixture scopes (`function`, `class`, `module`, `package`, `session`).
- **Scope:** Bounded strictly to the fixture setup phase (code executing prior to `yield` or `return`). Tasks submitted during setup are bound to `owner:fixture:<argname>:<uuid>`.
- **Finalization:** Finalized in `pytest_fixture_post_finalizer` after all teardown and finalizers for that fixture have finished executing.

### 3. Fixture Teardown Owners (`OwnerType.FIXTURE_TEARDOWN`)
- **The Setup vs. Teardown Ownership Boundary:** When a generator fixture yields control to pytest, `pytest_fixture_setup` resets the ContextVar token. Code executing after `yield` (or registered via `request.addfinalizer`) does **not** belong to the completed fixture setup owner. Instead, teardown execution is tracked under its own distinct, dedicated owner: `OwnerType.FIXTURE_TEARDOWN`.
- **The `FixtureDef.finish` Dispatcher:** State Guard instruments fixture teardown by wrapping `FixtureDef.finish` with a static dispatcher (`_state_guard_fixture_finish`). When pytest finalizes a fixture instance:
  1. **Phase 0 (Entry Registration):** Records re-entrant dispatcher entry depth per fixture definition.
  2. **Phase A (Instance Resolution & Nesting):** Resolves the active fixture instance sequence. If teardown execution is already active on this instance (nested or recursive `finish()` calls), it increments `dispatch_depth` and delegates directly without duplicating owners. If user teardown callbacks exist, it marks the outermost invocation for tracking.
  3. **Phase B (Owner Activation):** Registers a unique runtime teardown owner (`owner:fixture_teardown:<argname>:<def_token>:<node_token>:<param_key>:<instance_seq>`) and binds it to `_CURRENT_OWNER` via `ContextVar`, preserving prior context for safe restoration.
  4. **Phase C (Teardown Execution):** Calls the underlying `FixtureDef.finish()` inside a clean `try ... finally` block, executing user finalizers and post-yield logic under the active `FIXTURE_TEARDOWN` context.
  5. **Phase D (Cleanup & Finalization):** Restores the prior `ContextVar` context, pops the active teardown owner from registry, finalizes the teardown owner in the ledger with sequence $S_F$, and cleans up instance registries.
- **Non-Delegating Replacements & SCEN-10 Detection:** If a third-party plugin or user code replaces `FixtureDef.finish` with an implementation that does not delegate to State Guard's dispatcher, State Guard detects the bypass in `pytest_fixture_post_finalizer` by verifying that an expected post-finalizer sentinel was consumed without the dispatcher having been entered. When detected:
  - State Guard records a structured degradation (`"FixtureDef.finish was replaced by a non-delegating plugin; teardown ownership bypassed"`).
  - The fixture definition is added to `state.bypassed_fixtures`.
  - The setup owner is safely finalized to consume pending sentinels.
  - During the session finish audit, the presence of bypassed fixtures promotes an otherwise successful test session to `pytest.ExitCode.TESTS_FAILED` with an incomplete audit warning.
- **Limitations of Instrumentation Bypass Detection:** State Guard does **not** claim that every possible fixture teardown execution is successfully instrumented under all conditions. If an external plugin completely supplants the finish dispatch path, State Guard cannot observe the tasks submitted during that teardown; it guarantees only that such unmonitored execution is conclusively detected, flagged as an incomplete audit, and prevented from silently passing as clean.

---

## ContextVar Propagation Across Thread Boundaries

Python's `contextvars.ContextVar` provides context-local storage. However, standard `concurrent.futures.ThreadPoolExecutor` worker threads **do not automatically inherit ContextVar values** from the thread that submitted the task.

To bridge this gap without altering CPython internals:
1. **At Submission Time:** `_state_guard_submit()` reads `get_current_owner()` from the submitting thread's `ContextVar`.
2. **Context Capture:** The active owner record and newly allocated `task_id` are passed into the constructor of `_CallableWrapper`.
3. **At Worker Execution Time:** When a worker thread invokes `_CallableWrapper.__call__()`, it executes within a context manager:
   ```python
   with execution_context(self._owner, self._task_id):
       # ContextVar is explicitly restored in the worker thread
       return self._fn(*args, **kwargs)
   ```
4. **Nested Submissions:** If the user's callable spawns child tasks by submitting to another executor, the worker thread retains the parent owner context and registers child tasks with `parent_task_id` tracked in the ledger.

---

## Owner and Task State Dimensions

### Owner Lifecycle
The owner lifecycle is strictly one-way and irreversible:
$$\text{OwnerState.ACTIVE} \longrightarrow \text{OwnerState.FINALIZED}$$
Once an owner commits its finalization event ($S_F$) to the ledger, it can never be reopened, reset, or transferred.

### Task Dimensions
A task's lifecycle is modeled across three orthogonal, independent state dimensions in [models.py](file:///C:/Users/Rud/Desktop/state-guard/src/pytest_state_guard/models.py):

```mermaid
stateDiagram-v2
    direction LR

    state "Submission State" as SS {
        [*] --> RESERVED
        RESERVED --> SUBMIT_RETURNED
        RESERVED --> SUBMIT_RAISED_UNVERIFIED
    }

    state "Callable State" as CS {
        [*] --> NOT_STARTED
        NOT_STARTED --> RUNNING
        RUNNING --> EXITED_RETURNED
        RUNNING --> EXITED_RAISED
    }

    state "Future State" as FS {
        [*] --> UNKNOWN
        UNKNOWN --> PENDING
        PENDING --> FINISHED
        PENDING --> CANCELLED
    }
```

1. **Submission State (`SubmissionState`):**
   - `RESERVED`: Identity reserved in the ledger prior to calling `ThreadPoolExecutor.submit()`.
   - `SUBMIT_RETURNED`: Real `submit()` returned a `Future` object successfully.
   - `SUBMIT_RAISED_UNVERIFIED`: Real `submit()` raised an exception (e.g. `RuntimeError: can't start new thread`).
2. **Callable State (`CallableState`):**
   - `NOT_STARTED`: Callable has not been invoked by a worker thread.
   - `RUNNING`: Worker thread entered `_CallableWrapper` and committed `CALLABLE_STARTED`.
   - `EXITED_RETURNED`: Callable completed normally and returned a value.
   - `EXITED_RAISED`: Callable exited by raising an exception (captured with type and safe string fallback).
3. **Future State (`FutureState`):**
   - `UNKNOWN`: Future object not yet observed.
   - `PENDING`: Real `submit()` returned, future is pending.
   - `FINISHED`: Done callback observed future settlement (`set_result` or `set_exception`).
   - `CANCELLED`: Done callback observed future cancellation (`cancel()`).

---

## Post-Lifetime Submissions

If code associated with an owner context attempts to submit work after the owner has already finalized ($S_{\text{res}} > S_F$), State Guard commits the reservation event and classifies the task as `POST_LIFETIME_SUBMISSION` (Severity: `VIOLATION`).

This prevents detached background workers or asynchronous callbacks from continuing to spawn unmanaged work into the process after test teardown.

---

## Related Documentation

- [Concurrency & Interception Model](concurrency-model.md): Detailed mechanics of submit patching, locking, and atomicity boundaries.
- [Reporting & Pytest Integration](reporting.md): How findings are aggregated and rendered in test reports.
