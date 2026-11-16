from __future__ import annotations

import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

import pytest_state_guard.plugin as plugin
from pytest_state_guard.executor import (
    _REAL_ORIGINAL_SUBMIT,
    _state_guard_submit,
    get_active_ledger,
    install_patch,
    is_patched,
    remove_patch,
)
from pytest_state_guard.ledger import Ledger
from pytest_state_guard.models import OwnerState, OwnerType


@pytest.fixture(autouse=True)
def ensure_clean_lifecycle():
    """Ensure plugin lifecycle and executor patch are clean before and after each test."""
    # Pre-test cleanup
    if is_patched():
        try:
            remove_patch()
        except Exception:
            pass
    if getattr(ThreadPoolExecutor, "submit", None) is not _REAL_ORIGINAL_SUBMIT:
        ThreadPoolExecutor.submit = _REAL_ORIGINAL_SUBMIT

    with plugin._SESSION_LOCK:
        plugin._LIFECYCLE_STATE = plugin.PluginLifecycle.IDLE
        plugin._POISON_REASON = None
        plugin._ACTIVE_SESSION = None
        plugin._LAST_SESSION_LEDGER = None

    yield

    # Post-test cleanup
    if is_patched():
        try:
            remove_patch()
        except Exception:
            pass
    if getattr(ThreadPoolExecutor, "submit", None) is not _REAL_ORIGINAL_SUBMIT:
        ThreadPoolExecutor.submit = _REAL_ORIGINAL_SUBMIT

    with plugin._SESSION_LOCK:
        plugin._LIFECYCLE_STATE = plugin.PluginLifecycle.IDLE
        plugin._POISON_REASON = None
        plugin._ACTIVE_SESSION = None
        plugin._LAST_SESSION_LEDGER = None


class TestTestItemOwnership:
    """Integration tests for test-item lifecycle ownership and session lifecycle safety."""

    def test_test_call_ownership_lifecycle(self) -> None:
        """Verifies normal test-call owner creation during call and finalization at teardown."""
        code = """
import pytest
from concurrent.futures import ThreadPoolExecutor

def test_work_inside_call():
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "test_call_payload")
        assert f.result() == "test_call_payload"
"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "test_call.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(code)
            ret = pytest.main([p, "-q", f"--rootdir={td}"], plugins=[plugin])
            assert ret == 0

        ledger = plugin.get_last_session_ledger()
        assert ledger is not None

        test_owners = [
            o for o in ledger.get_all_owners() if o.owner_type == OwnerType.TEST_CALL
        ]
        assert len(test_owners) == 1
        owner = test_owners[0]
        assert owner.state == OwnerState.FINALIZED
        assert owner.finalized_seq is not None

        tasks = ledger.get_tasks_for_owner(owner.owner_id)
        assert len(tasks) == 1
        assert tasks[0].created_seq < owner.finalized_seq

    def test_test_call_exception_preserves_finalization(self) -> None:
        """Verifies test assertion failure resets ContextVar and finalizes test owner."""
        code = """
import pytest
from concurrent.futures import ThreadPoolExecutor

def test_failing_assertion():
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: 42)
        assert f.result() == 42
    assert False, "forced test failure"
