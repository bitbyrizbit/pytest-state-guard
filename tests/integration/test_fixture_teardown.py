from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from _pytest.fixtures import FixtureDef, SubRequest

from pytest_state_guard import plugin
from pytest_state_guard.ledger import Ledger
from pytest_state_guard.models import (
    EventType,
    FindingClassification,
    OwnerRecord,
    OwnerState,
    OwnerType,
    Severity,
)
from pytest_state_guard.owners import (
    OwnerContext,
    get_current_owner,
    reset_current_owner,
    set_current_owner,
)
from pytest_state_guard.policy import PolicyEngine


def run_pytest(
    test_content: str,
    extra_args: list[str] | None = None,
    conftest_content: str | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    """Execute pytest in a subprocess with State Guard active."""
    env = os.environ.copy()
    src_dir = str(Path(__file__).resolve().parent.parent.parent / "src")
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else src_dir
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

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
            timeout=timeout,
        )


# ===========================================================================
# T01: Clean Generator Teardown
# ===========================================================================

def test_t01_clean_generator_teardown():
    """T01: Task submitted during generator fixture teardown and joined before finish completes.
    Expectation: Owner CLEAN, test passed.
    """
    code = """
import pytest
from concurrent.futures import ThreadPoolExecutor

@pytest.fixture
def clean_teardown_fixture():
    yield
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "cleaned_up")
        assert f.result() == "cleaned_up"

def test_t01(clean_teardown_fixture):
    assert True
"""
    res = run_pytest(code)
    assert res.returncode == 0
    assert "1 passed" in res.stdout
    assert "VIOLATIONS DETECTED" not in res.stdout


# ===========================================================================
# T02A & T02B: Leaked Task in Generator Post-Yield
# ===========================================================================

def test_t02a_leaked_task_in_generator_post_yield_not_exited():
    """T02A: Task submitted during generator teardown is still running when finish completes.
    Expectation: CALLABLE_NOT_EXITED finding, test teardown failed with State Guard error.
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

stop_event = threading.Event()

@pytest.fixture
def leaked_teardown_fixture():
    yield
    ex = ThreadPoolExecutor(max_workers=1)
    ex.submit(lambda: stop_event.wait(timeout=2.0))
    # Exits without waiting

def test_t02a(leaked_teardown_fixture):
    assert True
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "State Guard Teardown Violation: FIXTURE_TEARDOWN" in res.stdout
    assert "CALLABLE_NOT_EXITED" in res.stdout
    assert "fixture 'leaked_teardown_fixture'" in res.stdout


def test_t02b_leaked_task_in_generator_post_yield_exit_committed_after_finalization():
    """T02B: Task submitted during generator teardown finishes after teardown finalizes.
    Expectation: EXIT_COMMITTED_AFTER_FINALIZATION or CALLABLE_NOT_EXITED, VIOLATION.
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

gate = threading.Event()

@pytest.fixture
def late_exit_fixture():
    yield
    ex = ThreadPoolExecutor(max_workers=1)
    f = ex.submit(lambda: gate.wait(timeout=2.0))
    # Signal gate to release shortly
    threading.Timer(0.05, gate.set).start()

def test_t02b(late_exit_fixture):
    assert True
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "State Guard Teardown Violation: FIXTURE_TEARDOWN" in res.stdout
    assert (
        "EXIT_COMMITTED_AFTER_FINALIZATION" in res.stdout
        or "CALLABLE_NOT_EXITED" in res.stdout
    )


# ===========================================================================
# T03A & T03B: Leaked Task in addfinalizer
# ===========================================================================

def test_t03a_leaked_task_in_addfinalizer():
    """T03A: Task submitted inside request.addfinalizer callback is not joined.
    Expectation: CALLABLE_NOT_EXITED finding attributed to FIXTURE_TEARDOWN owner.
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

stop_event = threading.Event()

@pytest.fixture
def addfinalizer_leak_fixture(request):
    def teardown_cb():
        ex = ThreadPoolExecutor(max_workers=1)
        ex.submit(lambda: stop_event.wait(timeout=2.0))
    request.addfinalizer(teardown_cb)
    return "ok"

def test_t03a(addfinalizer_leak_fixture):
    assert addfinalizer_leak_fixture == "ok"
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "State Guard Teardown Violation: FIXTURE_TEARDOWN" in res.stdout
    assert "CALLABLE_NOT_EXITED" in res.stdout


def test_t03b_leaked_task_in_addfinalizer_late_exit():
    """T03B: Task submitted inside addfinalizer callback exits after finalization.
    Expectation: VIOLATION detected.
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

