from __future__ import annotations

import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from typing import Any, Callable, Generator

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
from pytest_state_guard.models import OwnerRecord, OwnerState, OwnerType, Severity
from pytest_state_guard.owners import (
    OwnerContext,
    get_current_owner,
    reset_current_owner,
    set_current_owner,
)
from pytest_state_guard.policy import Finding, PolicyEngine
from pytest_state_guard.reporting import (
    ChainedTerminalRepr,
    StateGuardTerminalRepr,
    render_terminal_summary,
)


class PluginLifecycle(str, Enum):
    """Lifecycle state of State Guard plugin within the Python process."""

    IDLE = "IDLE"          # No active session; ready to start
    ACTIVE = "ACTIVE"      # Session actively running
    POISONED = "POISONED"  # Cleanup or installation failed; process cannot safely continue


class StateGuardSession:
    """Encapsulates per-pytest-session State Guard state."""

    def __init__(self, ledger: Ledger | None = None) -> None:
        self.session_id: str = uuid.uuid4().hex
        self.ledger = ledger or Ledger()
        self.active_fixture_owners: dict[FixtureDef[Any], str] = {}
        self.active_test_owners: dict[pytest.Item, str] = {}
        self.fixture_def_registry: dict[FixtureDef[Any], int] = {}
        self.node_registry: dict[str, int] = {}
        self.execution_counters: dict[tuple[int, int, str], int] = {}
        self.active_fixture_instances: dict[FixtureDef[Any], int] = {}
        self.post_finalizers: dict[tuple[FixtureDef[Any], int], Any] = {}
        self.active_teardown_owners: dict[tuple[FixtureDef[Any], int], str] = {}
        self.degradations: list[str] = []
        self.entered_dispatcher_depth: dict[FixtureDef[Any], int] = {}
        self.dispatched_instances: set[tuple[FixtureDef[Any], int]] = set()
        self.dispatch_depth: dict[tuple[FixtureDef[Any], int], int] = {}
        self.transient_entry_failures: set[FixtureDef[Any]] = set()
        self.session_entry_failures: set[FixtureDef[Any]] = set()
        self.uncertain_fixtures: set[FixtureDef[Any]] = set()
        self.bypassed_fixtures: set[FixtureDef[Any]] = set()


_SESSION_LOCK = threading.Lock()
_LIFECYCLE_STATE = PluginLifecycle.IDLE
_POISON_REASON: str | None = None
_ACTIVE_SESSION: StateGuardSession | None = None
_LAST_SESSION_LEDGER: Ledger | None = None
_LAST_SESSION_FINDINGS: dict[str, list[Finding]] | None = None
_LAST_SESSION_OWNERS: dict[str, OwnerRecord] | None = None
_LAST_SESSION_INCOMPLETE_REASON: str | None = None


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


def get_last_session_findings() -> dict[str, list[Finding]] | None:
    """Return the cached findings from the most recently concluded session audit."""
    with _SESSION_LOCK:
        return _LAST_SESSION_FINDINGS


def get_last_session_incomplete_reason() -> str | None:
    """Return the incomplete reason from the most recently concluded session, if any."""
    with _SESSION_LOCK:
        return _LAST_SESSION_INCOMPLETE_REASON



def _safe_exc_str(exc: BaseException | None) -> str:
    """Safely extract string representation of an exception without failing if __str__ raises."""
    if exc is None:
        return "None"
    try:
        msg = str(exc)
        return msg if type(msg) is str and msg else exc.__class__.__name__
    except BaseException:
        try:
            return f"<{exc.__class__.__name__} (unprintable)>"
        except BaseException:
            return "<UnprintableException>"


def _safe_record_degradation(state: StateGuardSession | None, message: str) -> None:
    """Record a non-fatal operational degradation string without throwing under any circumstance."""
    if state is None:
        return
    try:
        if hasattr(state, "degradations") and isinstance(state.degradations, list):
            state.degradations.append(str(message))
    except BaseException:
        pass


def _safe_finalize_owner(state: StateGuardSession, owner_id: str) -> None:
    """Attempt to finalize an owner in the ledger, isolating any errors behind safe degradation."""
    try:
        state.ledger.finalize_owner(owner_id)
    except BaseException as exc:
        _safe_record_degradation(
            state,
            f"Failed to finalize owner '{owner_id}': {_safe_exc_str(exc)}"
        )


def _get_fixture_identity_key(
    fixturedef: FixtureDef[Any],
    request: SubRequest,
    state: StateGuardSession,
) -> tuple[int, int, str]:
    """Derive deterministic identity key tokens for fixturedef, nodeid, and param."""
    def_token = state.fixture_def_registry.setdefault(fixturedef, len(state.fixture_def_registry) + 1)
    nodeid = request.node.nodeid if request is not None and getattr(request, "node", None) is not None else ""
    node_token = state.node_registry.setdefault(nodeid, len(state.node_registry) + 1)
    if request is not None and getattr(request, "param", None) is not None:
        param_idx = getattr(request, "param_index", None)
        param_key = f"p{param_idx}" if param_idx is not None else f"p{getattr(request, 'param', '')}"
    else:
        param_key = "p0"
    return (def_token, node_token, param_key)