"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "test_item_fail.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(code)
            ret = pytest.main([p, "-q", f"--rootdir={td}"], plugins=[plugin])
            assert ret == 1

        ledger = plugin.get_last_session_ledger()
        assert ledger is not None

        test_owners = [
            o for o in ledger.get_all_owners() if o.owner_type == OwnerType.TEST_CALL
        ]
        assert len(test_owners) == 1
        owner = test_owners[0]
        assert owner.state == OwnerState.FINALIZED
        assert owner.finalized_seq is not None

        tasks = ledger.get_tasks_for_owner(owner.owner_id)
        assert len(tasks) == 1

    def test_test_call_skip_preserves_finalization(self) -> None:
        """Verifies pytest.skip() during call resets ContextVar and finalizes test owner."""
        code = """
import pytest
from concurrent.futures import ThreadPoolExecutor

def test_skipped_call():
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "skip_payload")
        assert f.result() == "skip_payload"
    pytest.skip("skipping during call")
"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "test_skip.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(code)
            ret = pytest.main([p, "-q", f"--rootdir={td}"], plugins=[plugin])
            assert ret == 0

        ledger = plugin.get_last_session_ledger()
        assert ledger is not None

        test_owners = [
            o for o in ledger.get_all_owners() if o.owner_type == OwnerType.TEST_CALL
        ]
        assert len(test_owners) == 1
        owner = test_owners[0]
        assert owner.state == OwnerState.FINALIZED
        assert owner.finalized_seq is not None

    def test_test_teardown_finalization_ordering(self) -> None:
        """Verifies test-owner finalization executes after fixture finalizers have completed."""
        code = """
import pytest
from concurrent.futures import ThreadPoolExecutor

@pytest.fixture
def order_fixture():
    yield "ready"

def test_order(order_fixture):
    pass
"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "test_ord.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(code)
            ret = pytest.main([p, "-q", f"--rootdir={td}"], plugins=[plugin])
            assert ret == 0

        ledger = plugin.get_last_session_ledger()
        assert ledger is not None

        fix_owner = next(
            o for o in ledger.get_all_owners() if o.fixture_name == "order_fixture"
        )
        test_owner = next(
            o for o in ledger.get_all_owners() if o.owner_type == OwnerType.TEST_CALL
        )

        assert fix_owner.finalized_seq is not None
        assert test_owner.finalized_seq is not None
        # Fixture finalizer runs before test-owner finalization resumes in teardown
        assert fix_owner.finalized_seq < test_owner.finalized_seq

    def test_test_teardown_submissions_untracked(self) -> None:
        """Verifies tasks submitted during test teardown execute on untracked pass-through path."""
        code = """
import pytest
from concurrent.futures import ThreadPoolExecutor

@pytest.fixture
def teardown_submit_fixture():
    yield
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "teardown_task")
        assert f.result() == "teardown_task"

def test_has_teardown_submission(teardown_submit_fixture):
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "call_task")
        assert f.result() == "call_task"
"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "test_td_untracked.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(code)
            ret = pytest.main([p, "-q", f"--rootdir={td}"], plugins=[plugin])
            assert ret == 0

        ledger = plugin.get_last_session_ledger()
        assert ledger is not None

        test_owner = next(
            o for o in ledger.get_all_owners() if o.owner_type == OwnerType.TEST_CALL
        )
        tasks = ledger.get_tasks_for_owner(test_owner.owner_id)

        # Only the call_task was tracked under the test owner
        assert len(tasks) == 1
        # Total tracked tasks in ledger is 1 (the teardown submission was untracked pass-through)
        assert len(ledger.get_all_tasks()) == 1

    def test_multi_test_isolation(self) -> None:
        """Verifies tasks submitted in Test 1 are not attributed to Test 2."""
        code = """
import pytest
from concurrent.futures import ThreadPoolExecutor

def test_item_one():
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "t1_work")
        assert f.result() == "t1_work"

def test_item_two():
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "t2_work")
        assert f.result() == "t2_work"
