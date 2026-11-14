from __future__ import annotations

import dataclasses
import itertools
import threading
import time
from typing import Any, Mapping

from pytest_state_guard.models import (
    CallableState,
    DuplicateEntityError,
    EntityNotFoundError,
    EventRecord,
    EventType,
    FutureState,
    InvalidTransitionError,
    OwnerRecord,
    OwnerState,
    OwnerType,
    SubmissionState,
    TaskRecord,
)


class Ledger:
    """Thread-safe synchronized event ledger.

    Serializes all lifecycle commits under a single non-reentrant lock.
    Maintains an append-only event history alongside immutable point-in-time
    owner and task records.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq_counter = itertools.count(1)
        self._events: list[EventRecord] = []
        self._owners: dict[str, OwnerRecord] = {}
        self._tasks: dict[str, TaskRecord] = {}

    # -----------------------------------------------------------------------
    # Internal Locked Helpers
    # -----------------------------------------------------------------------

    def _commit_event_locked(
        self,
        event_type: EventType,
        owner_id: str,
        task_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> EventRecord:
        """Allocate seq_id and timestamp, append to history, and return event.

        MUST be invoked while holding self._lock.
        """
        ts_ns = time.monotonic_ns()
        seq_id = next(self._seq_counter)
        thread_id = threading.get_ident()

        event = EventRecord(
            seq_id=seq_id,
            ts_ns=ts_ns,
            event_type=event_type,
            owner_id=owner_id,
            task_id=task_id,
            thread_id=thread_id,
            details=details,
        )
        self._events.append(event)
        return event

    # -----------------------------------------------------------------------
    # Owner Lifecycle Commits
    # -----------------------------------------------------------------------

    def create_owner(
        self,
        owner_id: str,
        owner_type: OwnerType,
        nodeid: str,
        fixture_name: str | None = None,
    ) -> OwnerRecord:
        """Register a new active owner."""
        with self._lock:
            if owner_id in self._owners:
                raise DuplicateEntityError(f"Owner '{owner_id}' already exists")

            ev = self._commit_event_locked(
                event_type=EventType.OWNER_CREATED,
                owner_id=owner_id,
                details={"owner_type": owner_type.value, "nodeid": nodeid},
            )
            owner = OwnerRecord(
                owner_id=owner_id,
                owner_type=owner_type,
                nodeid=nodeid,
                created_seq=ev.seq_id,
                state=OwnerState.ACTIVE,
                fixture_name=fixture_name,
            )
            self._owners[owner_id] = owner
            return owner

    def finalize_owner(self, owner_id: str) -> OwnerRecord:
        """Commit owner finalization (irreversible one-way transition)."""
        with self._lock:
            if owner_id not in self._owners:
                raise EntityNotFoundError(f"Owner '{owner_id}' not found")

            current = self._owners[owner_id]
            if current.state == OwnerState.FINALIZED:
                raise InvalidTransitionError(
                    f"Owner '{owner_id}' is already finalized at seq {current.finalized_seq}"
                )

            ev = self._commit_event_locked(
                event_type=EventType.OWNER_FINALIZED,
                owner_id=owner_id,
            )
            updated = dataclasses.replace(
                current,
                state=OwnerState.FINALIZED,
                finalized_seq=ev.seq_id,
            )
            self._owners[owner_id] = updated
            return updated

    # -----------------------------------------------------------------------
    # Task Lifecycle Commits
    # -----------------------------------------------------------------------

    def reserve_task(
        self,
        task_id: str,
        owner_id: str,
        parent_task_id: str | None = None,
    ) -> TaskRecord:
        """Reserve a task identity under an active owner prior to submit()."""
        with self._lock:
            if task_id in self._tasks:
                raise DuplicateEntityError(f"Task '{task_id}' already exists")
            if owner_id not in self._owners:
                raise EntityNotFoundError(f"Owner '{owner_id}' not found")
            if parent_task_id is not None and parent_task_id not in self._tasks:
                raise EntityNotFoundError(f"Parent task '{parent_task_id}' not found")

            ev = self._commit_event_locked(
                event_type=EventType.TASK_RESERVED,
                owner_id=owner_id,
                task_id=task_id,
                details={"parent_task_id": parent_task_id},
            )
            task = TaskRecord(
                task_id=task_id,
                owner_id=owner_id,
                created_seq=ev.seq_id,
                submission_state=SubmissionState.RESERVED,
                callable_state=CallableState.NOT_STARTED,
                future_state=FutureState.UNKNOWN,
                parent_task_id=parent_task_id,
            )
            self._tasks[task_id] = task
            return task

    def commit_submit_returned(self, task_id: str) -> TaskRecord:
        """Commit successful return of ThreadPoolExecutor.submit()."""
        with self._lock:
            if task_id not in self._tasks:
                raise EntityNotFoundError(f"Task '{task_id}' not found")

            current = self._tasks[task_id]
            if current.submission_state != SubmissionState.RESERVED:
                raise InvalidTransitionError(
                    f"Cannot commit SUBMIT_RETURNED from submission state '{current.submission_state}'"
                )

            ev = self._commit_event_locked(
                event_type=EventType.SUBMIT_RETURNED,
                owner_id=current.owner_id,
                task_id=task_id,
            )
            new_future_state = (
                FutureState.PENDING
                if current.future_state == FutureState.UNKNOWN
                else current.future_state
            )
            updated = dataclasses.replace(
                current,
                submission_state=SubmissionState.SUBMIT_RETURNED,
                future_state=new_future_state,
            )
            self._tasks[task_id] = updated
            return updated

    def commit_submit_raised(
        self,
        task_id: str,
        exception_type: str | None = None,
        exception_message: str | None = None,
    ) -> TaskRecord:
        """Commit that ThreadPoolExecutor.submit() raised an exception.

        Transition is to SUBMIT_RAISED_UNVERIFIED because CPython may have enqueued
        the work item before failing to adjust the worker thread count.
        """
        with self._lock:
            if task_id not in self._tasks:
                raise EntityNotFoundError(f"Task '{task_id}' not found")

            current = self._tasks[task_id]
            if current.submission_state != SubmissionState.RESERVED:
                raise InvalidTransitionError(
                    f"Cannot commit SUBMIT_RAISED from submission state '{current.submission_state}'"
                )

            ev = self._commit_event_locked(
                event_type=EventType.SUBMIT_RAISED,
                owner_id=current.owner_id,
                task_id=task_id,
                details={"exception_type": exception_type},
            )
            updated = dataclasses.replace(
                current,
                submission_state=SubmissionState.SUBMIT_RAISED_UNVERIFIED,
                exception_type=exception_type,
                exception_message=exception_message,
            )
            self._tasks[task_id] = updated
            return updated

    def commit_callable_started(self, task_id: str) -> TaskRecord:
        """Commit that the wrapped callable began execution."""
        with self._lock:
            if task_id not in self._tasks:
                raise EntityNotFoundError(f"Task '{task_id}' not found")

            current = self._tasks[task_id]
            if current.callable_state != CallableState.NOT_STARTED:
                raise InvalidTransitionError(
                    f"Cannot start callable from state '{current.callable_state}'"
                )

            ev = self._commit_event_locked(
                event_type=EventType.CALLABLE_STARTED,
                owner_id=current.owner_id,
                task_id=task_id,
            )
            # If submit raised but callable executes, record anomaly event
            if current.submission_state == SubmissionState.SUBMIT_RAISED_UNVERIFIED:
                self._commit_event_locked(
                    event_type=EventType.EXECUTION_WITHOUT_RETURNED_FUTURE,
                    owner_id=current.owner_id,
                    task_id=task_id,
                )

            updated = dataclasses.replace(
                current,
                callable_state=CallableState.RUNNING,
            )
            self._tasks[task_id] = updated
            return updated

    def commit_callable_exited(
        self,
        task_id: str,
        outcome: CallableState,
        exception_type: str | None = None,
        exception_message: str | None = None,
    ) -> TaskRecord:
        """Commit that the wrapped callable exited (returned or raised)."""
        with self._lock:
            if task_id not in self._tasks:
                raise EntityNotFoundError(f"Task '{task_id}' not found")

            current = self._tasks[task_id]
            if outcome not in (CallableState.EXITED_RETURNED, CallableState.EXITED_RAISED):
                raise InvalidTransitionError(
                    f"Invalid callable exit outcome '{outcome}'; expected EXITED_RETURNED or EXITED_RAISED"
                )
            if current.callable_state != CallableState.RUNNING:
                raise InvalidTransitionError(
                    f"Cannot exit callable from state '{current.callable_state}'"
                )

            ev = self._commit_event_locked(
                event_type=EventType.CALLABLE_EXITED,
                owner_id=current.owner_id,
                task_id=task_id,
                details={"outcome": outcome.value, "exception_type": exception_type},
            )
            updated = dataclasses.replace(
                current,
                callable_state=outcome,
                exception_type=exception_type or current.exception_type,
                exception_message=exception_message or current.exception_message,
            )
            self._tasks[task_id] = updated
            return updated

    def commit_future_done(
        self,
        task_id: str,
        terminal_state: FutureState,
    ) -> TaskRecord:
        """Commit observation of Future done callback."""
        with self._lock:
            if task_id not in self._tasks:
                raise EntityNotFoundError(f"Task '{task_id}' not found")

            current = self._tasks[task_id]
            if terminal_state not in (FutureState.FINISHED, FutureState.CANCELLED):
                raise InvalidTransitionError(
                    f"Terminal future state must be FINISHED or CANCELLED, got '{terminal_state}'"
                )
            if current.future_state in (FutureState.FINISHED, FutureState.CANCELLED):
                raise InvalidTransitionError(
                    f"Task '{task_id}' future is already in terminal state '{current.future_state.value}'; "
                    f"cannot transition to '{terminal_state.value}'"
                )

            ev = self._commit_event_locked(
                event_type=EventType.FUTURE_DONE_CALLBACK_OBSERVED,
                owner_id=current.owner_id,
                task_id=task_id,
                details={"terminal_state": terminal_state.value},
            )
            updated = dataclasses.replace(
                current,
                future_state=terminal_state,
            )
            self._tasks[task_id] = updated
            return updated

    # -----------------------------------------------------------------------
    # Snapshots and Queries
    # -----------------------------------------------------------------------

    def get_owner(self, owner_id: str) -> OwnerRecord:
        """Retrieve an immutable snapshot of an owner record."""
        with self._lock:
            if owner_id not in self._owners:
                raise EntityNotFoundError(f"Owner '{owner_id}' not found")
            return self._owners[owner_id]

    def get_task(self, task_id: str) -> TaskRecord:
        """Retrieve an immutable snapshot of a task record."""
        with self._lock:
            if task_id not in self._tasks:
                raise EntityNotFoundError(f"Task '{task_id}' not found")
            return self._tasks[task_id]

    def get_all_owners(self) -> list[OwnerRecord]:
        """Return a snapshot list of all owner records."""
        with self._lock:
            return list(self._owners.values())

    def get_all_tasks(self) -> list[TaskRecord]:
        """Return a snapshot list of all task records."""
        with self._lock:
            return list(self._tasks.values())

    def get_tasks_for_owner(self, owner_id: str) -> list[TaskRecord]:
        """Return a snapshot list of tasks associated with an owner."""
        with self._lock:
            return [t for t in self._tasks.values() if t.owner_id == owner_id]

    def get_events(self) -> list[EventRecord]:
        """Return an immutable point-in-time snapshot of the event history."""
        with self._lock:
            return list(self._events)

    def get_events_for_owner(self, owner_id: str) -> list[EventRecord]:
        """Return events relevant to a specific owner."""
        with self._lock:
            return [e for e in self._events if e.owner_id == owner_id]

    def get_events_for_task(self, task_id: str) -> list[EventRecord]:
        """Return events relevant to a specific task."""
        with self._lock:
            return [e for e in self._events if e.task_id == task_id]

    def get_owner_snapshot(
        self, owner_id: str
    ) -> tuple[OwnerRecord, list[TaskRecord], list[EventRecord]]:
        """Return a single coherent atomic point-in-time snapshot of an owner, its tasks, and its events.

        All three collections are captured under a single acquisition of the ledger lock,
        guaranteeing that task and event records correspond to the exact same sequence point.
        The returned lists are freshly copied collections containing immutable records.
        """
        with self._lock:
            if owner_id not in self._owners:
                raise EntityNotFoundError(f"Owner '{owner_id}' not found")
            owner = self._owners[owner_id]
            tasks = [t for t in self._tasks.values() if t.owner_id == owner_id]
            events = [e for e in self._events if e.owner_id == owner_id]
            return owner, list(tasks), list(events)

    def get_finalized_session_snapshot(
        self,
    ) -> tuple[list[OwnerRecord], list[TaskRecord], list[EventRecord]]:
        """Return a single coherent atomic point-in-time snapshot of all finalized owners, tasks, and events.

        All records are captured under a single acquisition of the ledger lock,
        guaranteeing process-wide consistency across all owners and tasks at that sequence point.
        The returned lists are freshly copied collections containing immutable records.
        """
        with self._lock:
            finalized_owners = [
                o for o in self._owners.values() if o.state == OwnerState.FINALIZED
            ]
            tasks = list(self._tasks.values())
            events = list(self._events)
            return list(finalized_owners), list(tasks), list(events)
