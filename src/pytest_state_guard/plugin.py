from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from typing import Any, Generator

import pytest
from _pytest.fixtures import FixtureDef, SubRequest

from pytest_state_guard.executor import (
    _REAL_ORIGINAL_SUBMIT,
    _state_guard_submit,
    get_active_ledger,
    install_patch,
    is_patched,
    remove_patch,
)
from pytest_state_guard.ledger import Ledger
from pytest_state_guard.models import OwnerType
from pytest_state_guard.owners import OwnerContext, reset_current_owner, set_current_owner


class PluginLifecycle(str, Enum):
    """Lifecycle state of State Guard plugin within the Python process."""

    IDLE = "IDLE"          # No active session; ready to start
    ACTIVE = "ACTIVE"      # Session actively running
    POISONED = "POISONED"  # Cleanup or installation failed; process cannot safely continue


class StateGuardSession:
    """Encapsulates per-pytest-session State Guard state."""

    def __init__(self, ledger: Ledger | None = None) -> None:
        self.ledger = ledger or Ledger()
        self.active_fixture_owners: dict[FixtureDef[Any], str] = {}
        self.active_test_owners: dict[pytest.Item, str] = {}


_SESSION_LOCK = threading.Lock()
_LIFECYCLE_STATE = PluginLifecycle.IDLE
_POISON_REASON: str | None = None
_ACTIVE_SESSION: StateGuardSession | None = None
_LAST_SESSION_LEDGER: Ledger | None = None


def get_lifecycle_state() -> PluginLifecycle:
    """Return the current plugin lifecycle state."""
    with _SESSION_LOCK:
        return _LIFECYCLE_STATE


def get_poison_reason() -> str | None:
    """Return the reason for the POISONED lifecycle state, if any."""
    with _SESSION_LOCK:
        return _POISON_REASON


def get_active_session() -> StateGuardSession | None:
    """Return the active session state, if any."""
    with _SESSION_LOCK:
        return _ACTIVE_SESSION


def get_last_session_ledger() -> Ledger | None:
    """Return the ledger from the most recently concluded session, if any."""
    with _SESSION_LOCK:
        return _LAST_SESSION_LEDGER


def _get_verified_session(session: pytest.Session) -> StateGuardSession | None:
    """Return the session state iff it matches all four conditions of the verified ACTIVE invariant."""
    with _SESSION_LOCK:
        if _LIFECYCLE_STATE != PluginLifecycle.ACTIVE or _ACTIVE_SESSION is None:
            return None
        state = getattr(session, "_state_guard_session", None)
        if state is None or state is not _ACTIVE_SESSION:
            return None
        if not is_patched() or get_active_ledger() is not _ACTIVE_SESSION.ledger:
            return None
        return state


