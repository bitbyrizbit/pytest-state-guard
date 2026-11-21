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
    """Execute pytest in an isolated subprocess with State Guard active."""
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
# SCEN-01: Class-Scoped Teardown Audit
# ===========================================================================

def test_scen01_class_scoped_teardown_audit():
    """SCEN-01: Class-scoped fixture teardown task leak.
    Verifies that per-test reporting does NOT fail individual test methods (nodeid mismatch),
    while the authoritative Tier-2 session audit captures the shared-scope owner violation
    and promotes the session exit status from 0 to 1 (TESTS_FAILED).
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

gate = threading.Event()

@pytest.fixture(scope="class")
def class_leaked_fixture():
    yield
    ex = ThreadPoolExecutor(max_workers=1)
    ex.submit(lambda: gate.wait(timeout=2.0))
    # Unjoined task leaks across class teardown boundary

class TestClassScen01:
    def test_one(self, class_leaked_fixture):
        assert True

    def test_two(self, class_leaked_fixture):
        assert True
"""
    res = run_pytest(code, extra_args=["-v"])
    assert res.returncode == 1
    assert "2 passed" in res.stdout
    # Individual test methods did not fail in teardown
    assert "test_one PASSED" in res.stdout
    assert "test_two PASSED" in res.stdout
    # Session audit displays the violation for the class-scoped fixture
    assert "VIOLATIONS DETECTED" in res.stdout
    assert "CALLABLE_NOT_EXITED" in res.stdout
    assert "class_leaked_fixture" in res.stdout


# ===========================================================================
# SCEN-02: Sibling Fixture Isolation
# ===========================================================================

def test_scen02_sibling_fixture_isolation():
    """SCEN-02: Sibling fixture ownership isolation under diamond dependency.
    A clean sibling and a leaking sibling depend on a common root fixture.
    Verifies that sibling ownership remains distinct, clean sibling and root parent
    remain clean, and the violation is attributed strictly to the leaking sibling.
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

gate = threading.Event()

@pytest.fixture
def root_fixture():
    yield "root"
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "root_clean")
        assert f.result(timeout=5.0) == "root_clean"

@pytest.fixture
def child_clean(root_fixture):
    yield f"{root_fixture}_clean"
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "child_clean_done")
        assert f.result(timeout=5.0) == "child_clean_done"

@pytest.fixture
def child_leak(root_fixture):
    yield f"{root_fixture}_leak"
    ex = ThreadPoolExecutor(max_workers=1)
    ex.submit(lambda: gate.wait(timeout=2.0))

def test_diamond(child_clean, child_leak):
    assert True
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "State Guard Teardown Violation: FIXTURE_TEARDOWN" in res.stdout
    assert "fixture 'child_leak'" in res.stdout
    assert "CALLABLE_NOT_EXITED" in res.stdout
    # Neither child_clean nor root_fixture is flagged as violating
    assert "fixture 'child_clean'" not in res.stdout
    assert "fixture 'root_fixture'" not in res.stdout


# ===========================================================================
# SCEN-03: Cross-Scope Teardown Chain
# ===========================================================================

def test_scen03_cross_scope_teardown_chain():
    """SCEN-03: Cross-scope dependency chain (session -> module -> function).
    Finalizing function scope reports violation on test report; outer scopes remain clean
    at their respective module/session boundaries.
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

gate = threading.Event()

@pytest.fixture(scope="session")
def session_fix():
    yield "session_data"
    with ThreadPoolExecutor(max_workers=1) as ex:
        assert ex.submit(lambda: "session_clean").result(timeout=5.0) == "session_clean"

@pytest.fixture(scope="module")
def module_fix(session_fix):
    yield f"module_data_using_{session_fix}"
    with ThreadPoolExecutor(max_workers=1) as ex:
        assert ex.submit(lambda: "module_clean").result(timeout=5.0) == "module_clean"

@pytest.fixture(scope="function")
def func_fix(module_fix):
    yield f"func_data_using_{module_fix}"
    ex = ThreadPoolExecutor(max_workers=1)
    ex.submit(lambda: gate.wait(timeout=2.0))

def test_cross_chain(func_fix):
    assert True
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "State Guard Teardown Violation: FIXTURE_TEARDOWN" in res.stdout
    assert "fixture 'func_fix'" in res.stdout
    assert "CALLABLE_NOT_EXITED" in res.stdout
    # Outer scopes must not be flagged
    assert "fixture 'module_fix'" not in res.stdout
    assert "fixture 'session_fix'" not in res.stdout


# ===========================================================================
# SCEN-04: Mode B via Teardown Finalization Failure
# ===========================================================================

def test_scen04_mode_b_via_finalization_failure(monkeypatch):
    """SCEN-04: Mode B shutdown audit via teardown owner finalization failure.
    Direct in-process verification of exact teardown owner retention, degradation log,
    Mode B active_ids identification, incomplete-session reason, and exit code promotion.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_scen04"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    user_fin_ran = False
    def user_finalizer():
        nonlocal user_fin_ran
        user_fin_ran = True

    post_fin_sentinel = lambda: None
    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "fail_finalize_fix",
        "cached_result": ("val", None, None),
        "_finalizers": [post_fin_sentinel, user_finalizer],
    })()

    # Pre-register sentinel and active instance
    state.post_finalizers[(fixturedef_mock, 1)] = post_fin_sentinel
    state.active_fixture_instances[fixturedef_mock] = 1

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    # Capture expected runtime owner ID
    expected_owner_id = plugin._build_runtime_owner_id(fixturedef_mock, request_mock, state, 1)

    # Monkeypatch ledger.finalize_owner to raise when finalizing this teardown owner
    real_finalize_owner = ledger.finalize_owner
    def faulty_finalize_owner(owner_id: str):
        if owner_id == expected_owner_id:
            raise RuntimeError("Simulated ledger lock failure")
        return real_finalize_owner(owner_id)

    monkeypatch.setattr(state.ledger, "finalize_owner", faulty_finalize_owner)

    # Execute dispatcher
    plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    # 1. User teardown callback executed
    assert user_fin_ran is True

    # 2. Teardown owner exists in ledger
    owners_by_id = {o.owner_id: o for o in state.ledger.get_all_owners()}
    assert expected_owner_id in owners_by_id
    teardown_owner = owners_by_id[expected_owner_id]

    # 3. Owner type is FIXTURE_TEARDOWN
    assert teardown_owner.owner_type == OwnerType.FIXTURE_TEARDOWN

    # 4. State remains ACTIVE (forced finalization failed)
    assert teardown_owner.state == OwnerState.ACTIVE

    # 5. Expected degradation was recorded in state.degradations
    assert any(
        f"Failed to finalize owner '{expected_owner_id}': Simulated ledger lock failure" in deg
        for deg in state.degradations
    )

    # 6. Execute session finish audit directly
    plugin._ACTIVE_SESSION = state
    plugin._LIFECYCLE_STATE = plugin.PluginLifecycle.ACTIVE

    class DirectOutcome:
        excinfo = None
        def force_exception(self, exc: BaseException) -> None:
            raise exc

    plugin._evaluate_and_cleanup_session_finish(session_mock, DirectOutcome(), state)

    # 7. Mode B active_ids identified this exact owner ID
    all_owners = state.ledger.get_all_owners()
    active_ids = {o.owner_id for o in all_owners if o.state == OwnerState.ACTIVE}
    assert expected_owner_id in active_ids

    # 8. Incomplete session reason matches active owner count
    assert plugin._LAST_SESSION_INCOMPLETE_REASON == f"Incomplete fixture finalization: {len(active_ids)} owner(s) remained active"

    # 9. Session exit status promoted to TESTS_FAILED
    assert session_mock.exitstatus == pytest.ExitCode.TESTS_FAILED


# ===========================================================================
# SCEN-05: Composite Teardown Callbacks
# ===========================================================================

def test_scen05_composite_teardown_callbacks():
    """SCEN-05: Composite teardown callbacks (generator yield + multiple addfinalizers).
    Exact LIFO ordering: generator post-yield -> fin_2 -> fin_1 -> sentinel.
    Single FIXTURE_TEARDOWN owner encompasses all callbacks.
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

gate = threading.Event()
execution_order = []

@pytest.fixture
def composite_fixture(request):
    def fin_1():
        execution_order.append("fin_1")
        # Leaked task
        ex = ThreadPoolExecutor(max_workers=1)
        ex.submit(lambda: gate.wait(timeout=2.0))

    def fin_2():
        execution_order.append("fin_2")
        with ThreadPoolExecutor(max_workers=1) as ex:
            f = ex.submit(lambda: "fin_2_clean")
            assert f.result(timeout=5.0) == "fin_2_clean"

    request.addfinalizer(fin_1)
    request.addfinalizer(fin_2)

    yield "ready"

    execution_order.append("generator_post_yield")
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "gen_clean")
        assert f.result(timeout=5.0) == "gen_clean"

def test_composite(composite_fixture):
    pass

def test_verify_order():
    # Pytest executes composite_fixture teardown before test_verify_order
    assert execution_order == ["generator_post_yield", "fin_2", "fin_1"]
"""
    res = run_pytest(code)
    assert res.returncode == 1
    assert "State Guard Teardown Violation: FIXTURE_TEARDOWN" in res.stdout
    assert "CALLABLE_NOT_EXITED" in res.stdout
    assert "fixture 'composite_fixture'" in res.stdout


# ===========================================================================
# SCEN-06: Sentinel Registry & Integrity (Cases A & B)
# ===========================================================================

def test_scen06_case_a_missing_registry_entry_fails_open(monkeypatch):
    """SCEN-06 Case A: Missing sentinel registry entry.
    When state.post_finalizers entry is missing while finalizers are present,
    State Guard detects the missing sentinel, records degradation, and fails open
    to execute the original teardown uninstrumented without raising.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"_state_guard_session": state})()
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_case_a"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    user_fin_ran = False
    def user_finalizer():
        nonlocal user_fin_ran
        user_fin_ran = True

    post_fin_sentinel = lambda: None
    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "missing_sentinel_fix",
        "cached_result": ("val", None, None),
        "_finalizers": [post_fin_sentinel, user_finalizer],
    })()

    # Pre-register instance counter, but DO NOT register sentinel in state.post_finalizers!
    state.active_fixture_instances[fixturedef_mock] = 1
    assert (fixturedef_mock, 1) not in state.post_finalizers

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    # Execute dispatcher
    plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    # 1. User teardown callback executed (fail-open)
    assert user_fin_ran is True

    # 2. Degradation recorded for missing sentinel
    assert any(
        "Missing post-finalizer sentinel for fixture 'missing_sentinel_fix' at sequence 1" in deg
        for deg in state.degradations
    )

    # 3. No teardown owner was created in ledger
    assert len([o for o in state.ledger.get_all_owners() if o.owner_type == OwnerType.FIXTURE_TEARDOWN]) == 0