def _build_runtime_owner_id(
    fixturedef: FixtureDef[Any],
    request: SubRequest,
    state: StateGuardSession,
    instance_seq: int,
) -> str:
    """Build deterministic runtime owner ID encoding fixture name, def token, node token, param key, and seq."""
    def_token, node_token, param_key = _get_fixture_identity_key(fixturedef, request, state)
    return f"owner:fixture_teardown:{fixturedef.argname}:{def_token}:{node_token}:{param_key}:{instance_seq}"


def _has_user_teardown_callbacks(
    fixturedef: FixtureDef[Any],
    state: StateGuardSession,
    instance_seq: int,
) -> bool:
    """Determine whether fixturedef has user-registered teardown callbacks.

    Pytest registers ihook.pytest_fixture_post_finalizer as the first finalizer in
    fixturedef._finalizers (executed last due to LIFO pop). If fixturedef._finalizers
    contains only that post-finalizer sentinel, there are no user finalizers.
    """
    finalizers = getattr(fixturedef, "_finalizers", None)
    if not finalizers:
        return False

    sentinel = state.post_finalizers.get((fixturedef, instance_seq))
    if sentinel is None:
        _safe_record_degradation(
            state,
            f"Missing post-finalizer sentinel for fixture '{fixturedef.argname}' at sequence {instance_seq}"
        )
        return False

    if len(finalizers) == 1 and finalizers[0] is sentinel:
        return False

    return True


_FIXTURE_PATCH_LOCK = threading.Lock()
_ORIGINAL_FIXTUREDEF_FINISH: Callable[..., Any] = FixtureDef.finish
_IS_FIXTURE_PATCHED = False


def is_fixture_patched() -> bool:
    """Return True if State Guard fixture finish patch is currently active."""
    with _FIXTURE_PATCH_LOCK:
        return _IS_FIXTURE_PATCHED


def install_fixture_patch() -> None:
    """Install static dispatcher on FixtureDef.finish."""
    global _ORIGINAL_FIXTUREDEF_FINISH, _IS_FIXTURE_PATCHED
    with _FIXTURE_PATCH_LOCK:
        if _IS_FIXTURE_PATCHED:
            return
        if FixtureDef.finish is not _state_guard_fixture_finish:
            _ORIGINAL_FIXTUREDEF_FINISH = FixtureDef.finish
            FixtureDef.finish = _state_guard_fixture_finish  # type: ignore[method-assign]
        _IS_FIXTURE_PATCHED = True


def remove_fixture_patch() -> None:
    """Restore original FixtureDef.finish."""
    global _IS_FIXTURE_PATCHED
    with _FIXTURE_PATCH_LOCK:
        if not _IS_FIXTURE_PATCHED:
            return
        if getattr(FixtureDef, "finish", None) is _state_guard_fixture_finish:
            FixtureDef.finish = _ORIGINAL_FIXTUREDEF_FINISH  # type: ignore[method-assign]
        _IS_FIXTURE_PATCHED = False


def _safe_reset_current_owner(
    token: Any,
    state: StateGuardSession,
    owner_id: str | None,
    prior_owner: OwnerContext | None = None,
) -> None:
    """Best-effort reset of ContextVar owner context under recoverable execution conditions."""
    if token is not None:
        try:
            reset_current_owner(token)
            return
        except BaseException as token_exc:
            _safe_record_degradation(
                state,
                f"Context reset failed for teardown owner '{owner_id}': {_safe_exc_str(token_exc)}"
            )
    # Tokenless or reset failure: restore prior_owner if current differs
    try:
        if get_current_owner() is not prior_owner:
            set_current_owner(prior_owner)
    except BaseException as fallback_exc:
        _safe_record_degradation(
            state,
            f"Context fallback restoration failed for teardown owner '{owner_id}': {_safe_exc_str(fallback_exc)}"
        )


def _safe_cleanup_fixture_registries(
    state: StateGuardSession,
    fixturedef: FixtureDef[Any],
    marker_key: tuple[FixtureDef[Any], int] | None,
) -> None:
    """Best-effort cleanup of fixture tracking registries."""
    try:
        state.active_fixture_instances.pop(fixturedef, None)
    except BaseException:
        pass
    if marker_key is not None:
        try:
            state.post_finalizers.pop(marker_key, None)
        except BaseException:
            pass