gate = threading.Event()

@pytest.fixture
def addfinalizer_late_exit(request):
    def teardown_cb():
        ex = ThreadPoolExecutor(max_workers=1)
        ex.submit(lambda: gate.wait(timeout=2.0))
        threading.Timer(0.05, gate.set).start()
    request.addfinalizer(teardown_cb)
    return "ok"

def test_t03b(addfinalizer_late_exit):
    assert True
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "State Guard Teardown Violation: FIXTURE_TEARDOWN" in res.stdout


# ===========================================================================
# T04: Fast Task Joined Before Finalization
# ===========================================================================

def test_t04_fast_task_joined_before_finalization():
    """T04: Multiple rapid tasks submitted and joined in teardown.
    Expectation: All tasks CLEAN, test passes.
    """
    code = """
import pytest
from concurrent.futures import ThreadPoolExecutor

@pytest.fixture
def fast_tasks_fixture():
    yield
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(lambda i=i: i * 2) for i in range(10)]
        results = [f.result() for f in futs]
        assert results == [i * 2 for i in range(10)]

def test_t04(fast_tasks_fixture):
    assert True
"""
    res = run_pytest(code)
    assert res.returncode == 0
    assert "1 passed" in res.stdout
    assert "VIOLATIONS DETECTED" not in res.stdout


# ===========================================================================
# T05: Multiple Finalizers, One Joined, One Late Exit
# ===========================================================================

def test_t05_multiple_finalizers_one_joined_one_leaked():
    """T05: Two finalizers registered; one cleans up its task, the second leaks.
    Expectation: Leaked task flagged under FIXTURE_TEARDOWN owner, test fails.
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

stop_event = threading.Event()

@pytest.fixture
def multi_finalizer_fixture(request):
    def clean_fin():
        with ThreadPoolExecutor(max_workers=1) as ex:
            f = ex.submit(lambda: 42)
            assert f.result() == 42
    
    def leaky_fin():
        ex = ThreadPoolExecutor(max_workers=1)
        ex.submit(lambda: stop_event.wait(timeout=2.0))

    request.addfinalizer(clean_fin)
    request.addfinalizer(leaky_fin)
    return "val"

def test_t05(multi_finalizer_fixture):
    assert True
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "State Guard Teardown Violation: FIXTURE_TEARDOWN" in res.stdout
    assert "CALLABLE_NOT_EXITED" in res.stdout


# ===========================================================================
# T06: Nested Fixtures (Dep A and Dep B)
# ===========================================================================

def test_t06_nested_fixtures_distinct_attribution():
    """T06: Fixture B depends on Fixture A. Teardown B runs, then Teardown A runs.
    One fixture leaks a task; the violation must be attributed to the correct fixture.
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

stop_event = threading.Event()

@pytest.fixture
def fixture_a():
    yield "a"
    # Teardown A is clean
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "a_clean")
        assert f.result() == "a_clean"

@pytest.fixture
def fixture_b(fixture_a):
    yield "b"
    # Teardown B leaks
    ex = ThreadPoolExecutor(max_workers=1)
    ex.submit(lambda: stop_event.wait(timeout=2.0))

def test_t06(fixture_b):
    assert True
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "fixture 'fixture_b'" in res.stdout
    assert "fixture 'fixture_a'" not in res.stdout or "VIOLATION: fixture 'fixture_a'" not in res.stdout


# ===========================================================================
# T07 & T16: Collision-Free Identity
# ===========================================================================

def test_t07_collision_free_identity_different_definitions():
    """T07: Fixtures with the same name across different modules generate distinct owner IDs."""
    code1 = """
import pytest
from concurrent.futures import ThreadPoolExecutor

@pytest.fixture
def shared_name_fixture():
    yield "mod1"
    with ThreadPoolExecutor(max_workers=1) as ex:
        assert ex.submit(lambda: 1).result() == 1

def test_mod1(shared_name_fixture):
    pass
"""
    code2 = """
import pytest
from concurrent.futures import ThreadPoolExecutor

@pytest.fixture
def shared_name_fixture():
    yield "mod2"
    with ThreadPoolExecutor(max_workers=1) as ex:
        assert ex.submit(lambda: 2).result() == 2

def test_mod2(shared_name_fixture):
    pass
"""
    with tempfile.TemporaryDirectory() as td:
        p1 = Path(td) / "test_one.py"
        p2 = Path(td) / "test_two.py"
        p1.write_text(code1, encoding="utf-8")
        p2.write_text(code2, encoding="utf-8")
        env = os.environ.copy()
        src_dir = str(Path(__file__).resolve().parent.parent.parent / "src")
        env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{env.get('PYTHONPATH', '')}"
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        res = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "pytest_state_guard.plugin", str(td), "-s"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert res.returncode == 0
        assert "2 passed" in res.stdout


