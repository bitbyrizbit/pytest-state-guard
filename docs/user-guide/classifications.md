# Classifications & Severities Reference

Every task submitted under a tracked State Guard owner is evaluated by a pure, deterministic policy engine against the owner's committed finalization boundary ($S_F$). State Guard assigns each evaluated entity exactly one of **ten frozen classifications** across three severity categories.

---

## Classification Overview Table

| Classification | Severity | Evaluated Entity | Policy Ordering Rule | Summary Description |
| :--- | :---: | :---: | :--- | :--- |
| **`NO_OUTSTANDING_TASKS`** | `CLEAN` | Owner | $N_{\text{tasks}} = 0$ | Owner finalized its lifetime with zero submitted tasks. |
| **`COMPLETED_BEFORE_FINALIZATION`** | `CLEAN` | Task | $S_{\text{exit}} < S_F$ | Callable started and exited before owner finalization. |
| **`CANCELLED_BEFORE_START`** | `CLEAN` | Task | $S_{\text{cancel}} < S_F \land S_{\text{start}} = \emptyset$ | Future cancellation committed before finalization without execution. |
| **`OUTSTANDING_TASK`** | `VIOLATION` | Task | $S_{\text{ret}} < S_F \land S_{\text{start}} = \emptyset$ | Task accepted before finalization, but callable never started. |
| **`CALLABLE_NOT_EXITED`** | `VIOLATION` | Task | $S_{\text{start}} < S_F \land S_{\text{exit}} = \emptyset$ | Callable started before finalization, but has not committed exit. |
| **`EXECUTION_AFTER_OWNER_FINALIZATION`** | `VIOLATION` | Task | $S_{\text{start}} > S_F$ | Callable began execution after owner was already finalized. |
| **`EXIT_COMMITTED_AFTER_FINALIZATION`** | `VIOLATION` | Task | $S_{\text{start}} < S_F < S_{\text{exit}}$ | Callable started before finalization, but exited after finalization. |
| **`POST_LIFETIME_SUBMISSION`** | `VIOLATION` | Task | $S_{\text{res}} > S_F$ | Task submitted to executor after owner was already finalized. |
| **`SUBMISSION_UNRESOLVED`** | `UNCERTAIN` | Task | Submission in flight at $S_F$ | Submission neither returned nor raised before owner finalization. |
| **`SUBMISSION_FAILED_UNVERIFIED`** | `UNCERTAIN` | Task | $S_{\text{raise}} < S_F \land S_{\text{start}} = \emptyset$ | `submit()` raised before finalization, but work-item enqueueing cannot be ruled out. |

---

## Detailed Classification Reference

### 1. `NO_OUTSTANDING_TASKS`
- **Severity:** `CLEAN`
- **Entity:** Evaluated on the Owner record when `lifetime_task_count == 0`.
- **Committed Evidence:** The owner was created at $S_{\text{created}}$ and finalized at $S_F$ with zero task reservations committed during its active lifetime ($S_{\text{res}} < S_F$).
- **Meaning:** The test function or fixture setup ran and concluded without submitting any work to an instrumented `ThreadPoolExecutor`.
- **Next Steps:** No action needed. The owner is completely clean.

---

### 2. `COMPLETED_BEFORE_FINALIZATION`
- **Severity:** `CLEAN`
- **Entity:** Evaluated on a Task record.
- **Committed Evidence:** Both $S_{\text{start}}$ and $S_{\text{exit}}$ were committed to the ledger, and $S_{\text{exit}} < S_F$.
- **Meaning:** The wrapped callable began running and successfully finished (either returning a value or raising an application exception) before the owner boundary was reached.
- **Next Steps:** No action needed. The task's lifecycle was fully bounded within its submitter's scope.
- **Crucial Semantics:** This classification is based strictly on the committed callable-exit sequence ($S_{\text{exit}} < S_F$). It does not require a Future-done callback event, preserving clean status even if a Future callback commit was delayed.

---

### 3. `CANCELLED_BEFORE_START`
- **Severity:** `CLEAN`
- **Entity:** Evaluated on a Task record.
- **Committed Evidence:** A `FUTURE_DONE_CALLBACK_OBSERVED` event with `terminal_state="CANCELLED"` was committed before finalization ($S_{\text{cancel}} < S_F$), and no `CALLABLE_STARTED` event exists ($S_{\text{start}} = \emptyset$).
- **Meaning:** The submitted Future was explicitly cancelled before an executor worker thread could begin executing the user's callable.
- **Next Steps:** No action needed. Because the callable never executed and the task was cancelled before owner teardown, no background activity remains.

---

### 4. `OUTSTANDING_TASK`
- **Severity:** `VIOLATION`
- **Entity:** Evaluated on a Task record.
- **Committed Evidence:** `SUBMIT_RETURNED` committed before owner finalization ($S_{\text{ret}} < S_F$), but no `CALLABLE_STARTED` event was committed ($S_{\text{start}} = \emptyset$).
- **Meaning:** The executor accepted the task for execution, but at the moment the test or fixture teardown occurred, the callable was still sitting unexecuted in the executor's internal queue.
- **Why It Is a Violation:** The queued work will inevitably execute later in the process lifecycle, likely during a subsequent test, mutating shared state or causing intermittent test pollution.
- **Next Steps:** Ensure the submitting owner joins the task (e.g., using `future.result()` or `executor.shutdown(wait=True)`). If the task was intended to be cancelled, ensure `future.cancel()` was called and committed before teardown.

---

