"""Integration tests for ThreadPoolExecutor high-level APIs (map and shutdown).

Validates task lifecycle tracking, owner attribution, exception accounting,
and future cancellation under ThreadPoolExecutor.map() and
ThreadPoolExecutor.shutdown(cancel_futures=True).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
import pytest

from pytest_state_guard.executor import install_patch, remove_patch
from pytest_state_guard.ledger import Ledger
from pytest_state_guard.models import (
    CallableState,
    FindingClassification,
    FutureState,
    OwnerType,
    Severity,
    SubmissionState,
)
from pytest_state_guard.owners import active_owner
from pytest_state_guard.policy import PolicyEngine


class ExpectedMapError(RuntimeError):
    """Custom exception defined at module scope for deterministic qualname matching."""


@pytest.fixture
def ledger() -> Ledger:
    """Fixture providing an active State Guard ledger with executor patching."""
    led = Ledger()
    install_patch(led)
    yield led
    remove_patch()


class TestExecutorHighLevelApi:
    """Deterministic integration tests for executor map() and shutdown(cancel_futures=True)."""

    def test_map_fully_consumed_resolves_clean(self, ledger: Ledger) -> None:
        """When map() is fully consumed, all tasks complete before finalization with CLEAN status."""
        owner_id = "owner:map:complete"
        ledger.create_owner(owner_id, OwnerType.TEST_CALL, "tests/test_map.py::test_complete")
        policy = PolicyEngine()

        with active_owner(owner_id, OwnerType.TEST_CALL, "tests/test_map.py::test_complete"):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda x: x * 10, [1, 2, 3]))

        assert results == [10, 20, 30]

        ledger.finalize_owner(owner_id)
        findings = policy.evaluate_owner(owner_id, ledger)
        tasks = ledger.get_tasks_for_owner(owner_id)

        assert len(tasks) == 3
        assert all(t.submission_state == SubmissionState.SUBMIT_RETURNED for t in tasks)
        assert all(t.callable_state == CallableState.EXITED_RETURNED for t in tasks)
        assert all(t.future_state == FutureState.FINISHED for t in tasks)
        assert len(findings) == 3
        assert {f.task_id for f in findings} == {t.task_id for t in tasks}
        assert all(f.classification == FindingClassification.COMPLETED_BEFORE_FINALIZATION for f in findings)
        assert all(f.severity == Severity.CLEAN for f in findings)

    def test_map_partially_consumed_deterministic_running_and_queued(self, ledger: Ledger) -> None:
        """Partially consumed map with live iterator deterministically identifies running vs queued tasks.

        Preconditions (max_workers=1):
        - Task 1: Executes, completes, and is consumed via next(it).
        - Task 2: Begins execution on the single worker, signals start, and remains blocked.
        - Task 3: Remains pending in the executor work queue.
        - Owner finalizes while Task 2 is blocked and iterator is held open.
        """
        owner_id = "owner:map:partial"
        ledger.create_owner(owner_id, OwnerType.TEST_CALL, "tests/test_map.py::test_partial")
        policy = PolicyEngine()

        task1_started = threading.Event()
        release_task1 = threading.Event()
        task2_started = threading.Event()
        release_task2 = threading.Event()

        def worker_fn(item: int) -> int:
            if item == 1:
                task1_started.set()
                assert release_task1.wait(timeout=15.0)
                return 1
            elif item == 2:
                task2_started.set()
                assert release_task2.wait(timeout=15.0)
                return 2
            return 3

        executor = ThreadPoolExecutor(max_workers=1)
        it = None
        try:
            with active_owner(owner_id, OwnerType.TEST_CALL, "tests/test_map.py::test_partial"):
                it = executor.map(worker_fn, [1, 2, 3])

                # 1. Wait for Task 1 to run and consume it
                assert task1_started.wait(timeout=5.0)
                release_task1.set()
                assert next(it) == 1

                # 2. Wait for single worker to pick up Task 2 and block
                assert task2_started.wait(timeout=5.0)
                # At this exact moment: Task 2 is running, Task 3 is pending in queue

            # 3. Finalize owner while Task 2 is still blocked and Task 3 is queued
            ledger.finalize_owner(owner_id)
            findings = policy.evaluate_owner(owner_id, ledger)
            tasks = ledger.get_tasks_for_owner(owner_id)

            assert len(tasks) == 3
            assert len(findings) == 3
            assert {f.task_id for f in findings} == {t.task_id for t in tasks}

            # Identify tasks by semantic state rather than list index
            t_completed = next(t for t in tasks if t.callable_state == CallableState.EXITED_RETURNED)
            t_running = next(t for t in tasks if t.callable_state == CallableState.RUNNING)
            t_queued = next(t for t in tasks if t.callable_state == CallableState.NOT_STARTED)

            f_completed = next(f for f in findings if f.task_id == t_completed.task_id)
            f_running = next(f for f in findings if f.task_id == t_running.task_id)
            f_queued = next(f for f in findings if f.task_id == t_queued.task_id)

            assert f_completed.classification == FindingClassification.COMPLETED_BEFORE_FINALIZATION
            assert f_completed.severity == Severity.CLEAN

            assert f_running.classification == FindingClassification.CALLABLE_NOT_EXITED
            assert f_running.severity == Severity.VIOLATION

            assert f_queued.classification == FindingClassification.OUTSTANDING_TASK
            assert f_queued.severity == Severity.VIOLATION

        finally:
            release_task1.set()
            release_task2.set()
            del it
            executor.shutdown(wait=True)

    def test_map_callable_exception_lifecycle_and_companion_tasks(self, ledger: Ledger) -> None:
        """When a mapped callable raises an exception:
        1. The mapped exception propagates to the caller upon iteration.
        2. The failing task records EXITED_RAISED with exception metadata in the ledger.
        3. The concurrent companion task is not lost, remains accounted for, and completes cleanly after release.
        4. All tasks associated with the owner have valid final lifecycle states upon finalization.
        """
        owner_id = "owner:map:exception"
        ledger.create_owner(owner_id, OwnerType.TEST_CALL, "tests/test_map.py::test_exception")
        policy = PolicyEngine()

        fail_started = threading.Event()
        companion_started = threading.Event()
        release_companion = threading.Event()

        def worker_fn(item: int) -> int:
            if item == 1:
                fail_started.set()
                raise ExpectedMapError("map callable failure")
            elif item == 2:
                companion_started.set()
                assert release_companion.wait(timeout=15.0)
                return 2
            return 3

        executor = ThreadPoolExecutor(max_workers=2)
        it = None
        try:
            with active_owner(owner_id, OwnerType.TEST_CALL, "tests/test_map.py::test_exception"):
                it = executor.map(worker_fn, [1, 2, 3])

                # Ensure both workers have started execution
                assert fail_started.wait(timeout=5.0)
                assert companion_started.wait(timeout=5.0)

                # Consuming the first item propagates the mapped exception
                with pytest.raises(ExpectedMapError, match="map callable failure"):
                    next(it)

                # Clean up the iterator
                del it

                # Release companion worker and shut down executor
                release_companion.set()
                executor.shutdown(wait=True)

            ledger.finalize_owner(owner_id)
            findings = policy.evaluate_owner(owner_id, ledger)
            tasks = ledger.get_tasks_for_owner(owner_id)

            assert len(tasks) == 3
            assert len(findings) == 3
            assert {f.task_id for f in findings} == {t.task_id for t in tasks}

            # 1. Failing task verification
            t_failed = next(t for t in tasks if t.callable_state == CallableState.EXITED_RAISED)
            assert t_failed.exception_type == "ExpectedMapError"
            assert t_failed.exception_message == "map callable failure"
            assert t_failed.future_state == FutureState.FINISHED

            # 2. Companion task verification
            t_companion = next(t for t in tasks if t.callable_state == CallableState.EXITED_RETURNED)
            assert t_companion.future_state == FutureState.FINISHED

            # 3. Third task: assert only valid lifecycle combinations
            t_other = next(t for t in tasks if t.task_id not in (t_failed.task_id, t_companion.task_id))
            assert t_other.submission_state == SubmissionState.SUBMIT_RETURNED
            valid_lifecycle_combinations = {
                (CallableState.NOT_STARTED, FutureState.CANCELLED),
                (CallableState.EXITED_RETURNED, FutureState.FINISHED),
            }
            assert (t_other.callable_state, t_other.future_state) in valid_lifecycle_combinations

            # 4. All findings are CLEAN because all work settled before finalization
            assert all(f.severity == Severity.CLEAN for f in findings)

        finally:
            release_companion.set()
            executor.shutdown(wait=True)

    def test_shutdown_cancel_futures_wait_false_flags_running_leak(self, ledger: Ledger) -> None:
        """shutdown(wait=False, cancel_futures=True) cancels queued tasks but leaves running task
        unjoined, which State Guard flags as CALLABLE_NOT_EXITED upon finalization.
        """
        owner_id = "owner:cf:wait_false"
        ledger.create_owner(owner_id, OwnerType.TEST_CALL, "tests/test_cf.py::test_wait_false")
        policy = PolicyEngine()

        worker_started = threading.Event()
        release_worker = threading.Event()

        def blocked_worker() -> str:
            worker_started.set()
            assert release_worker.wait(timeout=15.0)
            return "done"

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            with active_owner(owner_id, OwnerType.TEST_CALL, "tests/test_cf.py::test_wait_false"):
                fut1 = executor.submit(blocked_worker)
                # 1. Prove task 1 has started executing
                assert worker_started.wait(timeout=5.0)

                # 2. Submit task 2; guaranteed queued because single worker is busy
                fut2 = executor.submit(lambda: "queued_never_runs")

                # 3. Shutdown without waiting; drains queue and cancels fut2
                executor.shutdown(wait=False, cancel_futures=True)
                assert fut2.cancelled() is True

            # 4. Finalize owner while worker is still blocked running fut1
            ledger.finalize_owner(owner_id)
            findings = policy.evaluate_owner(owner_id, ledger)
            tasks = ledger.get_tasks_for_owner(owner_id)

            assert len(tasks) == 2
            assert len(findings) == 2
            assert {f.task_id for f in findings} == {t.task_id for t in tasks}

            # Identify tasks by semantic state rather than list index
            t_running = next(t for t in tasks if t.callable_state == CallableState.RUNNING)
            t_cancelled = next(
                t for t in tasks
                if t.future_state == FutureState.CANCELLED and t.callable_state == CallableState.NOT_STARTED
            )

            f_running = next(f for f in findings if f.task_id == t_running.task_id)
            assert f_running.classification == FindingClassification.CALLABLE_NOT_EXITED
            assert f_running.severity == Severity.VIOLATION

            f_cancelled = next(f for f in findings if f.task_id == t_cancelled.task_id)
            assert f_cancelled.classification == FindingClassification.CANCELLED_BEFORE_START
            assert f_cancelled.severity == Severity.CLEAN

        finally:
            release_worker.set()
            executor.shutdown(wait=True)

    def test_shutdown_cancel_futures_wait_true_clean_settlement(self, ledger: Ledger) -> None:
        """shutdown(wait=True, cancel_futures=True) cancels queued tasks and joins running tasks,
        yielding a completely CLEAN owner evaluation without race conditions.

        Deterministic synchronization:
        1. Task 1 starts on single worker and remains blocked.
        2. Task 2 is queued behind it.
        3. Register a done callback on Task 2 signaling fut2_settled immediately after submit.
        4. A background helper thread calls executor.shutdown(wait=True, cancel_futures=True).
        5. Main thread awaits fut2_settled signal (guaranteeing cancellation was enacted while worker blocked).
        6. Main thread confirms fut2.cancelled() is True.
        7. Main thread releases Task 1.
        8. Background shutdown thread joins (proving worker thread exited cleanly).
        9. Owner is finalized and evaluated as completely CLEAN.
        """
        owner_id = "owner:cf:wait_true"
        ledger.create_owner(owner_id, OwnerType.TEST_CALL, "tests/test_cf.py::test_wait_true")
        policy = PolicyEngine()

        worker_started = threading.Event()
        release_worker = threading.Event()
        fut2_settled = threading.Event()
        shutdown_thread: threading.Thread | None = None

        def blocked_worker() -> str:
            worker_started.set()
            assert release_worker.wait(timeout=15.0)
            return "done"

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            with active_owner(owner_id, OwnerType.TEST_CALL, "tests/test_cf.py::test_wait_true"):
                fut1 = executor.submit(blocked_worker)
                assert worker_started.wait(timeout=5.0)

                # Task 2 queued behind single busy worker
                fut2 = executor.submit(lambda: "queued_never_runs")
                # Test callback registered after instrumented submission returns
                fut2.add_done_callback(lambda _: fut2_settled.set())

                # Invoke shutdown(wait=True, cancel_futures=True) in a separate thread.
                # Worker is STILL blocked, so shutdown thread will block waiting for worker.
                shutdown_thread = threading.Thread(
                    target=executor.shutdown,
                    kwargs={"wait": True, "cancel_futures": True},
                )
                shutdown_thread.start()

                # Wait for fut2 done callback (triggered when shutdown drains queue and cancels)
                assert fut2_settled.wait(timeout=5.0)
                assert fut2.cancelled() is True

                # Release the blocked worker so shutdown thread can complete
                release_worker.set()
                shutdown_thread.join(timeout=15.0)
                assert not shutdown_thread.is_alive()

                # Task 1 finished cleanly
                assert fut1.result(timeout=5.0) == "done"

            ledger.finalize_owner(owner_id)
            findings = policy.evaluate_owner(owner_id, ledger)
            tasks = ledger.get_tasks_for_owner(owner_id)

            assert len(tasks) == 2
            assert len(findings) == 2
            assert {f.task_id for f in findings} == {t.task_id for t in tasks}

            # Identify tasks by semantic state rather than list index
            t_completed = next(
                t for t in tasks
                if t.callable_state == CallableState.EXITED_RETURNED and t.future_state == FutureState.FINISHED
            )
            t_cancelled = next(
                t for t in tasks
                if t.future_state == FutureState.CANCELLED and t.callable_state == CallableState.NOT_STARTED
            )

            f_completed = next(f for f in findings if f.task_id == t_completed.task_id)
            assert f_completed.classification == FindingClassification.COMPLETED_BEFORE_FINALIZATION
            assert f_completed.severity == Severity.CLEAN

            f_cancelled = next(f for f in findings if f.task_id == t_cancelled.task_id)
            assert f_cancelled.classification == FindingClassification.CANCELLED_BEFORE_START
            assert f_cancelled.severity == Severity.CLEAN
            assert all(f.severity == Severity.CLEAN for f in findings)

        finally:
            release_worker.set()
            if shutdown_thread is not None and shutdown_thread.is_alive():
                shutdown_thread.join(timeout=15.0)
            executor.shutdown(wait=True)