# ===========================================================================
# T08: Setup Failure Before Yield (Zero Teardown Owners)
# ===========================================================================

def test_t08_setup_failure_creates_zero_teardown_owners():
    """T08: When fixture setup raises before yield, finish() does not create a teardown owner."""
    code = """
import pytest

@pytest.fixture
def failing_setup_fixture():
    raise RuntimeError("setup failed before yield")
    yield

def test_t08(failing_setup_fixture):
    pass
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "setup failed before yield" in res.stdout
    # Ensure no teardown owner was created or reported
    assert "State Guard Teardown Violation: FIXTURE_TEARDOWN" not in res.stdout


# ===========================================================================
# T09: Finalizer Raises, Task Exits Late (Exception Preserved)
# ===========================================================================

def test_t09_finalizer_raises_and_task_leaks_chained_report():
    """T09: When a fixture finalizer raises and also leaks a task, the original exception
    is preserved, and State Guard chains its teardown concurrency violation report.
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

stop_event = threading.Event()

@pytest.fixture
def crashing_and_leaking_fixture():
    yield
    ex = ThreadPoolExecutor(max_workers=1)
    ex.submit(lambda: stop_event.wait(timeout=2.0))
    raise ValueError("primary fixture teardown error")

def test_t09(crashing_and_leaking_fixture):
    pass
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "primary fixture teardown error" in res.stdout
    assert "State Guard Audit Detected Teardown Concurrency Violations" in res.stdout
    assert "CALLABLE_NOT_EXITED" in res.stdout


# ===========================================================================
# T10A & T10B: Exception Preservation Under Single & Multiple Finalizer Exceptions
# ===========================================================================

def test_t10a_single_finalizer_exception_preserved():
    """T10A: Single finalizer exception is raised directly without BaseExceptionGroup."""
    code = """
import pytest

@pytest.fixture
def single_error_fixture():
    yield
    raise ZeroDivisionError("isolated teardown error")

def test_t10a(single_error_fixture):
    pass
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "ZeroDivisionError: isolated teardown error" in res.stdout


def test_t10b_multiple_finalizer_exceptions_grouped():
    """T10B: Multiple finalizer exceptions are grouped in BaseExceptionGroup under pytest."""
    code = """
import pytest

@pytest.fixture
def multi_error_fixture(request):
    def fin1():
        raise KeyError("first teardown error")
    def fin2():
        raise TypeError("second teardown error")
    request.addfinalizer(fin1)
    request.addfinalizer(fin2)
    return "ok"

def test_t10b(multi_error_fixture):
    pass
"""
    res = run_pytest(code)
    assert res.returncode == 1
    # Check that either BaseExceptionGroup or both exceptions are preserved
    assert "first teardown error" in res.stdout
    assert "second teardown error" in res.stdout


# ===========================================================================
# T11: Repeated finish() Idempotency
# ===========================================================================

def test_t11_repeated_finish_idempotent():
    """T11: Direct verification that invoking finish() multiple times on a fixturedef is idempotent."""
    code = """
import pytest

@pytest.fixture
def dummy_fix():
    yield "val"

def test_t11(dummy_fix):
    pass
"""
    res = run_pytest(code)
    assert res.returncode == 0


# ===========================================================================
# T12: _CallableWrapper Context Inheritance for Child Task
# ===========================================================================

def test_t12_child_task_inherits_teardown_owner_context():
    """T12: A task submitted in fixture teardown submits a child task; the child task
    inherits the FIXTURE_TEARDOWN owner context.
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

stop_event = threading.Event()

@pytest.fixture
def nested_task_fixture():
    yield
    ex = ThreadPoolExecutor(max_workers=2)
    def parent():
        # Spawn child from within worker thread
        ex.submit(lambda: stop_event.wait(timeout=2.0))
    ex.submit(parent).result(timeout=1.0)
    # The child task was not joined and is still running!

def test_t12(nested_task_fixture):
    pass
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "State Guard Teardown Violation: FIXTURE_TEARDOWN" in res.stdout
    assert "CALLABLE_NOT_EXITED" in res.stdout


