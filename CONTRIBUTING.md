# Contributing to fw-fanctl

## Getting started

fw-fanctl is a small, focused project. Contributions are welcome — bug
reports, documentation improvements, and hardware testing are just as
valuable as code changes.

**Before starting work on a feature or large change**, please open an issue
to discuss the approach. This avoids wasted effort if the direction doesn't
fit the project.

## Hardware requirements

A Framework Laptop 13 (12th Gen Intel) is needed for live testing. If you
don't have one, you can still contribute:
- Code changes can be tested with `--dry-run` and the unit test suite
- Documentation, config validation, and sensor-plot improvements don't
  need hardware

## Running tests

Run from the repo root:

    python3 tests/test_fw_fanctl.py

For live hardware tests (requires Framework laptop + root):

    sudo python3 tests/test_fw_fanctl.py --live

The `--live` flag enables additional tests that read real sensor data, verify
EC communication, and check RAPL accessibility on actual hardware.

The test suite uses a custom lightweight harness (no pytest/unittest dependency)
and mocks all hardware interfaces, so the base suite runs on any machine with
Python 3.10+. The `--live` tests are additional and require Framework hardware.

## Code style

- Python 3.10+ (no external dependencies for the daemon itself)
- Shell scripts: bash with `set -euo pipefail`
- No linter enforced yet — just keep it readable

## Reporting issues

Include output of:

    sudo bash check.sh
    uname -r
    grep "model name" /proc/cpuinfo | head -1

## Pull requests

1. Fork the repo
2. Create a feature branch
3. Ensure tests pass
4. Submit PR with a clear description

## Security

If you discover a security issue (the daemon runs as root and communicates
with EC hardware), please report it privately via a GitHub security advisory
rather than opening a public issue.