def _state_guard_fixture_finish(self: FixtureDef[Any], request: SubRequest) -> None:
    """Static dispatcher wrapping FixtureDef.finish with structured evidence and deterministic recovery."""
    state = _get_verified_session(request.session)
    if state is None:
        _ORIGINAL_FIXTUREDEF_FINISH(self, request)
        return

    entry_registered = False
    marker_key: tuple[FixtureDef[Any], int] | None = None
    should_track = False
    is_nested = False
    is_outermost = False
    owner_registered = False
    owner_id: str | None = None
    token: Any = None
    prior_owner: OwnerContext | None = None

    try:
        # Phase 0: Structured Invocation-Owned Entry Registration
        try:
            cur_entry = state.entered_dispatcher_depth.get(self, 0)
            state.entered_dispatcher_depth[self] = cur_entry + 1
            entry_registered = True
        except Exception as entry_exc:
            try:
                state.fixture_def_registry.setdefault(self, len(state.fixture_def_registry) + 1)
            except BaseException:
                pass
            try:
                state.transient_entry_failures.add(self)
            except BaseException:
                pass
            try:
                state.session_entry_failures.add(self)
            except BaseException:
                pass
            try:
                state.uncertain_fixtures.add(self)
            except BaseException:
                pass
            try:
                object.__setattr__(self, "_sg_entry_failed_session_id", state.session_id)
            except BaseException:
                pass
            _safe_record_degradation(
                state,
                f"Failed to register dispatcher entry for fixture '{self.argname}': {_safe_exc_str(entry_exc)}"
            )

        # Phase A: State Guard Initialization (Fail-Open Boundary)
        # Note: _ORIGINAL_FIXTUREDEF_FINISH is NEVER called in Phase A!
        try:
            instance_seq = state.active_fixture_instances.get(self)
            if instance_seq is None:
                pending = [seq for (f, seq) in state.post_finalizers.keys() if f is self]
                if len(pending) == 1:
                    instance_seq = pending[0]
                elif len(pending) > 1:
                    _safe_record_degradation(
                        state,
                        f"Ambiguous pending fixture instances for '{self.argname}': {pending}; failing open without teardown tracking"
                    )
                    instance_seq = None
                else:
                    instance_seq = None

            if instance_seq is not None:
                marker_key = (self, instance_seq)

                if self.cached_result is not None:
                    current_depth = state.dispatch_depth.get(marker_key, 0)
                    if current_depth > 0:
                        is_nested = True
                        state.dispatch_depth[marker_key] = current_depth + 1
                        should_track = False
                    else:
                        is_outermost = True
                        state.dispatch_depth[marker_key] = 1
                        state.dispatched_instances.add(marker_key)
                        if _has_user_teardown_callbacks(self, state, instance_seq):
                            should_track = True

        except Exception as init_exc:
            _safe_record_degradation(
                state,
                f"Failed to initialize teardown tracking for fixture '{self.argname}': {_safe_exc_str(init_exc)}"
            )
            should_track = False
            if is_outermost and marker_key is not None:
                try:
                    state.dispatched_instances.discard(marker_key)
                    state.dispatch_depth.pop(marker_key, None)
                except BaseException:
                    pass
                is_outermost = False

        # Phase B: Tracking Setup (Ledger Owner & Context Activation)
        if should_track and is_outermost and marker_key is not None:
            new_owner_context = None
            try:
                owner_id = _build_runtime_owner_id(self, request, state, marker_key[1])
                nodeid = request.node.nodeid if request is not None and getattr(request, "node", None) is not None else ""
                state.ledger.create_owner(
                    owner_id=owner_id,
                    owner_type=OwnerType.FIXTURE_TEARDOWN,
                    nodeid=nodeid,
                    fixture_name=self.argname,
                )
                owner_registered = True
                state.active_teardown_owners[marker_key] = owner_id

                prior_owner = get_current_owner()
                new_owner_context = OwnerContext(
                    owner_id=owner_id,
                    owner_type=OwnerType.FIXTURE_TEARDOWN,
                    nodeid=nodeid,
                    fixture_name=self.argname,
                )
                token = set_current_owner(new_owner_context)

            except Exception as owner_exc:
                _safe_record_degradation(
                    state,
                    f"Failed to activate teardown owner for fixture '{self.argname}': {_safe_exc_str(owner_exc)}"
                )
                _safe_reset_current_owner(token, state, owner_id, prior_owner)
                token = None

                if owner_registered and owner_id is not None:
                    try:
                        state.active_teardown_owners.pop(marker_key, None)
                    except BaseException:
                        pass
                    _safe_finalize_owner(state, owner_id)
                    owner_registered = False

        # Phase C: Single Teardown Execution (Pure try ... finally)
        _ORIGINAL_FIXTUREDEF_FINISH(self, request)

    finally:
        # Phase D: Best-Effort Cleanup
        if is_nested and marker_key is not None:
            try:
                cur = state.dispatch_depth.get(marker_key, 1) - 1
                if cur > 0:
                    state.dispatch_depth[marker_key] = cur
                else:
                    state.dispatch_depth.pop(marker_key, None)
            except BaseException as dec_exc:
                _safe_record_degradation(state, f"Failed to decrement dispatch depth: {_safe_exc_str(dec_exc)}")

        if is_outermost and marker_key is not None:
            _safe_reset_current_owner(token, state, owner_id, prior_owner)

            if owner_registered and owner_id is not None:
                try:
                    state.active_teardown_owners.pop(marker_key, None)
                except BaseException as pop_exc:
                    _safe_record_degradation(state, f"Failed to pop teardown owner: {_safe_exc_str(pop_exc)}")
                _safe_finalize_owner(state, owner_id)

            try:
                state.dispatched_instances.discard(marker_key)
            except BaseException as disc_exc:
                _safe_record_degradation(state, f"Failed to discard dispatched instance: {_safe_exc_str(disc_exc)}")

            try:
                state.dispatch_depth.pop(marker_key, None)
            except BaseException as depth_exc:
                _safe_record_degradation(state, f"Failed to pop dispatch depth: {_safe_exc_str(depth_exc)}")

            _safe_cleanup_fixture_registries(state, self, marker_key)

        if entry_registered:
            try:
                cur_entry = state.entered_dispatcher_depth.get(self, 1) - 1
                if cur_entry > 0:
                    state.entered_dispatcher_depth[self] = cur_entry
                else:
                    state.entered_dispatcher_depth.pop(self, None)
            except BaseException as entry_pop_exc:
                _safe_record_degradation(state, f"Failed to decrement entry depth: {_safe_exc_str(entry_pop_exc)}")

        if not is_nested:
            try:
                state.transient_entry_failures.discard(self)
            except BaseException:
                pass


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
        if not is_fixture_patched():
            return None
        return state