"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "test_iso.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(code)
            ret = pytest.main([p, "-q", f"--rootdir={td}"], plugins=[plugin])
            assert ret == 0

        ledger = plugin.get_last_session_ledger()
        assert ledger is not None

        test_owners = [
            o for o in ledger.get_all_owners() if o.owner_type == OwnerType.TEST_CALL
        ]
        assert len(test_owners) == 2

        t1_tasks = ledger.get_tasks_for_owner(test_owners[0].owner_id)
        t2_tasks = ledger.get_tasks_for_owner(test_owners[1].owner_id)
        assert len(t1_tasks) == 1
        assert len(t2_tasks) == 1
        assert t1_tasks[0].task_id != t2_tasks[0].task_id

    def test_unsupported_concurrent_session_rejected(self) -> None:
        """Attempting to start a second session while ACTIVE raises RuntimeError and rejects contamination."""
        fake_session1 = pytest.Session.from_config(pytest.Config.fromdictargs({}, []))
        fake_session2 = pytest.Session.from_config(pytest.Config.fromdictargs({}, []))

        plugin.pytest_sessionstart(fake_session1)
        assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.ACTIVE

        # Attempt to start second session in same process
        with pytest.raises(
            RuntimeError,
            match="State Guard v1 does not support concurrent or nested pytest sessions",
        ):
            plugin.pytest_sessionstart(fake_session2)

        # Verify second session did not overwrite active session
        assert plugin.get_active_session() is not None
        assert fake_session1._state_guard_session is plugin.get_active_session()
        assert not hasattr(fake_session2, "_state_guard_session")

        plugin._pytest_sessionfinish_direct(fake_session1, 0)
        assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.IDLE

    def test_preexisting_patch_startup_failure_preserves_patch(self) -> None:
        """When an unowned pre-existing patch is active, startup fails without removing or disturbing it."""
        foreign_ledger = Ledger()
        install_patch(foreign_ledger)
        assert is_patched() is True
        assert get_active_ledger() is foreign_ledger

        fake_session = pytest.Session.from_config(pytest.Config.fromdictargs({}, []))

        # Attempt to start session with different ledger
        with pytest.raises(RuntimeError, match="already active with a different ledger"):
            plugin.pytest_sessionstart(fake_session)

        # Pre-existing patch MUST be preserved untouched
        assert is_patched() is True
        assert get_active_ledger() is foreign_ledger
        assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.IDLE
        assert plugin.get_active_session() is None
        assert not hasattr(fake_session, "_state_guard_session")

        remove_patch()
        assert is_patched() is False

    def test_session_startup_install_failure_cleanup(self) -> None:
        """Injected failure during install_patch rolls back state, re-raises error, and leaves is_patched False."""
        fake_session = pytest.Session.from_config(pytest.Config.fromdictargs({}, []))

        with patch("pytest_state_guard.plugin.install_patch", side_effect=ValueError("injected_install_error")):
            with pytest.raises(ValueError, match="injected_install_error"):
                plugin.pytest_sessionstart(fake_session)

        assert is_patched() is False
        assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.IDLE
        assert plugin.get_active_session() is None
        assert not hasattr(fake_session, "_state_guard_session")

    def test_startup_interruption_after_patch_install_rolls_back(self) -> None:
        """Injected failure after install_patch() but before session publication rolls back patch and returns to IDLE."""
        fake_session = pytest.Session.from_config(pytest.Config.fromdictargs({}, []))

        # Make assigning session._state_guard_session fail
        class FaultySession:
            def __setattr__(self, name: str, val: object) -> None:
                if name == "_state_guard_session":
                    raise RuntimeError("injected_publish_failure")
                super().__setattr__(name, val)

        faulty = FaultySession()
        with pytest.raises(RuntimeError, match="injected_publish_failure"):
            plugin.pytest_sessionstart(faulty)  # type: ignore[arg-type]

        # Patch was rolled back
        assert is_patched() is False
        assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.IDLE
        assert plugin.get_active_session() is None
        assert getattr(ThreadPoolExecutor, "submit", None) is _REAL_ORIGINAL_SUBMIT

    def test_startup_interruption_after_session_attribute_publish_rolls_back(self) -> None:
        """Injected failure after session attribute is published but before _ACTIVE_SESSION is set rolls back cleanly."""
        class Step2InterruptionSession:
            def __setattr__(self, name: str, val: object) -> None:
                super().__setattr__(name, val)
                if name == "_state_guard_session" and val is not None:
                    raise RuntimeError("injected_step_2_to_3_interruption")

        faulty_session = Step2InterruptionSession()

        with pytest.raises(RuntimeError, match="injected_step_2_to_3_interruption"):
            plugin.pytest_sessionstart(faulty_session)  # type: ignore[arg-type]

        # Patch installed by current attempt must be rolled back
        assert is_patched() is False
        assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.IDLE
        assert plugin.get_active_session() is None
        assert not hasattr(faulty_session, "_state_guard_session")
        assert getattr(ThreadPoolExecutor, "submit", None) is _REAL_ORIGINAL_SUBMIT

    def test_startup_interruption_after_session_publish_rolls_back(self) -> None:
        """Injected failure after _ACTIVE_SESSION is set but before ACTIVE state transitions rolls back cleanly."""
        fake_session = pytest.Session.from_config(pytest.Config.fromdictargs({}, []))

        real_install = plugin.install_patch

        def install_and_fail_after_publish(ledger: Ledger) -> None:
            real_install(ledger)
            # Patch is installed, now inject failure simulating an error before ACTIVE state transition
            raise RuntimeError("injected_publish_interruption")

        with patch("pytest_state_guard.plugin.install_patch", side_effect=install_and_fail_after_publish):
            with pytest.raises(RuntimeError, match="injected_publish_interruption"):
                plugin.pytest_sessionstart(fake_session)

        assert is_patched() is False
        assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.IDLE
        assert plugin.get_active_session() is None
        assert getattr(ThreadPoolExecutor, "submit", None) is _REAL_ORIGINAL_SUBMIT

    def test_partial_install_after_active_ledger_poisons_lifecycle(self) -> None:
        """Injected failure after _ACTIVE_LEDGER assignment detects incomplete state and transitions to POISONED."""
        fake_session = pytest.Session.from_config(pytest.Config.fromdictargs({}, []))

        def simulated_partial_install(ledger: Ledger) -> None:
            # Simulate partial installation: submit and ledger assigned, but _IS_PATCHED left False
            ThreadPoolExecutor.submit = _state_guard_submit
            import pytest_state_guard.executor as ex
            ex._ACTIVE_LEDGER = ledger
            ex._IS_PATCHED = False
            raise RuntimeError("interrupted_after_active_ledger")

        with patch("pytest_state_guard.plugin.install_patch", side_effect=simulated_partial_install):
            with pytest.raises(RuntimeError, match="interrupted_after_active_ledger"):
                plugin.pytest_sessionstart(fake_session)

        # Must be POISONED because Stage 3 cannot safely roll back without modifying Stage 2
        assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.POISONED
        assert plugin.get_poison_reason() is not None
        assert "Partial installation detected" in plugin.get_poison_reason()

    def test_partial_install_before_active_ledger_poisons_lifecycle(self) -> None:
        """Injected failure after wrapper assignment (before _ACTIVE_LEDGER) detects incomplete state and poisons."""
        fake_session = pytest.Session.from_config(pytest.Config.fromdictargs({}, []))

        def simulated_partial_install_step1(ledger: Ledger) -> None:
            # Step 1 done, step 2 interrupted
            ThreadPoolExecutor.submit = _state_guard_submit
            import pytest_state_guard.executor as ex
            ex._ACTIVE_LEDGER = None
            ex._IS_PATCHED = False
            raise RuntimeError("interrupted_before_active_ledger")

        with patch("pytest_state_guard.plugin.install_patch", side_effect=simulated_partial_install_step1):
            with pytest.raises(RuntimeError, match="interrupted_before_active_ledger"):
                plugin.pytest_sessionstart(fake_session)

        assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.POISONED
        assert plugin.get_poison_reason() is not None
        assert "Partial installation detected" in plugin.get_poison_reason()

    def test_foreign_wrapper_preservation_during_shutdown(self) -> None:
        """If a third-party foreign wrapper is installed over State Guard, remove_patch deactivates without clobbering."""
        fake_session = pytest.Session.from_config(pytest.Config.fromdictargs({}, []))
        plugin.pytest_sessionstart(fake_session)
        assert is_patched() is True

        # Third party wraps ThreadPoolExecutor.submit
        real_submit_now = ThreadPoolExecutor.submit
        def foreign_wrapper(self: object, fn: object, *args: object, **kwargs: object) -> object:
            return real_submit_now(self, fn, *args, **kwargs)

        ThreadPoolExecutor.submit = foreign_wrapper  # type: ignore[method-assign]

        # Shutdown
        plugin._pytest_sessionfinish_direct(fake_session, 0)
        assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.IDLE
        assert is_patched() is False
        # Foreign wrapper preserved!
        assert ThreadPoolExecutor.submit is foreign_wrapper

    def test_session_startup_rollback_failure_chaining(self) -> None:
        """If rollback itself raises during startup failure, both startup and rollback errors are chained and POISONED."""
        fake_session = pytest.Session.from_config(pytest.Config.fromdictargs({}, []))

        def install_and_fail(ledger: Ledger) -> None:
            install_patch(ledger)
            raise ValueError("startup_initial_failure")

        with patch("pytest_state_guard.plugin.install_patch", side_effect=install_and_fail):
            with patch("pytest_state_guard.plugin.remove_patch", side_effect=RuntimeError("rollback_cascade_failure")):
                with pytest.raises(ValueError, match="startup_initial_failure") as exc_info:
                    plugin.pytest_sessionstart(fake_session)
                assert isinstance(exc_info.value.__cause__, RuntimeError)
                assert "rollback_cascade_failure" in str(exc_info.value.__cause__)

        assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.POISONED
        assert "patch rollback failed" in str(plugin.get_poison_reason())

    def test_session_shutdown_remove_patch_failure_poisons_lifecycle(self) -> None:
        """Injected failure during remove_patch transitions plugin to POISONED, re-raises error, and asserts is_patched."""
        fake_session = pytest.Session.from_config(pytest.Config.fromdictargs({}, []))
        plugin.pytest_sessionstart(fake_session)
        assert is_patched() is True

        with patch("pytest_state_guard.plugin.remove_patch", side_effect=RuntimeError("removal_injected_error")):
            with pytest.raises(RuntimeError, match="removal_injected_error"):
                plugin._pytest_sessionfinish_direct(fake_session, 0)

        assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.POISONED
        assert "Patch removal failed" in str(plugin.get_poison_reason())
        assert is_patched() is True

    def test_shutdown_interruption_after_unpatch_poisons_lifecycle(self) -> None:
        """Injected failure during session reference cleanup after unpatching transitions to POISONED."""
        fake_session = pytest.Session.from_config(pytest.Config.fromdictargs({}, []))
        plugin.pytest_sessionstart(fake_session)
        assert is_patched() is True

        # Simulate exception during the final reference cleanup step
        class FaultyCleanupSession:
            def __delattr__(self, name: str) -> None:
                if name == "_state_guard_session":
                    raise RuntimeError("cleanup_reference_error")
                super().__delattr__(name)

        faulty = FaultyCleanupSession()
        faulty._state_guard_session = fake_session._state_guard_session  # type: ignore[attr-defined]

        with patch.object(plugin, "_ACTIVE_SESSION", faulty._state_guard_session):  # type: ignore[attr-defined]
            with pytest.raises(RuntimeError, match="cleanup_reference_error"):
                plugin._pytest_sessionfinish_direct(faulty, 0)  # type: ignore[arg-type]

        assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.POISONED
        assert "Session reference cleanup failed" in str(plugin.get_poison_reason())

    def test_duplicate_plugin_registration_idempotency(self) -> None:
        """Registering the plugin twice in the same session does not duplicate hook execution or emit duplicate events."""
        code = """
import pytest
from concurrent.futures import ThreadPoolExecutor

def test_single_owner():
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: 99)
        assert f.result() == 99
"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "test_dup.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(code)
            # Register plugin twice via command-line flags
            ret = pytest.main(
                [p, "-q", f"--rootdir={td}", "-p", "pytest_state_guard.plugin", "-p", "pytest_state_guard.plugin"]
            )
            assert ret == 0

        ledger = plugin.get_last_session_ledger()
        assert ledger is not None

        test_owners = [
            o for o in ledger.get_all_owners() if o.owner_type == OwnerType.TEST_CALL
        ]
        # Only 1 test owner was created despite duplicate registration
        assert len(test_owners) == 1
        assert test_owners[0].state == OwnerState.FINALIZED

        tasks = ledger.get_tasks_for_owner(test_owners[0].owner_id)
        assert len(tasks) == 1

    def test_active_invariant_requires_is_patched_and_matching_ledger(self) -> None:
        """Verifies _get_verified_session returns None if is_patched is False or get_active_ledger does not match."""
        fake_session = pytest.Session.from_config(pytest.Config.fromdictargs({}, []))
        plugin.pytest_sessionstart(fake_session)
        assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.ACTIVE

        # All 4 conditions hold
        assert plugin._get_verified_session(fake_session) is not None

        # Condition 3 fails: is_patched() is False
        with patch("pytest_state_guard.plugin.is_patched", return_value=False):
            assert plugin._get_verified_session(fake_session) is None

        # Condition 4 fails: get_active_ledger() does not match _ACTIVE_SESSION.ledger
        different_ledger = Ledger()
        with patch("pytest_state_guard.plugin.get_active_ledger", return_value=different_ledger):
            assert plugin._get_verified_session(fake_session) is None

        # Clean shutdown
        plugin._pytest_sessionfinish_direct(fake_session, 0)
        assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.IDLE

    def test_sequential_uncleaned_sessions_in_same_process(self) -> None:
        """Verify that two sequential pytest sessions in the exact same Python process
        both initialize, run tests, audit, and clean up to IDLE without manual resets."""
        import subprocess
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent.parent
        src_dir = str(repo_root / "src")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            test1_path = tmp_path / "test_1.py"
            test1_path.write_text(
                "def test_first():\n"
                "    import pytest_state_guard.plugin as plugin\n"
                "    import pytest_state_guard.executor as executor\n"
                "    from concurrent.futures import ThreadPoolExecutor\n"
                "    assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.ACTIVE, 'Session 1 not ACTIVE during test execution'\n"
                "    assert plugin.get_active_session() is not None, 'Session 1 active session is None during test execution'\n"
                "    assert executor.is_patched() is True, 'Session 1 executor is not patched during test execution'\n"
                "    with ThreadPoolExecutor(max_workers=1) as ex:\n"
                "        fut = ex.submit(lambda: 'payload1')\n"
                "        assert fut.result() == 'payload1'\n",
                encoding="utf-8",
            )
            test2_path = tmp_path / "test_2.py"
            test2_path.write_text(
                "def test_second():\n"
                "    import pytest_state_guard.plugin as plugin\n"
                "    import pytest_state_guard.executor as executor\n"
                "    from concurrent.futures import ThreadPoolExecutor\n"
                "    assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.ACTIVE, 'Session 2 not ACTIVE during test execution'\n"
                "    assert plugin.get_active_session() is not None, 'Session 2 active session is None during test execution'\n"
                "    assert executor.is_patched() is True, 'Session 2 executor is not patched during test execution'\n"
                "    with ThreadPoolExecutor(max_workers=1) as ex:\n"
                "        fut = ex.submit(lambda: 'payload2')\n"
                "        assert fut.result() == 'payload2'\n",
                encoding="utf-8",
            )

            script = f"""