# ---------------------------------------------------------------------------
# Pytest Session Lifecycle Hooks
# ---------------------------------------------------------------------------


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    """Initialize State Guard session and install patch transactionally."""
    global _LIFECYCLE_STATE, _POISON_REASON, _ACTIVE_SESSION
    with _SESSION_LOCK:
        if _LIFECYCLE_STATE == PluginLifecycle.POISONED:
            raise RuntimeError(
                f"Cannot start pytest session: State Guard is in a POISONED lifecycle state due to "
                f"a previous unrecoverable error: {_POISON_REASON}. The Python process must be restarted."
            )

        if _LIFECYCLE_STATE == PluginLifecycle.ACTIVE or _ACTIVE_SESSION is not None:
            raise RuntimeError(
                "State Guard v1 does not support concurrent or nested pytest sessions in the same process."
            )

        state = StateGuardSession()
        startup_exc: BaseException | None = None

        try:
            install_patch(state.ledger)
            session._state_guard_session = state
            _ACTIVE_SESSION = state
            _LIFECYCLE_STATE = PluginLifecycle.ACTIVE
        except BaseException as exc:
            startup_exc = exc

        if startup_exc is not None:
            # Ownership-Aware Rollback:
            # We ONLY remove the patch if our current attempt actually owns it!
            if is_patched() and get_active_ledger() is state.ledger:
                try:
                    remove_patch()
                except BaseException as r_exc:
                    _LIFECYCLE_STATE = PluginLifecycle.POISONED
                    _POISON_REASON = f"Startup failed and patch rollback failed: {r_exc}"
                    if hasattr(session, "_state_guard_session"):
                        delattr(session, "_state_guard_session")
                    _ACTIVE_SESSION = None
                    raise startup_exc from r_exc

            # Check for partial installation anomalies:
            wrapper_is_ours = getattr(ThreadPoolExecutor, "submit", None) is _state_guard_submit
            ledger_is_ours = get_active_ledger() is state.ledger

            if (wrapper_is_ours and not is_patched()) or (ledger_is_ours and not is_patched()):
                _LIFECYCLE_STATE = PluginLifecycle.POISONED
                _POISON_REASON = (
                    "Partial installation detected: wrapper or ledger partially set without patch completion. "
                    "Cannot safely restore state without restarting process."
                )
            elif not is_patched() or get_active_ledger() is not state.ledger:
                # Pre-existing patch owned by another entity was preserved, or clean rollback
                _LIFECYCLE_STATE = PluginLifecycle.IDLE

            if hasattr(session, "_state_guard_session"):
                delattr(session, "_state_guard_session")
            _ACTIVE_SESSION = None
            raise startup_exc


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Clean up State Guard session and remove patch transactionally."""
    global _LIFECYCLE_STATE, _POISON_REASON, _ACTIVE_SESSION, _LAST_SESSION_LEDGER
    with _SESSION_LOCK:
        state = getattr(session, "_state_guard_session", None)
        if state is None or state is not _ACTIVE_SESSION:
            return

        _LAST_SESSION_LEDGER = state.ledger
        shutdown_exc: BaseException | None = None

        try:
            remove_patch()
        except BaseException as exc:
            shutdown_exc = exc

        # Verify actual patch restoration
        patch_still_active = is_patched() or (
            getattr(ThreadPoolExecutor, "submit", None) is _state_guard_submit
        )

        if shutdown_exc is not None or patch_still_active:
            _LIFECYCLE_STATE = PluginLifecycle.POISONED
            _POISON_REASON = (
                f"Patch removal failed during session teardown: {shutdown_exc or 'patch still active'}"
            )
            if shutdown_exc is not None:
                raise shutdown_exc
            raise RuntimeError(f"State Guard shutdown verification failed: {_POISON_REASON}")

        # Patch is cleanly removed. Finalize session cleanup in a failure-safe boundary:
        try:
            if hasattr(session, "_state_guard_session"):
                delattr(session, "_state_guard_session")
            _ACTIVE_SESSION = None
            _LIFECYCLE_STATE = PluginLifecycle.IDLE
        except BaseException as clean_exc:
            _LIFECYCLE_STATE = PluginLifecycle.POISONED
            _POISON_REASON = f"Session reference cleanup failed during shutdown: {clean_exc}"
            raise clean_exc


# ---------------------------------------------------------------------------
# Fixture Lifecycle Hooks
# ---------------------------------------------------------------------------


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_fixture_setup(
    fixturedef: FixtureDef[Any], request: SubRequest
) -> Generator[None, None, None]:
    """Track fixture owner lifecycle and activate ContextVar during setup."""
    state = _get_verified_session(request.session)
    if state is None:
        yield
        return

    owner_id = f"owner:fixture:{fixturedef.argname}:{uuid.uuid4().hex}"
    state.ledger.create_owner(
        owner_id=owner_id,
        owner_type=OwnerType.FIXTURE_SETUP,
        nodeid=request.node.nodeid,
        fixture_name=fixturedef.argname,
    )
    state.active_fixture_owners[fixturedef] = owner_id
    token = set_current_owner(
        OwnerContext(
            owner_id=owner_id,
            owner_type=OwnerType.FIXTURE_SETUP,
            nodeid=request.node.nodeid,
            fixture_name=fixturedef.argname,
        )
    )

    try:
        yield
    finally:
        reset_current_owner(token)


@pytest.hookimpl
def pytest_fixture_post_finalizer(fixturedef: FixtureDef[Any], request: SubRequest) -> None:
    """Finalize fixture owner in the ledger after all fixture teardown has concluded."""
    state = _get_verified_session(request.session)
    if state is None:
        return

    owner_id = state.active_fixture_owners.pop(fixturedef, None)
    if owner_id is not None:
        state.ledger.finalize_owner(owner_id)


# ---------------------------------------------------------------------------
# Test Item Lifecycle Hooks
# ---------------------------------------------------------------------------


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, None, None]:
    """Track test-item owner lifecycle and activate ContextVar during call phase."""
    state = _get_verified_session(item.session)
    if state is None:
        yield
        return

    owner_id = f"owner:test:{item.nodeid}:{uuid.uuid4().hex}"
    state.ledger.create_owner(
        owner_id=owner_id,
        owner_type=OwnerType.TEST_CALL,
        nodeid=item.nodeid,
        fixture_name=None,
    )
    state.active_test_owners[item] = owner_id
    token = set_current_owner(
        OwnerContext(
            owner_id=owner_id,
            owner_type=OwnerType.TEST_CALL,
            nodeid=item.nodeid,
            fixture_name=None,
        )
    )

    try:
        yield
    finally:
        reset_current_owner(token)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(
    item: pytest.Item, nextitem: pytest.Item | None
) -> Generator[None, None, None]:
    """Finalize test-item owner in the ledger after all teardown and fixture finalizers conclude."""
    state = _get_verified_session(item.session)
    if state is None:
        yield
        return

    try:
        yield
    finally:
        owner_id = state.active_test_owners.pop(item, None)
        if owner_id is not None:
            state.ledger.finalize_owner(owner_id)