# ---------------------------------------------------------------------------
# Pytest Session Lifecycle Hooks
# ---------------------------------------------------------------------------


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    """Initialize State Guard session and install patch transactionally."""
    global _LIFECYCLE_STATE, _POISON_REASON, _ACTIVE_SESSION
    global _LAST_SESSION_FINDINGS, _LAST_SESSION_OWNERS, _LAST_SESSION_INCOMPLETE_REASON
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
            install_fixture_patch()
            session._state_guard_session = state
            _ACTIVE_SESSION = state
            _LIFECYCLE_STATE = PluginLifecycle.ACTIVE
            _LAST_SESSION_FINDINGS = None
            _LAST_SESSION_OWNERS = None
            _LAST_SESSION_INCOMPLETE_REASON = None
        except BaseException as exc:
            startup_exc = exc

        if startup_exc is not None:
            # Ownership-Aware Rollback:
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

            if is_fixture_patched():
                try:
                    remove_fixture_patch()
                except BaseException as r_exc:
                    _LIFECYCLE_STATE = PluginLifecycle.POISONED
                    _POISON_REASON = f"Startup failed and fixture patch rollback failed: {r_exc}"
                    if hasattr(session, "_state_guard_session"):
                        delattr(session, "_state_guard_session")
                    _ACTIVE_SESSION = None
                    raise startup_exc from r_exc

            # Check for partial installation anomalies:
            wrapper_is_ours = getattr(ThreadPoolExecutor, "submit", None) is _state_guard_submit
            ledger_is_ours = get_active_ledger() is state.ledger
            fixture_wrapper_is_ours = getattr(FixtureDef, "finish", None) is _state_guard_fixture_finish

            if (
                (wrapper_is_ours and not is_patched())
                or (ledger_is_ours and not is_patched())
                or (fixture_wrapper_is_ours and not is_fixture_patched())
            ):
                _LIFECYCLE_STATE = PluginLifecycle.POISONED
                _POISON_REASON = (
                    "Partial installation detected: wrapper or ledger partially set without patch completion. "
                    "Cannot safely restore state without restarting process."
                )
            elif (not is_patched() or get_active_ledger() is not state.ledger) and not is_fixture_patched():
                _LIFECYCLE_STATE = PluginLifecycle.IDLE

            if hasattr(session, "_state_guard_session"):
                delattr(session, "_state_guard_session")
            _ACTIVE_SESSION = None
            raise startup_exc



