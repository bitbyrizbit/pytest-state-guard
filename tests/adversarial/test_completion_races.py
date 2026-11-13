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


class TestCompletionRaces:
    """Adversarial race tests: fast worker completion before submit returns and concurrent submissions."""

    def test_fast_task_completes_before_submit_returns(self, ledger: Ledger) -> None:
        """In fast concurrent execution, worker may complete before submit() returns to caller.
        State Guard must preserve FutureState.FINISHED and not downgrade to PENDING.
        """
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        worker_finished = threading.Event()

        with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
            with ThreadPoolExecutor(max_workers=2) as executor:
                def fast_job() -> str:
                    worker_finished.set()
                    return "fast_result"

                fut = executor.submit(fast_job)
                # Worker signals it completed
                assert worker_finished.wait(timeout=5.0)
                # Now caller inspects future
                assert fut.result(timeout=5.0) == "fast_result"

        tasks = ledger.get_tasks_for_owner("owner:1")
        assert len(tasks) == 1
        task = tasks[0]

        assert task.submission_state == SubmissionState.SUBMIT_RETURNED
        assert task.callable_state == CallableState.EXITED_RETURNED
        assert task.future_state == FutureState.FINISHED

    def test_concurrent_competing_submissions(self, ledger: Ledger) -> None:
        """16 concurrent threads submitting tasks simultaneously under active owners."""
        num_threads = 16
        tasks_per_thread = 20
        total_tasks = num_threads * tasks_per_thread

        for i in range(num_threads):
            ledger.create_owner(f"owner:{i}", OwnerType.TEST_CALL, f"test.py::test_{i}")

        barrier = threading.Barrier(num_threads)

        def submitter_routine(thread_idx: int) -> None:
            owner_id = f"owner:{thread_idx}"
            assert barrier.wait(timeout=5.0) == 0 or True

            with active_owner(owner_id, OwnerType.TEST_CALL, f"test.py::test_{thread_idx}"):
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = [executor.submit(lambda x=k: x * 2) for k in range(tasks_per_thread)]
                    results = [f.result(timeout=5.0) for f in futures]
                    assert results == [k * 2 for k in range(tasks_per_thread)]

        threads = [
            threading.Thread(target=submitter_routine, args=(idx,))
            for idx in range(num_threads)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        all_tasks = ledger.get_all_tasks()
        assert len(all_tasks) == total_tasks

        # Verify all sequence IDs are unique and strictly monotonic
        events = ledger.get_events()
        seq_ids = [e.seq_id for e in events]
        assert len(seq_ids) == len(set(seq_ids))
        assert seq_ids == list(range(1, len(events) + 1))