def test_scen06_case_b_replaced_sentinel_leaves_setup_active(monkeypatch):
    """SCEN-06 Case B: Replaced sentinel in finalizers list.
    When foreign code replaces the sentinel in fixturedef._finalizers with a foreign callback,
    the dispatcher runs, treats the foreign callback as a user callback, and finalizes the
    teardown owner. However, because the original sentinel never executed, the post-finalizer
    hook never ran, leaving the setup owner in OwnerState.ACTIVE.
    Mode B catches the retained setup owner and promotes the session exit status.

    Architectural Distinction:
    - SCEN-06 Case B: The dispatcher runs, but sentinel replacement suppresses the post-finalizer
      hook, leaving the setup owner active. Mode B detects the retained owner.
    - SCEN-10: A non-delegating FixtureDef.finish replacement bypasses the dispatcher while the
      original sentinel executes. That is a separate Stage 15A production-detection contract.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_case_b"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    # 1. Setup Phase: create active setup owner
    setup_owner_id = "owner:fixture:replaced_sentinel_fix:uuid123"
    state.ledger.create_owner(
        owner_id=setup_owner_id,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_case_b",
        fixture_name="replaced_sentinel_fix",
    )

    original_sentinel_ran = False
    def original_sentinel():
        nonlocal original_sentinel_ran
        original_sentinel_ran = True

    foreign_ran = False
    def foreign_callback():
        nonlocal foreign_ran
        foreign_ran = True

    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "replaced_sentinel_fix",
        "cached_result": ("val", None, None),
        "_finalizers": [foreign_callback],  # Sentinel was replaced at index 0!
    })()

    # Record setup owner in active_fixture_owners and sentinel in post_finalizers
    state.active_fixture_owners[fixturedef_mock] = setup_owner_id
    state.post_finalizers[(fixturedef_mock, 1)] = original_sentinel
    state.active_fixture_instances[fixturedef_mock] = 1

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    # Execute dispatcher
    plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    # 1. Foreign callback executed
    assert foreign_ran is True

    # 2. Original sentinel was NOT executed
    assert original_sentinel_ran is False

    # 3. pytest_fixture_post_finalizer was NOT invoked for this fixture
    assert fixturedef_mock in state.active_fixture_owners

    # 4. Setup owner ledger record exists
    owners_by_id = {o.owner_id: o for o in state.ledger.get_all_owners()}
    assert setup_owner_id in owners_by_id
    setup_owner = owners_by_id[setup_owner_id]

    # 5. Setup owner remains in OwnerState.ACTIVE
    assert setup_owner.state == OwnerState.ACTIVE

    # 6. Teardown owner was independently created and finalized normally
    teardown_owner_id = plugin._build_runtime_owner_id(fixturedef_mock, request_mock, state, 1)
    assert teardown_owner_id in owners_by_id
    teardown_owner = owners_by_id[teardown_owner_id]
    assert teardown_owner.state == OwnerState.FINALIZED

    # 7. Session finish audit
    plugin._ACTIVE_SESSION = state
    plugin._LIFECYCLE_STATE = plugin.PluginLifecycle.ACTIVE

    class DirectOutcome:
        excinfo = None
        def force_exception(self, exc: BaseException) -> None:
            raise exc

    plugin._evaluate_and_cleanup_session_finish(session_mock, DirectOutcome(), state)

    # 8. Mode B active_ids contains the setup owner ID
    all_owners = state.ledger.get_all_owners()
    active_ids = {o.owner_id for o in all_owners if o.state == OwnerState.ACTIVE}
    assert setup_owner_id in active_ids

    # 9. Teardown owner is absent from active_ids
    assert teardown_owner_id not in active_ids

    # 10. Incomplete session reason matches active owner count
    assert plugin._LAST_SESSION_INCOMPLETE_REASON == f"Incomplete fixture finalization: {len(active_ids)} owner(s) remained active"

    # 11. Session exit status promoted to TESTS_FAILED
    assert session_mock.exitstatus == pytest.ExitCode.TESTS_FAILED


# ===========================================================================
# SCEN-07: Teardown KeyboardInterrupt
# ===========================================================================

def test_scen07_teardown_keyboard_interrupt_completes_finalizers(monkeypatch):
    """SCEN-07: User finalizer raises KeyboardInterrupt.
    Pytest's FixtureDef.finish() continues processing the remaining finalizers
    (including previous finalizers and the sentinel), State Guard's dispatcher cleans up
    context and finalizes the teardown owner, setup owner is finalized by sentinel,
    and KeyboardInterrupt propagates cleanly after processing.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"_state_guard_session": state})()
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_scen07"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    # Setup owner in ledger
    setup_owner_id = "owner:fixture:interrupt_fix:uuid777"
    state.ledger.create_owner(
        owner_id=setup_owner_id,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_scen07",
        fixture_name="interrupt_fix",
    )

    # Post-finalizer sentinel simulates pytest_fixture_post_finalizer
    sentinel_ran = False
    def post_fin_sentinel():
        nonlocal sentinel_ran
        sentinel_ran = True
        state.ledger.finalize_owner(setup_owner_id)

    fin_1_ran = False
    def fin_1():
        nonlocal fin_1_ran
        fin_1_ran = True

    def fin_2():
        raise KeyboardInterrupt("Simulated Ctrl+C in user finalizer")

    # In pytest, finalizers are popped LIFO: fin_2 runs first, then fin_1, then sentinel
    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "interrupt_fix",
        "cached_result": ("val", None, None),
        "_finalizers": [post_fin_sentinel, fin_1, fin_2],
    })()

    state.active_fixture_owners[fixturedef_mock] = setup_owner_id
    state.post_finalizers[(fixturedef_mock, 1)] = post_fin_sentinel
    state.active_fixture_instances[fixturedef_mock] = 1

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    # Execute dispatcher expecting KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt, match=r"Simulated Ctrl\+C in user finalizer"):
        plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    # 1. Subsequent finalizer fin_1 executed despite fin_2 raising KeyboardInterrupt
    assert fin_1_ran is True

    # 2. Sentinel executed and finalized setup owner
    assert sentinel_ran is True

    # 3. Setup owner is finalized in ledger
    owners_by_id = {o.owner_id: o for o in state.ledger.get_all_owners()}
    assert owners_by_id[setup_owner_id].state == OwnerState.FINALIZED

    # 4. Teardown owner was created and finalized in dispatcher's finally block
    teardown_owner_id = plugin._build_runtime_owner_id(fixturedef_mock, request_mock, state, 1)
    assert teardown_owner_id in owners_by_id
    assert owners_by_id[teardown_owner_id].state == OwnerState.FINALIZED

    # 5. Neither owner remains active (no Mode B leak)
    active_owners = [o for o in state.ledger.get_all_owners() if o.state == OwnerState.ACTIVE]
    assert len(active_owners) == 0

    # 6. Current ContextVar is reset
    assert get_current_owner() is None


# ===========================================================================
# SCEN-08: Sequential Context Isolation
# ===========================================================================

def test_scen08_sequential_context_isolation():
    """SCEN-08: Sequential ContextVar isolation across fixture teardowns and test boundaries.
    Verifies that during each teardown, the active owner is attributed solely to that fixture,
    that context resets upon teardown completion, and that subsequent tests do not inherit
    previous teardown owners.
    """
    code = """
import pytest
from pytest_state_guard.owners import get_current_owner

observed_owners = {}

@pytest.fixture
def fix_a():
    yield "a"
    cur = get_current_owner()
    observed_owners["fix_a_teardown"] = cur.fixture_name if cur else None

@pytest.fixture
def fix_b():
    yield "b"
    cur = get_current_owner()
    observed_owners["fix_b_teardown"] = cur.fixture_name if cur else None

def test_one(fix_a, fix_b):
    cur = get_current_owner()
    observed_owners["test_one_body"] = cur.nodeid if cur else None

@pytest.fixture
def fix_c():
    yield "c"
    cur = get_current_owner()
    observed_owners["fix_c_teardown"] = cur.fixture_name if cur else None

def test_two(fix_c):
    cur = get_current_owner()
    observed_owners["test_two_body"] = cur.nodeid if cur else None

def test_verify_isolation():
    # Teardowns executed in sequential order
    assert observed_owners["test_one_body"].endswith("test_case.py::test_one")
    assert observed_owners["fix_b_teardown"] == "fix_b"
    assert observed_owners["fix_a_teardown"] == "fix_a"
    assert observed_owners["test_two_body"].endswith("test_case.py::test_two")
    assert observed_owners["fix_c_teardown"] == "fix_c"
"""
    res = run_pytest(code)
    assert res.returncode == 0
    assert "3 passed" in res.stdout


# ===========================================================================
# SCEN-09: Multiple Violating Teardown Owners
# ===========================================================================

def test_scen09_multiple_violating_teardown_owners_aggregation():
    """SCEN-09: Multiple violating teardown owners associated with one test item.
    Verifies that when two fixtures leak tasks in teardown, both owners are independently
    represented with distinct identities, tie-breaking selects a primary owner on the test
    report, and the session summary aggregates BOTH violating fixtures.
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

gate_a = threading.Event()
gate_b = threading.Event()

@pytest.fixture
def leak_a():
    yield "a"
    ex = ThreadPoolExecutor(max_workers=1)
    ex.submit(lambda: gate_a.wait(timeout=2.0))

@pytest.fixture
def leak_b():
    yield "b"
    ex = ThreadPoolExecutor(max_workers=1)
    ex.submit(lambda: gate_b.wait(timeout=2.0))

def test_multi_violation(leak_a, leak_b):
    assert True
"""
    res = run_pytest(code)
    assert res.returncode == 1
    # Test report marks teardown failure
    assert "State Guard Teardown Violation: FIXTURE_TEARDOWN" in res.stdout
    assert "CALLABLE_NOT_EXITED" in res.stdout
    # Authoritative session audit reports BOTH leaking fixtures
    assert "VIOLATIONS DETECTED" in res.stdout
    assert "leak_a" in res.stdout
    assert "leak_b" in res.stdout