import sys
import pytest
import pytest_state_guard.plugin as plugin
import pytest_state_guard.executor as executor

# Session 1: Clean test pass with State Guard
ret1 = pytest.main(["-q", "-p", "pytest_state_guard.plugin", r"{test1_path}"])
assert ret1 == 0, f"Session 1 failed with exit code {{ret1}}"
assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.IDLE, "Session 1 did not reach IDLE"
assert plugin.get_active_session() is None, "Session 1 did not clear active session"
assert executor.is_patched() is False, "Session 1 did not remove patch"
findings1 = plugin.get_last_session_findings()
assert findings1 is not None, "Session 1 did not populate findings"

ledger1 = plugin.get_last_session_ledger()
assert ledger1 is not None, "Session 1 did not record ledger"
owners1, tasks1, events1 = ledger1.get_finalized_session_snapshot()

# Verify non-empty evidence in Session 1 (non-vacuous verification)
assert len(owners1) > 0, "Session 1 owners snapshot is unexpectedly empty"
assert len(tasks1) > 0, "Session 1 tasks snapshot is unexpectedly empty"
assert len(events1) > 0, "Session 1 events snapshot is unexpectedly empty"

s1_owner_ids = {{o.owner_id for o in owners1}}
s1_task_ids = {{t.task_id for t in tasks1}}
s1_event_owner_ids = {{e.owner_id for e in events1}}
s1_event_task_ids = {{e.task_id for e in events1 if e.task_id is not None}}
assert set(findings1.keys()) == s1_owner_ids, "Session 1 findings keys do not match snapshot owners"

