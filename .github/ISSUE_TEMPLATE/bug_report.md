---
name: Bug Report
about: Create a report to help us improve pytest-state-guard
title: "[BUG] "
labels: ["bug"]
assignees: ""
---

<!--
Thank you for reporting an issue with pytest-state-guard.

pytest-state-guard audits standard library concurrent.futures.ThreadPoolExecutor
task lifecycles against pytest test and fixture boundaries.

Please do not include sensitive data, credentials, internal server addresses,
or proprietary source code in bug reports.
-->

## Summary

<!-- A clear and concise description of the bug. -->

## Minimal Reproducible Example

<!--
Provide a self-contained test file or reproduction script demonstrating the issue.
Keep it as small as possible while still reproducing the behavior.
-->

```python
# test_reproduce.py
import pytest
from concurrent.futures import ThreadPoolExecutor

def test_example():
    pass
```

## Steps to Reproduce

1. Run command: `pytest test_reproduce.py ...`
2. Observe output...

## Expected Behavior

<!-- A clear description of what you expected to happen. -->

## Actual Behavior / Error Output

<!--
Include relevant terminal output, State Guard report tables, error messages,
or Python tracebacks.
-->

```text
PASTE OUTPUT HERE
```

## Environment Information

- **Operating System:** <!-- e.g., Ubuntu 22.04, Windows 11, macOS 14 -->
- **Python Version:** <!-- e.g., 3.11.8 (run: python --version) -->
- **Pytest Version:** <!-- e.g., 8.3.2 (run: pytest --version) -->
- **pytest-state-guard Version:** <!-- e.g., 0.1.1 (run: pip show pytest-state-guard) -->
- **Executor Pattern:** <!-- e.g., function-scoped fixture, session fixture, test inline -->

## Reproduction Without State Guard

<!--
Does the test pass or fail when State Guard is disabled?
Run: pytest -p no:state_guard
-->

- [ ] Issue reproduces even when State Guard is disabled (`pytest -p no:state_guard`)
- [ ] Issue only occurs when State Guard is active

## Additional Context

<!-- Any other context or diagnostic logs that might help investigate the issue. -->
