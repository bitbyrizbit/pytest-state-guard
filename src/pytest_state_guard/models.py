from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


_EXACT_SCALAR_TYPES = (str, int, float, bool, bytes)


def _freeze_key(k: Any) -> str:
    """Validate that detail keys are exact built-in strings."""
    if type(k) is not str:
        raise TypeError(f"Event details keys must be exact str, got '{type(k).__name__}'")
    return k


def _freeze_value(val: Any) -> Any:
    """Recursively freeze container values to immutable equivalents.

    Supported types:
    - None and exact built-in scalars (str, int, float, bool, bytes) returned as-is.
    - Mappings recursively converted to MappingProxyType.
    - Sequences (list, tuple) recursively converted to tuple.
    - Sets (set, frozenset) recursively converted to frozenset.

    Subclasses of built-in scalars and arbitrary Enum instances are rejected
    with TypeError to prevent mutable attributes or values from compromising
    deep immutability.
    """
    if val is None or type(val) in _EXACT_SCALAR_TYPES:
        return val
    elif isinstance(val, Mapping):
        return MappingProxyType({_freeze_key(k): _freeze_value(v) for k, v in val.items()})
    elif isinstance(val, (list, tuple)):
        return tuple(_freeze_value(v) for v in val)
    elif isinstance(val, (set, frozenset)):
        return frozenset(_freeze_value(v) for v in val)
    raise TypeError(
        f"Unsupported value type '{type(val).__name__}' in event details; "
        f"only exact built-in scalars (str, int, float, bool, bytes, NoneType) "
        f"and standard containers (mappings, sequences, sets) are supported"
    )


def _freeze_details(m: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """Recursively freeze an event details mapping to guarantee deep immutability."""
    if m is None:
        return None
    if not isinstance(m, Mapping):
        raise TypeError(f"Event details must be a Mapping, got '{type(m).__name__}'")
    return MappingProxyType({_freeze_key(k): _freeze_value(v) for k, v in m.items()})


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class OwnerType(str, Enum):
    """Supported owner types in v1."""

    FIXTURE_SETUP = "FIXTURE_SETUP"
    TEST_CALL = "TEST_CALL"
    FIXTURE_TEARDOWN = "FIXTURE_TEARDOWN"


class OwnerState(str, Enum):
    """Lifecycle states of a task owner (irreversible transition)."""

    ACTIVE = "ACTIVE"
    FINALIZED = "FINALIZED"


class SubmissionState(str, Enum):
    """State dimension for ThreadPoolExecutor.submit() invocation."""

    RESERVED = "RESERVED"
    SUBMIT_RETURNED = "SUBMIT_RETURNED"
    SUBMIT_RAISED_UNVERIFIED = "SUBMIT_RAISED_UNVERIFIED"


class CallableState(str, Enum):
    """State dimension for wrapped user callable execution."""

    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    EXITED_RETURNED = "EXITED_RETURNED"
    EXITED_RAISED = "EXITED_RAISED"


class FutureState(str, Enum):
    """Observed state dimension of concurrent.futures.Future."""

    UNKNOWN = "UNKNOWN"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"
    CANCELLED = "CANCELLED"


class Severity(str, Enum):
    """Finding severity categories."""

    CLEAN = "CLEAN"
    VIOLATION = "VIOLATION"
    UNCERTAIN = "UNCERTAIN"


class EventType(str, Enum):
    """Committed event types in the append-only ledger."""

    OWNER_CREATED = "OWNER_CREATED"
    OWNER_FINALIZED = "OWNER_FINALIZED"
    TASK_RESERVED = "TASK_RESERVED"
    SUBMIT_RETURNED = "SUBMIT_RETURNED"
    SUBMIT_RAISED = "SUBMIT_RAISED"
    CALLABLE_STARTED = "CALLABLE_STARTED"
    CALLABLE_EXITED = "CALLABLE_EXITED"
    FUTURE_DONE_CALLBACK_OBSERVED = "FUTURE_DONE_CALLBACK_OBSERVED"
    EXECUTION_WITHOUT_RETURNED_FUTURE = "EXECUTION_WITHOUT_RETURNED_FUTURE"


class FindingClassification(str, Enum):
    """Policy classifications established from committed ledger order."""

    NO_OUTSTANDING_TASKS = "NO_OUTSTANDING_TASKS"
    COMPLETED_BEFORE_FINALIZATION = "COMPLETED_BEFORE_FINALIZATION"
    CANCELLED_BEFORE_START = "CANCELLED_BEFORE_START"
    OUTSTANDING_TASK = "OUTSTANDING_TASK"
    CALLABLE_NOT_EXITED = "CALLABLE_NOT_EXITED"
    SUBMISSION_UNRESOLVED = "SUBMISSION_UNRESOLVED"
    SUBMISSION_FAILED_UNVERIFIED = "SUBMISSION_FAILED_UNVERIFIED"
    EXECUTION_AFTER_OWNER_FINALIZATION = "EXECUTION_AFTER_OWNER_FINALIZATION"
    EXIT_COMMITTED_AFTER_FINALIZATION = "EXIT_COMMITTED_AFTER_FINALIZATION"
    POST_LIFETIME_SUBMISSION = "POST_LIFETIME_SUBMISSION"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class StateGuardError(Exception):
    """Base exception for State Guard domain errors."""


class InvalidTransitionError(StateGuardError):
    """Raised when an illegal lifecycle or state transition is requested."""


class EntityNotFoundError(StateGuardError):
    """Raised when an entity ID is not registered in the ledger."""


class DuplicateEntityError(StateGuardError):
    """Raised when registering an entity ID that already exists."""


# ---------------------------------------------------------------------------
# Immutable Domain Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnerRecord:
    """Immutable representation of a task owner."""

    owner_id: str
    owner_type: OwnerType
    nodeid: str
    created_seq: int
    state: OwnerState = OwnerState.ACTIVE
    fixture_name: str | None = None
    finalized_seq: int | None = None


@dataclass(frozen=True)
class TaskRecord:
    """Immutable representation of a tracked task across its three dimensions."""

    task_id: str
    owner_id: str
    created_seq: int
    submission_state: SubmissionState = SubmissionState.RESERVED
    callable_state: CallableState = CallableState.NOT_STARTED
    future_state: FutureState = FutureState.UNKNOWN
    parent_task_id: str | None = None
    exception_type: str | None = None
    exception_message: str | None = None


@dataclass(frozen=True)
class EventRecord:
    """Immutable committed event in the ledger."""

    seq_id: int
    ts_ns: int
    event_type: EventType
    owner_id: str
    task_id: str | None = None
    thread_id: int | None = None
    details: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.details is not None:
            object.__setattr__(self, "details", _freeze_details(self.details))


@dataclass(frozen=True)
class Finding:
    """Diagnostic or violation finding evaluated against committed ledger state."""

    finding_id: str
    owner_id: str
    classification: FindingClassification
    severity: Severity
    evidence_seq_ids: tuple[int, ...]
    message: str
    task_id: str | None = None