### 5. `CALLABLE_NOT_EXITED`
- **Severity:** `VIOLATION`
- **Entity:** Evaluated on a Task record.
- **Committed Evidence:** `CALLABLE_STARTED` was committed before finalization ($S_{\text{start}} < S_F$), but no `CALLABLE_EXITED` event exists in the ledger snapshot ($S_{\text{exit}} = \emptyset$).
- **Meaning:** The worker thread began executing the user's callable, but as of owner finalization, the callable had not finished executing (or an instrumentation error prevented exit commitment).
- **Why It Is a Violation:** An active worker thread is actively running across the test boundary, posing severe data race and state pollution risks to subsequent tests.
- **Next Steps:** Verify that the test waits for the task's completion before returning, or ensure that long-running workers receive a termination signal (such as a `threading.Event`) that they observe before teardown.

---

### 6. `EXECUTION_AFTER_OWNER_FINALIZATION`
- **Severity:** `VIOLATION`
- **Entity:** Evaluated on a Task record.
- **Committed Evidence:** `CALLABLE_STARTED` committed after the owner's finalization boundary ($S_{\text{start}} > S_F$).
- **Meaning:** The task was queued during the owner's active lifetime, but it did not begin physical execution until after the owner had already finalized and exited.
- **Why It Is a Violation:** Demonstrates deferred task leakage: the submitter finished, but its work started executing in an arbitrary subsequent phase of the pytest session.
- **Next Steps:** Synchronize on task completion within the test or fixture before allowing the owner scope to complete.

---

### 7. `EXIT_COMMITTED_AFTER_FINALIZATION`
- **Severity:** `VIOLATION`
- **Entity:** Evaluated on a Task record.
- **Committed Evidence:** Callable started before finalization ($S_{\text{start}} < S_F$), but the exit event committed after finalization ($S_{\text{exit}} > S_F$).
- **Meaning:** The worker was executing when the owner finalized and only finished execution after teardown had concluded.
- **Why It Is a Violation:** Direct lifecycle overrun: the callable was executing concurrently with teardown or subsequent test setup.
- **Next Steps:** Block on the task's completion (`future.result()` or executor shutdown) inside the test body.

---

### 8. `POST_LIFETIME_SUBMISSION`
- **Severity:** `VIOLATION`
- **Entity:** Evaluated on a Task record.
- **Committed Evidence:** The task reservation event was committed after the owner was already finalized ($S_{\text{res}} > S_F$).
- **Meaning:** Code associated with a finalized owner context attempted to submit new work to an executor after the owner had already been destroyed.
- **Why It Is a Violation:** Submitting work after teardown indicates an orphaned worker thread or asynchronous callback is continuing to generate work on behalf of a dead test context.
- **Next Steps:** Check whether a detached background thread from a previous test is calling `executor.submit()`, or whether a fixture finalizer is improperly submitting work without awaiting it.

---

### 9. `SUBMISSION_UNRESOLVED`
- **Severity:** `UNCERTAIN`
- **Entity:** Evaluated on a Task record.
- **Committed Evidence:** Task reservation was committed ($S_{\text{res}} < S_F$), but neither `SUBMIT_RETURNED` nor `SUBMIT_RAISED` was committed prior to owner finalization ($S_F$), and the callable did not start before finalization.
- **Meaning:** The call to `executor.submit()` was still executing on the submitting thread when owner finalization occurred.
- **Why It Is Uncertain:** Because `submit()` did not complete before teardown, State Guard cannot definitively determine whether the task was successfully enqueued or rejected.
- **Next Steps:** Review test concurrency. An unresolved submission indicates heavy thread contention or deadlocks during `executor.submit()` invocation itself.

---

### 10. `SUBMISSION_FAILED_UNVERIFIED`
- **Severity:** `UNCERTAIN`
- **Entity:** Evaluated on a Task record.
- **Committed Evidence:** `SUBMIT_RAISED` committed before finalization ($S_{\text{raise}} < S_F$), and no `CALLABLE_STARTED` event exists ($S_{\text{start}} = \emptyset$).
- **Meaning:** `ThreadPoolExecutor.submit()` raised an exception (such as `RuntimeError: can't start new thread`).
- **Why It Is Uncertain:** In CPython, `ThreadPoolExecutor.submit()` enqueues the work item into an internal queue *before* attempting to start or awaken worker threads. If thread creation raises an exception, the work item may still reside in the queue and could be executed by an existing idle worker thread. Because worker execution cannot be conclusively ruled out, the finding is advisory UNCERTAIN.
- **Next Steps:** Investigate system thread limits or executor pool configuration. If existing worker threads are saturated, reduce `max_workers` or resolve thread leaks.

---

## Important Architectural Distinctions

To interpret State Guard diagnostics accurately, keep these distinctions in mind:

1. **Observed vs. Physical Instant:** State Guard sequence IDs ($S_*$) represent when an event committed to the serialized ledger under lock, not the exact physical instant of hardware execution.
2. **Callable Completion vs. Future Settlement:**
   - Callable completion is recorded when the user's function returns or raises (`CALLABLE_EXITED`).
   - Future settlement is observed when done-callbacks fire (`FUTURE_DONE_CALLBACK_OBSERVED`).
   - A missing Future callback does not imply that the callable failed to complete. If $S_{\text{exit}} < S_F$, the task is cleanly classified as `COMPLETED_BEFORE_FINALIZATION`.
3. **Uncertainty is Advisory:** `UNCERTAIN` findings (`SUBMISSION_UNRESOLVED` and `SUBMISSION_FAILED_UNVERIFIED`) represent ambiguous lifecycle evidence. They are displayed in the terminal audit summary for diagnostic awareness, but do not automatically cause test teardown failures unless concrete violations also occurred.

---

## Related Documentation

- [Test & Fixture Usage Guide](test-and-fixture-usage.md): How to structure tests to achieve `CLEAN` classifications.
- [Troubleshooting Guide](troubleshooting.md): Step-by-step diagnostic workflows for fixing `VIOLATION` findings.