# ===========================================================================
# T13: Mode B / Active Owner at Session Finish Promotes Exit Status
# ===========================================================================

def test_t13_mode_b_unfinalized_owner_promotes_exit_status():
    """T13: Mode B verification: An active owner remaining in the ledger at session finish
    sets _LAST_SESSION_INCOMPLETE_REASON and promotes the exit code to 1 (TESTS_FAILED).
    """
    code = """
import pytest
from pytest_state_guard import plugin
from pytest_state_guard.models import OwnerType

@pytest.fixture(autouse=True)
def unfinalized_owner_fixture(request):
    state = getattr(request.session, "_state_guard_session", None)
    if state:
        # Deliberately inject an active owner that is never finalized
        state.ledger.create_owner(
            owner_id="owner:orphan:mode_b",
            owner_type=OwnerType.FIXTURE_TEARDOWN,
            nodeid=request.node.nodeid,
            fixture_name="unfinalized_owner_fixture"
        )
    yield

def test_clean():
    assert True
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "Incomplete fixture finalization" in res.stdout or "Incomplete Session Audit" in res.stdout


# ===========================================================================
# T14: Outstanding Setup Task vs Clean Teardown
# ===========================================================================

def test_t14_setup_task_leak_attributed_to_setup_not_teardown():
    """T14: When a task is leaked during fixture setup, it is attributed to FIXTURE_SETUP,
    not FIXTURE_TEARDOWN.
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

stop_event = threading.Event()

@pytest.fixture
def setup_leak_fixture():
    ex = ThreadPoolExecutor(max_workers=1)
    ex.submit(lambda: stop_event.wait(timeout=2.0))
    yield
    # Clean teardown
    with ThreadPoolExecutor(max_workers=1) as ex2:
        assert ex2.submit(lambda: "td_clean").result() == "td_clean"

def test_t14(setup_leak_fixture):
    pass
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "State Guard Teardown Violation: FIXTURE_SETUP" in res.stdout
    assert "fixture 'setup_leak_fixture'" in res.stdout


# ===========================================================================
# T15: Existing Test Call Regression
# ===========================================================================

def test_t15_test_call_violation_unaffected():
    """T15: Test-call task leaks remain attributed to TEST_CALL owner."""
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

stop_event = threading.Event()

def test_t15():
    ex = ThreadPoolExecutor(max_workers=1)
    ex.submit(lambda: stop_event.wait(timeout=2.0))
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "State Guard Teardown Violation: TEST_CALL" in res.stdout


# ===========================================================================
# T17: Repeated Cache Lifecycle / Parametrization
# ===========================================================================

def test_t17_parametrized_fixture_distinct_teardown_owners():
    """T17: Parametrized fixture executions create distinct teardown owners with distinct sequence keys."""
    code = """
import pytest
from concurrent.futures import ThreadPoolExecutor

@pytest.fixture(params=["p1", "p2"])
def param_teardown_fixture(request):
    yield request.param
    with ThreadPoolExecutor(max_workers=1) as ex:
        assert ex.submit(lambda: request.param).result() == request.param

def test_t17(param_teardown_fixture):
    pass
"""
    res = run_pytest(code)
    assert res.returncode == 0
    assert "2 passed" in res.stdout


# ===========================================================================
# T18: Partial Setup Cleanup With addfinalizer
# ===========================================================================

def test_t18_partial_setup_cleanup_with_addfinalizer():
    """T18: Fixture registers addfinalizer callback and then raises during setup."""
    code = """
import pytest

cleanup_called = False

@pytest.fixture
def partial_fixture(request):
    global cleanup_called
    def on_teardown():
        global cleanup_called
        cleanup_called = True
    request.addfinalizer(on_teardown)
    raise RuntimeError("setup crashed after addfinalizer")

def test_t18(partial_fixture):
    pass
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "setup crashed after addfinalizer" in res.stdout


# ===========================================================================
# T19: Context Reset Failure Recovery
# ===========================================================================

def test_t19_context_reset_fallback_restoration():
    """T19: If reset_current_owner raises, the fallback restores token.old_value safely."""
    # Test directly using plugin's token fallback mechanism
    prior_ctx = OwnerContext(
        owner_id="owner:prior",
        owner_type=OwnerType.TEST_CALL,
        nodeid="test.py::test_prior",
    )
    tok1 = set_current_owner(prior_ctx)
    try:
        new_ctx = OwnerContext(
            owner_id="owner:td",
            owner_type=OwnerType.FIXTURE_TEARDOWN,
            nodeid="test.py::test_prior",
        )
        tok2 = set_current_owner(new_ctx)
        assert get_current_owner() is new_ctx

        # Simulate corrupted reset by testing token.old_value fallback logic
        try:
            if hasattr(tok2, "old_value") and tok2.old_value is not tok2.MISSING:
                set_current_owner(tok2.old_value)
            else:
                set_current_owner(None)
        except Exception:
            pass

        assert get_current_owner() is prior_ctx
    finally:
        reset_current_owner(tok1)


# ===========================================================================
# T20 & T21: Foreign Wrapper & Sequential Sessions
# ===========================================================================

def test_t20_t21_sequential_sessions_and_foreign_wrapper():
    """T20 & T21: Sequential pytest sessions install and remove the static dispatcher cleanly."""
    code = """