# ===========================================================================
# SCEN-10: Non-Delegating Wrapper Bypass & Telemetry Resilience (T-10.1 - T-10.14)
# ===========================================================================

def test_scen10_1_non_delegating_wrapper_baseline(monkeypatch):
    """T-10.1: Third-party non-delegating wrapper bypasses State Guard finish dispatcher.
    Sentinel executes, observes entered_dispatcher_depth == 0, records exact Stage 15A
    frozen degradation string, finalizes setup owner, and consumes sentinel.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_scen10_1"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    setup_owner_id = "owner:fixture:scen10_fix:uuid101"
    state.ledger.create_owner(
        owner_id=setup_owner_id,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_scen10_1",
        fixture_name="scen10_fix",
    )

    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "scen10_fix",
        "cached_result": ("val", None, None),
        "_finalizers": [],
    })()

    sentinel_callback = lambda: plugin.pytest_fixture_post_finalizer(fixturedef_mock, request_mock)
    fixturedef_mock._finalizers = [sentinel_callback]

    state.active_fixture_owners[fixturedef_mock] = setup_owner_id
    state.post_finalizers[(fixturedef_mock, 1)] = sentinel_callback
    state.active_fixture_instances[fixturedef_mock] = 1

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    # Foreign wrapper executes finalizers directly WITHOUT calling _state_guard_fixture_finish
    while fixturedef_mock._finalizers:
        fin = fixturedef_mock._finalizers.pop()
        fin()

    # 1. Exact frozen degradation recorded verbatim
    expected_degradation = "FixtureDef.finish was replaced by a non-delegating plugin; teardown ownership bypassed"
    assert expected_degradation in state.degradations

    # 2. Setup owner transitioned to FINALIZED
    owners_by_id = {o.owner_id: o for o in state.ledger.get_all_owners()}
    assert owners_by_id[setup_owner_id].state == OwnerState.FINALIZED

    # 3. Sentinel and active instance entries consumed
    assert (fixturedef_mock, 1) not in state.post_finalizers
    assert fixturedef_mock not in state.active_fixture_instances

    # 4. Zero teardown owners created
    teardown_owners = [o for o in state.ledger.get_all_owners() if o.owner_type == OwnerType.FIXTURE_TEARDOWN]
    assert len(teardown_owners) == 0


def test_scen10_2_delegating_foreign_wrapper_negative_control(monkeypatch):
    """T-10.2: Delegating foreign wrapper calls State Guard's dispatcher.
    Sentinel executes, detects dispatcher entry, records zero degradations, and finalizes teardown owner.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_scen10_2"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    setup_owner_id = "owner:fixture:scen10_del:uuid102"
    state.ledger.create_owner(
        owner_id=setup_owner_id,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_scen10_2",
        fixture_name="scen10_del",
    )

    teardown_called = False
    def user_fin():
        nonlocal teardown_called
        teardown_called = True

    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "scen10_del",
        "cached_result": ("val", None, None),
        "_finalizers": [],
    })()

    sentinel_callback = lambda: plugin.pytest_fixture_post_finalizer(fixturedef_mock, request_mock)
    fixturedef_mock._finalizers = [sentinel_callback, user_fin]

    state.active_fixture_owners[fixturedef_mock] = setup_owner_id
    state.post_finalizers[(fixturedef_mock, 1)] = sentinel_callback
    state.active_fixture_instances[fixturedef_mock] = 1

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    # Delegating wrapper calls _state_guard_fixture_finish
    plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    assert teardown_called is True
    # Zero SCEN-10 degradations recorded
    assert len(state.degradations) == 0

    # Setup owner and teardown owner finalized
    owners_by_id = {o.owner_id: o for o in state.ledger.get_all_owners()}
    assert owners_by_id[setup_owner_id].state == OwnerState.FINALIZED
    teardown_owners = [o for o in state.ledger.get_all_owners() if o.owner_type == OwnerType.FIXTURE_TEARDOWN]
    assert len(teardown_owners) == 1
    assert teardown_owners[0].state == OwnerState.FINALIZED


def test_scen10_3_identity_recovery_for_non_1_sequence(monkeypatch):
    """T-10.3: Identity recovery recovers non-1 sequence when active_fixture_instances is missing.
    Pending sentinel is at sequence 4; recovers seq=4, registers teardown owner with seq=4.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_scen10_3"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    setup_owner_id = "owner:fixture:seq4_fix:uuid103"
    state.ledger.create_owner(
        owner_id=setup_owner_id,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_scen10_3",
        fixture_name="seq4_fix",
    )

    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "seq4_fix",
        "cached_result": ("val", None, None),
        "_finalizers": [],
    })()

    sentinel_callback = lambda: plugin.pytest_fixture_post_finalizer(fixturedef_mock, request_mock)
    fixturedef_mock._finalizers = [sentinel_callback, lambda: None]

    state.active_fixture_owners[fixturedef_mock] = setup_owner_id
    state.post_finalizers[(fixturedef_mock, 4)] = sentinel_callback
    # active_fixture_instances intentionally missing!

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    # Teardown owner registered with sequence 4 (not 1!)
    expected_teardown_id = plugin._build_runtime_owner_id(fixturedef_mock, request_mock, state, 4)
    owners_by_id = {o.owner_id: o for o in state.ledger.get_all_owners()}
    assert expected_teardown_id in owners_by_id
    assert owners_by_id[expected_teardown_id].state == OwnerState.FINALIZED
    assert (fixturedef_mock, 4) not in state.post_finalizers


def test_scen10_4_ambiguous_candidate_sequences(monkeypatch):
    """T-10.4: Ambiguous candidate sequences (len(pending) > 1).
    Dispatcher fails open, sentinel does not record SCEN-10 and does not finalize setup owner,
    stale sentinels remain in post_finalizers, Mode B audit promotes exit status to TESTS_FAILED.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_scen10_4"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    setup_owner_id = "owner:fixture:ambig_fix:uuid104"
    state.ledger.create_owner(
        owner_id=setup_owner_id,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_scen10_4",
        fixture_name="ambig_fix",
    )

    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "ambig_fix",
        "cached_result": ("val", None, None),
        "_finalizers": [],
    })()

    sentinel_callback = lambda: plugin.pytest_fixture_post_finalizer(fixturedef_mock, request_mock)
    fixturedef_mock._finalizers = [sentinel_callback]

    state.active_fixture_owners[fixturedef_mock] = setup_owner_id
    state.post_finalizers[(fixturedef_mock, 2)] = sentinel_callback
    state.post_finalizers[(fixturedef_mock, 3)] = sentinel_callback
    # active_fixture_instances missing

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    # 1. Zero SCEN-10 degradations recorded
    assert "FixtureDef.finish was replaced by a non-delegating plugin; teardown ownership bypassed" not in state.degradations
    # 2. Ambiguous degradation recorded
    assert any("Ambiguous pending fixture instances" in deg for deg in state.degradations)

    # 3. Setup owner remains ACTIVE (not finalized!)
    owners_by_id = {o.owner_id: o for o in state.ledger.get_all_owners()}
    assert owners_by_id[setup_owner_id].state == OwnerState.ACTIVE

    # 4. Ambiguous entries preserved in post_finalizers
    assert (fixturedef_mock, 2) in state.post_finalizers
    assert (fixturedef_mock, 3) in state.post_finalizers

    # 5. Session audit detects active setup owner and stale sentinels -> promotes exit status
    class Outcome:
        excinfo = None
    plugin._evaluate_and_cleanup_session_finish(session_mock, Outcome(), state)
    assert session_mock.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert "Incomplete fixture finalization" in plugin._LAST_SESSION_INCOMPLETE_REASON


def test_scen10_5_same_fixturedef_recursive_finish(monkeypatch):
    """T-10.5: Same-FixtureDef recursive finish() execution.
    User finalizer explicitly calls fixturedef.finish(request) recursively.
    Inner dispatcher detects depth > 0, delegates without duplicate owner registration,
    outer dispatcher owns teardown owner finalization and marker cleanup.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_scen10_5"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    setup_owner_id = "owner:fixture:rec_fix:uuid105"
    state.ledger.create_owner(
        owner_id=setup_owner_id,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_scen10_5",
        fixture_name="rec_fix",
    )

    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "rec_fix",
        "cached_result": ("val", None, None),
        "_finalizers": [],
    })()

    inner_dispatch_occurred = False
    def recursive_user_fin():
        nonlocal inner_dispatch_occurred
        inner_dispatch_occurred = True
        # Recursive finish call on the same FixtureDef
        plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    sentinel_callback = lambda: plugin.pytest_fixture_post_finalizer(fixturedef_mock, request_mock)
    fixturedef_mock._finalizers = [sentinel_callback, recursive_user_fin]

    state.active_fixture_owners[fixturedef_mock] = setup_owner_id
    state.post_finalizers[(fixturedef_mock, 1)] = sentinel_callback
    state.active_fixture_instances[fixturedef_mock] = 1

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    assert inner_dispatch_occurred is True
    # Zero SCEN-10 degradations recorded
    assert len(state.degradations) == 0

    # Exactly one teardown owner created and finalized (no DuplicateEntityError!)
    teardown_owners = [o for o in state.ledger.get_all_owners() if o.owner_type == OwnerType.FIXTURE_TEARDOWN]
    assert len(teardown_owners) == 1
    assert teardown_owners[0].state == OwnerState.FINALIZED
    owners_by_id_rec = {o.owner_id: o for o in state.ledger.get_all_owners()}
    assert owners_by_id_rec[setup_owner_id].state == OwnerState.FINALIZED


def test_scen10_6a_outermost_initialization_interruption(monkeypatch):
    """T-10.6A: Outermost initialization interruption by KeyboardInterrupt.
    Phase A raises KeyboardInterrupt before Phase C; original finish is NEVER reached,
    sentinel NEVER runs, outer finally decrements entered_depth to 0, KeyboardInterrupt propagates,
    setup owner remains ACTIVE, and session audit promotes exit status to TESTS_FAILED.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_scen10_6a"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    setup_owner_id = "owner:fixture:inter_6a:uuid106a"
    state.ledger.create_owner(
        owner_id=setup_owner_id,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_scen10_6a",
        fixture_name="inter_6a",
    )

    sentinel_ran = False
    def sentinel_cb():
        nonlocal sentinel_ran
        sentinel_ran = True
        plugin.pytest_fixture_post_finalizer(fixturedef_mock, request_mock)

    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "inter_6a",
        "cached_result": ("val", None, None),
        "_finalizers": [sentinel_cb],
    })()

    state.active_fixture_owners[fixturedef_mock] = setup_owner_id
    state.post_finalizers[(fixturedef_mock, 1)] = sentinel_cb
    state.active_fixture_instances[fixturedef_mock] = 1

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    # Simulate KeyboardInterrupt inside sequence resolution (Phase A)
    class InterruptingInstancesDict(dict):
        def get(self, key, default=None):
            raise KeyboardInterrupt("Simulated Ctrl+C during Phase A sequence resolution")

    state.active_fixture_instances = InterruptingInstancesDict()

    with pytest.raises(KeyboardInterrupt, match="Simulated Ctrl\\+C during Phase A sequence resolution"):
        plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    # 1. Sentinel never executed
    assert sentinel_ran is False

    # 2. entered_dispatcher_depth was decremented back to 0
    assert state.entered_dispatcher_depth.get(fixturedef_mock, 0) == 0

    # 3. Setup owner remains ACTIVE and sentinel remains in post_finalizers
    owners_by_id = {o.owner_id: o for o in state.ledger.get_all_owners()}
    assert owners_by_id[setup_owner_id].state == OwnerState.ACTIVE
    assert (fixturedef_mock, 1) in state.post_finalizers

    # 4. Session audit promotes exit status to TESTS_FAILED
    class Outcome:
        excinfo = None
    plugin._evaluate_and_cleanup_session_finish(session_mock, Outcome(), state)
    assert session_mock.exitstatus == pytest.ExitCode.TESTS_FAILED


