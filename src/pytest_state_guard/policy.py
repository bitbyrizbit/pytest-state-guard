from __future__ import annotations

from typing import Mapping

from pytest_state_guard.ledger import Ledger
from pytest_state_guard.models import (
    EntityNotFoundError,
    EventRecord,
    EventType,
    Finding,
    FindingClassification,
    OwnerRecord,
    OwnerState,
    Severity,
    StateGuardError,
    TaskRecord,
)

# Frozen severity mapping per 03_FROZEN_V1_ARCHITECTURE.md Section 10
_FROZEN_SEVERITY_MAP: Mapping[FindingClassification, Severity] = {
    FindingClassification.NO_OUTSTANDING_TASKS: Severity.CLEAN,
    FindingClassification.COMPLETED_BEFORE_FINALIZATION: Severity.CLEAN,
    FindingClassification.CANCELLED_BEFORE_START: Severity.CLEAN,
    FindingClassification.OUTSTANDING_TASK: Severity.VIOLATION,
    FindingClassification.CALLABLE_NOT_EXITED: Severity.VIOLATION,
    FindingClassification.EXECUTION_AFTER_OWNER_FINALIZATION: Severity.VIOLATION,
    FindingClassification.EXIT_COMMITTED_AFTER_FINALIZATION: Severity.VIOLATION,
    FindingClassification.POST_LIFETIME_SUBMISSION: Severity.VIOLATION,
    FindingClassification.SUBMISSION_UNRESOLVED: Severity.UNCERTAIN,
    FindingClassification.SUBMISSION_FAILED_UNVERIFIED: Severity.UNCERTAIN,
}

_SEVERITY_ORDER = {
    Severity.VIOLATION: 0,
    Severity.UNCERTAIN: 1,
    Severity.CLEAN: 2,
}


def _validate_task_events(
    task: TaskRecord,
    events: list[EventRecord],
) -> None:
    """Validate task event stream against V1-V8 integrity constraints.

    Raises StateGuardError if any contradictory or malformed evidence is detected.
    """
    if not events:
        raise StateGuardError(f"No events found for task '{task.task_id}'.")

    # V1: Strict Monotonicity & Uniqueness
    for i in range(len(events) - 1):
        if events[i + 1].seq_id <= events[i].seq_id:
            raise StateGuardError(
                f"Corrupted sequence ordering for task '{task.task_id}': "
                f"event {events[i + 1].seq_id} <= {events[i].seq_id}."
            )

    # Find reservation event
    res_ev = next((e for e in events if e.event_type == EventType.TASK_RESERVED), None)
    if res_ev is None:
        raise StateGuardError(f"Missing TASK_RESERVED event for task '{task.task_id}'.")
    s_res = res_ev.seq_id

    # V2: Owner Attribution Consistency & V8: Reservation Precedence
    for e in events:
        if e.owner_id != task.owner_id:
            raise StateGuardError(
                f"Owner attribution mismatch for task '{task.task_id}': "
                f"expected '{task.owner_id}', got '{e.owner_id}'."
            )
        if e.seq_id < s_res:
            raise StateGuardError(
                f"Task event {e.event_type.value} at seq {e.seq_id} committed "
                f"before task reservation at seq {s_res} for task '{task.task_id}'."
            )

    # V4: Single Submission Event
    res_count = sum(1 for e in events if e.event_type == EventType.TASK_RESERVED)
    if res_count > 1:
        raise StateGuardError(
            f"Duplicate TASK_RESERVED events ({res_count}) for task '{task.task_id}'."
        )

    ret_events = [e for e in events if e.event_type == EventType.SUBMIT_RETURNED]
    raise_events = [e for e in events if e.event_type == EventType.SUBMIT_RAISED]

    if len(ret_events) > 1:
        raise StateGuardError(
            f"Duplicate SUBMIT_RETURNED events ({len(ret_events)}) for task '{task.task_id}'."
        )
    if len(raise_events) > 1:
        raise StateGuardError(
            f"Duplicate SUBMIT_RAISED events ({len(raise_events)}) for task '{task.task_id}'."
        )

    # V3: Submission Mutual Exclusivity
    if ret_events and raise_events:
        raise StateGuardError(
            f"Contradictory submission state for task '{task.task_id}': "
            f"recorded both SUBMIT_RETURNED (seq {ret_events[0].seq_id}) and "
            f"SUBMIT_RAISED (seq {raise_events[0].seq_id})."
        )

    # V6: Single Callable Lifecycle
    start_events = [e for e in events if e.event_type == EventType.CALLABLE_STARTED]
    exit_events = [e for e in events if e.event_type == EventType.CALLABLE_EXITED]

    if len(start_events) > 1:
        raise StateGuardError(
            f"Duplicate CALLABLE_STARTED events ({len(start_events)}) for task '{task.task_id}'."
        )
    if len(exit_events) > 1:
        raise StateGuardError(
            f"Duplicate CALLABLE_EXITED events ({len(exit_events)}) for task '{task.task_id}'."
        )

    # V5: Callable Exit Requires Start
    if exit_events and not start_events:
        raise StateGuardError(
            f"Callable exited at seq {exit_events[0].seq_id} without preceding "
            f"CALLABLE_STARTED for task '{task.task_id}'."
        )
    if exit_events and start_events and exit_events[0].seq_id < start_events[0].seq_id:
        raise StateGuardError(
            f"Callable exited at seq {exit_events[0].seq_id} before CALLABLE_STARTED "
            f"at seq {start_events[0].seq_id} for task '{task.task_id}'."
        )

    # V7: Cancellation vs. Execution Exclusivity
    done_cancel_events = [
        e
        for e in events
        if e.event_type == EventType.FUTURE_DONE_CALLBACK_OBSERVED
        and e.details is not None
        and e.details.get("terminal_state") == "CANCELLED"
    ]
    if done_cancel_events and start_events:
        raise StateGuardError(
            f"Contradictory evidence for task '{task.task_id}': task records both "
            f"Future cancellation (seq {done_cancel_events[0].seq_id}) and "
            f"callable execution start (seq {start_events[0].seq_id})."
        )


