from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from pytest_state_guard.ledger import Ledger
from pytest_state_guard.models import CallableState, FutureState
from pytest_state_guard.owners import (
    OwnerContext,
    execution_context,
    get_current_owner,
    get_current_task_id,
)

_PATCH_LOCK = threading.Lock()
_REAL_ORIGINAL_SUBMIT: Callable[..., Any] = ThreadPoolExecutor.submit
_ACTIVE_LEDGER: Ledger | None = None
_IS_PATCHED = False


def _safe_exception_metadata(exc: BaseException) -> tuple[str, str | None]:
    """Safely extract exception type and message without risking user __str__() failures."""
    exc_type = type(exc).__qualname__
    try:
        msg = str(exc)
        exc_msg = msg if type(msg) is str else None
    except BaseException:
        exc_msg = "<exception formatting raised>"
    return exc_type, exc_msg


class _CallableWrapper:
    """Wraps user callable to track execution lifecycle and establish worker context."""

    def __init__(
        self,
        fn: Callable[..., Any],
        task_id: str,
        owner: OwnerContext,
        ledger: Ledger,
    ) -> None:
        self._fn = fn
        self._task_id = task_id
        self._owner = owner
        self._ledger = ledger

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        with execution_context(self._owner, self._task_id):
            try:
                self._ledger.commit_callable_started(self._task_id)
            except Exception:
                # Fail-open: do not prevent user work from executing
                pass

            outcome = CallableState.EXITED_RETURNED
            exc_type: str | None = None
            exc_msg: str | None = None

            try:
                return self._fn(*args, **kwargs)
            except BaseException as exc:
                outcome = CallableState.EXITED_RAISED
                exc_type, exc_msg = _safe_exception_metadata(exc)
                raise
            finally:
                try:
                    self._ledger.commit_callable_exited(
                        self._task_id,
                        outcome=outcome,
                        exception_type=exc_type,
                        exception_message=exc_msg,
                    )
                except Exception:
                    # Do not let instrumentation error mask in-flight user exception
                    pass


class _FutureCallback:
    """Callback attached to Future to observe settlement."""

    def __init__(self, task_id: str, ledger: Ledger) -> None:
        self._task_id = task_id
        self._ledger = ledger

    def __call__(self, future: Future[Any]) -> None:
        try:
            if future.cancelled():
                terminal_state = FutureState.CANCELLED
            else:
                terminal_state = FutureState.FINISHED
            self._ledger.commit_future_done(self._task_id, terminal_state)
        except Exception:
            # Future callback exceptions are isolated; do not affect user Future status
            pass


def _state_guard_submit(
    self: ThreadPoolExecutor,
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Future[Any]:
    """Intercepts ThreadPoolExecutor.submit to track task lifecycles under active owners."""
    ledger = _ACTIVE_LEDGER
    owner = get_current_owner()

    # Unowned submission or inactive ledger: minimal overhead direct pass-through
    if ledger is None or owner is None:
        return _REAL_ORIGINAL_SUBMIT(self, fn, *args, **kwargs)

    # Generate collision-resistant full UUID task ID
    task_id = f"task:{uuid.uuid4().hex}"
    parent_task_id = get_current_task_id()

    # Step 1: Pre-submission reservation
    try:
        ledger.reserve_task(task_id, owner.owner_id, parent_task_id=parent_task_id)
    except Exception:
        # Fail-open: execute uninstrumented rather than breaking user submission
        return _REAL_ORIGINAL_SUBMIT(self, fn, *args, **kwargs)

    # Step 2: Wrap user callable
    wrapped_fn = _CallableWrapper(fn, task_id, owner, ledger)

    # Step 3: Invoke original submit
    try:
        future = _REAL_ORIGINAL_SUBMIT(self, wrapped_fn, *args, **kwargs)
    except BaseException as submit_exc:
        exc_type, exc_msg = _safe_exception_metadata(submit_exc)
        try:
            ledger.commit_submit_raised(
                task_id,
                exception_type=exc_type,
                exception_message=exc_msg,
            )
        except Exception:
            pass
        raise  # Re-raise original exception unchanged

    # Step 4: Record submit returned
    try:
        ledger.commit_submit_returned(task_id)
    except Exception:
        pass

    # Step 5: Attach done callback
    try:
        future.add_done_callback(_FutureCallback(task_id, ledger))
    except Exception:
        pass

    return future


def install_patch(ledger: Ledger) -> None:
    """Install ThreadPoolExecutor.submit monkeypatch.

    Thread-safe and idempotent for the same ledger.
    Raises RuntimeError if already patched with a different ledger.
    """
    global _ACTIVE_LEDGER, _IS_PATCHED

    with _PATCH_LOCK:
        if _IS_PATCHED:
            if _ACTIVE_LEDGER is ledger:
                return
            raise RuntimeError(
                "State Guard ThreadPoolExecutor patch is already active with a different ledger"
            )

        ThreadPoolExecutor.submit = _state_guard_submit  # type: ignore[method-assign]
        _ACTIVE_LEDGER = ledger
        _IS_PATCHED = True


def remove_patch() -> None:
    """Restore original ThreadPoolExecutor.submit.

    Safely handles foreign wrappers: if ThreadPoolExecutor.submit was wrapped
    by another library after State Guard, leaves the pointer untouched and
    disables interception via _ACTIVE_LEDGER = None.
    """
    global _ACTIVE_LEDGER, _IS_PATCHED

    with _PATCH_LOCK:
        if not _IS_PATCHED:
            return

        # Check if our wrapper is still the top-level method
        if getattr(ThreadPoolExecutor, "submit", None) is _state_guard_submit:
            ThreadPoolExecutor.submit = _REAL_ORIGINAL_SUBMIT  # type: ignore[method-assign]

        _ACTIVE_LEDGER = None
        _IS_PATCHED = False


def is_patched() -> bool:
    """Return True if State Guard patch is currently active."""
    with _PATCH_LOCK:
        return _IS_PATCHED


def get_active_ledger() -> Ledger | None:
    """Return the active ledger associated with the current patch, if any."""
    with _PATCH_LOCK:
        return _ACTIVE_LEDGER