def test_scen10_6b_nested_initialization_interruption(monkeypatch):
    """T-10.6B: Nested initialization interruption by KeyboardInterrupt.
    Outer finish is running. User finalizer triggers nested finish which raises KeyboardInterrupt.
    Inner finally decrements entered_depth from 2 to 1 (outer marker preserved!).
    Pytest catches the exception, continues draining stack, sentinel executes, observes
    entered_depth == 1 (> 0), and does NOT falsely report SCEN-10!
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_scen10_6b"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    setup_owner_id = "owner:fixture:inter_6b:uuid106b"
    state.ledger.create_owner(
        owner_id=setup_owner_id,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_scen10_6b",
        fixture_name="inter_6b",
    )

    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "inter_6b",
        "cached_result": ("val", None, None),
        "_finalizers": [],
    })()

    def nested_raising_fin():
        # Simulate inner invocation interrupted by KeyboardInterrupt
        try:
            # Inner invocation raises KeyboardInterrupt in inner setup
            cur = state.entered_dispatcher_depth.get(fixturedef_mock, 0)
            state.entered_dispatcher_depth[fixturedef_mock] = cur + 1
            raise KeyboardInterrupt("Simulated Ctrl+C in inner finish")
        finally:
            cur = state.entered_dispatcher_depth.get(fixturedef_mock, 1) - 1
            if cur > 0:
                state.entered_dispatcher_depth[fixturedef_mock] = cur
            else:
                state.entered_dispatcher_depth.pop(fixturedef_mock, None)

    sentinel_callback = lambda: plugin.pytest_fixture_post_finalizer(fixturedef_mock, request_mock)
    fixturedef_mock._finalizers = [sentinel_callback, nested_raising_fin]

    state.active_fixture_owners[fixturedef_mock] = setup_owner_id
    state.post_finalizers[(fixturedef_mock, 1)] = sentinel_callback
    state.active_fixture_instances[fixturedef_mock] = 1

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    # Pytest propagates KeyboardInterrupt after draining finalizers
    with pytest.raises(KeyboardInterrupt, match="Simulated Ctrl\\+C in inner finish"):
        plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    # Sentinel ran under entered_dispatcher_depth == 1; NO SCEN-10 recorded!
    assert "FixtureDef.finish was replaced by a non-delegating plugin; teardown ownership bypassed" not in state.degradations
    owners_by_id = {o.owner_id: o for o in state.ledger.get_all_owners()}
    assert owners_by_id[setup_owner_id].state == OwnerState.FINALIZED


def test_scen10_7_uninstrumented_fixture_teardown_control(monkeypatch):
    """T-10.7: Uninstrumented fixture teardown (len(pending) == 0).
    Dispatcher delegates cleanly, sentinel observes len(pending) == 0,
    does NOT record SCEN-10 degradation, no owner created or finalized.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_scen10_7"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "uninst_fix",
        "cached_result": ("val", None, None),
        "_finalizers": [],
    })()

    # Zero pending sentinels registered
    sentinel_callback = lambda: plugin.pytest_fixture_post_finalizer(fixturedef_mock, request_mock)
    fixturedef_mock._finalizers = [sentinel_callback]

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    # Zero degradations, zero owners
    assert len(state.degradations) == 0
    assert len(state.ledger.get_all_owners()) == 0


def test_scen10_8_dispatcher_initialization_failure_fail_open_cleanup(monkeypatch):
    """T-10.8: Dispatcher initialization failure fails open and leaves zero stale sentinel entries.
    Ordinary Exception in identity lookup: dispatcher logs degradation, keeps entered_depth=1,
    calls original finish, sentinel executes, observes entered_depth=1, does NOT record SCEN-10,
    setup owner is finalized, pending sentinel is consumed, session audit exits cleanly.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_scen10_8"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    setup_owner_id = "owner:fixture:init_fail_fix:uuid108"
    state.ledger.create_owner(
        owner_id=setup_owner_id,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_scen10_8",
        fixture_name="init_fail_fix",
    )

    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "init_fail_fix",
        "cached_result": ("val", None, None),
        "_finalizers": [],
    })()

    sentinel_callback = lambda: plugin.pytest_fixture_post_finalizer(fixturedef_mock, request_mock)
    fixturedef_mock._finalizers = [sentinel_callback]

    state.active_fixture_owners[fixturedef_mock] = setup_owner_id
    state.post_finalizers[(fixturedef_mock, 1)] = sentinel_callback
    state.active_fixture_instances[fixturedef_mock] = 1

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    # Simulate ordinary initialization error in _has_user_teardown_callbacks
    def raise_init_err(*args, **kwargs):
        raise RuntimeError("Simulated identity/callback initialization error")

    monkeypatch.setattr(plugin, "_has_user_teardown_callbacks", raise_init_err)

    plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    # 1. Zero SCEN-10 degradations recorded
    assert "FixtureDef.finish was replaced by a non-delegating plugin; teardown ownership bypassed" not in state.degradations
    # 2. Ordinary initialization degradation recorded
    assert any("Failed to initialize teardown tracking" in deg for deg in state.degradations)

    # 3. Setup owner is finalized
    owners_by_id = {o.owner_id: o for o in state.ledger.get_all_owners()}
    assert owners_by_id[setup_owner_id].state == OwnerState.FINALIZED

    # 4. Exact pending sentinel is consumed; active fixture instance is pruned
    assert (fixturedef_mock, 1) not in state.post_finalizers
    assert fixturedef_mock not in state.active_fixture_instances

    # 5. Session audit produces clean exit (no false TESTS_FAILED!)
    class Outcome:
        excinfo = None
    plugin._evaluate_and_cleanup_session_finish(session_mock, Outcome(), state)
    assert session_mock.exitstatus == pytest.ExitCode.OK
    assert plugin._LAST_SESSION_INCOMPLETE_REASON is None


def test_scen10_9_early_path_finalizer_runtime_error_single_execution(monkeypatch):
    """T-10.9: Early-path user finalizer raises RuntimeError.
    Asserts Phase C unwinds naturally into Phase D without calling _ORIGINAL_FIXTUREDEF_FINISH twice.
    Finalizer callback is called exactly once (call_count == 1).
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_scen10_9"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    setup_owner_id = "owner:fixture:single_exec:uuid109"
    state.ledger.create_owner(
        owner_id=setup_owner_id,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_scen10_9",
        fixture_name="single_exec",
    )

    finalizer_call_count = 0
    def failing_fin():
        nonlocal finalizer_call_count
        finalizer_call_count += 1
        raise RuntimeError("User teardown callback failure")

    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "single_exec",
        "cached_result": ("val", None, None),
        "_finalizers": [],
    })()

    sentinel_callback = lambda: plugin.pytest_fixture_post_finalizer(fixturedef_mock, request_mock)
    fixturedef_mock._finalizers = [sentinel_callback, failing_fin]

    state.active_fixture_owners[fixturedef_mock] = setup_owner_id
    state.post_finalizers[(fixturedef_mock, 1)] = sentinel_callback
    state.active_fixture_instances[fixturedef_mock] = 1

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    with pytest.raises(RuntimeError, match="User teardown callback failure"):
        plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    # Verifies finalizer callback was called EXACTLY ONCE (never replayed!)
    assert finalizer_call_count == 1


def test_scen10_10a_entry_registration_failure_audit_retention(monkeypatch):
    """T-10.10A: Entry registration failure preserves session evidence and promotes audit exit status.
    entered_dispatcher_depth increment raises RuntimeError; self is added to transient_entry_failures
    and session_entry_failures; sentinel observes transient failure and avoids SCEN-10; Phase D clears
    transient failure; session_entry_failures survives and causes session audit to promote exit status to TESTS_FAILED.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_scen10_10a"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    setup_owner_id = "owner:fixture:entry_fail_fix:uuid110a"
    state.ledger.create_owner(
        owner_id=setup_owner_id,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_scen10_10a",
        fixture_name="entry_fail_fix",
    )

    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "entry_fail_fix",
        "cached_result": ("val", None, None),
        "_finalizers": [],
    })()

    sentinel_callback = lambda: plugin.pytest_fixture_post_finalizer(fixturedef_mock, request_mock)
    fixturedef_mock._finalizers = [sentinel_callback]

    state.active_fixture_owners[fixturedef_mock] = setup_owner_id
    state.post_finalizers[(fixturedef_mock, 1)] = sentinel_callback
    state.active_fixture_instances[fixturedef_mock] = 1

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    # Simulate RuntimeError when incrementing entered_dispatcher_depth
    class FailingOnceEntryDict(dict):
        def __init__(self):
            super().__init__()
            self.called = False
        def get(self, key, default=None):
            if not self.called:
                self.called = True
                raise RuntimeError("Simulated entry depth increment error")
            return super().get(key, default)

    state.entered_dispatcher_depth = FailingOnceEntryDict()

    plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    # 1. Zero SCEN-10 recorded!
    assert "FixtureDef.finish was replaced by a non-delegating plugin; teardown ownership bypassed" not in state.degradations

    # 2. Transient entry failure cleared after outermost exit
    assert fixturedef_mock not in state.transient_entry_failures

    # 3. Persistent session failure survives!
    assert fixturedef_mock in state.session_entry_failures

    # 4. Session audit inspects session_entry_failures and promotes exit status to TESTS_FAILED
    class Outcome:
        excinfo = None
    plugin._evaluate_and_cleanup_session_finish(session_mock, Outcome(), state)
    assert session_mock.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert "dispatcher entry registration failure(s) recorded" in plugin._LAST_SESSION_INCOMPLETE_REASON


def test_scen10_10b_entry_registration_control_flow_interruption(monkeypatch):
    """T-10.10B: Entry registration control-flow interruption by KeyboardInterrupt.
    KeyboardInterrupt raised during entry depth increment propagates immediately via bare raise;
    entry_registered remains False; outer finally does not decrement unacquired reference count.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_scen10_10b"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "entry_inter_fix",
        "cached_result": ("val", None, None),
        "_finalizers": [],
    })()

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    # Simulate KeyboardInterrupt during entered_dispatcher_depth.get
    class InterruptingOnceEntryDict(dict):
        def __init__(self):
            super().__init__()
            self.called = False
        def get(self, key, default=None):
            if not self.called:
                self.called = True
                raise KeyboardInterrupt("Simulated Ctrl+C during entry registration")
            return super().get(key, default)

    state.entered_dispatcher_depth = InterruptingOnceEntryDict()

    with pytest.raises(KeyboardInterrupt, match="Simulated Ctrl\\+C during entry registration"):
        plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    # Entry depth was never registered; count is 0
    assert state.entered_dispatcher_depth.get(fixturedef_mock, 0) == 0


