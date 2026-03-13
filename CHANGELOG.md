# Changelog

## v1.0.0 — 2026-03-13

Initial open-source release.

- PI controller for RAPL PL1 thermal management
- SEN5 board sensor guard with hysteresis
- Idle mode with EPP management
- EC DDR fan_off threshold override (40→50°C) via merge-not-overwrite
- JSONL sensor logging with rotation
- 5-panel sensor-plot.sh visualization
- install.sh with atomic install/uninstall/upgrade support
- Comprehensive safety: bounds validation on EC writes, RAPL constraint
  verification, dt cap after suspend, sensor failure safe mode,
  ExecStopPost fallback, systemd hardening
- 363 unit tests (plus additional live-hardware tests on Framework hardware)
