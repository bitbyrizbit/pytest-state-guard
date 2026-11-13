from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from pytest_state_guard.models import OwnerType


@dataclass(frozen=True)
class OwnerContext:
    """Active owner context carried by submitting or executing thread."""

    owner_id: str
    owner_type: OwnerType
    nodeid: str
    fixture_name: str | None = None


_CURRENT_OWNER: contextvars.ContextVar[OwnerContext | None] = contextvars.ContextVar(
    "state_guard_current_owner", default=None
)

_CURRENT_TASK_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "state_guard_current_task_id", default=None
)


def get_current_owner() -> OwnerContext | None:
    """Return the active owner context, or None if unowned."""
    return _CURRENT_OWNER.get()


def get_current_task_id() -> str | None:
    """Return the currently executing task ID, or None if not inside a tracked task."""
    return _CURRENT_TASK_ID.get()


def set_current_owner(owner: OwnerContext | None) -> contextvars.Token[OwnerContext | None]:
    """Set the active owner context and return the restoration token."""
    return _CURRENT_OWNER.set(owner)


def reset_current_owner(token: contextvars.Token[OwnerContext | None]) -> None:
    """Restore the previous owner context using the provided token."""
    _CURRENT_OWNER.reset(token)


def set_current_task_id(task_id: str | None) -> contextvars.Token[str | None]:
    """Set the executing task ID and return the restoration token."""
    return _CURRENT_TASK_ID.set(task_id)


def reset_current_task_id(token: contextvars.Token[str | None]) -> None:
    """Restore the previous task ID using the provided token."""
    _CURRENT_TASK_ID.reset(token)


@contextmanager
def active_owner(
    owner_id: str,
    owner_type: OwnerType,
    nodeid: str,
    fixture_name: str | None = None,
) -> Iterator[OwnerContext]:
    """Scoped context manager setting the active owner and restoring prior context in finally."""
    owner = OwnerContext(
        owner_id=owner_id,
        owner_type=owner_type,
        nodeid=nodeid,
        fixture_name=fixture_name,
    )
    token = set_current_owner(owner)
    try:
        yield owner
    finally:
        reset_current_owner(token)


@contextmanager
def execution_context(
    owner: OwnerContext | None,
    task_id: str | None,
) -> Iterator[None]:
    """Scoped context manager setting worker execution context and resetting tokens in finally."""
    owner_token = set_current_owner(owner)
    task_token = set_current_task_id(task_id)
    try:
        yield
    finally:
        reset_current_task_id(task_token)
        reset_current_owner(owner_token)