def test_scen10_11_failure_marker_recording_failure_uncertain_defense(monkeypatch):
    """T-10.11: Multi-tier uncertain defense when transient set recording fails.
    Both entered_dispatcher_depth and transient_entry_failures.add raise RuntimeError.
    Fallback attaches _sg_entry_failed_session_id on fixturedef; sentinel observes structured
    failure marker, avoids SCEN-10, consumes sentinel, and session audit promotes exit status to TESTS_FAILED.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_scen10_11"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    setup_owner_id = "owner:fixture:uncertain_fix:uuid111"
    state.ledger.create_owner(
        owner_id=setup_owner_id,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_scen10_11",
        fixture_name="uncertain_fix",
    )

    access_log: list[tuple[str, Any]] = []

    class AuditedMockFixtureDef:
        def __init__(self):
            self.argname = "uncertain_fix"
            self.cached_result = ("val", None, None)
            self._finalizers = []

        def __getattribute__(self, name: str) -> Any:
            val = super().__getattribute__(name)
            if name == "_sg_entry_failed_session_id":
                access_log.append(("read", val))
            return val

        def __delattr__(self, name: str) -> None:
            if name == "_sg_entry_failed_session_id":
                access_log.append(("del", getattr(self, name, None)))
            super().__delattr__(name)

    fixturedef_mock = AuditedMockFixtureDef()

    sentinel_callback = lambda: plugin.pytest_fixture_post_finalizer(fixturedef_mock, request_mock)
    fixturedef_mock._finalizers = [sentinel_callback]

    state.active_fixture_owners[fixturedef_mock] = setup_owner_id
    state.post_finalizers[(fixturedef_mock, 1)] = sentinel_callback
    state.active_fixture_instances[fixturedef_mock] = 1

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    # Simulate failures in entry depth increment and all three structured sets
    class FailingOnceEntryDict(dict):
        def __init__(self):
            super().__init__()
            self.called = False
        def get(self, key, default=None):
            if not self.called:
                self.called = True
                raise RuntimeError("Entry depth increment error")
            return super().get(key, default)

    class FailingOnceSet(set):
        def __init__(self):
            super().__init__()
            self.called = False
        def add(self, item):
            if not self.called:
                self.called = True
                raise RuntimeError("Set add error")
            return super().add(item)

    state.entered_dispatcher_depth = FailingOnceEntryDict()
    state.transient_entry_failures = FailingOnceSet()
    state.session_entry_failures = FailingOnceSet()
    state.uncertain_fixtures = FailingOnceSet()

    plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

    # 1. Zero SCEN-10 recorded!
    assert "FixtureDef.finish was replaced by a non-delegating plugin; teardown ownership bypassed" not in state.degradations

    # 2. All three structured set insertions failed: fixturedef is in NONE of them!
    assert fixturedef_mock not in state.transient_entry_failures
    assert fixturedef_mock not in state.session_entry_failures
    assert fixturedef_mock not in state.uncertain_fixtures

    # 3. Structured attribute fallback was attached matching active session_id as the SOLE surviving evidence!
    assert getattr(fixturedef_mock, "_sg_entry_failed_session_id", None) == state.session_id

    # 4. Session audit detects uncertain state from attribute fallback -> promotes exit status to TESTS_FAILED
    class Outcome:
        excinfo = None
    plugin._evaluate_and_cleanup_session_finish(session_mock, Outcome(), state)
    assert session_mock.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert "uncertain instrumentation state" in plugin._LAST_SESSION_INCOMPLETE_REASON
    assert "uncertain_fix" in plugin._LAST_SESSION_INCOMPLETE_REASON

    # 5. Explicit ordering verification: session audit inspected the attribute BEFORE Step 4 pruned it
    assert ("read", state.session_id) in access_log
    assert ("del", state.session_id) in access_log
    read_index = access_log.index(("read", state.session_id))
    del_index = access_log.index(("del", state.session_id))
    assert read_index < del_index, f"Expected read before del in access log: {access_log}"

    # 6. Step 4 session reference cleanup verified to prune the fallback attribute post-audit
    assert not hasattr(fixturedef_mock, "_sg_entry_failed_session_id")


def test_scen10_12_synthetic_fixturedef_reuse_and_sequential_session_isolation(monkeypatch):
    """T-10.12: Synthetic FixtureDef object reuse defense and sequential session isolation.
    NOTE ON PYTEST LIFECYCLE:
    Pytest's FixtureManager.parsefactories constructs fresh FixtureDef instances during collection
    for every session; native pytest does not reuse FixtureDef objects across sessions.
    This test establishes two distinct guarantees:
    - Part 1 (Synthetic In-Process Reuse): Directly tests State Guard's session-scoping defense
      when the identical FixtureDef instance is passed into two sequential sessions:
      Session 1 attaches _sg_entry_failed_session_id, promotes exitstatus, and Step 4 cleans it up.
      Session 2 reuses the exact same instance (assert fixturedef_2 is fixturedef_1) and verifies that
      even if a foreign/stale session marker were present, Session 2 ignores it and completes with ExitCode.OK.
    - Part 2 (Actual Sequential Pytest Sessions): Runs sequential pytest sessions via subprocess,
      proving end-to-end that State Guard initializes, audits, and unpatches cleanly across runs.
    """
    # -----------------------------------------------------------------------
    # Part 1: In-process synthetic FixtureDef object reuse
    # -----------------------------------------------------------------------
    reused_fixturedef = type("MockFixtureDef", (), {
        "argname": "reused_fix",
        "cached_result": ("reused_val", None, None),
        "_finalizers": [],
    })()

    class FailingOnceEntryDict(dict):
        def __init__(self):
            super().__init__()
            self.called = False
        def get(self, key, default=None):
            if not self.called:
                self.called = True
                raise RuntimeError("Entry depth increment error")
            return super().get(key, default)

    class FailingOnceSet(set):
        def __init__(self):
            super().__init__()
            self.called = False
        def add(self, item):
            if not self.called:
                self.called = True
                raise RuntimeError("Set add error")
            return super().add(item)

    # --- SESSION 1 ---
    state_1 = plugin.StateGuardSession(ledger=Ledger())
    session_1 = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_1._state_guard_session = state_1
    node_1 = type("MockNode", (), {"nodeid": "test_case.py::test_s1"})()
    request_1 = type("MockRequest", (), {"session": session_1, "node": node_1, "param": None})()

    setup_owner_1 = "owner:fixture:reused_fix:s1"
    state_1.ledger.create_owner(
        owner_id=setup_owner_1,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_s1",
        fixture_name="reused_fix",
    )
    sentinel_1 = lambda: plugin.pytest_fixture_post_finalizer(reused_fixturedef, request_1)
    reused_fixturedef._finalizers = [sentinel_1]

    state_1.active_fixture_owners[reused_fixturedef] = setup_owner_1
    state_1.post_finalizers[(reused_fixturedef, 1)] = sentinel_1
    state_1.active_fixture_instances[reused_fixturedef] = 1

    state_1.entered_dispatcher_depth = FailingOnceEntryDict()
    state_1.transient_entry_failures = FailingOnceSet()
    state_1.session_entry_failures = FailingOnceSet()
    state_1.uncertain_fixtures = FailingOnceSet()

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state_1 if sess is session_1 else state_2)

    plugin._state_guard_fixture_finish(reused_fixturedef, request_1)

    # Marker attached with Session 1 session_id
    assert getattr(reused_fixturedef, "_sg_entry_failed_session_id", None) == state_1.session_id

    class DirectOutcome:
        excinfo = None
        def force_exception(self, exc: BaseException) -> None:
            raise exc

    plugin._evaluate_and_cleanup_session_finish(session_1, DirectOutcome(), state_1)
    assert session_1.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert "uncertain instrumentation state" in plugin._LAST_SESSION_INCOMPLETE_REASON

    # Verified: Step 4 cleaned up the marker
    assert not hasattr(reused_fixturedef, "_sg_entry_failed_session_id")

    # --- SESSION 2: Reusing exact same FixtureDef instance ---
    state_2 = plugin.StateGuardSession(ledger=Ledger())
    assert state_2.session_id != state_1.session_id

    session_2 = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_2._state_guard_session = state_2
    node_2 = type("MockNode", (), {"nodeid": "test_case.py::test_s2"})()
    request_2 = type("MockRequest", (), {"session": session_2, "node": node_2, "param": None})()

    fixturedef_2 = reused_fixturedef
    # Assert exact object identity reuse across sessions
    assert fixturedef_2 is reused_fixturedef

    # In Session 2, fixture setup ran and populated cached_result:
    fixturedef_2.cached_result = ("reused_val_2", None, None)

    # Simulate presence of a foreign session marker to test session ID scoping
    object.__setattr__(fixturedef_2, "_sg_entry_failed_session_id", "stale-foreign-session-uuid-999")

    setup_owner_2 = "owner:fixture:reused_fix:s2"
    state_2.ledger.create_owner(
        owner_id=setup_owner_2,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_s2",
        fixture_name="reused_fix",
    )
    sentinel_2 = lambda: plugin.pytest_fixture_post_finalizer(fixturedef_2, request_2)
    fixturedef_2._finalizers = [sentinel_2]

    state_2.active_fixture_owners[fixturedef_2] = setup_owner_2
    state_2.post_finalizers[(fixturedef_2, 1)] = sentinel_2
    state_2.active_fixture_instances[fixturedef_2] = 1

    # Session 2 executes finish normally
    plugin._state_guard_fixture_finish(fixturedef_2, request_2)

    # Sentinel ran and consumed; foreign marker did not cause false SCEN-10
    assert "FixtureDef.finish was replaced by a non-delegating plugin; teardown ownership bypassed" not in state_2.degradations

    # Session 2 audit: foreign marker ignored because ID does not match state_2.session_id
    plugin._evaluate_and_cleanup_session_finish(session_2, DirectOutcome(), state_2)
    assert session_2.exitstatus == pytest.ExitCode.OK
    assert plugin._LAST_SESSION_INCOMPLETE_REASON is None

    # -----------------------------------------------------------------------
    # Part 2: Subprocess execution verifying sequential session isolation
    # -----------------------------------------------------------------------
    code_sess1 = """
