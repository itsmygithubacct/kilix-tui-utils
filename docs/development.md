# Development

Moved verbatim from the README.

## Tests

```sh
python3 tests/run.py              # all suites, one subprocess each
python3 tests/run.py calculator   # one
```

## Versioning

This repository is a Kilix-pinned desktop and utility closure, not a
coordinated Plebian-OS release-core member. Selecting Kilix TUI as the desktop
is optional; managed Plebian-OS installs the same checkout eagerly because it
also supplies the unified utility suite. Its `VERSION` may advance
independently, and it does not receive the core’s coordinated `v<x.y.z>` tags.
A Plebian-OS release inherits the exact reviewed commit through the Kilix
commit that pins it.

## Why one repository

Before this, each dashboard was its own repo pinned by SHA, and each pinned
three further helper repos at their own SHAs — eight pins for two tools. This
collapses that into one checkout pinned once by Kilix’s dependency closure.