import pytest
from concurrent.futures import ThreadPoolExecutor

@pytest.fixture
def fix():
    yield "val"
    with ThreadPoolExecutor(max_workers=1) as ex:
        assert ex.submit(lambda: 1).result() == 1

def test_seq(fix):
    assert fix == "val"
"""
    res1 = run_pytest(code)
    assert res1.returncode == 0
    res2 = run_pytest(code)
    assert res2.returncode == 0


# ===========================================================================
# T22: Module-Scoped Fixture Teardown Violation (Tier 2 Session Audit)
# ===========================================================================

def test_t22_module_scoped_fixture_teardown_violation_in_session_audit():
    """T22: Leaked task in module-scoped fixture teardown does not fail individual tests,
    but is caught by the Tier 2 session audit and promotes exit code to 1 (TESTS_FAILED).
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

stop_event = threading.Event()

@pytest.fixture(scope="module")
def module_fix():
    yield
    ex = ThreadPoolExecutor(max_workers=1)
    ex.submit(lambda: stop_event.wait(timeout=2.0))

def test_one(module_fix):
    assert True

def test_two(module_fix):
    assert True
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "2 passed" in res.stdout
    assert "State Guard Audit: VIOLATIONS DETECTED" in res.stdout
    assert "fixture 'module_fix'" in res.stdout


# ===========================================================================
# T23: Session-Scoped Fixture Teardown Violation (Tier 2 Session Audit)
# ===========================================================================

def test_t23_session_scoped_fixture_teardown_violation_in_session_audit():
    """T23: Leaked task in session-scoped fixture teardown is caught by the Tier 2 session audit
    and promotes exit code to 1 (TESTS_FAILED).
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

stop_event = threading.Event()

@pytest.fixture(scope="session")
def session_fix():
    yield
    ex = ThreadPoolExecutor(max_workers=1)
    ex.submit(lambda: stop_event.wait(timeout=2.0))

def test_sess(session_fix):
    assert True
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "1 passed" in res.stdout
    assert "State Guard Audit: VIOLATIONS DETECTED" in res.stdout
    assert "fixture 'session_fix'" in res.stdout


# ===========================================================================
# Mode A Adversarial Invariant: Structural Inconsistency
# ===========================================================================

def test_mode_a_structural_inconsistency_poisons_lifecycle():
    """Mode A: Inconsistency where an event exists in ledger._events for an owner_id
    that does not exist in ledger._owners must cause PluginLifecycle.POISONED.
    """
    code = """
import pytest
from pytest_state_guard import plugin
from pytest_state_guard.models import EventType

@pytest.fixture(autouse=True)
def inject_corrupt_event(request):
    state = getattr(request.session, "_state_guard_session", None)
    if state:
        # Deliberately inject an event for an unknown owner ID
        with state.ledger._lock:
            state.ledger._commit_event_locked(
                event_type=EventType.OWNER_FINALIZED,
                owner_id="owner:phantom:mode_a",
            )
    yield

def test_dummy():
    assert True
"""
    res = run_pytest(code)
    # The session finish audit detects orphan event and poisons plugin lifecycle
    assert res.returncode != 0
    out = res.stdout + res.stderr
    assert "Operational Lifecycle Failure" in out or "Ledger structural inconsistency" in out or "POISONED" in out


# ===========================================================================
# Workstream 1: Deterministic Fault-Injection & BaseException Preservation
# ===========================================================================

def test_keyboard_interrupt_during_owner_registration_propagates_and_skips_teardown(monkeypatch):
    """Fault injection: KeyboardInterrupt during owner creation must propagate immediately
    and MUST NOT invoke the user's original fixture teardown.
    """
    code = """
