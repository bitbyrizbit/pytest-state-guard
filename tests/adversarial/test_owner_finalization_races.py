from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import pytest

from pytest_state_guard.executor import install_patch, remove_patch
from pytest_state_guard.ledger import Ledger
from pytest_state_guard.models import FindingClassification, OwnerType, Severity
from pytest_state_guard.owners import active_owner
from pytest_state_guard.policy import PolicyEngine


@pytest.fixture
def ledger() -> Ledger:
    led = Ledger()
    install_patch(led)
    yield led
    remove_patch()


class TestOwnerFinalizationRaces:
    """Adversarial concurrency tests evaluating real ThreadPoolExecutor races against owner finalization."""

    def test_race_exit_committed_after_finalization(self, ledger: Ledger) -> None:
        """Worker starts before finalization and blocks; owner finalizes; worker unblocks and exits.

        Policy must classify task as EXIT_COMMITTED_AFTER_FINALIZATION (VIOLATION).
        """
        ledger.create_owner("owner:race_exit", OwnerType.TEST_CALL, "test.py::test_exit_race")
        policy = PolicyEngine()

        worker_started = threading.Event()
        release_worker = threading.Event()

        with active_owner("owner:race_exit", OwnerType.TEST_CALL, "test.py::test_exit_race"):
            with ThreadPoolExecutor(max_workers=2) as executor:
                def slow_worker() -> str:
                    worker_started.set()
                    assert release_worker.wait(timeout=5.0)
                    return "done"

                fut = executor.submit(slow_worker)
                assert worker_started.wait(timeout=5.0)

                # Owner finalizes while worker is running
                ledger.finalize_owner("owner:race_exit")

                # Worker is released and commits its exit
                release_worker.set()
                assert fut.result(timeout=5.0) == "done"

        findings = policy.evaluate_owner("owner:race_exit", ledger)
        assert len(findings) == 1
        f = findings[0]
        assert f.classification == FindingClassification.EXIT_COMMITTED_AFTER_FINALIZATION
        assert f.severity == Severity.VIOLATION
        assert f.finding_id == f"finding:owner:race_exit:{f.task_id}"

    def test_race_execution_after_finalization(self, ledger: Ledger) -> None:
        """Worker task 2 is queued behind blocker task 1. Owner finalizes before task 2 starts.
        Task 2 then begins execution.

        Policy must classify task 2 as EXECUTION_AFTER_OWNER_FINALIZATION (VIOLATION).
        """
        ledger.create_owner("owner:race_start", OwnerType.TEST_CALL, "test.py::test_start_race")
        policy = PolicyEngine()

        blocker_running = threading.Event()
        release_blocker = threading.Event()
        task2_started = threading.Event()

        # Single worker forces task 2 to queue behind blocker
        with ThreadPoolExecutor(max_workers=1) as executor:
            with active_owner("owner:race_start", OwnerType.TEST_CALL, "test.py::test_start_race"):
                def blocker() -> None:
                    blocker_running.set()
                    assert release_blocker.wait(timeout=5.0)

                def queued_task() -> str:
                    task2_started.set()
                    return "queued_result"

                fut1 = executor.submit(blocker)
                assert blocker_running.wait(timeout=5.0)

                # Task 2 is submitted and returned, but queued
                fut2 = executor.submit(queued_task)

            # Finalize owner while task 2 is still queued and has not started
            ledger.finalize_owner("owner:race_start")

            # Release blocker, allowing task 2 to start after finalization
            release_blocker.set()
            assert task2_started.wait(timeout=5.0)
            assert fut2.result(timeout=5.0) == "queued_result"
            fut1.result(timeout=5.0)

        findings = policy.evaluate_owner("owner:race_start", ledger)
        # 2 tasks under owner: blocker and queued_task
        assert len(findings) == 2

        task2_findings = [f for f in findings if f.classification == FindingClassification.EXECUTION_AFTER_OWNER_FINALIZATION]
        assert len(task2_findings) == 1
        assert task2_findings[0].severity == Severity.VIOLATION

    def test_race_cancellation_vs_start(self, ledger: Ledger) -> None:
        """Verify cancellation classification when cancel succeeds before callable starts."""
        ledger.create_owner("owner:race_cancel", OwnerType.TEST_CALL, "test.py::test_cancel_race")
        policy = PolicyEngine()

        blocker_started = threading.Event()
        release_blocker = threading.Event()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with active_owner("owner:race_cancel", OwnerType.TEST_CALL, "test.py::test_cancel_race"):
                def blocker() -> None:
                    blocker_started.set()
                    assert release_blocker.wait(timeout=5.0)

                fut_block = executor.submit(blocker)
                assert blocker_started.wait(timeout=5.0)

                # Task 2 is queued behind blocker
                fut_cancel = executor.submit(lambda: "never_runs")

                # Cancel task 2 before it starts
                cancelled = fut_cancel.cancel()
                assert cancelled is True

            # Finalize owner after cancellation was committed
            ledger.finalize_owner("owner:race_cancel")
            release_blocker.set()
            fut_block.result(timeout=5.0)

        findings = policy.evaluate_owner("owner:race_cancel", ledger)
        cancel_findings = [f for f in findings if f.classification == FindingClassification.CANCELLED_BEFORE_START]
        assert len(cancel_findings) == 1
        assert cancel_findings[0].severity == Severity.CLEAN

    def test_race_child_submission_after_parent_owner_finalized(self, ledger: Ledger) -> None:
        """Parent task is executing under owner. Owner finalizes. Parent submits child task.

        Child task must be classified as POST_LIFETIME_SUBMISSION (VIOLATION).
        """
        ledger.create_owner("owner:race_child", OwnerType.TEST_CALL, "test.py::test_child_race")
        policy = PolicyEngine()

        parent_started = threading.Event()
        release_parent = threading.Event()
        child_submitted = threading.Event()

        with ThreadPoolExecutor(max_workers=4) as executor:
            with active_owner("owner:race_child", OwnerType.TEST_CALL, "test.py::test_child_race"):
                def parent_work() -> None:
                    parent_started.set()
                    assert release_parent.wait(timeout=5.0)
                    # Submits child task after owner was finalized
                    child_fut = executor.submit(lambda: "child_done")
                    child_submitted.set()
                    assert child_fut.result(timeout=5.0) == "child_done"

                parent_fut = executor.submit(parent_work)
                assert parent_started.wait(timeout=5.0)

            # Owner finalizes while parent is executing
            ledger.finalize_owner("owner:race_child")

            # Release parent to submit child task
            release_parent.set()
            assert child_submitted.wait(timeout=5.0)
            parent_fut.result(timeout=5.0)

        findings = policy.evaluate_owner("owner:race_child", ledger)
        post_sub_findings = [f for f in findings if f.classification == FindingClassification.POST_LIFETIME_SUBMISSION]
        assert len(post_sub_findings) == 1
        assert post_sub_findings[0].severity == Severity.VIOLATION

    def test_race_evolving_classification_under_concurrency(self, ledger: Ledger) -> None:
        """Demonstrates that a task running at initial evaluation (CALLABLE_NOT_EXITED)
        evolves to EXIT_COMMITTED_AFTER_FINALIZATION under the same stable finding_id
        once the worker finishes.
        """
        ledger.create_owner("owner:race_evolve", OwnerType.TEST_CALL, "test.py::test_evolve_race")
        policy = PolicyEngine()

        worker_started = threading.Event()
        release_worker = threading.Event()

        with active_owner("owner:race_evolve", OwnerType.TEST_CALL, "test.py::test_evolve_race"):
            with ThreadPoolExecutor(max_workers=2) as executor:
                def worker() -> str:
                    worker_started.set()
                    assert release_worker.wait(timeout=5.0)
                    return "evolved"

                fut = executor.submit(worker)
                assert worker_started.wait(timeout=5.0)

                # Owner finalizes while worker is executing
                ledger.finalize_owner("owner:race_evolve")

                # Snapshot 1: evaluated while worker is running
                f_running = policy.evaluate_owner("owner:race_evolve", ledger)[0]
                assert f_running.classification == FindingClassification.CALLABLE_NOT_EXITED
                assert f_running.severity == Severity.VIOLATION
                stable_id = f_running.finding_id

                # Unblock worker to exit
                release_worker.set()
                assert fut.result(timeout=5.0) == "evolved"

                # Snapshot 2: evaluated after worker completed
                f_completed = policy.evaluate_owner("owner:race_evolve", ledger)[0]
                assert f_completed.classification == FindingClassification.EXIT_COMMITTED_AFTER_FINALIZATION
                assert f_completed.severity == Severity.VIOLATION
                assert f_completed.finding_id == stable_id
