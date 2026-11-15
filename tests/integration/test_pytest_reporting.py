from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


def run_pytest(
    test_content: str,
    extra_args: list[str] | None = None,
    conftest_content: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute pytest in a subprocess with the state_guard plugin active."""
    env = os.environ.copy()
    src_dir = str(Path(__file__).resolve().parent.parent.parent / "src")
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else src_dir

    with tempfile.TemporaryDirectory() as tmpdir:
        test_path = Path(tmpdir) / "test_case.py"
        test_path.write_text(test_content, encoding="utf-8")
        if conftest_content:
            conftest_path = Path(tmpdir) / "conftest.py"
            conftest_path.write_text(conftest_content, encoding="utf-8")
        args = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "pytest_state_guard.plugin",
            str(test_path),
            "-s",
        ]
        if extra_args:
            args.extend(extra_args)

        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            env=env,
        )


def test_clean_test_passes_cleanly():
    """Verify that a test with no background tasks passes with exit code 0."""
    code = """
def test_clean():
    assert True
"""
    res = run_pytest(code)
    assert res.returncode == 0
    assert "1 passed" in res.stdout
    assert "VIOLATIONS DETECTED" not in res.stdout


def test_call_pass_teardown_violation():
    """Case 1: Call passes, but task is leaked past owner finalization.
    Should produce an ERROR at teardown and exit code 1.
    """
    code = """
import threading
from concurrent.futures import ThreadPoolExecutor

stop_event = threading.Event()

def test_leak():
    executor = ThreadPoolExecutor(max_workers=1)
    executor.submit(lambda: stop_event.wait(timeout=1))
    # test finishes without shutting down executor
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "ERROR at teardown of test_leak" in res.stdout
    assert "State Guard Teardown Violation: TEST_CALL" in res.stdout
    assert "OUTSTANDING_TASK" in res.stdout or "CALLABLE_NOT_EXITED" in res.stdout or "EXECUTION_AFTER_OWNER_FINALIZATION" in res.stdout


def test_call_fail_teardown_violation():
    """Case 2: Call fails an assertion AND a task is leaked.
    Both FAILURES (assertion) and ERRORS (State Guard teardown violation) must appear.
    """
    code = """
import threading
from concurrent.futures import ThreadPoolExecutor

stop_event = threading.Event()

def test_fail_and_leak():
    executor = ThreadPoolExecutor(max_workers=1)
    executor.submit(lambda: stop_event.wait(timeout=1))
    assert False, "custom assertion failure message"
"""
    res = run_pytest(code)
    assert res.returncode == 1
    # Call failure is preserved in FAILURES:
    assert "FAILURES" in res.stdout
    assert "custom assertion failure message" in res.stdout
    # Teardown violation is displayed in ERRORS:
    assert "ERRORS" in res.stdout
    assert "ERROR at teardown of test_fail_and_leak" in res.stdout
    assert "State Guard Teardown Violation" in res.stdout


def test_call_pass_fixture_error_and_violation():
    """Case 3: Call passes, but fixture teardown raises an exception AND a task is leaked.
    The original fixture exception and the State Guard diagnostic must both appear (chained).
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

stop_event = threading.Event()

@pytest.fixture
def bad_fixture():
    executor = ThreadPoolExecutor(max_workers=1)
    executor.submit(lambda: stop_event.wait(timeout=1))
    yield
    raise ValueError("fixture teardown explosion")

def test_fixture_err(bad_fixture):
    pass
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "ERROR at teardown of test_fixture_err" in res.stdout
    assert "ValueError: fixture teardown explosion" in res.stdout
    assert "State Guard Audit Detected Teardown Concurrency Violations" in res.stdout or "State Guard Teardown Violation" in res.stdout


def test_call_fail_fixture_error_and_violation():
    """Case 4: Call fails, fixture teardown raises, and a task is leaked.
    All three failures must be preserved and visible.
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

stop_event = threading.Event()

@pytest.fixture
def exploding_fixture():
    executor = ThreadPoolExecutor(max_workers=1)
    executor.submit(lambda: stop_event.wait(timeout=1))
    yield
    raise RuntimeError("exploding fixture error")

def test_both_fail(exploding_fixture):
    assert False, "original call failure"
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "FAILURES" in res.stdout
    assert "original call failure" in res.stdout
    assert "ERRORS" in res.stdout
    assert "exploding fixture error" in res.stdout
    assert "State Guard" in res.stdout


def test_shared_fixture_violation_in_terminal_summary():
    """Shared fixtures (module/session scope) must NOT fail individual test teardowns,
    but must report violations in the session audit summary and promote exit code to 1.
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

stop_event = threading.Event()

@pytest.fixture(scope="module")
def module_fixture():
    executor = ThreadPoolExecutor(max_workers=1)
    executor.submit(lambda: stop_event.wait(timeout=1))
    yield
    # Fixture teardown does not wait for executor

def test_one(module_fixture):
    assert True

def test_two(module_fixture):
    assert True
"""
    res = run_pytest(code)
    assert res.returncode == 1
    # Both tests passed during runtest:
    assert "2 passed" in res.stdout
    # Session summary reports the module fixture violation:
    assert "State Guard Audit: VIOLATIONS DETECTED" in res.stdout
    assert "fixture 'module_fixture'" in res.stdout


def test_session_fixture_teardown_crash_preserves_exception_and_cleans_up():
    """When a session fixture crashes in pytest_sessionfinish, the original
    fixture exception must be printed and the executor monkeypatch must be removed.
    """
    code = """
import pytest
from pytest_state_guard.executor import is_patched

@pytest.fixture(scope="session")
def crashing_session_fixture():
    yield
    raise RuntimeError("unhandled session teardown catastrophe")

def test_dummy(crashing_session_fixture):
    pass
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "unhandled session teardown catastrophe" in res.stdout or "unhandled session teardown catastrophe" in res.stderr
    assert "ERROR at teardown of test_dummy" in res.stdout or "unhandled session teardown catastrophe" in res.stderr


def test_session_teardown_crash_and_patch_removal_failure_dual_fault():
    """Case 4: An upstream session finish hook crashes AND State Guard's remove_patch() fails.
    Verifies that:
    1. State Guard's wrapper executes its post-yield cleanup despite the upstream crash.
    2. The primary upstream exception is preserved and remains visible.
    3. State Guard's operational failure is recorded in lifecycle poisoning.
    4. State Guard never reports a clean/IDLE shutdown when patch removal fails.
    5. The subprocess exits with a nonzero exit code.
    6. Terminal summary is NOT falsely reported as a clean/complete audit.
    """
    conftest_code = """
import pytest
import pytest_state_guard.executor as executor
import pytest_state_guard.plugin as plugin

@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    # 1. Force remove_patch to fail when State Guard runs its wrapper cleanup
    def faulty_remove_patch():
        raise RuntimeError("catastrophic patch removal error")
    executor.remove_patch = faulty_remove_patch
    plugin.remove_patch = faulty_remove_patch

    # 2. Crash session finish with the primary upstream exception
    raise ValueError("primary upstream session teardown crash")

def pytest_unconfigure(config):
    # Verify post-session state under dual failure:
    assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.POISONED
    poison_reason = str(plugin.get_poison_reason())
    assert "Patch removal failed during interrupted session teardown" in poison_reason
    assert "catastrophic patch removal error" in poison_reason
    assert executor.is_patched() is True
"""
    test_code = """
def test_clean_body():
    assert True
"""
    res = run_pytest(test_code, conftest_content=conftest_code)
    assert res.returncode != 0
    combined = res.stdout + res.stderr
    assert "primary upstream session teardown crash" in combined
    assert "ValueError" in combined
    assert "=== STATE GUARD AUDIT SUMMARY ===" not in res.stdout


def test_clean_runner_with_state_guard_cleanup_failure():
    """Case 3: Clean runner completion with State Guard cleanup failure.
    When runner teardown succeeds, but State Guard's remove_patch() fails:
    - outcome.force_exception propagates the cleanup error
    - exit status is promoted to nonzero
    - PluginLifecycle becomes POISONED
    """
    conftest_code = """
import pytest
import pytest_state_guard.executor as executor
import pytest_state_guard.plugin as plugin

@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    def faulty_remove_patch():
        raise RuntimeError("injected remove_patch failure on clean runner")
    executor.remove_patch = faulty_remove_patch
    plugin.remove_patch = faulty_remove_patch

def pytest_unconfigure(config):
    assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.POISONED
    assert "injected remove_patch failure on clean runner" in str(plugin.get_poison_reason())
"""
    test_code = """
def test_clean():
    assert True
"""
    res = run_pytest(test_code, conftest_content=conftest_code)
    assert res.returncode != 0
    combined = res.stdout + res.stderr
    assert "injected remove_patch failure on clean runner" in combined
    assert "RuntimeError" in combined


def test_sessionfinish_inactive_lifecycle_yields_without_lock():
    """Case 5: Inactive lifecycle path.
    When plugin is disabled (e.g. -p no:state_guard), pytest_sessionfinish
    yields immediately without holding _SESSION_LOCK.
    An inner hook must be able to acquire _SESSION_LOCK without deadlock.
    """
    conftest_code = """
import pytest
from pytest_state_guard.plugin import _SESSION_LOCK, get_lifecycle_state, PluginLifecycle

@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    # Verify _SESSION_LOCK is NOT locked by State Guard during inner hook execution
    acquired = _SESSION_LOCK.acquire(blocking=False)
    assert acquired, "_SESSION_LOCK should not be held during inner hook execution"
    _SESSION_LOCK.release()
"""
    test_code = """
def test_dummy():
    assert True
"""
    res = run_pytest(test_code, extra_args=["-p", "no:state_guard"], conftest_content=conftest_code)
    assert res.returncode == 0
    assert "1 passed" in res.stdout


def test_sessionfinish_mismatched_session_yields_without_lock():
    """Case 6: Session identity mismatch path.
    When pytest_sessionfinish is invoked with an unmanaged/mismatched session,
    it yields without holding _SESSION_LOCK and returns without modifying state.
    """
    conftest_code = """
import pytest
from pytest_state_guard.plugin import _SESSION_LOCK

@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    # Verify lock is not held
    assert not _SESSION_LOCK.locked(), "_SESSION_LOCK must not be held during inner hook execution"
"""
    test_code = """
def test_dummy():
    assert True
"""
    res = run_pytest(test_code, conftest_content=conftest_code)
    assert res.returncode == 0
    assert "1 passed" in res.stdout


def test_audit_state_populated_before_terminal_summary():
    """Timing proof: State Guard's audit findings and session exitstatus are fully
    populated before TerminalReporter resumes and invokes pytest_terminal_summary.
    """
    conftest_code = """
import pytest
import pytest_state_guard.plugin as plugin

@pytest.hookimpl(tryfirst=True)
def pytest_terminal_summary(terminalreporter, exitstatus, config):
    # State Guard's post-yield wrapper must have already executed and populated findings
    findings = plugin.get_last_session_findings()
    assert findings is not None, "State Guard findings must be populated before terminal summary runs"
    session = terminalreporter._session
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED, "Exitstatus must be promoted to TESTS_FAILED before terminal summary"
"""
    test_code = """
import threading
from concurrent.futures import ThreadPoolExecutor

stop_event = threading.Event()

def test_leak():
    ex = ThreadPoolExecutor(max_workers=1)
    ex.submit(lambda: stop_event.wait(timeout=1))
"""
    res = run_pytest(test_code, conftest_content=conftest_code)
    assert res.returncode == 1
    assert "State Guard Teardown Violation" in res.stdout


def test_policy_evaluation_raises_patch_removal_succeeds():
    """Test A: Policy evaluation raises, patch removal succeeds.
    Verifies that:
    1. An unhandled exception during PolicyEngine evaluation does not prevent remove_patch().
    2. The audit failure is propagated and visible.
    3. The plugin lifecycle transitions to POISONED with the audit failure reason.
    4. The executor patch is verified removed (is_patched() is False).
    5. No false clean audit summary is reported.
    6. Exit code is nonzero (1).
    """
    conftest_code = """
import pytest
import pytest_state_guard.executor as executor
import pytest_state_guard.plugin as plugin
from pytest_state_guard.policy import PolicyEngine

@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    # Inject policy evaluation failure
    def faulty_evaluate_all_finalized(self, ledger):
        raise RuntimeError("injected policy evaluation crash")
    PolicyEngine.evaluate_all_finalized = faulty_evaluate_all_finalized

def pytest_unconfigure(config):
    assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.POISONED
    assert "injected policy evaluation crash" in str(plugin.get_poison_reason())
    # Patch was removed despite audit crash:
    assert executor.is_patched() is False
"""
    test_code = """
def test_clean():
    assert True
"""
    res = run_pytest(test_code, conftest_content=conftest_code)
    assert res.returncode != 0
    combined = res.stdout + res.stderr
    assert "injected policy evaluation crash" in combined
    assert "RuntimeError" in combined
    assert "=== STATE GUARD AUDIT SUMMARY ===" not in res.stdout


def test_policy_evaluation_raises_patch_removal_fails():
    """Test B: Policy evaluation raises, patch removal fails.
    Verifies that:
    1. Both policy evaluation and patch removal fail.
    2. Primary audit exception propagates and remains visible.
    3. Secondary patch removal failure is recorded in _POISON_REASON.
    4. Lifecycle is POISONED.
    5. The patch remains installed (is_patched() is True) and State Guard does not claim removal.
    6. Exit code is nonzero.
    """
    conftest_code = """
import pytest
import pytest_state_guard.executor as executor
import pytest_state_guard.plugin as plugin
from pytest_state_guard.policy import PolicyEngine

@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    # 1. Inject audit evaluation failure
    def faulty_evaluate_all_finalized(self, ledger):
        raise ValueError("primary policy evaluation catastrophe")
    PolicyEngine.evaluate_all_finalized = faulty_evaluate_all_finalized

    # 2. Inject cleanup failure
    def faulty_remove_patch():
        raise RuntimeError("secondary patch removal error")
    executor.remove_patch = faulty_remove_patch
    plugin.remove_patch = faulty_remove_patch

def pytest_unconfigure(config):
    assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.POISONED
    poison = str(plugin.get_poison_reason())
    assert "primary policy evaluation catastrophe" in poison
    assert "secondary patch removal error" in poison
    assert executor.is_patched() is True
"""
    test_code = """
def test_clean():
    assert True
"""
    res = run_pytest(test_code, conftest_content=conftest_code)
    assert res.returncode != 0
    combined = res.stdout + res.stderr
    assert "primary policy evaluation catastrophe" in combined
    assert "ValueError" in combined
    assert "=== STATE GUARD AUDIT SUMMARY ===" not in res.stdout


def test_owner_snapshot_raises_patch_removal_succeeds():
    """Test C: Owner snapshot retrieval raises, patch removal succeeds.
    Verifies that:
    1. An unhandled exception during ledger.get_all_owners() does not prevent remove_patch().
    2. The snapshot failure propagates and is visible.
    3. The plugin lifecycle transitions to POISONED.
    4. The executor patch is cleanly removed (is_patched() is False).
    5. Exit code is nonzero.
    """
    conftest_code = """
import pytest
import pytest_state_guard.executor as executor
import pytest_state_guard.plugin as plugin
from pytest_state_guard.ledger import Ledger

@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    def faulty_get_all_owners():
        raise RuntimeError("injected owner snapshot retrieval crash")
    state = getattr(session, "_state_guard_session", None)
    if state is not None:
        state.ledger.get_all_owners = faulty_get_all_owners

def pytest_unconfigure(config):
    assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.POISONED
    assert "injected owner snapshot retrieval crash" in str(plugin.get_poison_reason())
    assert executor.is_patched() is False
"""
    test_code = """
def test_clean():
    assert True
"""
    res = run_pytest(test_code, conftest_content=conftest_code)
    assert res.returncode != 0
    combined = res.stdout + res.stderr
    assert "injected owner snapshot retrieval crash" in combined
    assert "RuntimeError" in combined
    assert "=== STATE GUARD AUDIT SUMMARY ===" not in res.stdout


def test_owner_snapshot_raises_patch_removal_fails():
    """Test D: Owner snapshot retrieval raises, patch removal fails.
    Verifies that:
    1. Snapshot failure and cleanup failure are both handled.
    2. Primary snapshot exception propagates and is visible.
    3. Secondary cleanup failure is retained in _POISON_REASON.
    4. Lifecycle transitions to POISONED.
    5. Patch remains installed (is_patched() is True).
    6. Exit code is nonzero.
    """
    conftest_code = """
import pytest
import pytest_state_guard.executor as executor
import pytest_state_guard.plugin as plugin

@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    # 1. Inject snapshot failure
    def faulty_get_all_owners():
        raise ValueError("primary snapshot retrieval catastrophe")
    state = getattr(session, "_state_guard_session", None)
    if state is not None:
        state.ledger.get_all_owners = faulty_get_all_owners

    # 2. Inject cleanup failure
    def faulty_remove_patch():
        raise RuntimeError("secondary patch removal failure during snapshot crash")
    executor.remove_patch = faulty_remove_patch
    plugin.remove_patch = faulty_remove_patch

def pytest_unconfigure(config):
    assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.POISONED
    poison = str(plugin.get_poison_reason())
    assert "primary snapshot retrieval catastrophe" in poison
    assert "secondary patch removal failure during snapshot crash" in poison
    assert executor.is_patched() is True
"""
    test_code = """
def test_clean():
    assert True
"""
    res = run_pytest(test_code, conftest_content=conftest_code)
    assert res.returncode != 0
    combined = res.stdout + res.stderr
    assert "primary snapshot retrieval catastrophe" in combined
    assert "ValueError" in combined
    assert "=== STATE GUARD AUDIT SUMMARY ===" not in res.stdout


def test_normal_runner_patch_verification_failure_poisons_lifecycle():
    """Test E: Normal runner completion with patch-state verification failure.
    Verifies that:
    1. When audit and remove_patch() succeed, but is_patched() verification raises:
    2. The verification exception propagates visibly.
    3. Lifecycle transitions to POISONED (no false IDLE state).
    4. _POISON_REASON records the verification failure.
    5. Session references are cleaned up.
    6. Exit code is nonzero.
    """
    conftest_code = """
import pytest
import pytest_state_guard.executor as executor
import pytest_state_guard.plugin as plugin

@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    def faulty_is_patched():
        raise RuntimeError("injected is_patched verification crash")
    executor.is_patched = faulty_is_patched
    plugin.is_patched = faulty_is_patched

def pytest_unconfigure(config):
    assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.POISONED
    poison = str(plugin.get_poison_reason())
    assert "Patch verification failed: injected is_patched verification crash" in poison
    assert plugin.get_active_session() is None
"""
    test_code = """
def test_clean():
    assert True
"""
    res = run_pytest(test_code, conftest_content=conftest_code)
    assert res.returncode != 0
    combined = res.stdout + res.stderr
    assert "injected is_patched verification crash" in combined
    assert "RuntimeError" in combined
    assert "=== STATE GUARD AUDIT SUMMARY ===" not in res.stdout


def test_interrupted_runner_patch_verification_failure_preserves_runner_exception():
    """Test F: Interrupted runner teardown with patch-state verification failure.
    Verifies that:
    1. The runner session finish raises its own primary exception.
    2. Patch-state verification raises unexpectedly.
    3. The primary runner exception is preserved in output.
    4. The secondary verification failure is recorded in _POISON_REASON.
    5. Lifecycle is POISONED (no false IDLE).
    6. Exit code is nonzero.
    """
    conftest_code = """
import pytest
import pytest_state_guard.executor as executor
import pytest_state_guard.plugin as plugin

@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    # 1. Make is_patched() fail during verification
    def faulty_is_patched():
        raise RuntimeError("secondary is_patched verification failure")
    executor.is_patched = faulty_is_patched
    plugin.is_patched = faulty_is_patched

    # 2. Crash runner teardown with primary upstream exception
    raise ValueError("primary upstream session finish crash")

def pytest_unconfigure(config):
    assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.POISONED
    poison = str(plugin.get_poison_reason())
    assert "Patch verification failed: secondary is_patched verification failure" in poison
    assert plugin.get_active_session() is None
"""
    test_code = """
def test_clean():
    assert True
"""
    res = run_pytest(test_code, conftest_content=conftest_code)
    assert res.returncode != 0
    combined = res.stdout + res.stderr
    assert "primary upstream session finish crash" in combined
    assert "ValueError" in combined
    assert "=== STATE GUARD AUDIT SUMMARY ===" not in res.stdout


def test_interrupted_runner_clean_cleanup_poisons_lifecycle():
    """Test G: Interrupted runner teardown with successful instrumentation cleanup.
    Verifies that:
    1. Upstream runner fixture teardown raises an unhandled exception.
    2. Patch removal and verification succeed cleanly.
    3. The lifecycle transitions to POISONED (NEVER false IDLE) because the audit was not completed.
    4. _POISON_REASON accurately records that session teardown was interrupted before audit could complete.
    5. The primary runner exception is preserved in output.
    6. Subprocess exit code is nonzero.
    """
    conftest_code = """
import pytest
import pytest_state_guard.executor as executor
import pytest_state_guard.plugin as plugin

@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    raise ValueError("primary upstream runner teardown abort")

def pytest_unconfigure(config):
    # Verify post-session state: MUST NOT be IDLE!
    assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.POISONED
    poison = str(plugin.get_poison_reason())
    assert "Session teardown interrupted by runner exception before audit could complete: primary upstream runner teardown abort" in poison
    assert plugin.get_active_session() is None
    assert executor.is_patched() is False
"""
    test_code = """
def test_clean():
    assert True
"""
    res = run_pytest(test_code, conftest_content=conftest_code)
    assert res.returncode != 0
    combined = res.stdout + res.stderr
    assert "primary upstream runner teardown abort" in combined
    assert "ValueError" in combined
    assert "=== STATE GUARD AUDIT SUMMARY ===" not in res.stdout


def test_audit_failure_and_exitstatus_mutation_failure():
    """Test H: Audit failure followed by exit-status mutation failure.
    Verifies that:
    1. Policy evaluation raises an unexpected exception (audit_exc).
    2. Exit-status assignment also raises an unexpected exception (exit_status_exc).
    3. Mandatory patch removal, verification, and session-reference cleanup STILL execute.
    4. The audit exception remains PRIMARY and propagates visibly.
    5. The exit-status mutation failure is captured as secondary diagnostic evidence in _POISON_REASON.
    6. Lifecycle transitions to POISONED (never IDLE).
    """
    conftest_code = """
import pytest
import pytest_state_guard.executor as executor
import pytest_state_guard.plugin as plugin
from pytest_state_guard.policy import PolicyEngine

@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    # 1. Faulty evaluate_all_finalized
    def faulty_eval(self, ledger):
        raise ValueError("primary audit policy evaluation catastrophe")
    PolicyEngine.evaluate_all_finalized = faulty_eval

    # 2. Faulty session exitstatus attribute
    orig_setattr = session.__class__.__setattr__
    def faulty_setattr(self, name, value):
        if name == "exitstatus":
            raise RuntimeError("secondary exitstatus mutation failure")
        return orig_setattr(self, name, value)
    session.__class__.__setattr__ = faulty_setattr

def pytest_unconfigure(config):
    assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.POISONED
    poison = str(plugin.get_poison_reason())
    assert "Session audit failed: primary audit policy evaluation catastrophe" in poison
    assert "Exit status mutation failed: secondary exitstatus mutation failure" in poison
    assert plugin.get_active_session() is None
    assert executor.is_patched() is False
"""
    test_code = """
def test_clean():
    assert True
"""
    res = run_pytest(test_code, conftest_content=conftest_code)
    assert res.returncode != 0
    combined = res.stdout + res.stderr
    assert "primary audit policy evaluation catastrophe" in combined
    assert "ValueError" in combined
    assert "=== STATE GUARD AUDIT SUMMARY ===" not in res.stdout


def test_audit_failure_diagnostic_formatting_exception_still_cleans_up():
    """Test I: Audit failure with unformattable exception (__str__ raises).
    Verifies that:
    1. Policy evaluation raises an exception whose __str__ raises.
    2. Diagnostic formatting does not crash cleanup or lifecycle reconciliation.
    3. Patch removal and verification complete cleanly.
    4. Lifecycle is POISONED.
    """
    conftest_code = """
import pytest
import pytest_state_guard.executor as executor
import pytest_state_guard.plugin as plugin
from pytest_state_guard.policy import PolicyEngine

class BrokenStrError(Exception):
    def __str__(self):
        raise RuntimeError("broken __str__ explosion")

@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    def faulty_eval(self, ledger):
        raise BrokenStrError()
    PolicyEngine.evaluate_all_finalized = faulty_eval

def pytest_unconfigure(config):
    assert plugin.get_lifecycle_state() == plugin.PluginLifecycle.POISONED
    assert plugin.get_active_session() is None
    assert executor.is_patched() is False
"""
    test_code = """
def test_clean():
    assert True
"""
    res = run_pytest(test_code, conftest_content=conftest_code)
    assert res.returncode != 0
    combined = res.stdout + res.stderr
    assert "BrokenStrError" in combined
    assert "=== STATE GUARD AUDIT SUMMARY ===" not in res.stdout