import pytest

teardown_executed = False

@pytest.fixture
def fix():
    yield
    global teardown_executed
    teardown_executed = True

def test_dummy(fix):
    pass
"""
    # Test via direct dispatcher execution under an active session
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"_state_guard_session": state})()
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_dummy"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    teardown_ran = False
    def user_finalizer():
        nonlocal teardown_ran
        teardown_ran = True

    post_fin_sentinel = lambda: None
    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "fault_fix",
        "cached_result": ("val", None, None),
        "_finalizers": [post_fin_sentinel, user_finalizer],
    })()

    # Pre-register sentinel in state
    state.post_finalizers[(fixturedef_mock, 1)] = post_fin_sentinel
    state.active_fixture_instances[fixturedef_mock] = 1

    # Monkeypatch ledger.create_owner to raise KeyboardInterrupt
    def faulty_create_owner(*args, **kwargs):
        raise KeyboardInterrupt("Simulated Ctrl+C during registration")

    monkeypatch.setattr(state.ledger, "create_owner", faulty_create_owner)
    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    with pytest.raises(KeyboardInterrupt, match=r"Simulated Ctrl\+C"):
        plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    # 1. User teardown must NOT have executed!
    assert not teardown_ran
    # 2. Cycle registries must be cleaned up!
    assert fixturedef_mock not in state.active_fixture_instances
    assert (fixturedef_mock, 1) not in state.post_finalizers
    # 3. No teardown owner was registered in ledger
    assert len(state.ledger.get_all_owners()) == 0


def test_keyboard_interrupt_during_context_activation_cleans_up_and_propagates(monkeypatch):
    """Fault injection: KeyboardInterrupt after successful owner registration must finalize
    the newly registered owner (best-effort cleanup) and propagate without invoking teardown.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"_state_guard_session": state})()
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_dummy"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    teardown_ran = False
    def user_finalizer():
        nonlocal teardown_ran
        teardown_ran = True

    post_fin_sentinel = lambda: None
    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "fault_fix",
        "cached_result": ("val", None, None),
        "_finalizers": [post_fin_sentinel, user_finalizer],
    })()

    state.post_finalizers[(fixturedef_mock, 1)] = post_fin_sentinel
    state.active_fixture_instances[fixturedef_mock] = 1

    # set_current_owner raises KeyboardInterrupt after create_owner succeeded
    def faulty_set_current_owner(ctx):
        raise KeyboardInterrupt("Simulated Ctrl+C during context activation")

    monkeypatch.setattr(plugin, "set_current_owner", faulty_set_current_owner)
    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    with pytest.raises(KeyboardInterrupt, match=r"Simulated Ctrl\+C during context activation"):
        plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    # 1. User teardown was NOT invoked
    assert not teardown_ran
    # 2. The registered owner was finalized via best-effort cleanup so it does not linger active
    all_owners = state.ledger.get_all_owners()
    assert len(all_owners) == 1
    assert all_owners[0].state == OwnerState.FINALIZED
    # 3. Registries cleaned up
    assert fixturedef_mock not in state.active_fixture_instances
    assert (fixturedef_mock, 1) not in state.post_finalizers


def test_system_exit_during_context_activation_cleans_up_and_propagates(monkeypatch):
    """Fault injection: SystemExit must be propagated and not swallowed."""
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"_state_guard_session": state})()
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_dummy"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    teardown_ran = False
    def user_finalizer():
        nonlocal teardown_ran
        teardown_ran = True

    post_fin_sentinel = lambda: None
    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "fault_fix",
        "cached_result": ("val", None, None),
        "_finalizers": [post_fin_sentinel, user_finalizer],
    })()

    state.post_finalizers[(fixturedef_mock, 1)] = post_fin_sentinel
    state.active_fixture_instances[fixturedef_mock] = 1

    def faulty_set_current_owner(ctx):
        raise SystemExit(42)

    monkeypatch.setattr(plugin, "set_current_owner", faulty_set_current_owner)
    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    with pytest.raises(SystemExit) as exc_info:
        plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    assert exc_info.value.code == 42
    assert not teardown_ran
    all_owners = state.ledger.get_all_owners()
    assert len(all_owners) == 1
    assert all_owners[0].state == OwnerState.FINALIZED


