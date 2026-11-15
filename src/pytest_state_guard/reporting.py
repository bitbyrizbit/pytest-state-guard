from __future__ import annotations

from typing import Any, Mapping, Sequence
from _pytest._code.code import TerminalRepr

from .models import FindingClassification, OwnerRecord, Severity
from .policy import Finding


class StateGuardTerminalRepr(TerminalRepr):
    """Custom TerminalRepr rendering State Guard teardown violations."""

    def __init__(self, owner: OwnerRecord, findings: Sequence[Finding]) -> None:
        self.owner = owner
        self.findings = list(findings)

    def toterminal(self, tw: Any) -> None:
        """Render formatted State Guard failure output to pytest TerminalWriter."""
        header = f"State Guard Teardown Violation: {self.owner.owner_type.value}"
        if self.owner.fixture_name:
            header += f" (fixture '{self.owner.fixture_name}')"
        header += f" [{self.owner.nodeid}]"

        tw.line()
        tw.sep("-", header, red=True, bold=True)
        tw.line(f"Owner ID: {self.owner.owner_id}", red=True)
        if self.owner.finalized_seq is not None:
            tw.line(
                f"Owner Finalized Sequence: {self.owner.finalized_seq} (created at seq {self.owner.created_seq})",
                red=True,
            )

        for finding in self.findings:
            if finding.severity != Severity.VIOLATION:
                continue
            tw.line()
            tw.line(f"  * Finding: {finding.finding_id}", bold=True, red=True)
            tw.line(f"    Classification: {finding.classification.value}")
            tw.line(f"    Task ID:        {finding.task_id}")
            tw.line(f"    Evidence:       {finding.message}")
            if finding.evidence_seq_ids:
                seq_str = ", ".join(str(s) for s in finding.evidence_seq_ids)
                tw.line(f"    Sequences:      {seq_str}")
        tw.line()


class ChainedTerminalRepr(TerminalRepr):
    """Chains an original pytest longrepr (e.g. fixture exception) with State Guard violations."""

    def __init__(
        self,
        original_repr: Any,
        owner: OwnerRecord,
        findings: Sequence[Finding],
    ) -> None:
        self.original_repr = original_repr
        self.state_guard_repr = StateGuardTerminalRepr(owner, findings)

    def toterminal(self, tw: Any) -> None:
        """Render original representation first, followed by State Guard diagnostic."""
        if hasattr(self.original_repr, "toterminal"):
            self.original_repr.toterminal(tw)
        elif self.original_repr is not None:
            tw.line(str(self.original_repr))

        tw.line()
        tw.sep("-", "State Guard Audit Detected Teardown Concurrency Violations", red=True, bold=True)
        self.state_guard_repr.toterminal(tw)


def render_terminal_summary(
    terminalreporter: Any,
    findings_by_owner: Mapping[str, Sequence[Finding]],
    owners_by_id: Mapping[str, OwnerRecord],
    incomplete_reason: str | None = None,
    poison_reason: str | None = None,
) -> None:
    """Render the authoritative session-level State Guard audit section."""
    tw = terminalreporter._tw

    if poison_reason:
        tw.line()
        tw.sep("=", "State Guard: Operational Lifecycle Failure", red=True, bold=True)
        tw.line(f"CRITICAL: {poison_reason}", red=True)
        tw.line("The test environment state is unverified due to a plugin operational failure.")
        return

    if incomplete_reason:
        tw.line()
        tw.sep("=", "State Guard: Incomplete Session Audit", yellow=True, bold=True)
        tw.line(f"WARNING: {incomplete_reason}", yellow=True)
        tw.line("Fixture finalization was interrupted; unfinalized owners were excluded from policy evaluation.")

    violations_found: list[tuple[OwnerRecord, list[Finding]]] = []
    uncertain_found: list[tuple[OwnerRecord, list[Finding]]] = []

    for owner_id, findings in sorted(findings_by_owner.items()):
        owner = owners_by_id.get(owner_id)
        if owner is None:
            continue
        v_list = [f for f in findings if f.severity == Severity.VIOLATION]
        u_list = [f for f in findings if f.severity == Severity.UNCERTAIN]
        if v_list:
            violations_found.append((owner, v_list))
        if u_list:
            uncertain_found.append((owner, u_list))

    if violations_found:
        tw.line()
        tw.sep("=", "State Guard Audit: VIOLATIONS DETECTED", red=True, bold=True)
        for owner, findings in violations_found:
            target = f"Owner: {owner.owner_type.value}"
            if owner.fixture_name:
                target += f" (fixture '{owner.fixture_name}')"
            target += f" [{owner.nodeid}]"
            tw.line(target, bold=True, red=True)
            for f in findings:
                tw.line(f"  - [{f.classification.value}] Task '{f.task_id}': {f.message}")
                if f.evidence_seq_ids:
                    seq_str = ", ".join(str(s) for s in f.evidence_seq_ids)
                    tw.line(f"    Sequences: {seq_str}")
        tw.line()

    if uncertain_found:
        tw.line()
        tw.sep("=", "State Guard Audit: UNCERTAIN LIFECYCLE INVARIANTS", yellow=True, bold=True)
        for owner, findings in uncertain_found:
            target = f"Owner: {owner.owner_type.value}"
            if owner.fixture_name:
                target += f" (fixture '{owner.fixture_name}')"
            target += f" [{owner.nodeid}]"
            tw.line(target, bold=True, yellow=True)
            for f in findings:
                tw.line(f"  - [{f.classification.value}] Task '{f.task_id}': {f.message}")
                if f.evidence_seq_ids:
                    seq_str = ", ".join(str(s) for s in f.evidence_seq_ids)
                    tw.line(f"    Sequences: {seq_str}")
        tw.line()
