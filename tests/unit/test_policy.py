from __future__ import annotations

import threading
import pytest

from pytest_state_guard.ledger import Ledger
from pytest_state_guard.models import (
    CallableState,
    EntityNotFoundError,
    EventRecord,
    EventType,
    FindingClassification,
    FutureState,
    OwnerRecord,
    OwnerState,
    OwnerType,
    Severity,
    StateGuardError,
    TaskRecord,
)
from pytest_state_guard.policy import PolicyEngine


class TestPolicyClassificationMatrix:
    """Verifies that every frozen classification and severity is correctly produced."""

    def test_no_outstanding_tasks(self) -> None:
        ledger = Ledger()
        owner = ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_empty")
        finalized = ledger.finalize_owner("owner:1")

        policy = PolicyEngine()
        findings = policy.evaluate_owner("owner:1", ledger)

        assert len(findings) == 1
        f = findings[0]
        assert f.finding_id == "finding:owner:1:owner"
        assert f.owner_id == "owner:1"
        assert f.task_id is None
        assert f.classification == FindingClassification.NO_OUTSTANDING_TASKS
        assert f.severity == Severity.CLEAN
        assert f.evidence_seq_ids == (owner.created_seq, finalized.finalized_seq)

    def test_completed_before_finalization(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_clean")
        ledger.reserve_task("task:1", "owner:1")
        ledger.commit_submit_returned("task:1")
        ledger.commit_callable_started("task:1")
        ledger.commit_callable_exited("task:1", CallableState.EXITED_RETURNED)
        finalized = ledger.finalize_owner("owner:1")

        policy = PolicyEngine()
        findings = policy.evaluate_owner("owner:1", ledger)

        assert len(findings) == 1
        f = findings[0]
        assert f.finding_id == "finding:owner:1:task:1"
        assert f.classification == FindingClassification.COMPLETED_BEFORE_FINALIZATION
        assert f.severity == Severity.CLEAN
        assert f.evidence_seq_ids == (2, 4, 5, finalized.finalized_seq)

    def test_completed_before_finalization_with_raised_callable(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_clean_raise")
        ledger.reserve_task("task:1", "owner:1")
        ledger.commit_submit_returned("task:1")
        ledger.commit_callable_started("task:1")
        ledger.commit_callable_exited(
            "task:1",
            CallableState.EXITED_RAISED,
            exception_type="ValueError",
            exception_message="error in task",
        )
        finalized = ledger.finalize_owner("owner:1")

        policy = PolicyEngine()
        findings = policy.evaluate_owner("owner:1", ledger)

        assert len(findings) == 1
        f = findings[0]
        assert f.classification == FindingClassification.COMPLETED_BEFORE_FINALIZATION
        assert f.severity == Severity.CLEAN

    def test_cancelled_before_start(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_cancel")
        ledger.reserve_task("task:1", "owner:1")
        ledger.commit_submit_returned("task:1")
        ledger.commit_future_done("task:1", FutureState.CANCELLED)
        finalized = ledger.finalize_owner("owner:1")

        policy = PolicyEngine()
        findings = policy.evaluate_owner("owner:1", ledger)

        assert len(findings) == 1
        f = findings[0]
        assert f.finding_id == "finding:owner:1:task:1"
        assert f.classification == FindingClassification.CANCELLED_BEFORE_START
        assert f.severity == Severity.CLEAN
        assert f.evidence_seq_ids == (2, 4, finalized.finalized_seq)

    def test_outstanding_task_pending(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_outstanding")
        ledger.reserve_task("task:1", "owner:1")
        ledger.commit_submit_returned("task:1")
        finalized = ledger.finalize_owner("owner:1")

        policy = PolicyEngine()
        findings = policy.evaluate_owner("owner:1", ledger)

        assert len(findings) == 1
        f = findings[0]
        assert f.finding_id == "finding:owner:1:task:1"
        assert f.classification == FindingClassification.OUTSTANDING_TASK
        assert f.severity == Severity.VIOLATION
        assert f.evidence_seq_ids == (2, 3, finalized.finalized_seq)

    def test_callable_not_exited(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_running")
        ledger.reserve_task("task:1", "owner:1")
        ledger.commit_submit_returned("task:1")
        ledger.commit_callable_started("task:1")
        finalized = ledger.finalize_owner("owner:1")

        policy = PolicyEngine()
        findings = policy.evaluate_owner("owner:1", ledger)

        assert len(findings) == 1
        f = findings[0]
        assert f.finding_id == "finding:owner:1:task:1"
        assert f.classification == FindingClassification.CALLABLE_NOT_EXITED
        assert f.severity == Severity.VIOLATION
        assert f.evidence_seq_ids == (2, 4, finalized.finalized_seq)

    def test_execution_after_owner_finalization(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_late_start")
        ledger.reserve_task("task:1", "owner:1")
        ledger.commit_submit_returned("task:1")
        finalized = ledger.finalize_owner("owner:1")

        # Worker starts after finalization
        ledger.commit_callable_started("task:1")

        policy = PolicyEngine()
        findings = policy.evaluate_owner("owner:1", ledger)

        assert len(findings) == 1
        f = findings[0]
        assert f.finding_id == "finding:owner:1:task:1"
        assert f.classification == FindingClassification.EXECUTION_AFTER_OWNER_FINALIZATION
        assert f.severity == Severity.VIOLATION
        assert f.evidence_seq_ids == (2, finalized.finalized_seq, 5)

    def test_exit_committed_after_finalization(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_late_exit")
        ledger.reserve_task("task:1", "owner:1")
        ledger.commit_submit_returned("task:1")
        ledger.commit_callable_started("task:1")
        finalized = ledger.finalize_owner("owner:1")

        # Worker exits after finalization
        ledger.commit_callable_exited("task:1", CallableState.EXITED_RETURNED)

        policy = PolicyEngine()
        findings = policy.evaluate_owner("owner:1", ledger)

        assert len(findings) == 1
        f = findings[0]
        assert f.finding_id == "finding:owner:1:task:1"
        assert f.classification == FindingClassification.EXIT_COMMITTED_AFTER_FINALIZATION
        assert f.severity == Severity.VIOLATION
        assert f.evidence_seq_ids == (2, 4, finalized.finalized_seq, 6)

    def test_post_lifetime_submission(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_post_sub")
        finalized = ledger.finalize_owner("owner:1")

        # Task submitted after finalization
        ledger.reserve_task("task:1", "owner:1")

        policy = PolicyEngine()
        findings = policy.evaluate_owner("owner:1", ledger)

        # Must produce BOTH owner-level NO_OUTSTANDING_TASKS and task-level POST_LIFETIME_SUBMISSION
        assert len(findings) == 2

        task_f = next(f for f in findings if f.task_id == "task:1")
        assert task_f.finding_id == "finding:owner:1:task:1"
        assert task_f.classification == FindingClassification.POST_LIFETIME_SUBMISSION
        assert task_f.severity == Severity.VIOLATION
        assert task_f.evidence_seq_ids == (finalized.finalized_seq, 3)

        owner_f = next(f for f in findings if f.task_id is None)
        assert owner_f.finding_id == "finding:owner:1:owner"
        assert owner_f.classification == FindingClassification.NO_OUTSTANDING_TASKS
        assert owner_f.severity == Severity.CLEAN

    def test_submission_unresolved(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_unresolved")
        ledger.reserve_task("task:1", "owner:1")
        finalized = ledger.finalize_owner("owner:1")

        policy = PolicyEngine()
        findings = policy.evaluate_owner("owner:1", ledger)

        assert len(findings) == 1
        f = findings[0]
        assert f.finding_id == "finding:owner:1:task:1"
        assert f.classification == FindingClassification.SUBMISSION_UNRESOLVED
        assert f.severity == Severity.UNCERTAIN
        assert f.evidence_seq_ids == (2, finalized.finalized_seq)

    def test_submission_failed_unverified(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_submit_raise")
        ledger.reserve_task("task:1", "owner:1")
        ledger.commit_submit_raised("task:1", "RuntimeError", "worker start failed")
        finalized = ledger.finalize_owner("owner:1")

        policy = PolicyEngine()
        findings = policy.evaluate_owner("owner:1", ledger)

        assert len(findings) == 1
        f = findings[0]
        assert f.finding_id == "finding:owner:1:task:1"
        assert f.classification == FindingClassification.SUBMISSION_FAILED_UNVERIFIED
        assert f.severity == Severity.UNCERTAIN
        assert f.evidence_seq_ids == (2, 3, finalized.finalized_seq)

    def test_late_submit_returned_remains_unresolved(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_late_sub_ret")
        ledger.reserve_task("task:1", "owner:1")
        finalized = ledger.finalize_owner("owner:1")

        # Submit returns after finalization, callable never runs
        ledger.commit_submit_returned("task:1")

        policy = PolicyEngine()
        findings = policy.evaluate_owner("owner:1", ledger)

        assert len(findings) == 1
        f = findings[0]
        assert f.finding_id == "finding:owner:1:task:1"
        assert f.classification == FindingClassification.SUBMISSION_UNRESOLVED
        assert f.severity == Severity.UNCERTAIN
        assert f.evidence_seq_ids == (2, finalized.finalized_seq, 4)

    def test_late_submit_raised_remains_unresolved(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_late_sub_raise")
        ledger.reserve_task("task:1", "owner:1")
        finalized = ledger.finalize_owner("owner:1")

        # Submit raises after finalization, callable never runs
        ledger.commit_submit_raised("task:1", "RuntimeError", "worker failed late")

        policy = PolicyEngine()
        findings = policy.evaluate_owner("owner:1", ledger)

        assert len(findings) == 1
        f = findings[0]
        assert f.finding_id == "finding:owner:1:task:1"
        assert f.classification == FindingClassification.SUBMISSION_UNRESOLVED
        assert f.severity == Severity.UNCERTAIN
        assert f.evidence_seq_ids == (2, finalized.finalized_seq, 4)


class TestPolicyClassificationEvolution:
    """Verifies that findings preserve stable identity across event accumulation."""

    def test_callable_not_exited_evolves_to_exit_committed_after_finalization(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_evolve_exit")
        ledger.reserve_task("task:1", "owner:1")
        ledger.commit_submit_returned("task:1")
        ledger.commit_callable_started("task:1")
        ledger.finalize_owner("owner:1")

        policy = PolicyEngine()

        # Initial evaluation: worker running
        f1 = policy.evaluate_owner("owner:1", ledger)[0]
        assert f1.finding_id == "finding:owner:1:task:1"
        assert f1.classification == FindingClassification.CALLABLE_NOT_EXITED
        assert f1.severity == Severity.VIOLATION
        assert f1.evidence_seq_ids == (2, 4, 5)

        # Worker exits later
        ledger.commit_callable_exited("task:1", CallableState.EXITED_RETURNED)

        # Re-evaluation: evolves classification under same finding_id
        f2 = policy.evaluate_owner("owner:1", ledger)[0]
        assert f2.finding_id == "finding:owner:1:task:1"
        assert f2.classification == FindingClassification.EXIT_COMMITTED_AFTER_FINALIZATION
        assert f2.severity == Severity.VIOLATION
        assert f2.evidence_seq_ids == (2, 4, 5, 6)

    def test_outstanding_task_evolves_to_execution_after_finalization(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_evolve_start")
        ledger.reserve_task("task:1", "owner:1")
        ledger.commit_submit_returned("task:1")
        ledger.finalize_owner("owner:1")

        policy = PolicyEngine()

        # Initial evaluation: task pending
        f1 = policy.evaluate_owner("owner:1", ledger)[0]
        assert f1.finding_id == "finding:owner:1:task:1"
        assert f1.classification == FindingClassification.OUTSTANDING_TASK
        assert f1.severity == Severity.VIOLATION
        assert f1.evidence_seq_ids == (2, 3, 4)

        # Worker starts later
        ledger.commit_callable_started("task:1")

        # Re-evaluation: evolves classification under same finding_id
        f2 = policy.evaluate_owner("owner:1", ledger)[0]
        assert f2.finding_id == "finding:owner:1:task:1"
        assert f2.classification == FindingClassification.EXECUTION_AFTER_OWNER_FINALIZATION
        assert f2.severity == Severity.VIOLATION
        assert f2.evidence_seq_ids == (2, 4, 5)

    def test_cancellation_after_finalization_remains_outstanding_task(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_late_cancel")
        ledger.reserve_task("task:1", "owner:1")
        ledger.commit_submit_returned("task:1")
        finalized = ledger.finalize_owner("owner:1")

        # Future cancel occurs post-finalization
        ledger.commit_future_done("task:1", FutureState.CANCELLED)

        policy = PolicyEngine()
        findings = policy.evaluate_owner("owner:1", ledger)

        # Must remain a violation (at finalization, it was uncancelled and outstanding)
        assert len(findings) == 1
        f = findings[0]
        assert f.classification == FindingClassification.OUTSTANDING_TASK
        assert f.severity == Severity.VIOLATION

    def test_late_submit_then_callable_start_evolves_to_execution_after_finalization(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_late_sub_start")
        ledger.reserve_task("task:1", "owner:1")
        finalized = ledger.finalize_owner("owner:1")

        policy = PolicyEngine()

        # Snapshot 1: Submit unresolved at finalization
        f1 = policy.evaluate_owner("owner:1", ledger)[0]
        assert f1.finding_id == "finding:owner:1:task:1"
        assert f1.classification == FindingClassification.SUBMISSION_UNRESOLVED
        assert f1.severity == Severity.UNCERTAIN

        # Submit returns late (post-finalization)
        ledger.commit_submit_returned("task:1")

        # Snapshot 2: Still SUBMISSION_UNRESOLVED under same finding_id
        f2 = policy.evaluate_owner("owner:1", ledger)[0]
        assert f2.finding_id == "finding:owner:1:task:1"
        assert f2.classification == FindingClassification.SUBMISSION_UNRESOLVED
        assert f2.severity == Severity.UNCERTAIN
        assert f2.evidence_seq_ids == (2, finalized.finalized_seq, 4)

        # Callable starts post-finalization
        ledger.commit_callable_started("task:1")

        # Snapshot 3: Evolves to EXECUTION_AFTER_OWNER_FINALIZATION under same finding_id
        f3 = policy.evaluate_owner("owner:1", ledger)[0]
        assert f3.finding_id == "finding:owner:1:task:1"
        assert f3.classification == FindingClassification.EXECUTION_AFTER_OWNER_FINALIZATION
        assert f3.severity == Severity.VIOLATION
        assert f3.evidence_seq_ids == (2, finalized.finalized_seq, 5)


class TestPolicyValidationRules:
    """Verifies that malformed or contradictory event streams trigger StateGuardError (Rules V1-V8)."""

    def test_v1_non_monotonic_sequence_raises(self) -> None:
        policy = PolicyEngine()
        owner = OwnerRecord(
            owner_id="o1",
            owner_type=OwnerType.TEST_CALL,
            nodeid="test.py",
            created_seq=1,
            state=OwnerState.FINALIZED,
            finalized_seq=10,
        )
        task = TaskRecord(task_id="t1", owner_id="o1", created_seq=2)
        # Non-monotonic sequence IDs: 5 then 3
        events = [
            EventRecord(seq_id=5, ts_ns=100, event_type=EventType.TASK_RESERVED, owner_id="o1", task_id="t1"),
            EventRecord(seq_id=3, ts_ns=200, event_type=EventType.SUBMIT_RETURNED, owner_id="o1", task_id="t1"),
        ]

        with pytest.raises(StateGuardError, match="Corrupted sequence ordering"):
            policy.evaluate_task(task, owner, events)

    def test_v2_owner_attribution_mismatch_raises(self) -> None:
        policy = PolicyEngine()
        owner = OwnerRecord(
            owner_id="o1",
            owner_type=OwnerType.TEST_CALL,
            nodeid="test.py",
            created_seq=1,
            state=OwnerState.FINALIZED,
            finalized_seq=10,
        )
        task = TaskRecord(task_id="t1", owner_id="o1", created_seq=2)
        # Event owner_id is o2 instead of o1
        events = [
            EventRecord(seq_id=2, ts_ns=100, event_type=EventType.TASK_RESERVED, owner_id="o1", task_id="t1"),
            EventRecord(seq_id=3, ts_ns=200, event_type=EventType.SUBMIT_RETURNED, owner_id="o2", task_id="t1"),
        ]

        with pytest.raises(StateGuardError, match="Owner attribution mismatch"):
            policy.evaluate_task(task, owner, events)

    def test_v3_conflicting_submission_events_raises(self) -> None:
        policy = PolicyEngine()
        owner = OwnerRecord(
            owner_id="o1",
            owner_type=OwnerType.TEST_CALL,
            nodeid="test.py",
            created_seq=1,
            state=OwnerState.FINALIZED,
            finalized_seq=10,
        )
        task = TaskRecord(task_id="t1", owner_id="o1", created_seq=2)
        events = [
            EventRecord(seq_id=2, ts_ns=100, event_type=EventType.TASK_RESERVED, owner_id="o1", task_id="t1"),
            EventRecord(seq_id=3, ts_ns=200, event_type=EventType.SUBMIT_RETURNED, owner_id="o1", task_id="t1"),
            EventRecord(seq_id=4, ts_ns=300, event_type=EventType.SUBMIT_RAISED, owner_id="o1", task_id="t1"),
        ]

        with pytest.raises(StateGuardError, match="Contradictory submission state"):
            policy.evaluate_task(task, owner, events)

    def test_v4_duplicate_submission_event_raises(self) -> None:
        policy = PolicyEngine()
        owner = OwnerRecord(
            owner_id="o1",
            owner_type=OwnerType.TEST_CALL,
            nodeid="test.py",
            created_seq=1,
            state=OwnerState.FINALIZED,
            finalized_seq=10,
        )
        task = TaskRecord(task_id="t1", owner_id="o1", created_seq=2)
        events = [
            EventRecord(seq_id=2, ts_ns=100, event_type=EventType.TASK_RESERVED, owner_id="o1", task_id="t1"),
            EventRecord(seq_id=3, ts_ns=200, event_type=EventType.TASK_RESERVED, owner_id="o1", task_id="t1"),
        ]

        with pytest.raises(StateGuardError, match="Duplicate TASK_RESERVED"):
            policy.evaluate_task(task, owner, events)

    def test_v5_callable_exit_without_start_raises(self) -> None:
        policy = PolicyEngine()
        owner = OwnerRecord(
            owner_id="o1",
            owner_type=OwnerType.TEST_CALL,
            nodeid="test.py",
            created_seq=1,
            state=OwnerState.FINALIZED,
            finalized_seq=10,
        )
        task = TaskRecord(task_id="t1", owner_id="o1", created_seq=2)
        events = [
            EventRecord(seq_id=2, ts_ns=100, event_type=EventType.TASK_RESERVED, owner_id="o1", task_id="t1"),
            EventRecord(seq_id=3, ts_ns=200, event_type=EventType.SUBMIT_RETURNED, owner_id="o1", task_id="t1"),
            EventRecord(seq_id=4, ts_ns=300, event_type=EventType.CALLABLE_EXITED, owner_id="o1", task_id="t1"),
        ]

        with pytest.raises(StateGuardError, match="without preceding CALLABLE_STARTED"):
            policy.evaluate_task(task, owner, events)

    def test_v6_multiple_callable_starts_raises(self) -> None:
        policy = PolicyEngine()
        owner = OwnerRecord(
            owner_id="o1",
            owner_type=OwnerType.TEST_CALL,
            nodeid="test.py",
            created_seq=1,
            state=OwnerState.FINALIZED,
            finalized_seq=10,
        )
        task = TaskRecord(task_id="t1", owner_id="o1", created_seq=2)
        events = [
            EventRecord(seq_id=2, ts_ns=100, event_type=EventType.TASK_RESERVED, owner_id="o1", task_id="t1"),
            EventRecord(seq_id=3, ts_ns=200, event_type=EventType.SUBMIT_RETURNED, owner_id="o1", task_id="t1"),
            EventRecord(seq_id=4, ts_ns=300, event_type=EventType.CALLABLE_STARTED, owner_id="o1", task_id="t1"),
            EventRecord(seq_id=5, ts_ns=400, event_type=EventType.CALLABLE_STARTED, owner_id="o1", task_id="t1"),
        ]

        with pytest.raises(StateGuardError, match="Duplicate CALLABLE_STARTED"):
            policy.evaluate_task(task, owner, events)

    def test_v7_cancellation_contradicted_by_execution_raises(self) -> None:
        """Dedicated negative test: Cancellation observed at seq 4, callable started at seq 6."""
        policy = PolicyEngine()
        owner = OwnerRecord(
            owner_id="o1",
            owner_type=OwnerType.TEST_CALL,
            nodeid="test.py",
            created_seq=1,
            state=OwnerState.FINALIZED,
            finalized_seq=5,
        )
        task = TaskRecord(task_id="t1", owner_id="o1", created_seq=2)
        events = [
            EventRecord(seq_id=2, ts_ns=100, event_type=EventType.TASK_RESERVED, owner_id="o1", task_id="t1"),
            EventRecord(seq_id=3, ts_ns=200, event_type=EventType.SUBMIT_RETURNED, owner_id="o1", task_id="t1"),
            EventRecord(
                seq_id=4,
                ts_ns=300,
                event_type=EventType.FUTURE_DONE_CALLBACK_OBSERVED,
                owner_id="o1",
                task_id="t1",
                details={"terminal_state": "CANCELLED"},
            ),
            EventRecord(seq_id=6, ts_ns=500, event_type=EventType.CALLABLE_STARTED, owner_id="o1", task_id="t1"),
        ]

        with pytest.raises(
            StateGuardError,
            match="Contradictory evidence for task 't1': task records both Future cancellation.*callable execution start",
        ):
            policy.evaluate_task(task, owner, events)

    def test_v8_event_preceding_reservation_raises(self) -> None:
        policy = PolicyEngine()
        owner = OwnerRecord(
            owner_id="o1",
            owner_type=OwnerType.TEST_CALL,
            nodeid="test.py",
            created_seq=1,
            state=OwnerState.FINALIZED,
            finalized_seq=10,
        )
        task = TaskRecord(task_id="t1", owner_id="o1", created_seq=3)
        # Event at seq 2 precedes reservation at seq 3
        events = [
            EventRecord(seq_id=2, ts_ns=100, event_type=EventType.SUBMIT_RETURNED, owner_id="o1", task_id="t1"),
            EventRecord(seq_id=3, ts_ns=200, event_type=EventType.TASK_RESERVED, owner_id="o1", task_id="t1"),
        ]

        with pytest.raises(StateGuardError, match="committed before task reservation"):
            policy.evaluate_task(task, owner, events)


class TestPolicyGeneralContract:
    """Verifies general policy engine contracts: ordering, unfinalized owners, and session evaluation."""

    def test_unfinalized_owner_raises_error(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:active", OwnerType.TEST_CALL, "test.py::test_active")

        policy = PolicyEngine()
        with pytest.raises(StateGuardError, match="owner is not finalized"):
            policy.evaluate_owner("owner:active", ledger)

    def test_missing_owner_raises_entity_not_found(self) -> None:
        ledger = Ledger()
        policy = PolicyEngine()
        with pytest.raises(EntityNotFoundError):
            policy.evaluate_owner("owner:missing", ledger)

    def test_deterministic_ordering_and_idempotence(self) -> None:
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_multi")

        # Task 1: Clean
        ledger.reserve_task("task:clean", "owner:1")
        ledger.commit_submit_returned("task:clean")
        ledger.commit_callable_started("task:clean")
        ledger.commit_callable_exited("task:clean", CallableState.EXITED_RETURNED)

        # Task 2: Uncertain
        ledger.reserve_task("task:uncertain", "owner:1")
        ledger.commit_submit_raised("task:uncertain", "RuntimeError")

        # Task 3: Violation
        ledger.reserve_task("task:violation", "owner:1")
        ledger.commit_submit_returned("task:violation")

        ledger.finalize_owner("owner:1")

        policy = PolicyEngine()
        findings1 = policy.evaluate_owner("owner:1", ledger)
        findings2 = policy.evaluate_owner("owner:1", ledger)

        # Idempotence: repeated calls produce identical results
        assert findings1 == findings2

        # Deterministic order: Violations first, then Uncertain, then Clean
        assert len(findings1) == 3
        assert findings1[0].severity == Severity.VIOLATION
        assert findings1[0].task_id == "task:violation"

        assert findings1[1].severity == Severity.UNCERTAIN
        assert findings1[1].task_id == "task:uncertain"

        assert findings1[2].severity == Severity.CLEAN
        assert findings1[2].task_id == "task:clean"

    def test_evaluate_all_finalized(self) -> None:
        ledger = Ledger()

        # Finalized owner 1
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_1")
        ledger.reserve_task("task:1", "owner:1")
        ledger.commit_submit_returned("task:1")
        ledger.finalize_owner("owner:1")

        # Finalized owner 2 (empty)
        ledger.create_owner("owner:2", OwnerType.TEST_CALL, "test.py::test_2")
        ledger.finalize_owner("owner:2")

        # Active owner 3 (should be ignored by evaluate_all_finalized)
        ledger.create_owner("owner:3", OwnerType.TEST_CALL, "test.py::test_3")

        policy = PolicyEngine()
        all_findings = policy.evaluate_all_finalized(ledger)

        assert set(all_findings.keys()) == {"owner:1", "owner:2"}
        assert len(all_findings["owner:1"]) == 1
        assert all_findings["owner:1"][0].classification == FindingClassification.OUTSTANDING_TASK
        assert len(all_findings["owner:2"]) == 1
        assert all_findings["owner:2"][0].classification == FindingClassification.NO_OUTSTANDING_TASKS

    def test_evaluate_all_finalized_snapshot_isolation(self) -> None:
        """Demonstrate that evaluate_all_finalized captures one snapshot and isolates
        subsequent ledger commits occurring during/after snapshot acquisition.
        """
        ledger = Ledger()
        ledger.create_owner("owner:1", OwnerType.TEST_CALL, "test.py::test_iso_1")
        ledger.reserve_task("task:1", "owner:1")
        ledger.commit_submit_returned("task:1")
        ledger.finalize_owner("owner:1")

        policy = PolicyEngine()

        # Wrap ledger.get_finalized_session_snapshot to trigger a concurrent commit
        # immediately after the snapshot is captured under lock.
        original_get_snapshot = ledger.get_finalized_session_snapshot
        snapshot_taken = threading.Event()
        mutation_done = threading.Event()

        def hooked_get_snapshot():
            result = original_get_snapshot()
            snapshot_taken.set()
            assert mutation_done.wait(timeout=5.0)
            return result

        ledger.get_finalized_session_snapshot = hooked_get_snapshot  # type: ignore[method-assign]

        def concurrent_committer():
            assert snapshot_taken.wait(timeout=5.0)
            # Commit a second task after snapshot capture
            ledger.reserve_task("task:2", "owner:1")
            ledger.commit_submit_returned("task:2")
            mutation_done.set()

        t = threading.Thread(target=concurrent_committer)
        t.start()

        all_findings = policy.evaluate_all_finalized(ledger)
        t.join(timeout=5.0)

        # The evaluated snapshot must ONLY contain task:1
        findings_owner1 = all_findings["owner:1"]
        assert len(findings_owner1) == 1
        assert findings_owner1[0].task_id == "task:1"

        # Restore original method and perform a subsequent evaluation
        ledger.get_finalized_session_snapshot = original_get_snapshot  # type: ignore[method-assign]
        all_findings_2 = policy.evaluate_all_finalized(ledger)

        # The second evaluation captures the new state and observes both tasks
        findings_owner1_new = all_findings_2["owner:1"]
        assert len(findings_owner1_new) == 2
        task_ids = {f.task_id for f in findings_owner1_new}
        assert task_ids == {"task:1", "task:2"}