import pytest

@pytest.fixture
def fix_one():
    yield "val"

def test_s1(fix_one):
    assert True
"""
    res1 = run_pytest(code_sess1)
    assert res1.returncode == 0
    assert "1 passed" in res1.stdout

    code_sess2 = """
import pytest

@pytest.fixture
def fix_two():
    yield "val2"

def test_s2(fix_two):
    assert True
"""
    res2 = run_pytest(code_sess2)
    assert res2.returncode == 0
    assert "1 passed" in res2.stdout


def test_scen10_13_same_name_fixture_isolation(monkeypatch):
    """T-10.13: Two distinct fixtures sharing the same argname 'database'.
    fix_a experiences an entry failure; fix_b has a non-delegating foreign wrapper.
    fix_a's failure marker does NOT suppress SCEN-10 for fix_b!
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_scen10_13"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    # Create two distinct fixturedefs with the same argname "database"
    fix_a = type("MockFixtureDef", (), {"argname": "database", "cached_result": ("a", None, None), "_finalizers": []})()
    fix_b = type("MockFixtureDef", (), {"argname": "database", "cached_result": ("b", None, None), "_finalizers": []})()

    setup_a_id = "owner:fixture:database_a:uuid113a"
    setup_b_id = "owner:fixture:database_b:uuid113b"
    state.ledger.create_owner(owner_id=setup_a_id, owner_type=OwnerType.FIXTURE_SETUP, nodeid="test_case.py::test_scen10_13", fixture_name="database")
    state.ledger.create_owner(owner_id=setup_b_id, owner_type=OwnerType.FIXTURE_SETUP, nodeid="test_case.py::test_scen10_13", fixture_name="database")

    state.active_fixture_owners[fix_a] = setup_a_id
    state.active_fixture_owners[fix_b] = setup_b_id
    state.post_finalizers[(fix_a, 1)] = lambda: None
    state.post_finalizers[(fix_b, 1)] = lambda: None
    state.active_fixture_instances[fix_a] = 1
    state.active_fixture_instances[fix_b] = 1

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    # fix_a incurs entry registration failure
    state.transient_entry_failures.add(fix_a)

    # Execute sentinel for fix_a: observes fix_a in transient_entry_failures -> NO SCEN-10
    plugin.pytest_fixture_post_finalizer(fix_a, request_mock)
    assert "FixtureDef.finish was replaced by a non-delegating plugin; teardown ownership bypassed" not in state.degradations

    # Execute sentinel for fix_b (foreign wrapper bypassed finish):
    # fix_b is NOT in transient_entry_failures, entered_depth == 0 -> SCEN-10 RECORDED!
    plugin.pytest_fixture_post_finalizer(fix_b, request_mock)
    assert "FixtureDef.finish was replaced by a non-delegating plugin; teardown ownership bypassed" in state.degradations


def test_scen10_14_contextvar_tokenless_recovery(monkeypatch):
    """T-10.14: ContextVar tokenless recovery when token is lost or reset raises.
    Simulates token being None while ContextVar was modified to new_owner_context.
    Asserts _safe_reset_current_owner restores ContextVar to prior_owner (both when
    prior_owner was None and when prior_owner was an active OwnerContext).
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)

    # Case 1: prior_owner is None
    prior_owner = None
    reset_current_owner(set_current_owner(None))  # Ensure initial is None
    assert get_current_owner() is None

    new_context = OwnerContext(owner_id="owner:new:1", owner_type=OwnerType.FIXTURE_TEARDOWN, nodeid="node1")
    set_current_owner(new_context)
    assert get_current_owner() == new_context

    # Simulate token being lost (token is None)
    plugin._safe_reset_current_owner(token=None, state=state, owner_id="owner:new:1", prior_owner=prior_owner)
    assert get_current_owner() is None  # Unconditionally restored to None!

    # Case 2: prior_owner was an active OwnerContext
    prior_context = OwnerContext(owner_id="owner:prior:2", owner_type=OwnerType.FIXTURE_SETUP, nodeid="node2")
    set_current_owner(prior_context)
    assert get_current_owner() == prior_context

    new_context_2 = OwnerContext(owner_id="owner:new:2", owner_type=OwnerType.FIXTURE_TEARDOWN, nodeid="node2")
    set_current_owner(new_context_2)
    assert get_current_owner() == new_context_2

    # Simulate token being lost
    plugin._safe_reset_current_owner(token=None, state=state, owner_id="owner:new:2", prior_owner=prior_context)
    assert get_current_owner() == prior_context  # Unconditionally restored to prior_context!
    reset_current_owner(set_current_owner(None))


def test_session_audit_early_failure_guarantees_cleanup(monkeypatch):
    """Verify that an early exception inside Step 1 audit (e.g. state.ledger.get_all_owners raises)
    does NOT trigger UnboundLocalError in Step 1b exit status evaluation, and guarantees that
    mandatory cleanup (remove_patch, remove_fixture_patch, patch-state verification, reference cleanup)
    executes starting from a verified actively patched state, reconciling lifecycle state to POISONED.
    """
    state = plugin.StateGuardSession(ledger=Ledger())
    plugin.install_patch(state.ledger)
    plugin.install_fixture_patch()

    # Pre-condition check: establish that both patches were actually installed and active
    assert plugin.is_patched() is True
    assert plugin.is_fixture_patched() is True
    assert getattr(ThreadPoolExecutor, "submit", None) is plugin._state_guard_submit
    assert getattr(FixtureDef, "finish", None) is plugin._state_guard_fixture_finish

    plugin._ACTIVE_SESSION = state
    plugin._LIFECYCLE_STATE = plugin.PluginLifecycle.ACTIVE

    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state

    # Spy on cleanup functions to prove they were actually invoked
    orig_remove_patch = plugin.remove_patch
    orig_remove_fixture_patch = plugin.remove_fixture_patch
    remove_patch_invoked = False
    remove_fixture_patch_invoked = False

    def spy_remove_patch():
        nonlocal remove_patch_invoked
        remove_patch_invoked = True
        return orig_remove_patch()

    def spy_remove_fixture_patch():
        nonlocal remove_fixture_patch_invoked
        remove_fixture_patch_invoked = True
        return orig_remove_fixture_patch()

    monkeypatch.setattr(plugin, "remove_patch", spy_remove_patch)
    monkeypatch.setattr(plugin, "remove_fixture_patch", spy_remove_fixture_patch)

    # Monkeypatch ledger.get_all_owners to raise early in Step 1
    def failing_get_all_owners():
        raise RuntimeError("Simulated early ledger audit failure")

    monkeypatch.setattr(state.ledger, "get_all_owners", failing_get_all_owners)

    class DirectOutcome:
        excinfo = None
        forced_exc = None
        def force_exception(self, exc: BaseException) -> None:
            self.forced_exc = exc

    outcome = DirectOutcome()
    plugin._evaluate_and_cleanup_session_finish(session_mock, outcome, state)

    # 1. No UnboundLocalError occurred! Audit exception was captured and forced cleanly.
    assert outcome.forced_exc is not None
    assert isinstance(outcome.forced_exc, RuntimeError)
    assert "Simulated early ledger audit failure" in str(outcome.forced_exc)

    # 2. Session exit status promoted to TESTS_FAILED
    assert session_mock.exitstatus == pytest.ExitCode.TESTS_FAILED

    # 3. Explicit call evidence: both patch removal functions were actually executed
    assert remove_patch_invoked is True
    assert remove_fixture_patch_invoked is True

    # 4. Patch state verification: patches are confirmed removed
    assert not plugin.is_patched()
    assert not plugin.is_fixture_patched()
    assert getattr(ThreadPoolExecutor, "submit", None) is not plugin._state_guard_submit
    assert getattr(FixtureDef, "finish", None) is not plugin._state_guard_fixture_finish

    # 5. Lifecycle state poisoned with audit exception reason
    assert plugin._LIFECYCLE_STATE == plugin.PluginLifecycle.POISONED
    assert "Simulated early ledger audit failure" in str(plugin._POISON_REASON)

    # 6. Session references cleaned up
    assert plugin._ACTIVE_SESSION is None
    assert not hasattr(session_mock, "_state_guard_session")


# ===========================================================================
# Milestone 3D: Further Adversarial Validation (M3D-01 through M3D-07)
# ===========================================================================


def test_m3d01_cross_fixture_recursive_finish_context_stacking(monkeypatch):
    """M3D-01: Natural cross-fixture dependency-finalization nesting and ContextVar stacking.
    For fixture B depending on fixture A, A's teardown dispatcher activates owner A,
    A's original finalizer invokes B's finish(), B's dispatcher activates owner B with A's owner
    as prior context, B finishes and restores A's owner context, and A finishes and restores
    its prior context (None). Both teardown owners are finalized with zero DuplicateEntityError
    and zero SCEN-10 degradations.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_m3d01"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    setup_owner_a = "owner:fixture:fix_a:uuid_a"
    setup_owner_b = "owner:fixture:fix_b:uuid_b"
    state.ledger.create_owner(
        owner_id=setup_owner_a,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_m3d01",
        fixture_name="fix_a",
    )
    state.ledger.create_owner(
        owner_id=setup_owner_b,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_m3d01",
        fixture_name="fix_b",
    )

    fixturedef_b = type("MockFixtureDef", (), {
        "argname": "fix_b",
        "cached_result": ("val_b", None, None),
        "_finalizers": [],
    })()
    fixturedef_a = type("MockFixtureDef", (), {
        "argname": "fix_a",
        "cached_result": ("val_a", None, None),
        "_finalizers": [],
    })()

    context_history: list[str | None] = []

    def user_finalizer_b():
        ctx = get_current_owner()
        context_history.append(ctx.owner_id if ctx else None)

    sentinel_b = lambda: plugin.pytest_fixture_post_finalizer(fixturedef_b, request_mock)
    fixturedef_b._finalizers = [sentinel_b, user_finalizer_b]

    def user_finalizer_a():
        ctx_before_b = get_current_owner()
        context_history.append(ctx_before_b.owner_id if ctx_before_b else None)
        # Natural pytest finalization: A invokes B's finish()
        plugin._state_guard_fixture_finish(fixturedef_b, request_mock)
        ctx_after_b = get_current_owner()
        context_history.append(ctx_after_b.owner_id if ctx_after_b else None)

    sentinel_a = lambda: plugin.pytest_fixture_post_finalizer(fixturedef_a, request_mock)
    fixturedef_a._finalizers = [sentinel_a, user_finalizer_a]

    state.active_fixture_owners[fixturedef_a] = setup_owner_a
    state.post_finalizers[(fixturedef_a, 1)] = sentinel_a
    state.active_fixture_instances[fixturedef_a] = 1

    state.active_fixture_owners[fixturedef_b] = setup_owner_b
    state.post_finalizers[(fixturedef_b, 1)] = sentinel_b
    state.active_fixture_instances[fixturedef_b] = 1

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    # Initial context is cleanly isolated
    init_tok = set_current_owner(None)
    try:
        assert get_current_owner() is None

        # Execute dispatcher on fixture A
        plugin._state_guard_fixture_finish(fixturedef_a, request_mock)

        # Context restored back to None after outermost A finishes
        assert get_current_owner() is None
    finally:
        reset_current_owner(init_tok)

    # Zero SCEN-10 degradations
    assert len(state.degradations) == 0

    # Verify context transitions:
    # 1. Inside user_finalizer_a before calling B.finish(): current owner is teardown A
    # 2. Inside user_finalizer_b: current owner is teardown B
    # 3. Inside user_finalizer_a after B.finish() returns: current owner is restored to teardown A
    assert len(context_history) == 3
    assert ":fix_a:" in context_history[0]
    assert ":fix_b:" in context_history[1]
    assert context_history[2] == context_history[0]

    # Verify both teardown owners created and finalized
    td_owners = [o for o in state.ledger.get_all_owners() if o.owner_type == OwnerType.FIXTURE_TEARDOWN]
    assert len(td_owners) == 2
    td_owners_by_name = {o.fixture_name: o for o in td_owners}
    assert td_owners_by_name["fix_a"].state == OwnerState.FINALIZED
    assert td_owners_by_name["fix_b"].state == OwnerState.FINALIZED

    # Both setup owners finalized
    all_owners = {o.owner_id: o for o in state.ledger.get_all_owners()}
    assert all_owners[setup_owner_a].state == OwnerState.FINALIZED
    assert all_owners[setup_owner_b].state == OwnerState.FINALIZED