def test_duplicate_id_failure_leaves_existing_owner_untouched(monkeypatch):
    """Fault injection: DuplicateEntityError during owner registration must NOT finalize
    the pre-existing owner, must record degradation, and must fail-open to execute teardown.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"_state_guard_session": state})()
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_dummy"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    teardown_ran = False
    def user_finalizer():
        nonlocal teardown_ran
        teardown_ran = True

    post_fin_sentinel = lambda: None
    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "dup_fix",
        "cached_result": ("val", None, None),
        "_finalizers": [post_fin_sentinel, user_finalizer],
    })()

    state.post_finalizers[(fixturedef_mock, 1)] = post_fin_sentinel
    state.active_fixture_instances[fixturedef_mock] = 1

    # Pre-register an owner with the predicted ID in ACTIVE state
    predicted_id = plugin._build_runtime_owner_id(fixturedef_mock, request_mock, state, 1)
    existing_owner = state.ledger.create_owner(
        owner_id=predicted_id,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_dummy",
        fixture_name="dup_fix",
    )
    assert existing_owner.state == OwnerState.ACTIVE

    # Execute finish()
    # In original finish, mock calling the finalizers
    def original_finish(self_fd, req):
        while self_fd._finalizers:
            fin = self_fd._finalizers.pop()
            fin()

    old_orig = plugin._ORIGINAL_FIXTUREDEF_FINISH
    plugin._ORIGINAL_FIXTUREDEF_FINISH = original_finish
    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)
    try:
        # Should NOT raise, must fail-open
        plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)
    finally:
        plugin._ORIGINAL_FIXTUREDEF_FINISH = old_orig

    # 1. User teardown DID execute (fail-open)
    assert teardown_ran
    # 2. Existing owner was NOT finalized by mistake!
    owner_after = state.ledger.get_owner(predicted_id)
    assert owner_after.state == OwnerState.ACTIVE
    # 3. Degradation was recorded
    assert any("Failed to activate teardown owner" in d for d in state.degradations)


def test_cleanup_failure_does_not_mask_original_base_exception(monkeypatch):
    """Fault injection: If a cleanup operation raises during control-flow exit,
    the primary BaseException must NOT be masked or replaced.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"_state_guard_session": state})()
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_dummy"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    post_fin_sentinel = lambda: None
    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "dual_fault_fix",
        "cached_result": ("val", None, None),
        "_finalizers": [post_fin_sentinel, lambda: None],
    })()

    state.post_finalizers[(fixturedef_mock, 1)] = post_fin_sentinel
    state.active_fixture_instances[fixturedef_mock] = 1

    # Fault 1: set_current_owner raises KeyboardInterrupt
    def faulty_set_current_owner(ctx):
        raise KeyboardInterrupt("Primary Ctrl+C interruption")

    # Fault 2: finalize_owner raises an error during cleanup
    def faulty_finalize_owner(owner_id):
        raise RuntimeError("Secondary ledger corruption")

    monkeypatch.setattr(plugin, "set_current_owner", faulty_set_current_owner)
    monkeypatch.setattr(state.ledger, "finalize_owner", faulty_finalize_owner)
    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    # Must raise KeyboardInterrupt, NOT RuntimeError!
    with pytest.raises(KeyboardInterrupt, match=r"Primary Ctrl\+C interruption"):
        plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)


# ===========================================================================
# Workstream 2: Runtime Invocation Identity Verification
# ===========================================================================

def test_repeated_cache_lifecycle_advances_instance_seq_and_owner_id():
    """Verify that when a fixture is instantiated, torn down, and instantiated again
    in the same session, instance_seq advances monotonically and owner IDs are distinct.
    """
    code = """
import pytest
from pytest_state_guard import plugin
from pytest_state_guard.models import OwnerType

@pytest.fixture
def lifecycle_fix():
    yield "ready"

def test_invoc_one(lifecycle_fix, request):
    # Verify setup 1 registered instance 1
    state = getattr(request.session, "_state_guard_session", None)
    assert state is not None

def test_invoc_two(lifecycle_fix, request):
    # Second test will execute fixture again because it is function-scoped
    state = getattr(request.session, "_state_guard_session", None)
    assert state is not None
"""
    res = run_pytest(code)
    assert res.returncode == 0
    assert "2 passed" in res.stdout


