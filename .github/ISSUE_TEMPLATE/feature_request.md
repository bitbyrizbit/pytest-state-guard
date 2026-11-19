---
name: Feature Request
about: Suggest an idea or improvement for pytest-state-guard
title: "[FEATURE] "
labels: ["enhancement"]
assignees: ""
---

<!--
Thank you for suggesting an improvement to pytest-state-guard.

SCOPE NOTICE:
pytest-state-guard has a deliberately narrow, focused architectural scope:
deterministic lifecycle audit of standard library `concurrent.futures.ThreadPoolExecutor`
tasks against pytest test and fixture boundaries.

Feature requests should align with this core domain. Proposals to support
other concurrency primitives (such as ProcessPoolExecutor, asyncio, or custom
thread pools) require separate architectural design evaluation and are not
authorized for v1.
-->

## Problem Statement

<!--
A clear and concise description of the problem this feature would solve.
Example: "I am always frustrated when [...]"
-->

## Proposed Behavior

<!--
A clear and concise description of what you want to happen.
Include proposed CLI options, configuration keys, or report output if applicable.
-->

## Concrete Use Case

<!--
Describe a realistic test or fixture scenario that demonstrates the value of this feature.
-->

```python
# Example scenario
```

## Alternatives Considered

<!--
A description of any alternative solutions, patterns, or workarounds you have considered.
-->

## Compatibility & Architectural Impact

<!--
Considerations regarding:
1. Ledger guarantees (observed events vs. committed events)
2. Fail-open execution safety (State Guard must never crash host test suites)
3. Performance overhead during test runs
4. Backward compatibility with existing pytest suites
-->

## Additional Context

<!-- Any other context, references, or mockups related to the feature request. -->
