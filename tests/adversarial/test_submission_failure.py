from __future__ import annotations

import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import pytest

from pytest_state_guard.executor import install_patch, remove_patch
from pytest_state_guard.ledger import Ledger
from pytest_state_guard.models import (
    CallableState,
    EventType,
    FutureState,
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


class TestSubmissionFailure:
    """Adversarial tests for ThreadPoolExecutor.submit failure paths and unverified enqueue races."""

    def test_submit_raises_transitions_to_unverified(self, ledger: Ledger) -> None:
        """When submit raises, task transitions to SUBMIT_RAISED_UNVERIFIED and re-raises user error."""
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        # Mock underlying submit to simulate OS thread exhaustion error
        with patch(
            "pytest_state_guard.executor._REAL_ORIGINAL_SUBMIT",
            side_effect=RuntimeError("can't start new thread"),
        ):
            with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    with pytest.raises(RuntimeError, match="can't start new thread"):
                        executor.submit(lambda: "never_runs")

        tasks = ledger.get_tasks_for_owner("owner:1")
        assert len(tasks) == 1
        task = tasks[0]

        assert task.submission_state == SubmissionState.SUBMIT_RAISED_UNVERIFIED
        assert task.future_state == FutureState.UNKNOWN
        assert task.exception_type == "RuntimeError"

        events = ledger.get_events_for_task(task.task_id)
        types = [e.event_type for e in events]
        assert EventType.TASK_RESERVED in types
        assert EventType.SUBMIT_RAISED in types

    def test_work_executes_despite_submit_raising(self, ledger: Ledger) -> None:
        """Simulates CPython race where work was enqueued before thread start failed.
        When worker executes, it must commit EXECUTION_WITHOUT_RETURNED_FUTURE.
        """
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        work_item_callable = None

        def mock_submit_failing(self: ThreadPoolExecutor, fn: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal work_item_callable
            # Work item was prepared and passed to submit
            work_item_callable = fn
            # But thread adjustment fails after enqueueing
            raise RuntimeError("can't start new thread")

        # Install mock on underlying submit
        with patch("pytest_state_guard.executor._REAL_ORIGINAL_SUBMIT", side_effect=mock_submit_failing):
            with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    with pytest.raises(RuntimeError, match="can't start new thread"):
                        executor.submit(lambda: "executed_anyway")

        tasks = ledger.get_tasks_for_owner("owner:1")
        assert len(tasks) == 1
        task = tasks[0]
        assert task.submission_state == SubmissionState.SUBMIT_RAISED_UNVERIFIED

        # Now an existing worker thread picks up and runs the enqueued work_item_callable
        assert work_item_callable is not None
        result = work_item_callable()
        assert result == "executed_anyway"

        # Re-fetch task state from ledger
        updated_task = ledger.get_task(task.task_id)
        assert updated_task.callable_state == CallableState.EXITED_RETURNED
        assert updated_task.submission_state == SubmissionState.SUBMIT_RAISED_UNVERIFIED
        assert updated_task.future_state == FutureState.UNKNOWN

        # Verify EXECUTION_WITHOUT_RETURNED_FUTURE event is committed
        events = ledger.get_events_for_task(task.task_id)
        types = [e.event_type for e in events]
        assert EventType.TASK_RESERVED in types
        assert EventType.SUBMIT_RAISED in types
        assert EventType.CALLABLE_STARTED in types
        assert EventType.EXECUTION_WITHOUT_RETURNED_FUTURE in types
        assert EventType.CALLABLE_EXITED in types

    def test_submit_raised_and_commit_submit_raised_failure_preserves_original_exception(
        self, ledger: Ledger
    ) -> None:
        """If original submit() raises AND commit_submit_raised() also raises, caller receives original error."""
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        class OriginalSubmitError(Exception):
            pass

        with patch(
            "pytest_state_guard.executor._REAL_ORIGINAL_SUBMIT",
            side_effect=OriginalSubmitError("original submit failed"),
        ):
            with patch.object(
                ledger,
                "commit_submit_raised",
                side_effect=ValueError("ledger bookkeeping failed"),
            ):
                with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        # Caller must observe OriginalSubmitError, NOT ValueError
                        with pytest.raises(OriginalSubmitError, match="original submit failed"):
                            executor.submit(lambda: "never_runs")
