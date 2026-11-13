from __future__ import annotations

import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import threading
import pytest

from pytest_state_guard.executor import install_patch, remove_patch
from pytest_state_guard.ledger import Ledger
from pytest_state_guard.models import (
    CallableState,
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


class TestCallbackRaces:
    """Adversarial race tests: callback timing vs submit return and cancellation vs callable start."""

    def test_cancellation_racing_with_callable_start_while_pending(self, ledger: Ledger) -> None:
        """When cancel succeeds while pending, future_state is CANCELLED and callable remains NOT_STARTED."""
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        worker_holding = threading.Event()
        release_worker = threading.Event()

        with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
            with ThreadPoolExecutor(max_workers=1) as executor:
                # 1. Occupy single worker
                def blocker() -> None:
                    worker_holding.set()
                    assert release_worker.wait(timeout=5.0)

                fut1 = executor.submit(blocker)
                assert worker_holding.wait(timeout=5.0)

                # 2. Submit second task while worker is occupied
                fut2 = executor.submit(lambda: "never_runs")

                # 3. Cancel while pending in queue
                cancelled = fut2.cancel()
                assert cancelled is True

                # Release blocker
                release_worker.set()
                fut1.result(timeout=5.0)

        tasks = ledger.get_tasks_for_owner("owner:1")
        assert len(tasks) == 2

        cancelled_task = tasks[1]
        assert cancelled_task.future_state == FutureState.CANCELLED
        # Crucial invariant: callable never started
        assert cancelled_task.callable_state == CallableState.NOT_STARTED

    def test_cancellation_attempt_after_callable_starts(self, ledger: Ledger) -> None:
        """When cancel is attempted after callable begins running, cancel fails;
        callable completes normally and future transitions to FINISHED.
        """
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        callable_started = threading.Event()
        release_callable = threading.Event()

        with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
            with ThreadPoolExecutor(max_workers=1) as executor:
                def long_job() -> str:
                    callable_started.set()
                    assert release_callable.wait(timeout=5.0)
                    return "finished_normally"

                fut = executor.submit(long_job)
                # Wait until callable is actively running inside worker
                assert callable_started.wait(timeout=5.0)

                # Attempt to cancel running task - must fail
                cancelled = fut.cancel()
                assert cancelled is False

                # Allow callable to finish
                release_callable.set()
                assert fut.result(timeout=5.0) == "finished_normally"

        tasks = ledger.get_tasks_for_owner("owner:1")
        assert len(tasks) == 1
        task = tasks[0]

        assert task.submission_state == SubmissionState.SUBMIT_RETURNED
        assert task.callable_state == CallableState.EXITED_RETURNED
        assert task.future_state == FutureState.FINISHED