def _evaluate_and_cleanup_session_finish(
    session: pytest.Session, outcome: Any, state: SessionState
) -> None:
    """Internal helper executing post-yield audit, exit status update, and cleanup.

    MUST be called while holding _SESSION_LOCK.
    Guarantees that remove_patch(), patch-state verification, and session reference
    cleanup are each treated as fallible operations with full exception isolation.
    Under no circumstances can an unexpected exception during audit, unpatching,
    or verification bypass lifecycle state reconciliation.
    """
    global _LIFECYCLE_STATE, _POISON_REASON, _ACTIVE_SESSION, _LAST_SESSION_LEDGER
    global _LAST_SESSION_FINDINGS, _LAST_SESSION_OWNERS, _LAST_SESSION_INCOMPLETE_REASON

    _LAST_SESSION_LEDGER = state.ledger

    # Pluggy old-style wrapper semantics: inspect outcome.excinfo
    # outcome.excinfo is (exc_type, exc_val, exc_tb) or None
    runner_excinfo = getattr(outcome, "excinfo", None)

    if runner_excinfo is not None:
        # Case B & Case C: Runner fixture teardown raised an exception!
        exc_val = runner_excinfo[1]
        _LAST_SESSION_FINDINGS = {}

        # Fallible owner snapshot retrieval in interrupted path:
        # Do not discard snapshot failures; record in incomplete-session diagnostic.
        try:
            _LAST_SESSION_OWNERS = {o.owner_id: o for o in state.ledger.get_all_owners()}
            _LAST_SESSION_INCOMPLETE_REASON = (
                f"Session teardown interrupted by runner exception: {_safe_exc_str(exc_val)}"
            )
        except BaseException as snap_exc:
            _LAST_SESSION_OWNERS = {}
            _LAST_SESSION_INCOMPLETE_REASON = (
                f"Session teardown interrupted by runner exception: {_safe_exc_str(exc_val)}; "
                f"additionally, owner snapshot retrieval failed: {_safe_exc_str(snap_exc)}"
            )

        # Cleanup Step 1: Attempt emergency patch removal
        shutdown_exc: BaseException | None = None
        try:
            remove_patch()
        except BaseException as exc:
            shutdown_exc = exc

        try:
            remove_fixture_patch()
        except BaseException as exc:
            if shutdown_exc is None:
                shutdown_exc = exc

        # Cleanup Step 2: Fallible patch-state verification
        verification_exc: BaseException | None = None
        patch_still_active: bool = False
        try:
            patch_still_active = (
                is_patched()
                or (getattr(ThreadPoolExecutor, "submit", None) is _state_guard_submit)
                or is_fixture_patched()
                or (getattr(FixtureDef, "finish", None) is _state_guard_fixture_finish)
            )
        except BaseException as v_exc:
            verification_exc = v_exc
            patch_still_active = True

        # Cleanup Step 3: Session reference cleanup (attempted where safe)
        ref_cleanup_exc: BaseException | None = None
        try:
            if hasattr(session, "_state_guard_session"):
                delattr(session, "_state_guard_session")
            _ACTIVE_SESSION = None
        except BaseException as clean_exc:
            ref_cleanup_exc = clean_exc

        # Cleanup Step 4: Reconcile cleanup outcome under interrupted runner
        cleanup_failures: list[str] = []
        if shutdown_exc is not None:
            cleanup_failures.append(
                f"Patch removal failed during interrupted session teardown: {_safe_exc_str(shutdown_exc)}"
            )
        if verification_exc is not None:
            cleanup_failures.append(f"Patch verification failed: {_safe_exc_str(verification_exc)}")
        elif patch_still_active:
            cleanup_failures.append("Patch still active after removal")
        if ref_cleanup_exc is not None:
            cleanup_failures.append(f"Session reference cleanup failed: {_safe_exc_str(ref_cleanup_exc)}")

        # Interrupted runner teardown ALWAYS poisons lifecycle because the audit was not completed.
        # IDLE is never set unless audit succeeded, patch removal succeeded, verification confirmed
        # patch is absent, and session references were cleaned up.
        _LIFECYCLE_STATE = PluginLifecycle.POISONED
        if cleanup_failures:
            _POISON_REASON = (
                f"Session teardown interrupted by runner exception before audit could complete: "
                f"{_safe_exc_str(exc_val)}; additionally, {'; '.join(cleanup_failures)}"
            )
        else:
            _POISON_REASON = (
                f"Session teardown interrupted by runner exception before audit could complete: "
                f"{_safe_exc_str(exc_val)}"
            )

        # CRITICAL: Preserve the original runner exception in outcome!
        # Do not overwrite or force another exception into outcome.
        return

    # Normal runner completion (outcome.excinfo is None)
    # Step 1: Perform audit inside try/except block so audit failure NEVER skips cleanup!
    audit_exc: BaseException | None = None
    has_violation: bool = False
    active_ids: set[str] = set()
    stale_sentinels: set[tuple[FixtureDef[Any], int]] = set()
    stale_teardowns: set[tuple[FixtureDef[Any], int]] = set()
    session_entry_failures: set[FixtureDef[Any]] = set()
    uncertain_fixtures: set[FixtureDef[Any]] = set()
    attr_failures: set[FixtureDef[Any]] = set()
    bypassed_fixtures: set[FixtureDef[Any]] = set()
    try:
        all_owners = state.ledger.get_all_owners()
        _LAST_SESSION_OWNERS = {o.owner_id: o for o in all_owners}
        all_ids = set(_LAST_SESSION_OWNERS.keys())
        finalized_ids = {o.owner_id for o in all_owners if o.state == OwnerState.FINALIZED}
        active_ids = all_ids - finalized_ids

        # Mode A check: Inconsistency between ledger events and owner records
        events = state.ledger.get_events()
        orphan_event_owners = {e.owner_id for e in events if e.owner_id not in all_ids}
        if orphan_event_owners:
            raise RuntimeError(
                f"Ledger structural inconsistency detected: events exist for unknown owner(s): {orphan_event_owners}"
            )

        stale_sentinels = set(state.post_finalizers.keys())
        stale_teardowns = set(state.active_teardown_owners.keys())
        session_entry_failures = set(state.session_entry_failures)
        uncertain_fixtures = set(state.uncertain_fixtures)
        bypassed_fixtures = set(getattr(state, "bypassed_fixtures", ()))
        known_fixtures: set[FixtureDef[Any]] = (
            set(state.fixture_def_registry.keys())
            | set(state.active_fixture_owners.keys())
            | set(state.active_fixture_instances.keys())
            | {f for (f, _) in state.post_finalizers.keys()}
            | set(state.entered_dispatcher_depth.keys())
            | set(state.session_entry_failures)
            | set(state.uncertain_fixtures)
            | set(state.transient_entry_failures)
            | set(bypassed_fixtures)
        )
        attr_failures = {
            f for f in known_fixtures
            if getattr(f, "_sg_entry_failed_session_id", None) == state.session_id
        }

        incomplete_parts: list[str] = []
        if active_ids:
            incomplete_parts.append(f"{len(active_ids)} owner(s) remained active")
        if stale_sentinels:
            incomplete_parts.append(f"{len(stale_sentinels)} unresolved sentinel(s) remained in registry")
        if stale_teardowns:
            incomplete_parts.append(f"{len(stale_teardowns)} teardown owner(s) remained unfinalized")
        if session_entry_failures:
            incomplete_parts.append(
                f"{len(session_entry_failures)} dispatcher entry registration failure(s) recorded: {sorted(f.argname for f in session_entry_failures)}"
            )
        if uncertain_fixtures or attr_failures:
            all_uncertain = uncertain_fixtures | attr_failures
            incomplete_parts.append(
                f"{len(all_uncertain)} fixture(s) in uncertain instrumentation state: {sorted(f.argname for f in all_uncertain)}"
            )
        if bypassed_fixtures:
            incomplete_parts.append(
                f"{len(bypassed_fixtures)} fixture(s) bypassed by non-delegating plugin: {sorted(f.argname for f in bypassed_fixtures)}"
            )

        if incomplete_parts:
            _LAST_SESSION_INCOMPLETE_REASON = (
                f"Incomplete fixture finalization: {'; '.join(incomplete_parts)}"
            )
        else:
            _LAST_SESSION_INCOMPLETE_REASON = None

        # Policy evaluation using exact Stage 4 API: evaluate_all_finalized(state.ledger)
        engine = PolicyEngine()
        findings_by_owner = engine.evaluate_all_finalized(state.ledger)
        _LAST_SESSION_FINDINGS = findings_by_owner

        has_violation = any(
            any(f.severity == Severity.VIOLATION for f in f_list)
            for f_list in findings_by_owner.values()
        )
    except BaseException as exc:
        audit_exc = exc
        _LAST_SESSION_FINDINGS = {}
        try:
            _LAST_SESSION_INCOMPLETE_REASON = (
                f"Session audit failed due to exception: {_safe_exc_str(exc)}"
            )
        except BaseException:
            _LAST_SESSION_INCOMPLETE_REASON = "Session audit failed due to an exception"

    # Step 1b: Independently guarded exit-status mutation
    # Promotes exit code if violations were detected OR if audit failed.
    # Must NEVER allow an exception here to bypass subsequent cleanup!
    exit_status_exc: BaseException | None = None
    if (
        has_violation
        or audit_exc is not None
        or bool(active_ids)
        or bool(stale_sentinels)
        or bool(stale_teardowns)
        or bool(session_entry_failures)
        or bool(uncertain_fixtures)
        or bool(attr_failures)
        or bool(bypassed_fixtures)
    ):
        try:
            if getattr(session, "exitstatus", None) == pytest.ExitCode.OK:
                session.exitstatus = pytest.ExitCode.TESTS_FAILED
        except BaseException as s_exc:
            exit_status_exc = s_exc

    # Step 2: MANDATORY instrumentation cleanup, regardless of audit or exit-status outcome!
    shutdown_exc = None
    try:
        remove_patch()
    except BaseException as exc:
        shutdown_exc = exc

    fixture_shutdown_exc: BaseException | None = None
    try:
        remove_fixture_patch()
    except BaseException as exc:
        fixture_shutdown_exc = exc

    # Step 3: Fallible patch-state verification
    verification_exc = None
    patch_still_active = False
    try:
        patch_still_active = (
            is_patched()
            or (getattr(ThreadPoolExecutor, "submit", None) is _state_guard_submit)
            or is_fixture_patched()
            or (getattr(FixtureDef, "finish", None) is _state_guard_fixture_finish)
        )
    except BaseException as v_exc:
        verification_exc = v_exc
        patch_still_active = True

    # Step 4: Session reference cleanup (attempted even if verification raised)
    ref_cleanup_exc = None
    try:
        # Clear session-scoped attributes attached to fixtures
        known_fixtures_cleanup: set[FixtureDef[Any]] = (
            set(state.fixture_def_registry.keys())
            | set(state.active_fixture_owners.keys())
            | set(state.active_fixture_instances.keys())
            | {f for (f, _) in state.post_finalizers.keys()}
            | set(state.entered_dispatcher_depth.keys())
            | set(state.session_entry_failures)
            | set(state.uncertain_fixtures)
            | set(state.transient_entry_failures)
            | set(getattr(state, "bypassed_fixtures", ()))
        )
        for f in known_fixtures_cleanup:
            if hasattr(f, "_sg_entry_failed_session_id"):
                try:
                    delattr(f, "_sg_entry_failed_session_id")
                except BaseException:
                    pass

        if hasattr(session, "_state_guard_session"):
            try:
                delattr(session, "_state_guard_session")
            except AttributeError:
                pass
        _ACTIVE_SESSION = None
    except BaseException as clean_exc:
        ref_cleanup_exc = clean_exc

    # Step 5: Consolidate cleanup outcome and secondary failures
    cleanup_failures = []
    primary_cleanup_err: BaseException | None = None

    if exit_status_exc is not None:
        cleanup_failures.append(f"Exit status mutation failed: {_safe_exc_str(exit_status_exc)}")
        if primary_cleanup_err is None:
            primary_cleanup_err = exit_status_exc

    if shutdown_exc is not None:
        cleanup_failures.append(
            f"Patch removal failed during session teardown: {_safe_exc_str(shutdown_exc)}"
        )
        if primary_cleanup_err is None:
            primary_cleanup_err = shutdown_exc

    if fixture_shutdown_exc is not None:
        cleanup_failures.append(
            f"Fixture patch removal failed during session teardown: {_safe_exc_str(fixture_shutdown_exc)}"
        )
        if primary_cleanup_err is None:
            primary_cleanup_err = fixture_shutdown_exc
        if primary_cleanup_err is None:
            primary_cleanup_err = shutdown_exc

    if verification_exc is not None:
        cleanup_failures.append(f"Patch verification failed: {_safe_exc_str(verification_exc)}")
        if primary_cleanup_err is None:
            primary_cleanup_err = verification_exc
    elif patch_still_active:
        cleanup_failures.append("Patch still active after removal")
        if primary_cleanup_err is None:
            primary_cleanup_err = RuntimeError("State Guard patch remains active after removal")

    if ref_cleanup_exc is not None:
        cleanup_failures.append(
            f"Session reference cleanup failed: {_safe_exc_str(ref_cleanup_exc)}"
        )
        if primary_cleanup_err is None:
            primary_cleanup_err = ref_cleanup_exc

    # Step 6: Determine lifecycle state and propagate exceptions
    if audit_exc is not None and cleanup_failures:
        # Dual failure: Audit crashed AND secondary cleanup/exit-status failure!
        # Primary exception: audit_exc
        # Secondary failure recorded in _POISON_REASON
        _LIFECYCLE_STATE = PluginLifecycle.POISONED
        _POISON_REASON = (
            f"Session audit failed: {_safe_exc_str(audit_exc)}; additionally, {'; '.join(cleanup_failures)}"
        )
        if hasattr(outcome, "force_exception"):
            outcome.force_exception(audit_exc)
        else:
            raise audit_exc
        return

    if audit_exc is not None:
        # Audit failed, but patch removal, verification, reference cleanup, and exit status succeeded!
        _LIFECYCLE_STATE = PluginLifecycle.POISONED
        _POISON_REASON = f"Session audit failed: {_safe_exc_str(audit_exc)}"
        if hasattr(outcome, "force_exception"):
            outcome.force_exception(audit_exc)
        else:
            raise audit_exc
        return

    if cleanup_failures:
        # Audit succeeded, but cleanup, verification, or exit-status mutation failed!
        _LIFECYCLE_STATE = PluginLifecycle.POISONED
        _POISON_REASON = f"Session teardown cleanup failed: {'; '.join(cleanup_failures)}"
        err = primary_cleanup_err or RuntimeError(
            f"State Guard shutdown verification failed: {_POISON_REASON}"
        )
        if hasattr(outcome, "force_exception"):
            outcome.force_exception(err)
        else:
            raise err
        return

    # All operations (audit, exit-status, unpatch, verification, reference cleanup) succeeded!
    _LIFECYCLE_STATE = PluginLifecycle.IDLE
    _POISON_REASON = None


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_sessionfinish(
    session: pytest.Session, exitstatus: int
) -> Generator[None, Any, None]:
    """Clean up State Guard session, evaluate policy, and remove patch transactionally."""
    # 1. Evaluate whether this invocation should handle session cleanup and audit under lock.
    with _SESSION_LOCK:
        state = getattr(session, "_state_guard_session", None)
        should_handle = (
            _LIFECYCLE_STATE in (PluginLifecycle.ACTIVE, PluginLifecycle.POISONED)
            and state is not None
            and state is _ACTIVE_SESSION
        )

    # 2. Exactly ONE yield point in the entire function, strictly OUTSIDE _SESSION_LOCK.
    #    The inner pytest hook chain (runner fixture teardown, other plugins) executes
    #    without State Guard holding _SESSION_LOCK on ANY control-flow path.
    outcome = yield

    # 3. If this invocation was not active or mismatched, return immediately without touching state.
    if not should_handle or state is None:
        return

    # 4. Post-yield audit and cleanup reacquire _SESSION_LOCK.
    with _SESSION_LOCK:
        _evaluate_and_cleanup_session_finish(session, outcome, state)


