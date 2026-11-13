from __future__ import annotations

import pytest

from pytest_state_guard.ledger import Ledger
from pytest_state_guard.models import (
    CallableState,
    EventType,
    FutureState,
    InvalidTransitionError,
    OwnerType,
    SubmissionState,
)


class TestTaskStateTransitions:
    """Tests covering independent state dimensions: submission, callable, and future."""

    @pytest.fixture
    def ledger(self) -> Ledger:
        led = Ledger()
        led.create_owner("owner:1", OwnerType.TEST_CALL, "test_file.py::test_case")
        return led

    def test_normal_successful_lifecycle(self, ledger: Ledger) -> None:
        task = ledger.reserve_task("task:1", "owner:1")
        assert task.submission_state == SubmissionState.RESERVED
        assert task.callable_state == CallableState.NOT_STARTED
        assert task.future_state == FutureState.UNKNOWN

        task = ledger.commit_submit_returned("task:1")
        assert task.submission_state == SubmissionState.SUBMIT_RETURNED
        assert task.future_state == FutureState.PENDING

        task = ledger.commit_callable_started("task:1")
        assert task.callable_state == CallableState.RUNNING

        task = ledger.commit_callable_exited("task:1", CallableState.EXITED_RETURNED)
        assert task.callable_state == CallableState.EXITED_RETURNED

        task = ledger.commit_future_done("task:1", FutureState.FINISHED)
        assert task.future_state == FutureState.FINISHED

    def test_callable_exits_with_exception(self, ledger: Ledger) -> None:
        ledger.reserve_task("task:1", "owner:1")
        ledger.commit_submit_returned("task:1")
        ledger.commit_callable_started("task:1")

        task = ledger.commit_callable_exited(
            "task:1",
            CallableState.EXITED_RAISED,
            exception_type="ValueError",
            exception_message="Task failed intentionally",
        )
        assert task.callable_state == CallableState.EXITED_RAISED
        assert task.exception_type == "ValueError"
        assert task.exception_message == "Task failed intentionally"

        task = ledger.commit_future_done("task:1", FutureState.FINISHED)
        assert task.future_state == FutureState.FINISHED

    def test_callable_starts_and_completes_before_submit_returned(self, ledger: Ledger) -> None:
        """In fast concurrent execution, worker may run before submit returns."""
        task = ledger.reserve_task("task:1", "owner:1")

        # Callable starts and completes while submit is still pending
        task = ledger.commit_callable_started("task:1")
        assert task.callable_state == CallableState.RUNNING
        assert task.submission_state == SubmissionState.RESERVED

        task = ledger.commit_callable_exited("task:1", CallableState.EXITED_RETURNED)
        assert task.callable_state == CallableState.EXITED_RETURNED

        # Future callback might even run before submit returns
        task = ledger.commit_future_done("task:1", FutureState.FINISHED)
        assert task.future_state == FutureState.FINISHED

        # Now submit returns
        task = ledger.commit_submit_returned("task:1")
        assert task.submission_state == SubmissionState.SUBMIT_RETURNED
        # FutureState is preserved as FINISHED, not downgraded to PENDING
        assert task.future_state == FutureState.FINISHED

    def test_submission_raises_unverified_and_subsequent_execution(self, ledger: Ledger) -> None:
        """When submit raises, it transitions to SUBMIT_RAISED_UNVERIFIED.
        Work item may still execute if CPython enqueued it prior to thread start failure.
        """
        task = ledger.reserve_task("task:1", "owner:1")

        task = ledger.commit_submit_raised(
            "task:1",
            exception_type="RuntimeError",
            exception_message="can't start new thread",
        )
        assert task.submission_state == SubmissionState.SUBMIT_RAISED_UNVERIFIED
        assert task.future_state == FutureState.UNKNOWN
        assert task.exception_type == "RuntimeError"

        # Worker thread executes task later
        task = ledger.commit_callable_started("task:1")
        assert task.callable_state == CallableState.RUNNING

        events = ledger.get_events_for_task("task:1")
        event_types = [e.event_type for e in events]
        assert EventType.TASK_RESERVED in event_types
        assert EventType.SUBMIT_RAISED in event_types
        assert EventType.CALLABLE_STARTED in event_types
        assert EventType.EXECUTION_WITHOUT_RETURNED_FUTURE in event_types

        task = ledger.commit_callable_exited("task:1", CallableState.EXITED_RETURNED)
        assert task.callable_state == CallableState.EXITED_RETURNED
        # Future state remains UNKNOWN because caller never received a Future
        assert task.future_state == FutureState.UNKNOWN

    def test_future_cancelled_before_callable_starts(self, ledger: Ledger) -> None:
        ledger.reserve_task("task:1", "owner:1")
        ledger.commit_submit_returned("task:1")

        task = ledger.commit_future_done("task:1", FutureState.CANCELLED)
        assert task.future_state == FutureState.CANCELLED
        assert task.callable_state == CallableState.NOT_STARTED

    def test_invalid_submission_state_transitions(self, ledger: Ledger) -> None:
        ledger.reserve_task("task:1", "owner:1")
        ledger.commit_submit_returned("task:1")

        # Cannot commit submit_returned again
        with pytest.raises(InvalidTransitionError):
            ledger.commit_submit_returned("task:1")

        # Cannot commit submit_raised after submit_returned
        with pytest.raises(InvalidTransitionError):
            ledger.commit_submit_raised("task:1")

    def test_invalid_callable_state_transitions(self, ledger: Ledger) -> None:
        ledger.reserve_task("task:1", "owner:1")

        # Cannot exit before starting
        with pytest.raises(InvalidTransitionError):
            ledger.commit_callable_exited("task:1", CallableState.EXITED_RETURNED)

        ledger.commit_callable_started("task:1")
        # Cannot start again while RUNNING
        with pytest.raises(InvalidTransitionError):
            ledger.commit_callable_started("task:1")

        # Invalid exit outcome
        with pytest.raises(InvalidTransitionError):
            ledger.commit_callable_exited("task:1", CallableState.RUNNING)

        ledger.commit_callable_exited("task:1", CallableState.EXITED_RETURNED)
        # Cannot exit again after already exited
        with pytest.raises(InvalidTransitionError):
            ledger.commit_callable_exited("task:1", CallableState.EXITED_RETURNED)

    def test_invalid_future_terminal_state(self, ledger: Ledger) -> None:
        ledger.reserve_task("task:1", "owner:1")
        with pytest.raises(InvalidTransitionError):
            ledger.commit_future_done("task:1", FutureState.PENDING)
        with pytest.raises(InvalidTransitionError):
            ledger.commit_future_done("task:1", FutureState.UNKNOWN)

    def test_future_terminal_state_irreversibility(self, ledger: Ledger) -> None:
        """Terminal states FINISHED and CANCELLED are strictly irreversible.
        Duplicate and contradictory transitions must be rejected without mutating state
        or consuming sequence IDs.
        """
        # Case 1: task ends in FINISHED
        ledger.reserve_task("task:finished", "owner:1")
        ledger.commit_submit_returned("task:finished")
        task = ledger.commit_future_done("task:finished", FutureState.FINISHED)
        assert task.future_state == FutureState.FINISHED

        events_count = len(ledger.get_events())

        # Attempt FINISHED -> FINISHED (duplicate)
        with pytest.raises(InvalidTransitionError, match="already in terminal state 'FINISHED'"):
            ledger.commit_future_done("task:finished", FutureState.FINISHED)

        # Invariants: no event appended, state untouched
        assert len(ledger.get_events()) == events_count
        assert ledger.get_task("task:finished").future_state == FutureState.FINISHED

        # Attempt FINISHED -> CANCELLED (contradictory)
        with pytest.raises(InvalidTransitionError, match="already in terminal state 'FINISHED'"):
            ledger.commit_future_done("task:finished", FutureState.CANCELLED)

        assert len(ledger.get_events()) == events_count
        assert ledger.get_task("task:finished").future_state == FutureState.FINISHED

        # Case 2: task ends in CANCELLED
        ledger.reserve_task("task:cancelled", "owner:1")
        ledger.commit_submit_returned("task:cancelled")
        task = ledger.commit_future_done("task:cancelled", FutureState.CANCELLED)
        assert task.future_state == FutureState.CANCELLED

        events_count = len(ledger.get_events())

        # Attempt CANCELLED -> CANCELLED (duplicate)
        with pytest.raises(InvalidTransitionError, match="already in terminal state 'CANCELLED'"):
            ledger.commit_future_done("task:cancelled", FutureState.CANCELLED)

        assert len(ledger.get_events()) == events_count
        assert ledger.get_task("task:cancelled").future_state == FutureState.CANCELLED

        # Attempt CANCELLED -> FINISHED (contradictory)
        with pytest.raises(InvalidTransitionError, match="already in terminal state 'CANCELLED'"):
            ledger.commit_future_done("task:cancelled", FutureState.FINISHED)

        assert len(ledger.get_events()) == events_count
        assert ledger.get_task("task:cancelled").future_state == FutureState.CANCELLED

        # Sequence counter is not consumed; next valid event receives strictly gapless sequence
        next_task = ledger.reserve_task("task:next", "owner:1")
        assert next_task.created_seq == events_count + 1
