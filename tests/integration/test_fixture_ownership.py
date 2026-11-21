from __future__ import annotations

import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

import pytest_state_guard.plugin as plugin
from pytest_state_guard.models import OwnerState, OwnerType


class TestFixtureOwnership:
    """Integration tests for fixture lifecycle ownership in pytest."""

    def test_function_fixture_ownership_lifecycle(self) -> None:
        """Verifies standard function fixture setup, task submission, and post-finalizer finalization."""
        code = """
import pytest
from concurrent.futures import ThreadPoolExecutor

@pytest.fixture
def sample_fixture():
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "fixture_work")
        assert f.result() == "fixture_work"
    yield "fixture_val"

def test_uses_fixture(sample_fixture):
    assert sample_fixture == "fixture_val"
"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "test_func.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(code)
            ret = pytest.main([p, "-q", f"--rootdir={td}"], plugins=[plugin])
            assert ret == 0

        ledger = plugin.get_last_session_ledger()
        assert ledger is not None

        fixture_owners = [
            o for o in ledger.get_all_owners()
            if o.fixture_name == "sample_fixture" and o.owner_type == OwnerType.FIXTURE_SETUP
        ]
        assert len(fixture_owners) == 1
        owner = fixture_owners[0]
        assert owner.owner_type == OwnerType.FIXTURE_SETUP
        assert owner.state == OwnerState.FINALIZED
        assert owner.finalized_seq is not None

        teardown_owners = [
            o for o in ledger.get_all_owners()
            if o.fixture_name == "sample_fixture" and o.owner_type == OwnerType.FIXTURE_TEARDOWN
        ]
        assert len(teardown_owners) == 1
        assert teardown_owners[0].state == OwnerState.FINALIZED

        tasks = ledger.get_tasks_for_owner(owner.owner_id)
        assert len(tasks) == 1
        assert tasks[0].created_seq < owner.finalized_seq

    def test_static_fixture_dependencies_sequential(self) -> None:
        """Verifies static fixture dependencies execute sequentially with non-nested owner contexts."""
        code = """
import pytest
from concurrent.futures import ThreadPoolExecutor

@pytest.fixture
def dep_b():
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "b_work")
        assert f.result() == "b_work"
    return "b_val"

@pytest.fixture
def dep_a(dep_b):
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "a_work")
        assert f.result() == "a_work"
    return f"a_with_{dep_b}"

def test_static_deps(dep_a):
    assert "b_val" in dep_a
"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "test_static.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(code)
            ret = pytest.main([p, "-q", f"--rootdir={td}"], plugins=[plugin])
            assert ret == 0

        ledger = plugin.get_last_session_ledger()
        assert ledger is not None

        owner_b = next(o for o in ledger.get_all_owners() if o.fixture_name == "dep_b")
        owner_a = next(o for o in ledger.get_all_owners() if o.fixture_name == "dep_a")

        # Static dependency order: B is set up before A
        assert owner_b.created_seq < owner_a.created_seq

        tasks_b = ledger.get_tasks_for_owner(owner_b.owner_id)
        tasks_a = ledger.get_tasks_for_owner(owner_a.owner_id)
        assert len(tasks_b) == 1
        assert len(tasks_a) == 1
        assert tasks_b[0].owner_id == owner_b.owner_id
        assert tasks_a[0].owner_id == owner_a.owner_id

    def test_dynamic_fixture_requests_nested_attribution(self) -> None:
        """Verifies dynamic fixture requests restore parent owner context before, during, and after."""
        code = """
import pytest
from concurrent.futures import ThreadPoolExecutor

@pytest.fixture
def child_fixture():
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "child_task")
        assert f.result() == "child_task"
    return "child_done"

@pytest.fixture
def parent_fixture(request):
    with ThreadPoolExecutor(max_workers=1) as ex:
        f1 = ex.submit(lambda: "parent_before")
        assert f1.result() == "parent_before"

        child_val = request.getfixturevalue("child_fixture")
        assert child_val == "child_done"

        f2 = ex.submit(lambda: "parent_after")
        assert f2.result() == "parent_after"
    return "parent_done"

def test_dynamic_nesting(parent_fixture):
    assert parent_fixture == "parent_done"
"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "test_dyn.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(code)
            ret = pytest.main([p, "-q", f"--rootdir={td}"], plugins=[plugin])
            assert ret == 0

        ledger = plugin.get_last_session_ledger()
        assert ledger is not None

        parent_owner = next(
            o for o in ledger.get_all_owners() if o.fixture_name == "parent_fixture"
        )
        child_owner = next(
            o for o in ledger.get_all_owners() if o.fixture_name == "child_fixture"
        )

        parent_tasks = ledger.get_tasks_for_owner(parent_owner.owner_id)
        child_tasks = ledger.get_tasks_for_owner(child_owner.owner_id)

        # 2 tasks for parent (before and after), 1 task for child (during)
        assert len(parent_tasks) == 2
        assert len(child_tasks) == 1

        # Ordering: parent task 1 < child task < parent task 2
        assert parent_tasks[0].created_seq < child_tasks[0].created_seq
        assert child_tasks[0].created_seq < parent_tasks[1].created_seq

    def test_generator_fixture_teardown_untracked(self) -> None:
        """Verifies submissions during generator fixture teardown execute along untracked pass-through path."""
        code = """
import pytest
from concurrent.futures import ThreadPoolExecutor

@pytest.fixture
def gen_fixture():
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "setup_work")
        assert f.result() == "setup_work"
    yield "ready"
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "teardown_work")
        assert f.result() == "teardown_work"

def test_gen(gen_fixture):
    pass