def test_m3d02_generator_exit_during_teardown_preserves_cleanup_and_propagates(monkeypatch):
    """M3D-02: GeneratorExit during teardown preserves cleanup and propagates unswallowed.
    Simulates a generator fixture raising GeneratorExit (BaseException) during Phase C.
    Asserts Phase D cleans up completely (ContextVar reset, owner popped and finalized,
    dispatch depth popped, dispatched_instances discarded, entered_dispatcher_depth decremented),
    and GeneratorExit propagates out unswallowed without being masked or wrapped.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_m3d02"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    setup_owner_id = "owner:fixture:gen_exit_fix:uuid_m3d02"
    state.ledger.create_owner(
        owner_id=setup_owner_id,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_m3d02",
        fixture_name="gen_exit_fix",
    )

    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "gen_exit_fix",
        "cached_result": ("val", None, None),
        "_finalizers": [],
    })()

    def generator_teardown_raising_generator_exit():
        raise GeneratorExit("simulated generator close")

    sentinel_callback = lambda: plugin.pytest_fixture_post_finalizer(fixturedef_mock, request_mock)
    fixturedef_mock._finalizers = [sentinel_callback, generator_teardown_raising_generator_exit]

    marker_key = (fixturedef_mock, 1)
    state.active_fixture_owners[fixturedef_mock] = setup_owner_id
    state.post_finalizers[marker_key] = sentinel_callback
    state.active_fixture_instances[fixturedef_mock] = 1

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    # Initial context is cleanly isolated
    init_tok = set_current_owner(None)
    try:
        assert get_current_owner() is None

        # Execute dispatcher; GeneratorExit must propagate cleanly
        with pytest.raises(GeneratorExit, match="simulated generator close"):
            plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)

        # 1. ContextVar reset to prior context (None)
        assert get_current_owner() is None
    finally:
        reset_current_owner(init_tok)

    # 2. Teardown owner was popped from active_teardown_owners and finalized
    assert marker_key not in state.active_teardown_owners
    td_owners = [o for o in state.ledger.get_all_owners() if o.owner_type == OwnerType.FIXTURE_TEARDOWN]
    assert len(td_owners) == 1
    assert td_owners[0].state == OwnerState.FINALIZED

    # 3. Dispatch registries popped and cleaned
    assert marker_key not in state.dispatched_instances
    assert marker_key not in state.dispatch_depth
    assert state.entered_dispatcher_depth.get(fixturedef_mock, 0) == 0

    # 4. Fixture instance tracking popped
    assert fixturedef_mock not in state.active_fixture_instances


def test_m3d03_invalid_non_none_token_reset_fallback():
    """M3D-03: Invalid non-None token reset fallback restores exact prior owner and records degradation.
    Consumes a valid token so it becomes invalidated, changes the current owner context,
    and invokes _safe_reset_current_owner with the consumed token.
    Asserts reset_current_owner(token) raises ValueError, _safe_reset_current_owner catches it,
    records the degradation message, and successfully executes the fallback set_current_owner(prior_owner).
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)

    prior_ctx = OwnerContext(
        owner_id="owner:test:prior_m3d03:1",
        owner_type=OwnerType.TEST_CALL,
        nodeid="test_case.py::test_m3d03",
    )
    # Set initial context to prior_ctx
    tok_prior = set_current_owner(prior_ctx)
    try:
        td_ctx = OwnerContext(
            owner_id="owner:fixture:td_m3d03:2",
            owner_type=OwnerType.FIXTURE_TEARDOWN,
            nodeid="test_case.py::test_m3d03",
            fixture_name="td_fix",
        )
        token_td = set_current_owner(td_ctx)
        assert get_current_owner() is td_ctx

        # Consume token_td once to invalidate it!
        reset_current_owner(token_td)
        assert get_current_owner() is prior_ctx

        # Set context to an unrelated temporary context so current differs from prior_ctx
        other_ctx = OwnerContext(
            owner_id="owner:fixture:other_m3d03:3",
            owner_type=OwnerType.FIXTURE_TEARDOWN,
            nodeid="test_case.py::test_m3d03",
        )
        set_current_owner(other_ctx)
        assert get_current_owner() is other_ctx

        # Passing the already-consumed token_td to reset_current_owner raises ValueError
        # Invoke _safe_reset_current_owner with the invalid non-None token:
        plugin._safe_reset_current_owner(
            token=token_td,
            state=state,
            owner_id="owner:fixture:td_m3d03:2",
            prior_owner=prior_ctx,
        )

        # 1. Fallback restored exact prior owner:
        assert get_current_owner() is prior_ctx

        # 2. Degradation recorded with token failure message:
        assert len(state.degradations) == 1
        deg = state.degradations[0]
        assert "Context reset failed for teardown owner 'owner:fixture:td_m3d03:2'" in deg
        assert "has already been used once" in deg or "ValueError" in deg
    finally:
        reset_current_owner(tok_prior)


