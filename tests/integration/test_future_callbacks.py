from __future__ import annotations

import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import threading
import pytest

from pytest_state_guard.executor import install_patch, remove_patch
from pytest_state_guard.ledger import Ledger
from pytest_state_guard.models import (
    EventType,
    FutureState,
    OwnerType,
)
from pytest_state_guard.owners import active_owner


@pytest.fixture
def ledger() -> Ledger:
    led = Ledger()
    install_patch(led)
    yield led
    remove_patch()


class TestFutureCallbacks:
    """Integration tests covering Future done callback observations and terminal states."""

    def test_future_done_callback_records_finished(self, ledger: Ledger) -> None:
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
            with ThreadPoolExecutor(max_workers=1) as executor:
                fut = executor.submit(lambda: "finished_val")
                assert fut.result(timeout=5.0) == "finished_val"

        tasks = ledger.get_tasks_for_owner("owner:1")
        assert len(tasks) == 1
        assert tasks[0].future_state == FutureState.FINISHED

        events = ledger.get_events_for_task(tasks[0].task_id)
        assert any(e.event_type == EventType.FUTURE_DONE_CALLBACK_OBSERVED for e in events)

    def test_future_cancellation_records_cancelled(self, ledger: Ledger) -> None:
        """When a queued task is cancelled before execution, FutureState transitions to CANCELLED."""
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        worker_blocked = threading.Event()
        release_worker = threading.Event()

        with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
            with ThreadPoolExecutor(max_workers=1) as executor:
                # 1. Block the single worker thread
                def blocker() -> None:
                    worker_blocked.set()
                    assert release_worker.wait(timeout=5.0)

                fut1 = executor.submit(blocker)
                assert worker_blocked.wait(timeout=5.0)

                # 2. Submit second task while worker is occupied
                fut2 = executor.submit(lambda: "should_not_run")

                # 3. Cancel second task while it is pending in queue
                cancelled = fut2.cancel()
                assert cancelled is True
                assert fut2.cancelled() is True

                # Release blocker
                release_worker.set()
                fut1.result(timeout=5.0)

        tasks = ledger.get_tasks_for_owner("owner:1")
        assert len(tasks) == 2

        task2 = tasks[1]
        assert task2.future_state == FutureState.CANCELLED

    def test_immediate_done_callback_execution(self, ledger: Ledger) -> None:
        """If a Future finishes before callback registration, callback runs immediately."""
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
            with ThreadPoolExecutor(max_workers=1) as executor:
                fut = executor.submit(lambda: 42)
                # Ensure future is done
                assert fut.result(timeout=5.0) == 42

        tasks = ledger.get_tasks_for_owner("owner:1")
        assert len(tasks) == 1
        assert tasks[0].future_state == FutureState.FINISHED

    def test_commit_future_done_failure_does_not_affect_future_result(self, ledger: Ledger) -> None:
        """If ledger.commit_future_done raises inside callback, future result remains delivered."""
        from unittest.mock import patch

        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        with patch.object(ledger, "commit_future_done", side_effect=RuntimeError("callback commit failed")):
            with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    fut = executor.submit(lambda: "future_result_safe")
                    assert fut.result(timeout=5.0) == "future_result_safe"

    def test_commit_future_done_failure_does_not_affect_future_exception(self, ledger: Ledger) -> None:
        """If ledger.commit_future_done raises inside callback, future exception remains preserved."""
        from unittest.mock import patch

        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        class JobError(Exception):
            pass

        with patch.object(ledger, "commit_future_done", side_effect=RuntimeError("callback commit failed")):
            with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    def bad_job() -> None:
                        raise JobError("job broke")

                    fut = executor.submit(bad_job)
                    with pytest.raises(JobError, match="job broke"):
                        fut.result(timeout=5.0)