# Session 2: Second independent test in same process without resetting plugin globals
ret2 = pytest.main(["-q", "-p", "pytest_state_guard.plugin", r"{test2_path}"])
assert ret2 == 0, f"Session 2 failed with exit code {{ret2}}"
assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.IDLE, "Session 2 did not reach IDLE"
assert plugin.get_active_session() is None, "Session 2 did not clear active session"
assert executor.is_patched() is False, "Session 2 did not remove patch"
findings2 = plugin.get_last_session_findings()
assert findings2 is not None, "Session 2 did not populate findings"

ledger2 = plugin.get_last_session_ledger()
assert ledger2 is not None, "Session 2 did not record ledger"
assert ledger2 is not ledger1, "Session 2 reused Session 1 ledger instance"
owners2, tasks2, events2 = ledger2.get_finalized_session_snapshot()

# Verify non-empty evidence in Session 2 (non-vacuous verification)
assert len(owners2) > 0, "Session 2 owners snapshot is unexpectedly empty"
assert len(tasks2) > 0, "Session 2 tasks snapshot is unexpectedly empty"
assert len(events2) > 0, "Session 2 events snapshot is unexpectedly empty"

s2_owner_ids = {{o.owner_id for o in owners2}}
s2_task_ids = {{t.task_id for t in tasks2}}
s2_event_owner_ids = {{e.owner_id for e in events2}}
s2_event_task_ids = {{e.task_id for e in events2 if e.task_id is not None}}
assert set(findings2.keys()) == s2_owner_ids, "Session 2 findings keys do not match snapshot owners"

# 1. Owner IDs are disjoint across sessions
assert s1_owner_ids.isdisjoint(s2_owner_ids), f"Cross-session owner leakage detected: {{s1_owner_ids & s2_owner_ids}}"

# 2. Task IDs are disjoint across sessions
assert s1_task_ids.isdisjoint(s2_task_ids), f"Cross-session task leakage detected: {{s1_task_ids & s2_task_ids}}"

# 3. Event records from Session 2 contain no owner or task references originating in Session 1
assert s1_owner_ids.isdisjoint(s2_event_owner_ids), f"Session 2 events reference Session 1 owners: {{s1_owner_ids & s2_event_owner_ids}}"
assert s1_task_ids.isdisjoint(s2_event_task_ids), f"Session 2 events reference Session 1 tasks: {{s1_task_ids & s2_event_task_ids}}"

sys.stderr.write("\\nSEQUENTIAL_SESSIONS_VERIFIED\\n")
"""
            env = os.environ.copy()
            existing_pp = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{existing_pp}" if existing_pp else src_dir

            res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=60.0)
            assert res.returncode == 0, f"Sequential sessions failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
            assert "SEQUENTIAL_SESSIONS_VERIFIED" in res.stderr