def _pytest_sessionfinish_direct(session: pytest.Session, exitstatus: int) -> None:
    """Direct synchronous session cleanup for unit testing harness."""
    with _SESSION_LOCK:
        state = getattr(session, "_state_guard_session", None)
        if (
            _LIFECYCLE_STATE not in (PluginLifecycle.ACTIVE, PluginLifecycle.POISONED)
            or state is None
            or state is not _ACTIVE_SESSION
        ):
            return

        class _DirectOutcome:
            excinfo = None

            def force_exception(self, exc: BaseException) -> None:
                raise exc

        _evaluate_and_cleanup_session_finish(session, _DirectOutcome(), state)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    """Emergency backstop ensuring patch removal if shutdown was interrupted."""
    global _LIFECYCLE_STATE, _POISON_REASON
    with _SESSION_LOCK:
        if is_patched():
            try:
                remove_patch()
            except BaseException as exc:
                _LIFECYCLE_STATE = PluginLifecycle.POISONED
                _POISON_REASON = f"Emergency unconfigure patch removal failed: {exc}"
        if is_fixture_patched():
            try:
                remove_fixture_patch()
            except BaseException as exc:
                _LIFECYCLE_STATE = PluginLifecycle.POISONED
                _POISON_REASON = f"Emergency unconfigure fixture patch removal failed: {exc}"


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

    ident_key = _get_fixture_identity_key(fixturedef, request, state)
    instance_seq = state.execution_counters.get(ident_key, 0) + 1
    state.execution_counters[ident_key] = instance_seq
    state.active_fixture_instances[fixturedef] = instance_seq

    if getattr(fixturedef, "_finalizers", None):
        state.post_finalizers[(fixturedef, instance_seq)] = fixturedef._finalizers[0]

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

    # Step 1: Inspect pending sentinels registered during setup
    pending = [seq for (f, seq) in state.post_finalizers.keys() if f is fixturedef]

    # Step 2: Evaluate detection conditions via structured object identity
    has_unique_sentinel = (len(pending) == 1)
    dispatcher_was_entered = (state.entered_dispatcher_depth.get(fixturedef, 0) > 0)
    entry_failed_structured = (
        fixturedef in state.transient_entry_failures
        or fixturedef in state.uncertain_fixtures
        or (getattr(fixturedef, "_sg_entry_failed_session_id", None) == state.session_id)
    )

    # Step 3: SCEN-10 Detection Predicate
    # SCEN-10 triggers IF AND ONLY IF unique sentinel exists AND dispatcher was NOT entered AND zero entry failure occurred
    if has_unique_sentinel and not dispatcher_was_entered and not entry_failed_structured:
        instance_seq = pending[0]
        marker_key = (fixturedef, instance_seq)

        # Frozen Stage 15A degradation string verbatim
        _safe_record_degradation(
            state,
            "FixtureDef.finish was replaced by a non-delegating plugin; teardown ownership bypassed"
        )
        try:
            state.bypassed_fixtures.add(fixturedef)
        except BaseException:
            pass

        owner_id = state.active_fixture_owners.pop(fixturedef, None)
        if owner_id is not None:
            _safe_finalize_owner(state, owner_id)

        try:
            state.post_finalizers.pop(marker_key, None)
            state.active_fixture_instances.pop(fixturedef, None)
        except BaseException as cleanup_exc:
            _safe_record_degradation(state, f"Failed to prune post-finalizer registry: {_safe_exc_str(cleanup_exc)}")
        return

    if len(pending) > 1:
        # Ambiguous registry state: do NOT classify as SCEN-10; leave setup owner ACTIVE for Mode B audit
        _safe_record_degradation(
            state,
            f"Ambiguous pending fixture instances for '{fixturedef.argname}': {pending}; leaving active owner for session audit"
        )
        return

    # Normal execution, fail-open execution, or entry-registration failure:
    owner_id = state.active_fixture_owners.pop(fixturedef, None)
    if owner_id is not None:
        _safe_finalize_owner(state, owner_id)

    # Authoritative sentinel consumption for unique instrumented instances
    if has_unique_sentinel:
        instance_seq = pending[0]
        marker_key = (fixturedef, instance_seq)
        try:
            state.post_finalizers.pop(marker_key, None)
            state.active_fixture_instances.pop(fixturedef, None)
        except BaseException as prune_exc:
            _safe_record_degradation(state, f"Failed to prune sentinel after finish: {_safe_exc_str(prune_exc)}")


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


