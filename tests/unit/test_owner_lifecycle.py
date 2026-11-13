from __future__ import annotations

import dataclasses
import pytest

from pytest_state_guard.ledger import Ledger
from pytest_state_guard.models import (
    DuplicateEntityError,
    EntityNotFoundError,
    EventType,
    InvalidTransitionError,
    OwnerState,
    OwnerType,
)


class TestOwnerLifecycle:
    """Tests for the frozen Owner lifecycle model: ACTIVE -> FINALIZED (one-way)."""

    def test_owner_creation_in_active_state(self) -> None:
        ledger = Ledger()
        owner = ledger.create_owner(
            owner_id="owner:fix:1",
            owner_type=OwnerType.FIXTURE_SETUP,
            nodeid="tests/test_service.py::test_case",
            fixture_name="db_session",
        )

        assert owner.owner_id == "owner:fix:1"
        assert owner.owner_type == OwnerType.FIXTURE_SETUP
        assert owner.nodeid == "tests/test_service.py::test_case"
        assert owner.fixture_name == "db_session"
        assert owner.state == OwnerState.ACTIVE
        assert owner.created_seq == 1
        assert owner.finalized_seq is None

        # Verify event history
        events = ledger.get_events_for_owner("owner:fix:1")
        assert len(events) == 1
        assert events[0].event_type == EventType.OWNER_CREATED
        assert events[0].seq_id == 1

    def test_valid_one_way_transition_to_finalized(self) -> None:
        ledger = Ledger()
        created = ledger.create_owner("owner:test:1", OwnerType.TEST_CALL, "test.py::test_one")
        assert created.state == OwnerState.ACTIVE

        # Reserve a task to increment sequence ID
        ledger.reserve_task("task:1", "owner:test:1")

        finalized = ledger.finalize_owner("owner:test:1")
        assert finalized.state == OwnerState.FINALIZED
        assert finalized.finalized_seq == 3
        assert finalized.created_seq == 1

        # Snapshot in ledger is updated
        current = ledger.get_owner("owner:test:1")
        assert current.state == OwnerState.FINALIZED
        assert current.finalized_seq == 3

        # Event recorded
        events = ledger.get_events_for_owner("owner:test:1")
        assert len(events) == 3
        assert events[0].event_type == EventType.OWNER_CREATED
        assert events[1].event_type == EventType.TASK_RESERVED
        assert events[2].event_type == EventType.OWNER_FINALIZED
        assert events[2].seq_id == 3

    def test_rejection_of_finalization_after_already_finalized(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_one")
        ledger.finalize_owner("owner:1")

        with pytest.raises(InvalidTransitionError, match="already finalized"):
            ledger.finalize_owner("owner:1")

    def test_immutability_of_previously_committed_owner_records(self) -> None:
        ledger = Ledger()
        initial_record = ledger.create_owner("owner:1", OwnerType.FIXTURE_SETUP, "test.py::test_one")

        # Attempting to mutate record in-place raises FrozenInstanceError
        with pytest.raises(dataclasses.FrozenInstanceError):
            initial_record.state = OwnerState.FINALIZED  # type: ignore[misc]

        finalized_record = ledger.finalize_owner("owner:1")

        # Initial record instance in user's hand remains ACTIVE (pure immutable object)
        assert initial_record.state == OwnerState.ACTIVE
        assert finalized_record.state == OwnerState.FINALIZED
        assert initial_record is not finalized_record

    def test_owner_identity_and_provenance_preservation(self) -> None:
        """Domain provenance testing: linking tasks to distinct owners and parent tasks."""
        ledger = Ledger()
        owner_a = ledger.create_owner("owner:fix:parent", OwnerType.FIXTURE_SETUP, "test.py::test_a", "parent_fix")
        owner_b = ledger.create_owner("owner:test:child", OwnerType.TEST_CALL, "test.py::test_a")

        task_1 = ledger.reserve_task("task:1", "owner:fix:parent")
        task_2 = ledger.reserve_task("task:2", "owner:test:child", parent_task_id="task:1")

        assert task_1.owner_id == "owner:fix:parent"
        assert task_1.parent_task_id is None

        assert task_2.owner_id == "owner:test:child"
        assert task_2.parent_task_id == "task:1"

        # Tasks for each owner are isolated
        tasks_a = ledger.get_tasks_for_owner("owner:fix:parent")
        tasks_b = ledger.get_tasks_for_owner("owner:test:child")

        assert [t.task_id for t in tasks_a] == ["task:1"]
        assert [t.task_id for t in tasks_b] == ["task:2"]

    def test_rejection_of_task_with_nonexistent_parent(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_a")
        with pytest.raises(EntityNotFoundError, match="Parent task 'task:missing' not found"):
            ledger.reserve_task("task:child", "owner:1", parent_task_id="task:missing")
