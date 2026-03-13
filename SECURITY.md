# Security Policy

## Scope

fw-fanctl runs as root and communicates with the Framework Laptop's Embedded
Controller (EC) via ectool. It writes to RAPL sysfs (CPU power limits),
CPU EPP (energy performance preference), and EC RAM (thermal thresholds via
`ectool thermalset`). A vulnerability in the daemon or in ectool could
potentially affect hardware behavior.

## Reporting a vulnerability

If you discover a security issue, please report it privately via a
GitHub security advisory (on the repository's Security tab → "Report a
vulnerability") rather than opening a public issue.

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact

You should receive a response within 7 days.

## Vendored binary

The vendored `ectool` binary in `vendor/` is a pre-built binary from a
third-party source. Its provenance is documented in `vendor/README.md`.
If you have concerns about the binary, build ectool from source instead —
`install-ectool.sh` does this automatically when build dependencies are
available.
