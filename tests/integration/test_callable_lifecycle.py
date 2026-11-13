from __future__ import annotations

import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import threading
import pytest

from pytest_state_guard.executor import install_patch, remove_patch
from pytest_state_guard.ledger import Ledger
from pytest_state_guard.models import (
    CallableState,
    EventType,
    OwnerState,
    OwnerType,
)
from pytest_state_guard.owners import (
    active_owner,
    get_current_owner,
    get_current_task_id,
)


@pytest.fixture
def ledger() -> Ledger:
    led = Ledger()
    install_patch(led)
    yield led
    remove_patch()


class TestCallableLifecycle:
    """Integration tests for worker callable execution, ContextVars, and provenance."""

    def test_callable_started_and_exited_recorded(self, ledger: Ledger) -> None:
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
            with ThreadPoolExecutor(max_workers=1) as executor:
                fut = executor.submit(lambda: 123)
                assert fut.result(timeout=5.0) == 123

        tasks = ledger.get_tasks_for_owner("owner:1")
        assert len(tasks) == 1
        task = tasks[0]
        assert task.callable_state == CallableState.EXITED_RETURNED

        events = ledger.get_events_for_task(task.task_id)
        types = [e.event_type for e in events]
        assert EventType.CALLABLE_STARTED in types
        assert EventType.CALLABLE_EXITED in types

    def test_callable_raising_records_exception_metadata(self, ledger: Ledger) -> None:
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
            with ThreadPoolExecutor(max_workers=1) as executor:
                def failing_job() -> None:
                    raise ValueError("invalid value provided")

                fut = executor.submit(failing_job)
                with pytest.raises(ValueError):
                    fut.result(timeout=5.0)

        tasks = ledger.get_tasks_for_owner("owner:1")
        assert len(tasks) == 1
        task = tasks[0]
        assert task.callable_state == CallableState.EXITED_RAISED
        assert task.exception_type == "ValueError"
        assert task.exception_message == "invalid value provided"

    def test_nested_child_task_inherits_owner_and_records_parent_id(self, ledger: Ledger) -> None:
        """A task submitted from inside a running worker callable inherits owner and records parent_task_id."""
        ledger.create_owner("owner:parent", OwnerType.FIXTURE_SETUP, "test.py::test_fix", "my_fix")

        with active_owner("owner:parent", OwnerType.FIXTURE_SETUP, "test.py::test_fix", "my_fix"):
            with ThreadPoolExecutor(max_workers=2) as executor:
                def parent_work() -> int:
                    # Inside worker: submit child task
                    child_fut = executor.submit(lambda: 99)
                    return child_fut.result(timeout=5.0)

                parent_fut = executor.submit(parent_work)
                assert parent_fut.result(timeout=5.0) == 99

        tasks = ledger.get_tasks_for_owner("owner:parent")
        assert len(tasks) == 2

        # One parent task, one child task
        parent_tasks = [t for t in tasks if t.parent_task_id is None]
        child_tasks = [t for t in tasks if t.parent_task_id is not None]

        assert len(parent_tasks) == 1
        assert len(child_tasks) == 1

        parent_task = parent_tasks[0]
        child_task = child_tasks[0]

        assert child_task.owner_id == "owner:parent"
        assert child_task.parent_task_id == parent_task.task_id

    def test_child_submission_after_owner_finalization(self, ledger: Ledger) -> None:
        """A running worker submitting a child task after owner finalization preserves parent
        provenance while created_seq > finalized_seq identifies post-lifetime submission.
        """
        ledger.create_owner("owner:delayed", OwnerType.FIXTURE_SETUP, "test.py::test_fix")

        worker_started = threading.Event()
        owner_finalized_signal = threading.Event()

        child_task_holder: list[str] = []

        with ThreadPoolExecutor(max_workers=2) as executor:
            with active_owner("owner:delayed", OwnerType.FIXTURE_SETUP, "test.py::test_fix"):
                def parent_worker() -> None:
                    # Signal that worker is running under active owner
                    worker_started.set()
                    # Wait until test signals that owner has finalized
                    assert owner_finalized_signal.wait(timeout=5.0)
                    # Now submit child task under inherited context
                    child_fut = executor.submit(lambda: "child_done")
                    assert child_fut.result(timeout=5.0) == "child_done"

                fut = executor.submit(parent_worker)

            # Wait for parent worker to begin executing
            assert worker_started.wait(timeout=5.0)

            # Owner finalizes while worker is still running
            finalized_owner = ledger.finalize_owner("owner:delayed")
            assert finalized_owner.state == OwnerState.FINALIZED
            finalized_seq = finalized_owner.finalized_seq
            assert finalized_seq is not None

            # Release worker to submit child task
            owner_finalized_signal.set()
            fut.result(timeout=5.0)

        tasks = ledger.get_tasks_for_owner("owner:delayed")
        assert len(tasks) == 2

        parent_task = next(t for t in tasks if t.parent_task_id is None)
        child_task = next(t for t in tasks if t.parent_task_id is not None)

        # Parent-task provenance is preserved
        assert child_task.parent_task_id == parent_task.task_id
        assert child_task.owner_id == "owner:delayed"

        # Explicit invariant: child task reservation committed AFTER owner finalization
        assert child_task.created_seq > finalized_seq
        # Parent task reservation committed BEFORE owner finalization
        assert parent_task.created_seq < finalized_seq

    def test_worker_thread_reuse_cleans_context(self, ledger: Ledger) -> None:
        """Worker thread reuse does not retain prior task or owner context."""
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        with ThreadPoolExecutor(max_workers=1) as executor:
            # 1. Run owned task
            with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
                fut1 = executor.submit(lambda: "job1")
                assert fut1.result(timeout=5.0) == "job1"

            # 2. Run unowned task on the exact same single worker thread
            def inspect_ambient_context() -> tuple[Any, Any]:
                return get_current_owner(), get_current_task_id()

            fut2 = executor.submit(inspect_ambient_context)
            ambient_owner, ambient_task_id = fut2.result(timeout=5.0)

            assert ambient_owner is None
            assert ambient_task_id is None

    def test_context_restoration_under_nested_and_exception_paths(self) -> None:
        """Verify ContextVar restoration under nesting and exceptions."""
        from pytest_state_guard.owners import OwnerContext, set_current_owner

        initial_owner = OwnerContext("owner:init", OwnerType.TEST_CALL, "node")
        token = set_current_owner(initial_owner)

        try:
            # Nested context manager raises
            with pytest.raises(RuntimeError, match="nested error"):
                with active_owner("owner:nested", OwnerType.FIXTURE_SETUP, "node"):
                    assert get_current_owner().owner_id == "owner:nested"  # type: ignore[union-attr]
                    raise RuntimeError("nested error")

            # Invariant: initial owner restored in finally
            assert get_current_owner() is initial_owner
        finally:
            set_current_owner(None)

    def test_commit_callable_started_failure_still_executes_user_code(self, ledger: Ledger) -> None:
        """If commit_callable_started raises, user callable still executes to completion."""
        from unittest.mock import patch

        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        with patch.object(ledger, "commit_callable_started", side_effect=RuntimeError("commit started error")):
            with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    fut = executor.submit(lambda: "user_code_completed")
                    assert fut.result(timeout=5.0) == "user_code_completed"

    def test_commit_callable_exited_failure_preserves_user_return_value(self, ledger: Ledger) -> None:
        """If commit_callable_exited raises in finally, user return value is still delivered."""
        from unittest.mock import patch

        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        with patch.object(ledger, "commit_callable_exited", side_effect=RuntimeError("commit exited error")):
            with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    fut = executor.submit(lambda: "result_preserved")
                    assert fut.result(timeout=5.0) == "result_preserved"

    def test_commit_callable_exited_failure_preserves_user_exception(self, ledger: Ledger) -> None:
        """If commit_callable_exited raises when user code failed, the original user exception is re-raised."""
        from unittest.mock import patch

        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")

        class PrimaryUserError(Exception):
            pass

        with patch.object(ledger, "commit_callable_exited", side_effect=RuntimeError("commit exited error")):
            with active_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1"):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    def failing_task() -> None:
                        raise PrimaryUserError("original user problem")

                    fut = executor.submit(failing_task)
                    with pytest.raises(PrimaryUserError, match="original user problem"):
                        fut.result(timeout=5.0)