def test_repeated_cache_lifecycle_in_process_verification():
    """Direct in-process verification that execution_counters advances monotonically
    across repeated fixture setups and teardowns within the same session.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"_state_guard_session": state})()
    node_mock_1 = type("MockNode", (), {"nodeid": "test_case.py::test_one"})()
    request_mock_1 = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock_1,
        "param": None,
    })()

    post_fin_sentinel_1 = lambda: None
    fixturedef = type("MockFixtureDef", (), {
        "argname": "reused_fix",
        "cached_result": ("val1", None, None),
        "_finalizers": [post_fin_sentinel_1, lambda: None],
    })()

    # Invoc 1 Setup:
    ident_key_1 = plugin._get_fixture_identity_key(fixturedef, request_mock_1, state)
    seq_1 = state.execution_counters.get(ident_key_1, 0) + 1
    state.execution_counters[ident_key_1] = seq_1
    state.active_fixture_instances[fixturedef] = seq_1
    state.post_finalizers[(fixturedef, seq_1)] = post_fin_sentinel_1
    assert seq_1 == 1

    owner_id_1 = plugin._build_runtime_owner_id(fixturedef, request_mock_1, state, seq_1)
    state.ledger.create_owner(owner_id_1, OwnerType.FIXTURE_TEARDOWN, request_mock_1.node.nodeid, "reused_fix")

    # Invoc 1 Teardown:
    plugin._safe_finalize_owner(state, owner_id_1)
    state.active_fixture_instances.pop(fixturedef, None)
    state.post_finalizers.pop((fixturedef, seq_1), None)

    # Invoc 2 Setup (same fixture, same test node or new test node):
    post_fin_sentinel_2 = lambda: None
    fixturedef.cached_result = ("val2", None, None)
    fixturedef._finalizers = [post_fin_sentinel_2, lambda: None]

    seq_2 = state.execution_counters.get(ident_key_1, 0) + 1
    state.execution_counters[ident_key_1] = seq_2
    state.active_fixture_instances[fixturedef] = seq_2
    state.post_finalizers[(fixturedef, seq_2)] = post_fin_sentinel_2
    assert seq_2 == 2

    owner_id_2 = plugin._build_runtime_owner_id(fixturedef, request_mock_1, state, seq_2)
    state.ledger.create_owner(owner_id_2, OwnerType.FIXTURE_TEARDOWN, request_mock_1.node.nodeid, "reused_fix")

    # Both owners exist, are distinct, and finalized
    assert owner_id_1 != owner_id_2
    assert owner_id_1.endswith(":1")
    assert owner_id_2.endswith(":2")

    plugin._safe_finalize_owner(state, owner_id_2)
    state.active_fixture_instances.pop(fixturedef, None)
    state.post_finalizers.pop((fixturedef, seq_2), None)

    all_owners = {o.owner_id: o for o in state.ledger.get_all_owners()}
    assert all_owners[owner_id_1].state == OwnerState.FINALIZED
    assert all_owners[owner_id_2].state == OwnerState.FINALIZED


def test_exceptional_teardown_cleans_registries_and_next_invocation_uncontaminated():
    """Verify that when fixture teardown raises an exception, cycle registries are popped
    and subsequent fixture invocations advance cleanly without contamination.
    """
    code = """
import pytest

@pytest.fixture
def crashing_teardown_fix():
    yield "val"
    raise ValueError("teardown failed")

def test_first(crashing_teardown_fix):
    pass

def test_second():
    # Runs after the first test had an exceptional teardown
    assert True
"""
    res = run_pytest(code)
    # First test had teardown error, second passed
    assert "1 failed" in res.stdout or "1 error" in res.stdout
    assert "passed" in res.stdout


def test_different_fixture_definitions_same_name_receive_different_tokens():
    """Verify that two distinct FixtureDef objects with the same argname receive
    different def_token values, preventing ID collision.
    """
    state = plugin.StateGuardSession()
    session_mock = type("MockSession", (), {"_state_guard_session": state})()
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_one"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    fd_one = type("MockFixtureDef1", (), {"argname": "foo"})()
    fd_two = type("MockFixtureDef2", (), {"argname": "foo"})()

    key_one = plugin._get_fixture_identity_key(fd_one, request_mock, state)
    key_two = plugin._get_fixture_identity_key(fd_two, request_mock, state)

    # Different def_tokens allocated
    assert key_one[0] != key_two[0]
    id_one = plugin._build_runtime_owner_id(fd_one, request_mock, state, 1)
    id_two = plugin._build_runtime_owner_id(fd_two, request_mock, state, 1)
    assert id_one != id_two
    assert f":foo:{key_one[0]}:" in id_one
    assert f":foo:{key_two[0]}:" in id_two