class PolicyEngine:
    """Pure, deterministic policy engine evaluating committed ledger evidence against owner boundaries."""

    def evaluate_owner(self, owner_id: str, ledger: Ledger) -> list[Finding]:
        """Evaluate a finalized owner and all associated tasks against the owner's boundary.

        Acquires exactly one coherent atomic point-in-time snapshot from the ledger
        and performs all evaluation strictly against that snapshot.

        Args:
            owner_id: Unique identifier of the owner to evaluate.
            ledger: Synchronized event ledger.

        Returns:
            A deterministically sorted list of Finding records.

        Raises:
            EntityNotFoundError: If owner_id is not registered in the ledger.
            StateGuardError: If the owner is active (not finalized) or evidence is malformed.
        """
        owner, tasks, events = ledger.get_owner_snapshot(owner_id)
        return self._evaluate_owner_from_snapshot(owner, tasks, events)

    def evaluate_all_finalized(self, ledger: Ledger) -> dict[str, list[Finding]]:
        """Evaluate all currently finalized owners in the ledger.

        Acquires exactly one coherent atomic point-in-time session snapshot from the ledger
        and performs all evaluation strictly against that snapshot.

        Args:
            ledger: Synchronized event ledger.

        Returns:
            A dictionary mapping owner_id to its list of Finding records.
        """
        finalized_owners, tasks, events = ledger.get_finalized_session_snapshot()

        tasks_by_owner: dict[str, list[TaskRecord]] = {o.owner_id: [] for o in finalized_owners}
        events_by_owner: dict[str, list[EventRecord]] = {o.owner_id: [] for o in finalized_owners}

        for t in tasks:
            if t.owner_id in tasks_by_owner:
                tasks_by_owner[t.owner_id].append(t)
        for e in events:
            if e.owner_id in events_by_owner:
                events_by_owner[e.owner_id].append(e)

        results: dict[str, list[Finding]] = {}
        for owner in finalized_owners:
            owner_tasks = tasks_by_owner[owner.owner_id]
            owner_events = events_by_owner[owner.owner_id]
            results[owner.owner_id] = self._evaluate_owner_from_snapshot(
                owner, owner_tasks, owner_events
            )

        return results

    def _evaluate_owner_from_snapshot(
        self,
        owner: OwnerRecord,
        tasks: list[TaskRecord],
        events: list[EventRecord],
    ) -> list[Finding]:
        """Evaluate owner and tasks using an already captured coherent snapshot."""
        if owner.state != OwnerState.FINALIZED or owner.finalized_seq is None:
            raise StateGuardError(
                f"Cannot evaluate owner '{owner.owner_id}': owner is not finalized (state={owner.state.value})."
            )

        s_finalized = owner.finalized_seq
        findings: list[Finding] = []

        # Partition events by task_id
        task_events_map: dict[str, list[EventRecord]] = {t.task_id: [] for t in tasks}
        for e in events:
            if e.task_id is not None and e.task_id in task_events_map:
                task_events_map[e.task_id].append(e)

        # Count lifetime submissions: s_res < s_finalized
        lifetime_task_count = 0
        for t in tasks:
            t_events = task_events_map[t.task_id]
            res_ev = next((e for e in t_events if e.event_type == EventType.TASK_RESERVED), None)
            s_res = res_ev.seq_id if res_ev is not None else t.created_seq
            if s_res < s_finalized:
                lifetime_task_count += 1

        # Owner-level finding if zero lifetime tasks
        if lifetime_task_count == 0:
            findings.append(
                Finding(
                    finding_id=f"finding:{owner.owner_id}:owner",
                    owner_id=owner.owner_id,
                    classification=FindingClassification.NO_OUTSTANDING_TASKS,
                    severity=Severity.CLEAN,
                    evidence_seq_ids=(owner.created_seq, s_finalized),
                    message=f"Owner '{owner.owner_id}' completed its lifetime with zero submitted tasks.",
                    task_id=None,
                )
            )

        # Evaluate each task
        for t in tasks:
            t_events = sorted(task_events_map[t.task_id], key=lambda e: e.seq_id)
            findings.append(self.evaluate_task(t, owner, t_events))

        # Deterministic sort: Violations first, then Uncertain, then Clean.
        # Ties broken by first evidence seq_id, then task_id.
        def _sort_key(f: Finding) -> tuple[int, int, str]:
            first_seq = f.evidence_seq_ids[0] if f.evidence_seq_ids else 0
            task_str = f.task_id or ""
            return (_SEVERITY_ORDER[f.severity], first_seq, task_str)

        findings.sort(key=_sort_key)
        return findings

    def evaluate_task(
        self,
        task: TaskRecord,
        owner: OwnerRecord,
        task_events: list[EventRecord],
    ) -> Finding:
        """Pure evaluation of a single task's event stream against a finalized owner boundary.

        Args:
            task: Snapshot of the task record.
            owner: Snapshot of the finalized owner record (must have finalized_seq).
            task_events: Committed events for this task, strictly sorted by seq_id.

        Returns:
            The single authoritative Finding for this task.

        Raises:
            StateGuardError: If task events violate integrity rules V1-V8.
        """
        if owner.finalized_seq is None:
            raise StateGuardError(f"Owner '{owner.owner_id}' has no finalized_seq.")

        _validate_task_events(task, task_events)

        s_finalized = owner.finalized_seq

        # Extract sequence markers
        res_ev = next(e for e in task_events if e.event_type == EventType.TASK_RESERVED)
        s_res = res_ev.seq_id

        ret_ev = next((e for e in task_events if e.event_type == EventType.SUBMIT_RETURNED), None)
        s_ret = ret_ev.seq_id if ret_ev is not None else None

        raise_ev = next((e for e in task_events if e.event_type == EventType.SUBMIT_RAISED), None)
        s_raise = raise_ev.seq_id if raise_ev is not None else None

        start_ev = next((e for e in task_events if e.event_type == EventType.CALLABLE_STARTED), None)
        s_start = start_ev.seq_id if start_ev is not None else None

        exit_ev = next((e for e in task_events if e.event_type == EventType.CALLABLE_EXITED), None)
        s_exit = exit_ev.seq_id if exit_ev is not None else None

        done_cancel_ev = next(
            (
                e
                for e in task_events
                if e.event_type == EventType.FUTURE_DONE_CALLBACK_OBSERVED
                and e.details is not None
                and e.details.get("terminal_state") == "CANCELLED"
            ),
            None,
        )
        s_done_cancel = done_cancel_ev.seq_id if done_cancel_ev is not None else None

        # Precedence Decision Tree
        classification: FindingClassification
        evidence: tuple[int, ...]
        message: str

        # 1. Post-lifetime submission
        if s_res > s_finalized:
            classification = FindingClassification.POST_LIFETIME_SUBMISSION
            ev_list = [s_finalized, s_res]
            if s_start is not None:
                ev_list.append(s_start)
            if s_exit is not None:
                ev_list.append(s_exit)
            evidence = tuple(ev_list)
            message = (
                f"Task '{task.task_id}' was reserved at seq {s_res} after owner '{owner.owner_id}' "
                f"was finalized at seq {s_finalized}."
            )

        # 2. Cancelled before start
        elif s_done_cancel is not None and s_done_cancel < s_finalized and s_start is None:
            classification = FindingClassification.CANCELLED_BEFORE_START
            evidence = (s_res, s_done_cancel, s_finalized)
            message = (
                f"Task '{task.task_id}' future cancellation was observed at seq {s_done_cancel} "
                f"before owner finalization at seq {s_finalized} without callable execution."
            )

        # 3. Completed before finalization
        elif s_exit is not None and s_exit < s_finalized:
            classification = FindingClassification.COMPLETED_BEFORE_FINALIZATION
            assert s_start is not None
            evidence = (s_res, s_start, s_exit, s_finalized)
            message = (
                f"Task '{task.task_id}' callable completed and committed exit at seq {s_exit} "
                f"before owner finalization at seq {s_finalized}."
            )

        # 4. Execution started after finalization
        elif s_start is not None and s_start > s_finalized:
            classification = FindingClassification.EXECUTION_AFTER_OWNER_FINALIZATION
            ev_list = [s_res, s_finalized, s_start]
            if s_exit is not None:
                ev_list.append(s_exit)
            evidence = tuple(ev_list)
            message = (
                f"Task '{task.task_id}' began callable execution at seq {s_start} "
                f"after owner finalization at seq {s_finalized}."
            )

        # 5. Exit committed after finalization
        elif s_start is not None and s_start < s_finalized and s_exit is not None and s_exit > s_finalized:
            classification = FindingClassification.EXIT_COMMITTED_AFTER_FINALIZATION
            evidence = (s_res, s_start, s_finalized, s_exit)
            message = (
                f"Task '{task.task_id}' callable began at seq {s_start} before finalization, "
                f"but exit was committed at seq {s_exit} after finalization at seq {s_finalized}."
            )

        # 6. Callable not exited
        elif s_start is not None and s_start < s_finalized and s_exit is None:
            classification = FindingClassification.CALLABLE_NOT_EXITED
            evidence = (s_res, s_start, s_finalized)
            message = (
                f"Task '{task.task_id}' callable began execution at seq {s_start} before finalization, "
                f"but has not committed an exit event."
            )

        # 7. Outstanding task (submit returned before finalization, never started)
        elif s_ret is not None and s_ret < s_finalized and s_start is None:
            classification = FindingClassification.OUTSTANDING_TASK
            evidence = (s_res, s_ret, s_finalized)
            message = (
                f"Task '{task.task_id}' was accepted for execution before finalization "
                f"at seq {s_ret} but callable has not started."
            )

        # 8. Submission failed unverified (submit raised before finalization, never started)
        elif s_raise is not None and s_raise < s_finalized and s_start is None:
            classification = FindingClassification.SUBMISSION_FAILED_UNVERIFIED
            evidence = (s_res, s_raise, s_finalized)
            message = (
                f"Task '{task.task_id}' ThreadPoolExecutor.submit raised before finalization at seq {s_raise}, "
                f"but worker work-item enqueueing cannot be conclusively ruled out."
            )

        # 9. Submission unresolved (neither returned nor raised before finalization, no callable start)
        else:
            classification = FindingClassification.SUBMISSION_UNRESOLVED
            if s_ret is not None and s_ret > s_finalized:
                evidence = (s_res, s_finalized, s_ret)
                message = (
                    f"Task '{task.task_id}' submission was unresolved at owner finalization at seq {s_finalized}; "
                    f"submit subsequently returned at seq {s_ret} without callable execution."
                )
            elif s_raise is not None and s_raise > s_finalized:
                evidence = (s_res, s_finalized, s_raise)
                message = (
                    f"Task '{task.task_id}' submission was unresolved at owner finalization at seq {s_finalized}; "
                    f"submit subsequently raised at seq {s_raise} without callable execution."
                )
            else:
                evidence = (s_res, s_finalized)
                message = (
                    f"Task '{task.task_id}' was reserved at seq {s_res} but submission status was unresolved."
                )

        severity = _FROZEN_SEVERITY_MAP[classification]
        finding_id = f"finding:{owner.owner_id}:{task.task_id}"

        return Finding(
            finding_id=finding_id,
            owner_id=owner.owner_id,
            classification=classification,
            severity=severity,
            evidence_seq_ids=evidence,
            message=message,
            task_id=task.task_id,
        )
