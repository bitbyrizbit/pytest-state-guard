from __future__ import annotations

import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import pytest

from pytest_state_guard.executor import (
    _REAL_ORIGINAL_SUBMIT,
    install_patch,
    is_patched,
    remove_patch,
)
from pytest_state_guard.ledger import Ledger
from pytest_state_guard.models import (
    EventType,
    OwnerType,
    SubmissionState,
)
from pytest_state_guard.owners import active_owner


@pytest.fixture
def ledger() -> Ledger:
    led = Ledger()
    install_patch(led)
    yield led
    remove_patch()


class TestExecutorSubmission:
    """Integration tests for ThreadPoolExecutor.submit interception and lifecycle."""

    def test_unowned_submission_bypasses_tracking(self, ledger: Ledger) -> None:
        """Unowned submissions pass through unmodified with no task records or events."""
        with ThreadPoolExecutor(max_workers=2) as executor:
            fut = executor.submit(lambda x: x * 2, 21)
            assert fut.result(timeout=5.0) == 42

        # Invariants: ledger is completely empty
        assert len(ledger.get_events()) == 0
        assert len(ledger.get_all_tasks()) == 0

    def test_owned_submission_records_reserved_and_returned(self, ledger: Ledger) -> None:
        """Owned submissions record reservation and successful return with stable owner."""
        ledger.create_owner("owner:test:1", OwnerType.TEST_CALL, "tests/test_a.py::test_case")

        with active_owner("owner:test:1", OwnerType.TEST_CALL, "tests/test_a.py::test_case"):
            with ThreadPoolExecutor(max_workers=2) as executor:
                fut = executor.submit(lambda: "success")
                result = fut.result(timeout=5.0)

        assert result == "success"

        tasks = ledger.get_tasks_for_owner("owner:test:1")
        assert len(tasks) == 1
        task = tasks[0]
        assert task.owner_id == "owner:test:1"
        assert task.submission_state == SubmissionState.SUBMIT_RETURNED

        events = ledger.get_events_for_owner("owner:test:1")
        event_types = [e.event_type for e in events]
        assert EventType.TASK_RESERVED in event_types
        assert EventType.SUBMIT_RETURNED in event_types

    def test_user_return_value_preserved(self, ledger: Ledger) -> None:
        """Complex user return value types (tuples, dicts, instances) are preserved identically."""
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        complex_obj = {"key": (1, [2, 3], {"nested": True})}

        with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
            with ThreadPoolExecutor(max_workers=1) as executor:
                fut = executor.submit(lambda: complex_obj)
                res = fut.result(timeout=5.0)

        assert res is complex_obj

    def test_user_exception_preserved(self, ledger: Ledger) -> None:
        """Original user exceptions and causes are preserved in Future without modification."""
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        class CustomUserError(Exception):
            pass

        with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
            with ThreadPoolExecutor(max_workers=1) as executor:
                fut = executor.submit(lambda: (_ for _ in ()).throw(CustomUserError("intentional failure")))
                with pytest.raises(CustomUserError, match="intentional failure") as exc_info:
                    fut.result(timeout=5.0)

        assert exc_info.type is CustomUserError
        assert "intentional failure" in str(exc_info.value)

    def test_buggy_exception_str_does_not_mask_exception(self, ledger: Ledger) -> None:
        """A user exception whose __str__() raises must not mask the original exception."""
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        class BuggyStrError(Exception):
            def __str__(self) -> str:
                raise RuntimeError("buggy __str__ failed")

        with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
            with ThreadPoolExecutor(max_workers=1) as executor:
                def work() -> None:
                    raise BuggyStrError("original payload")

                fut = executor.submit(work)
                with pytest.raises(BuggyStrError):
                    fut.result(timeout=5.0)

        # Exception type is recorded, message fallback is used safely
        tasks = ledger.get_tasks_for_owner("owner:1")
        assert len(tasks) == 1
        assert tasks[0].exception_type == "TestExecutorSubmission.test_buggy_exception_str_does_not_mask_exception.<locals>.BuggyStrError"
        assert tasks[0].exception_message == "<exception formatting raised>"

    def test_patch_idempotency_and_removal(self, ledger: Ledger) -> None:
        """Calling install_patch multiple times with the same ledger is safe and idempotent."""
        install_patch(ledger)
        assert is_patched()

        # Second install with same ledger is a no-op
        install_patch(ledger)
        assert is_patched()

        # Install with different ledger raises RuntimeError
        other_ledger = Ledger()
        with pytest.raises(RuntimeError, match="already active with a different ledger"):
            install_patch(other_ledger)

        # Remove patch restores original submit
        remove_patch()
        assert not is_patched()

        # Second remove is safe no-op
        remove_patch()
        assert not is_patched()

    def test_safe_removal_with_foreign_wrapper(self, ledger: Ledger) -> None:
        """If another library wrapped submit after State Guard, removal does not break foreign wrapper."""
        original_sg_submit = ThreadPoolExecutor.submit

        def foreign_wrapper(self: ThreadPoolExecutor, fn: Any, *args: Any, **kwargs: Any) -> Any:
            return original_sg_submit(self, fn, *args, **kwargs)

        # Foreign library patches submit on top of State Guard
        ThreadPoolExecutor.submit = foreign_wrapper  # type: ignore[method-assign]
        try:
            # When State Guard removes patch, it must NOT reset to _ORIGINAL_SUBMIT
            remove_patch()
            assert ThreadPoolExecutor.submit is foreign_wrapper
            assert not is_patched()
        finally:
            # Clean up foreign patch and ensure ThreadPoolExecutor is restored
            ThreadPoolExecutor.submit = _REAL_ORIGINAL_SUBMIT  # type: ignore[method-assign]
            remove_patch()

    def test_reserve_task_failure_fails_open_to_uninstrumented_submit(self, ledger: Ledger) -> None:
        """If ledger.reserve_task fails, original submit proceeds uninstrumented and delivers Future."""
        from unittest.mock import patch

        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        with patch.object(ledger, "reserve_task", side_effect=RuntimeError("ledger reservation error")):
            with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    fut = executor.submit(lambda: "uninstrumented_success")
                    # User receives Future and its result without error
                    assert fut.result(timeout=5.0) == "uninstrumented_success"

        # No task was committed to ledger
        assert len(ledger.get_tasks_for_owner("owner:1")) == 0

    def test_commit_submit_returned_failure_still_delivers_future(self, ledger: Ledger) -> None:
        """If ledger.commit_submit_returned fails, the Future is still delivered to caller."""
        from unittest.mock import patch

        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        with patch.object(ledger, "commit_submit_returned", side_effect=RuntimeError("commit returned error")):
            with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    fut = executor.submit(lambda: "delivered_ok")
                    assert fut.result(timeout=5.0) == "delivered_ok"

    def test_in_flight_submission_during_patch_removal(self, ledger: Ledger) -> None:
        """An in-flight submission completes cleanly even if remove_patch() is called while executing."""
        import threading

        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        worker_started = threading.Event()
        release_worker = threading.Event()

        with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
            with ThreadPoolExecutor(max_workers=2) as executor:
                def in_flight_job() -> str:
                    worker_started.set()
                    assert release_worker.wait(timeout=5.0)
                    return "in_flight_success"

                fut = executor.submit(in_flight_job)
                # Wait until worker begins execution
                assert worker_started.wait(timeout=5.0)

                # Patch is removed while submission is active in worker
                remove_patch()
                assert not is_patched()

                # Release worker
                release_worker.set()
                assert fut.result(timeout=5.0) == "in_flight_success"

                # Subsequent submit passes through uninstrumented
                fut2 = executor.submit(lambda: "pass_through")
                assert fut2.result(timeout=5.0) == "pass_through"

    def test_concurrent_install_and_remove_patch(self, ledger: Ledger) -> None:
        """Concurrent calls to install_patch and remove_patch must not deadlock or corrupt state."""
        import threading

        num_threads = 8
        iterations = 30
        barrier = threading.Barrier(num_threads)
        errors: list[Exception] = []

        def worker(thread_idx: int) -> None:
            try:
                barrier.wait(timeout=5.0)
                for _ in range(iterations):
                    if thread_idx % 2 == 0:
                        install_patch(ledger)
                    else:
                        remove_patch()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert not errors
        # Clean final state
        remove_patch()
        assert not is_patched()
