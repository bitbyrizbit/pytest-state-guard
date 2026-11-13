from __future__ import annotations

import concurrent.futures
import dataclasses
import threading
import pytest

from pytest_state_guard.ledger import Ledger
from pytest_state_guard.models import (
    CallableState,
    DuplicateEntityError,
    EntityNotFoundError,
    EventType,
    FutureState,
    InvalidTransitionError,
    OwnerState,
    OwnerType,
    SubmissionState,
)


class TestLedgerBasics:
    """Basic single-threaded ledger operations and guarantees."""

    def test_strictly_increasing_sequence_ids(self) -> None:
        ledger = Ledger()
        owner = ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test_file.py::test_one")
        assert owner.created_seq == 1

        task = ledger.reserve_task("task:1", "owner:1")
        assert task.created_seq == 2

        task = ledger.commit_submit_returned("task:1")
        events = ledger.get_events()
        assert len(events) == 3
        seqs = [e.seq_id for e in events]
        assert seqs == [1, 2, 3]

    def test_timestamps_captured_inside_lock(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test_file.py::test_one")
        ledger.reserve_task("task:1", "owner:1")
        events = ledger.get_events()
        assert len(events) == 2
        assert events[0].ts_ns > 0
        assert events[1].ts_ns >= events[0].ts_ns

    def test_atomic_event_append_and_state_transition(self) -> None:
        ledger = Ledger()
        owner = ledger.create_owner("owner:1", OwnerType.FIXTURE_SETUP, "test_file.py::test_one")
        task = ledger.reserve_task("task:1", "owner:1")

        events_before = len(ledger.get_events())
        assert task.submission_state == SubmissionState.RESERVED

        updated = ledger.commit_submit_returned("task:1")
        events_after = len(ledger.get_events())

        assert events_after == events_before + 1
        assert updated.submission_state == SubmissionState.SUBMIT_RETURNED
        assert ledger.get_task("task:1").submission_state == SubmissionState.SUBMIT_RETURNED

    def test_rejection_of_duplicate_entities(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_a")
        with pytest.raises(DuplicateEntityError):
            ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_a")

        ledger.reserve_task("task:1", "owner:1")
        with pytest.raises(DuplicateEntityError):
            ledger.reserve_task("task:1", "owner:1")

    def test_rejection_of_missing_entities(self) -> None:
        ledger = Ledger()
        with pytest.raises(EntityNotFoundError):
            ledger.finalize_owner("non_existent_owner")
        with pytest.raises(EntityNotFoundError):
            ledger.reserve_task("task:1", "non_existent_owner")
        with pytest.raises(EntityNotFoundError):
            ledger.commit_submit_returned("non_existent_task")

    def test_no_partial_state_changes_when_transition_fails(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_a")
        task = ledger.reserve_task("task:1", "owner:1")

        events_count_before = len(ledger.get_events())

        # Illegal: cannot commit CALLABLE_EXITED before CALLABLE_STARTED
        with pytest.raises(InvalidTransitionError):
            ledger.commit_callable_exited("task:1", CallableState.EXITED_RETURNED)

        # Invariants: no event appended, no state mutated
        assert len(ledger.get_events()) == events_count_before
        assert ledger.get_task("task:1").callable_state == CallableState.NOT_STARTED

    def test_snapshot_isolation(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_a")
        ledger.reserve_task("task:1", "owner:1")

        # Returned list is a copy
        events_snapshot = ledger.get_events()
        events_snapshot.clear()
        assert len(ledger.get_events()) == 2

        owners_snapshot = ledger.get_all_owners()
        owners_snapshot.clear()
        assert len(ledger.get_all_owners()) == 1

        # Records themselves are frozen dataclasses
        task = ledger.get_task("task:1")
        with pytest.raises(dataclasses.FrozenInstanceError):
            task.submission_state = SubmissionState.SUBMIT_RETURNED  # type: ignore[misc]

    def test_event_record_details_deep_immutability_mappings_and_scalars(self) -> None:
        ledger = Ledger()
        owner = ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_a")

        raw_details = {"param": "alpha", "nested": {"key": 42}}
        ledger.reserve_task("task:1", "owner:1")

        events = ledger.get_events()
        owner_event = events[0]
        assert owner_event.details is not None
        assert owner_event.details["owner_type"] == "TEST_CALL"

        # Attempting to reassign field raises FrozenInstanceError
        with pytest.raises(dataclasses.FrozenInstanceError):
            owner_event.details = {}  # type: ignore[misc]

        # Attempting to mutate mapping directly raises TypeError (MappingProxyType)
        with pytest.raises(TypeError):
            owner_event.details["owner_type"] = "MUTATED"  # type: ignore[index]

        with pytest.raises(AttributeError):
            owner_event.details.clear()  # type: ignore[attr-defined]

        # Test deep immutability with nested dictionary
        from pytest_state_guard.models import EventRecord
        ev = EventRecord(
            seq_id=99,
            ts_ns=1000,
            event_type=EventType.TASK_RESERVED,
            owner_id="owner:1",
            details=raw_details,
        )
        assert ev.details is not None
        assert ev.details["nested"]["key"] == 42

        # Mutating nested mapping raises TypeError
        with pytest.raises(TypeError):
            ev.details["nested"]["key"] = 999  # type: ignore[index]

        # Mutating original dictionary passed in does not affect frozen record
        raw_details["param"] = "mutated_externally"
        raw_details["nested"]["key"] = 999
        assert ev.details["param"] == "alpha"
        assert ev.details["nested"]["key"] == 42

    def test_event_record_details_deep_immutability_nested_lists_and_sequences(self) -> None:
        """Verify that nested lists and sequences are frozen to immutable tuples."""
        from pytest_state_guard.models import EventRecord

        tags = ["fixture", "background-task"]
        attempts = [1, 2, 3]
        records = [{"step": 1}, {"step": 2}]
        raw_details = {
            "tags": tags,
            "metadata": {"attempts": attempts},
            "records": records,
        }

        ev = EventRecord(
            seq_id=1,
            ts_ns=1000,
            event_type=EventType.TASK_RESERVED,
            owner_id="owner:1",
            details=raw_details,
        )
        assert ev.details is not None

        # 1. Lists nested directly inside mappings become immutable tuples
        assert isinstance(ev.details["tags"], tuple)
        assert ev.details["tags"] == ("fixture", "background-task")

        with pytest.raises(AttributeError):
            ev.details["tags"].append("mutated")  # type: ignore[attr-defined]

        with pytest.raises(TypeError):
            ev.details["tags"][0] = "mutated"  # type: ignore[index]

        # 2. Lists nested inside nested mappings become immutable tuples
        assert isinstance(ev.details["metadata"]["attempts"], tuple)
        assert ev.details["metadata"]["attempts"] == (1, 2, 3)

        with pytest.raises(AttributeError):
            ev.details["metadata"]["attempts"].append(4)  # type: ignore[attr-defined]

        with pytest.raises(TypeError):
            ev.details["metadata"]["attempts"][0] = 99  # type: ignore[index]

        # 3. Lists of mappings become tuples of MappingProxyType
        assert isinstance(ev.details["records"], tuple)
        assert ev.details["records"][0]["step"] == 1

        with pytest.raises(AttributeError):
            ev.details["records"].append({"step": 3})  # type: ignore[attr-defined]

        with pytest.raises(TypeError):
            ev.details["records"][0]["step"] = 99  # type: ignore[index]

        # 4. Mutation of original inputs after event construction does not affect committed details
        tags.append("mutated_after")
        attempts.append(4)
        records[0]["step"] = 99
        raw_details["metadata"]["new_key"] = "leaked"  # type: ignore[index]

        assert ev.details["tags"] == ("fixture", "background-task")
        assert ev.details["metadata"]["attempts"] == (1, 2, 3)
        assert ev.details["records"][0]["step"] == 1
        assert "new_key" not in ev.details["metadata"]

    def test_event_record_details_deep_immutability_sets(self) -> None:
        """Verify that nested sets are frozen to immutable frozensets."""
        from pytest_state_guard.models import EventRecord

        categories = {"alpha", "beta"}
        raw_details = {"categories": categories}

        ev = EventRecord(
            seq_id=1,
            ts_ns=1000,
            event_type=EventType.TASK_RESERVED,
            owner_id="owner:1",
            details=raw_details,
        )
        assert ev.details is not None
        assert isinstance(ev.details["categories"], frozenset)
        assert ev.details["categories"] == frozenset({"alpha", "beta"})

        with pytest.raises(AttributeError):
            ev.details["categories"].add("gamma")  # type: ignore[attr-defined]

        # Modifying original input set does not affect committed record
        categories.add("gamma")
        assert ev.details["categories"] == frozenset({"alpha", "beta"})

    def test_event_record_details_rejection_of_unsupported_mutable_types(self) -> None:
        """Verify that arbitrary mutable objects and non-string keys are explicitly rejected."""
        from pytest_state_guard.models import EventRecord

        class CustomMutablePayload:
            def __init__(self) -> None:
                self.val = "mutable"

        # Arbitrary mutable object rejected
        with pytest.raises(TypeError, match="Unsupported value type 'CustomMutablePayload'"):
            EventRecord(
                seq_id=1,
                ts_ns=1000,
                event_type=EventType.TASK_RESERVED,
                owner_id="owner:1",
                details={"custom": CustomMutablePayload()},
            )

        # Non-string key rejected
        with pytest.raises(TypeError, match="Event details keys must be exact str"):
            EventRecord(
                seq_id=1,
                ts_ns=1000,
                event_type=EventType.TASK_RESERVED,
                owner_id="owner:1",
                details={123: "value"},  # type: ignore[dict-item]
            )

    def test_event_record_details_rejection_of_enums(self) -> None:
        """Verify that Enum instances cannot enter event details to bypass immutability."""
        from enum import Enum
        from pytest_state_guard.models import EventRecord

        class MutableEnum(Enum):
            TAGS = ["fixture", "background-task"]

        class ScalarEnum(Enum):
            STATUS = "active"

        # Enum with mutable list value rejected
        with pytest.raises(TypeError, match="Unsupported value type 'MutableEnum'"):
            EventRecord(
                seq_id=1,
                ts_ns=1000,
                event_type=EventType.TASK_RESERVED,
                owner_id="owner:1",
                details={"meta": MutableEnum.TAGS},
            )

        # Even scalar Enum instances must be passed as their primitive value (e.g. enum.value)
        with pytest.raises(TypeError, match="Unsupported value type 'ScalarEnum'"):
            EventRecord(
                seq_id=1,
                ts_ns=1000,
                event_type=EventType.TASK_RESERVED,
                owner_id="owner:1",
                details={"status": ScalarEnum.STATUS},
            )

    def test_event_record_details_rejection_of_scalar_subclasses(self) -> None:
        """Verify that subclasses of built-in scalar types cannot bypass immutability."""
        from pytest_state_guard.models import EventRecord

        class CustomStr(str):
            def __init__(self, s: str) -> None:
                self.extra = ["mutable", "attribute"]

        class CustomInt(int):
            def __init__(self, i: int) -> None:
                self.extra = ["mutable", "attribute"]

        class CustomKey(str):
            pass

        # Custom subclass of str rejected
        with pytest.raises(TypeError, match="Unsupported value type 'CustomStr'"):
            EventRecord(
                seq_id=1,
                ts_ns=1000,
                event_type=EventType.TASK_RESERVED,
                owner_id="owner:1",
                details={"label": CustomStr("payload")},
            )

        # Custom subclass of int rejected
        with pytest.raises(TypeError, match="Unsupported value type 'CustomInt'"):
            EventRecord(
                seq_id=1,
                ts_ns=1000,
                event_type=EventType.TASK_RESERVED,
                owner_id="owner:1",
                details={"count": CustomInt(42)},
            )

        # Custom subclass of str as key rejected
        with pytest.raises(TypeError, match="Event details keys must be exact str"):
            EventRecord(
                seq_id=1,
                ts_ns=1000,
                event_type=EventType.TASK_RESERVED,
                owner_id="owner:1",
                details={CustomKey("label"): "value"},  # type: ignore[dict-item]
            )


class TestLedgerConcurrency:
    """Multi-threaded stress and ordering tests using synchronization primitives."""

    def test_concurrent_event_commits_unique_and_monotonic_seq_ids(self) -> None:
        ledger = Ledger()
        owner = ledger.create_owner("owner:main", OwnerType.TEST_CALL, "test_file.py::test_concurrent")

        num_threads = 16
        tasks_per_thread = 25
        total_tasks = num_threads * tasks_per_thread

        barrier = threading.Barrier(num_threads)

        def worker_routine(thread_idx: int) -> None:
            barrier.wait()  # Align thread start precisely
            for i in range(tasks_per_thread):
                task_id = f"t:{thread_idx}:{i}"
                ledger.reserve_task(task_id, "owner:main")
                ledger.commit_submit_returned(task_id)
                ledger.commit_callable_started(task_id)
                ledger.commit_callable_exited(task_id, CallableState.EXITED_RETURNED)
                ledger.commit_future_done(task_id, FutureState.FINISHED)

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker_routine, idx) for idx in range(num_threads)]
            for fut in futures:
                fut.result()

        events = ledger.get_events()
        # 1 OWNER_CREATED + total_tasks * 5 events per task
        expected_events = 1 + total_tasks * 5
        assert len(events) == expected_events

        # Verify all sequence IDs are strictly increasing by 1 and completely unique
        seq_ids = [e.seq_id for e in events]
        assert seq_ids == list(range(1, expected_events + 1))
        assert len(set(seq_ids)) == expected_events

        # Verify all tasks ended in consistent states
        all_tasks = ledger.get_all_tasks()
        assert len(all_tasks) == total_tasks
        for t in all_tasks:
            assert t.submission_state == SubmissionState.SUBMIT_RETURNED
            assert t.callable_state == CallableState.EXITED_RETURNED
            assert t.future_state == FutureState.FINISHED

    def test_concurrent_reads_and_writes_snapshot_consistency(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:main", OwnerType.TEST_CALL, "test.py::test_rw")

        total_ops = 100
        stop_event = threading.Event()
        read_errors: list[Exception] = []

        def writer_loop(thread_id: int) -> None:
            for counter in range(total_ops):
                tid = f"task_{thread_id}_{counter}"
                ledger.reserve_task(tid, "owner:main")
                ledger.commit_submit_returned(tid)

        def reader_loop() -> None:
            while not stop_event.is_set():
                try:
                    events = ledger.get_events()
                    tasks = ledger.get_all_tasks()
                    for i in range(len(events) - 1):
                        assert events[i].seq_id < events[i + 1].seq_id
                    for t in tasks:
                        assert t.owner_id == "owner:main"
                except Exception as e:
                    read_errors.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            r_futs = [executor.submit(reader_loop) for _ in range(3)]
            w_futs = [executor.submit(writer_loop, idx) for idx in range(3)]

            for wf in w_futs:
                wf.result()

            stop_event.set()
            for rf in r_futs:
                rf.result()

        assert not read_errors