"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "test_gen.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(code)
            ret = pytest.main([p, "-q", f"--rootdir={td}"], plugins=[plugin])
            assert ret == 0

        ledger = plugin.get_last_session_ledger()
        assert ledger is not None

        owner = next(
            o for o in ledger.get_all_owners()
            if o.fixture_name == "gen_fixture" and o.owner_type == OwnerType.FIXTURE_SETUP
        )
        tasks = ledger.get_tasks_for_owner(owner.owner_id)
        assert len(tasks) == 1

        teardown_owner = next(
            o for o in ledger.get_all_owners()
            if o.fixture_name == "gen_fixture" and o.owner_type == OwnerType.FIXTURE_TEARDOWN
        )
        teardown_tasks = ledger.get_tasks_for_owner(teardown_owner.owner_id)
        assert len(teardown_tasks) == 1
        assert len(ledger.get_all_tasks()) == 2

    def test_module_scoped_fixture_lifecycle(self) -> None:
        """Verifies module-scoped fixture remains active across multiple tests and finalizes at module teardown."""
        code = """
import pytest
from concurrent.futures import ThreadPoolExecutor

@pytest.fixture(scope="module")
def mod_fixture():
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "mod_work")
        assert f.result() == "mod_work"
    yield "mod_val"

def test_mod_1(mod_fixture):
    assert mod_fixture == "mod_val"

def test_mod_2(mod_fixture):
    assert mod_fixture == "mod_val"
"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "test_mod.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(code)
            ret = pytest.main([p, "-q", f"--rootdir={td}"], plugins=[plugin])
            assert ret == 0

        ledger = plugin.get_last_session_ledger()
        assert ledger is not None

        mod_owner = next(
            o for o in ledger.get_all_owners() if o.fixture_name == "mod_fixture"
        )
        assert mod_owner.state == OwnerState.FINALIZED

        test_owners = [
            o for o in ledger.get_all_owners() if o.owner_type == OwnerType.TEST_CALL
        ]
        assert len(test_owners) == 2

        # Module fixture created before test 1, outlives test 1's teardown, and finalizes during module cleanup
        assert mod_owner.created_seq < test_owners[0].created_seq
        assert mod_owner.finalized_seq is not None
        assert test_owners[0].finalized_seq is not None
        assert test_owners[1].finalized_seq is not None
        assert mod_owner.finalized_seq > test_owners[0].finalized_seq

    def test_session_scoped_fixture_lifecycle(self) -> None:
        """Verifies session-scoped fixture finalizes at session teardown."""
        code = """
import pytest
from concurrent.futures import ThreadPoolExecutor

@pytest.fixture(scope="session")
def sess_fixture():
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "sess_work")
        assert f.result() == "sess_work"
    yield "sess_val"

def test_sess_user(sess_fixture):
    assert sess_fixture == "sess_val"
"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "test_sess.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(code)
            ret = pytest.main([p, "-q", f"--rootdir={td}"], plugins=[plugin])
            assert ret == 0

        ledger = plugin.get_last_session_ledger()
        assert ledger is not None

        sess_owner = next(
            o for o in ledger.get_all_owners() if o.fixture_name == "sess_fixture"
        )
        assert sess_owner.state == OwnerState.FINALIZED
        assert sess_owner.finalized_seq is not None

    def test_parametrized_fixture_distinct_owners(self) -> None:
        """Verifies each parametrized fixture invocation generates a distinct owner and finalizes cleanly."""
        code = """
import pytest
from concurrent.futures import ThreadPoolExecutor

@pytest.fixture(params=["param_1", "param_2"])
def param_fixture(request):
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: request.param)
        assert f.result() == request.param
    yield request.param

def test_param_consumer(param_fixture):
    pass
"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "test_param.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(code)
            ret = pytest.main([p, "-q", f"--rootdir={td}"], plugins=[plugin])
            assert ret == 0

        ledger = plugin.get_last_session_ledger()
        assert ledger is not None

        param_owners = [
            o for o in ledger.get_all_owners()
            if o.fixture_name == "param_fixture" and o.owner_type == OwnerType.FIXTURE_SETUP
        ]
        assert len(param_owners) == 2
        assert param_owners[0].owner_id != param_owners[1].owner_id
        assert param_owners[0].state == OwnerState.FINALIZED
        assert param_owners[1].state == OwnerState.FINALIZED

        tasks_0 = ledger.get_tasks_for_owner(param_owners[0].owner_id)
        tasks_1 = ledger.get_tasks_for_owner(param_owners[1].owner_id)
        assert len(tasks_0) == 1
        assert len(tasks_1) == 1

    def test_failing_fixture_setup_finalization(self) -> None:
        """Verifies when fixture setup raises, context is reset and owner is finalized in post_finalizer."""
        code = """
import pytest
from concurrent.futures import ThreadPoolExecutor

@pytest.fixture
def broken_fixture():
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(lambda: "failing_setup")
        assert f.result() == "failing_setup"
    raise RuntimeError("fixture_setup_failed")

def test_never_runs(broken_fixture):
    pass
"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "test_failing_fixture_setup.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(code)
            ret = pytest.main([p, "-q", f"--rootdir={td}"], plugins=[plugin])
            # Return code 1 because fixture setup error occurred
            assert ret == 1

        ledger = plugin.get_last_session_ledger()
        assert ledger is not None

        broken_owner = next(
            o for o in ledger.get_all_owners() if o.fixture_name == "broken_fixture"
        )
        # Owner was finalized despite setup failure
        assert broken_owner.state == OwnerState.FINALIZED
        assert broken_owner.finalized_seq is not None

        # Test call never ran, so no test owner was created
        test_owners = [
            o for o in ledger.get_all_owners() if o.owner_type == OwnerType.TEST_CALL
        ]
        assert len(test_owners) == 0