# ---------------------------------------------------------------------------
# Reporting Hooks
# ---------------------------------------------------------------------------


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, None]:
    """Inspect teardown phase and attach State Guard violations as teardown errors."""
    outcome = yield
    rep = outcome.get_result()
    if rep.when != "teardown":
        return

    state = _get_verified_session(item.session)
    if state is None:
        return

    engine = PolicyEngine()
    all_owners = state.ledger.get_all_owners()

    # Per-test attribution rule:
    # Match finalized TEST_CALL, finalized function-scoped FIXTURE_SETUP, and FIXTURE_TEARDOWN for this item.nodeid
    item_owners = [
        o for o in all_owners
        if o.nodeid == item.nodeid
        and o.state == OwnerState.FINALIZED
        and o.owner_type in (OwnerType.TEST_CALL, OwnerType.FIXTURE_SETUP, OwnerType.FIXTURE_TEARDOWN)
    ]

    item_violations: list[tuple[OwnerRecord, list[Finding]]] = []
    for owner in item_owners:
        findings = engine.evaluate_owner(owner.owner_id, state.ledger)
        violations = [f for f in findings if f.severity == Severity.VIOLATION]
        if violations:
            item_violations.append((owner, violations))

    if not item_violations:
        return

    # Select primary owner for reporting (TEST_CALL preferred if present)
    primary_owner, primary_findings = item_violations[0]
    for owner, viols in item_violations:
        if owner.owner_type == OwnerType.TEST_CALL:
            primary_owner, primary_findings = owner, viols
            break

    if rep.outcome == "passed":
        rep.outcome = "failed"
        rep.longrepr = StateGuardTerminalRepr(primary_owner, primary_findings)
    else:
        # Teardown already failed due to fixture error; chain without overwriting original
        rep.longrepr = ChainedTerminalRepr(rep.longrepr, primary_owner, primary_findings)


@pytest.hookimpl
def pytest_terminal_summary(
    terminalreporter: Any,
    exitstatus: int | pytest.ExitCode,
    config: pytest.Config,
) -> None:
    """Render authoritative State Guard session audit summary."""
    with _SESSION_LOCK:
        findings = _LAST_SESSION_FINDINGS or {}
        owners = _LAST_SESSION_OWNERS or {}
        incomplete_reason = _LAST_SESSION_INCOMPLETE_REASON
        poison_reason = _POISON_REASON if _LIFECYCLE_STATE == PluginLifecycle.POISONED else None

    render_terminal_summary(
        terminalreporter=terminalreporter,
        findings_by_owner=findings,
        owners_by_id=owners,
        incomplete_reason=incomplete_reason,
        poison_reason=poison_reason,
    )