def test_m3d04_premature_sentinel_consumption(monkeypatch):
    """M3D-04: Premature sentinel consumption detects dispatcher bypass, records SCEN-10 degradation,
    finalizes setup owner, prunes sentinel, fails open on subsequent dispatcher call without orphan owner,
    and evaluates session audit exit status.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state
    node_mock = type("MockNode", (), {"nodeid": "test_case.py::test_m3d04"})()
    request_mock = type("MockRequest", (), {
        "session": session_mock,
        "node": node_mock,
        "param": None,
    })()

    setup_owner_id = "owner:fixture:premature_fix:uuid_m3d04"
    state.ledger.create_owner(
        owner_id=setup_owner_id,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid="test_case.py::test_m3d04",
        fixture_name="premature_fix",
    )

    user_finalizer_ran = False
    def user_finalizer():
        nonlocal user_finalizer_ran
        user_finalizer_ran = True

    sentinel_callback = lambda: plugin.pytest_fixture_post_finalizer(fixturedef_mock, request_mock)
    fixturedef_mock = type("MockFixtureDef", (), {
        "argname": "premature_fix",
        "cached_result": ("val", None, None),
        "_finalizers": [user_finalizer],
    })()

    marker_key = (fixturedef_mock, 1)
    state.active_fixture_owners[fixturedef_mock] = setup_owner_id
    state.post_finalizers[marker_key] = sentinel_callback
    state.active_fixture_instances[fixturedef_mock] = 1

    monkeypatch.setattr(plugin, "_get_verified_session", lambda sess: state)

    # Premature invocation of the sentinel callback BEFORE finish() is called
    sentinel_callback()

    # 1. Post-finalizer hook detected unentered dispatcher -> exact frozen SCEN-10 degradation recorded
    expected_scen10_deg = "FixtureDef.finish was replaced by a non-delegating plugin; teardown ownership bypassed"
    assert expected_scen10_deg in state.degradations

    # 2. Setup owner is finalized
    owners_by_id = {o.owner_id: o for o in state.ledger.get_all_owners()}
    assert owners_by_id[setup_owner_id].state == OwnerState.FINALIZED

    # 3. Sentinel and active instance entries consumed
    assert marker_key not in state.post_finalizers
    assert fixturedef_mock not in state.active_fixture_instances

    # 4. Subsequent dispatcher invocation runs user finalizer without creating orphan teardown owner
    plugin._state_guard_fixture_finish(fixturedef_mock, request_mock)
    assert user_finalizer_ran is True
    td_owners = [o for o in state.ledger.get_all_owners() if o.owner_type == OwnerType.FIXTURE_TEARDOWN]
    assert len(td_owners) == 0

    # 5. Session audit evaluation
    outcome_mock = type("MockOutcome", (), {"excinfo": None})()
    with plugin._SESSION_LOCK:
        plugin._LIFECYCLE_STATE = plugin.PluginLifecycle.ACTIVE
        plugin._ACTIVE_SESSION = state
        plugin._evaluate_and_cleanup_session_finish(session_mock, outcome_mock, state)

    assert session_mock.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert plugin._LAST_SESSION_INCOMPLETE_REASON is not None
    assert "bypassed by non-delegating plugin" in plugin._LAST_SESSION_INCOMPLETE_REASON
    assert "premature_fix" in plugin._LAST_SESSION_INCOMPLETE_REASON


def test_m3d05_policy_engine_failure_during_session_audit(monkeypatch):
    """M3D-05: PolicyEngine failure during session audit promotes exit status and guarantees cleanup.
    Monkeypatches PolicyEngine.evaluate_all_finalized to raise RuntimeError during audit.
    Asserts audit_exc is captured, exit status is promoted to TESTS_FAILED, mandatory cleanup executes
    (both patches removed, session references cleaned up).
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state

    remove_patch_invoked = False
    remove_fixture_patch_invoked = False
    orig_remove_patch = plugin.remove_patch
    orig_remove_fixture_patch = plugin.remove_fixture_patch

    def spy_remove_patch():
        nonlocal remove_patch_invoked
        remove_patch_invoked = True
        return orig_remove_patch()

    def spy_remove_fixture_patch():
        nonlocal remove_fixture_patch_invoked
        remove_fixture_patch_invoked = True
        return orig_remove_fixture_patch()

    monkeypatch.setattr(plugin, "remove_patch", spy_remove_patch)
    monkeypatch.setattr(plugin, "remove_fixture_patch", spy_remove_fixture_patch)

    def crashing_evaluate_all_finalized(self, led):
        raise RuntimeError("policy eval crash in session audit")

    monkeypatch.setattr(PolicyEngine, "evaluate_all_finalized", crashing_evaluate_all_finalized)

    class DirectOutcome:
        excinfo = None
        forced_exc = None
        def force_exception(self, exc: BaseException) -> None:
            self.forced_exc = exc

    outcome = DirectOutcome()

    with plugin._SESSION_LOCK:
        plugin._LIFECYCLE_STATE = plugin.PluginLifecycle.ACTIVE
        plugin._ACTIVE_SESSION = state
        plugin._evaluate_and_cleanup_session_finish(session_mock, outcome, state)

    # 1. Audit exception captured and forced
    assert outcome.forced_exc is not None
    assert isinstance(outcome.forced_exc, RuntimeError)
    assert "policy eval crash in session audit" in str(outcome.forced_exc)

    # 2. Session exit status promoted to TESTS_FAILED
    assert session_mock.exitstatus == pytest.ExitCode.TESTS_FAILED

    # 3. Both patch removal functions executed
    assert remove_patch_invoked is True
    assert remove_fixture_patch_invoked is True

    # 4. Patches confirmed removed
    assert not plugin.is_patched()
    assert not plugin.is_fixture_patched()

    # 5. Session reference cleaned up
    assert plugin._ACTIVE_SESSION is None
    assert not hasattr(session_mock, "_state_guard_session")


def test_m3d06_poisoned_lifecycle_rejects_subsequent_session():
    """M3D-06: Poisoned lifecycle rejects subsequent session on startup and preserves pre-existing global state.
    Pre-sets lifecycle state to POISONED with specific reason.
    Asserts pytest_sessionstart raises RuntimeError referencing poison reason,
    pre-existing patch state and active-session reference remain unaltered,
    and new session does not have _state_guard_session attached.
    """
    orig_lifecycle = plugin._LIFECYCLE_STATE
    orig_reason = plugin._POISON_REASON
    orig_active = plugin._ACTIVE_SESSION

    try:
        plugin._LIFECYCLE_STATE = plugin.PluginLifecycle.POISONED
        plugin._POISON_REASON = "Pre-existing unrecoverable fault"

        # Record pre-existing patch state and active session reference
        patch_before = plugin.is_patched()
        fixture_patch_before = plugin.is_fixture_patched()
        active_before = plugin._ACTIVE_SESSION

        new_session = type("MockSession", (), {})()

        with pytest.raises(RuntimeError) as exc_info:
            plugin.pytest_sessionstart(new_session)

        # 1. Expected poison error message
        assert "Cannot start pytest session: State Guard is in a POISONED lifecycle state" in str(exc_info.value)
        assert "Pre-existing unrecoverable fault" in str(exc_info.value)

        # 2. Pre-existing global state preserved
        assert plugin.is_patched() == patch_before
        assert plugin.is_fixture_patched() == fixture_patch_before
        assert plugin._ACTIVE_SESSION is active_before

        # 3. New session was not modified
        assert not hasattr(new_session, "_state_guard_session")
    finally:
        plugin._LIFECYCLE_STATE = orig_lifecycle
        plugin._POISON_REASON = orig_reason
        plugin._ACTIVE_SESSION = orig_active


def test_m3d07_dual_failure_test_call_assertion_and_teardown_concurrency_leak():
    """M3D-07: Dual failure: test-call assertion failure plus fixture teardown concurrency violation.
    Uses deterministic threading.Event synchronization without arbitrary sleeps.
    Asserts both the test body assertion failure and the teardown concurrency violation are reported,
    and pytest exit status is 1 (TESTS_FAILED).
    """
    code = """
import threading
import pytest
from concurrent.futures import ThreadPoolExecutor

task_started = threading.Event()
release_task = threading.Event()

@pytest.fixture
def leaking_teardown_fix():
    yield "fixture_ok"
    ex = ThreadPoolExecutor(max_workers=1)
    def bg_worker():
        task_started.set()
        release_task.wait(timeout=10.0)
    ex.submit(bg_worker)
    # Ensure worker has actually started running before teardown completes:
    task_started.wait(timeout=5.0)
    # Teardown exits while bg_worker is still running!

def test_dual_failure(leaking_teardown_fix):
    # Test call body fails directly:
    assert 1 == 2, "intentional test call assertion failure"

def pytest_sessionfinish():
    # Deterministic cleanup: ensure worker is unblocked so process exits cleanly
    release_task.set()
"""
    res = run_pytest(code)
    assert res.returncode == 1
    # 1. Test body assertion failure is present in output:
    assert "assert 1 == 2" in res.stdout
    assert "intentional test call assertion failure" in res.stdout

    # 2. State Guard teardown concurrency violation is present in output:
    assert "State Guard Teardown Violation: FIXTURE_TEARDOWN" in res.stdout
    assert "fixture 'leaking_teardown_fix'" in res.stdout
    assert "CALLABLE_NOT_EXITED" in res.stdout


def test_m3d08_aggregated_incomplete_diagnostics_coexisting_conditions():
    """M3D-08: Focused verification of aggregated diagnostic builder under coexisting incomplete conditions.
    Simulates uncertain fixtures and SCEN-10 bypassed fixtures coexisting in the same session,
    including distinct fixture definitions that share an argname.
    Asserts:
    1. _LAST_SESSION_INCOMPLETE_REASON contains both diagnostic clauses joined by semicolon.
    2. Fixture names within each clause are sorted deterministically.
    3. The count represents distinct fixture definitions, preserving duplicates when definitions share an argname.
    4. Session exit status is promoted to TESTS_FAILED.
    """
    ledger = Ledger()
    state = plugin.StateGuardSession(ledger=ledger)
    session_mock = type("MockSession", (), {"exitstatus": pytest.ExitCode.OK})()
    session_mock._state_guard_session = state

    # Create two uncertain fixture definitions with unsorted names
    fix_u1 = type("MockFixtureDef", (), {"argname": "zeta_fix"})()
    fix_u2 = type("MockFixtureDef", (), {"argname": "beta_fix"})()
    state.uncertain_fixtures.add(fix_u1)
    state.uncertain_fixtures.add(fix_u2)

    # Create three bypassed fixture definitions, two sharing the same argname 'db_fix'
    fix_b1 = type("MockFixtureDef", (), {"argname": "db_fix"})()
    fix_b2 = type("MockFixtureDef", (), {"argname": "db_fix"})()  # Distinct definition, same argname
    fix_b3 = type("MockFixtureDef", (), {"argname": "api_fix"})()
    state.bypassed_fixtures.add(fix_b1)
    state.bypassed_fixtures.add(fix_b2)
    state.bypassed_fixtures.add(fix_b3)

    assert len(state.bypassed_fixtures) == 3

    outcome_mock = type("MockOutcome", (), {"excinfo": None})()
    with plugin._SESSION_LOCK:
        plugin._LIFECYCLE_STATE = plugin.PluginLifecycle.ACTIVE
        plugin._ACTIVE_SESSION = state
        plugin._evaluate_and_cleanup_session_finish(session_mock, outcome_mock, state)

    # 1. Exit status promoted to TESTS_FAILED
    assert session_mock.exitstatus == pytest.ExitCode.TESTS_FAILED

    # 2. Both conditions present in _LAST_SESSION_INCOMPLETE_REASON
    reason = plugin._LAST_SESSION_INCOMPLETE_REASON
    assert reason is not None
    assert reason.startswith("Incomplete fixture finalization: ")

    # 3. Uncertain condition clause: count=2, sorted deterministically ['beta_fix', 'zeta_fix']
    expected_uncertain_clause = "2 fixture(s) in uncertain instrumentation state: ['beta_fix', 'zeta_fix']"
    assert expected_uncertain_clause in reason

    # 4. Bypassed condition clause: count=3 (distinct definitions), sorted deterministically preserving duplicates ['api_fix', 'db_fix', 'db_fix']
    expected_bypassed_clause = "3 fixture(s) bypassed by non-delegating plugin: ['api_fix', 'db_fix', 'db_fix']"
    assert expected_bypassed_clause in reason

    # 5. Clauses joined cleanly without masking
    assert f"{expected_uncertain_clause}; {expected_bypassed_clause}" in reason




