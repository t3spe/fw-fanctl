# fw-pwrctl vs fw-fanctrl

[fw-fanctrl](https://github.com/TamtamHero/fw-fanctrl) by TamtamHero is the
most popular open-source fan controller for Framework laptops (425+ stars,
19 contributors). This document compares the two projects.

## Different approach to the same problem

Both reduce fan noise on Framework laptops running Linux. They take
fundamentally different paths:

| | **fw-pwrctl** | **fw-fanctrl** |
|---|---|---|
| **Mechanism** | Adjusts CPU power limit (RAPL PL1); EC controls the fan autonomously | Overrides the EC and sets fan duty directly via `ectool fanduty` |
| **Algorithm** | PI (proportional-integral) closed-loop controller | Moving average + linear interpolation on a temp→speed curve |
| **Hardware** | Framework 13, 12th Gen Intel only | Framework 13 + 16, Intel + AMD |

fw-pwrctl never touches the fan — it lowers the CPU's power budget so the
board cools down and the EC slows the fan on its own. fw-fanctrl takes
direct control of the fan and sets a duty cycle based on the current
temperature.

## Safety

| | **fw-pwrctl** | **fw-fanctrl** |
|---|---|---|
| **Safety layers** | 3 independent (SEN5 board guard, CPU critical override, sensor failure safe mode) | 1 (returns to EC auto on exit) |
| **SIGKILL / OOM** | `ExecStopPost` restores PL1 + EC fan even after hard kill | Fan stays at last-set duty |
| **EC authority** | EC always controls the fan | Daemon overrides EC |
| **Sensor failure** | Forces PL1 to minimum, re-scans hwmon after 10 failures | Defaults to 50°C reading |

fw-pwrctl's indirect approach means the EC retains full authority over the
fan at all times. If the daemon dies for any reason, the fan still responds
to board temperatures normally — only PL1 stays at whatever it was last set
to (restored to 28W by `ExecStopPost`).

fw-fanctrl overrides the EC with `ectool fanduty`. If the daemon exits
cleanly, `autofanctrl` hands control back. A SIGKILL or OOM kill skips
cleanup, leaving the fan at its last-commanded speed.

## Algorithm

**fw-pwrctl** uses a PI controller that converges to a temperature setpoint:

    error = median_temp − setpoint
    PL1 = pl1_max − (Kp × error + Ki × ∫error)

Features: median filter (rejects turbo spikes), anti-windup (prevents
integral accumulation when output is saturated), asymmetric rate limiting
(smooth transitions), and idle mode (caps PL1 when cool).

**fw-fanctrl** uses a moving average with an asymmetric response trick:

    effective_temp = min(moving_average, current_temp)
    fan_speed = interpolate(speed_curve, effective_temp)

The `min()` means fans spin down immediately when load drops (uses current
temp) but resist spinning up for transient spikes (uses lower average).
Simple, intuitive, easy to tune by hand.

## Usability

| | **fw-pwrctl** | **fw-fanctrl** |
|---|---|---|
| **Configuration** | 25 numeric PI parameters (Kp, Ki, rate limits, etc.) | Named strategy presets ("lazy", "medium", "agile", etc.) |
| **Runtime control** | Restart to change settings | `fw-fanctrl use lazy` — live switching via Unix socket |
| **AC/battery** | Not handled | Auto-switches strategy on AC disconnect |
| **Barrier to entry** | Requires understanding PI control to tune | Anyone can read a temp→speed curve |

fw-fanctrl ships 7 built-in strategies and lets users switch between them
at runtime without restarting the service. fw-pwrctl requires editing
`config.json` and restarting.

## Observability

fw-pwrctl logs rich JSONL sensor snapshots every 2 seconds (CPU stats,
memory, throttle counts, board temps, lm-sensors data, controller state)
with automatic rotation and gzip compression. `sensor-plot.sh` generates a
5-panel dashboard from these logs.

fw-fanctrl prints status to stdout. No persistent logging, no plotting.

## Code

| | **fw-pwrctl** | **fw-fanctrl** |
|---|---|---|
| **Language** | Python | Python |
| **Core logic** | 1,444 lines in 1 file | ~804 lines across 39 files (1,075 total with boilerplate) |
| **Tests** | 2,547 lines (1.76:1 test-to-code ratio) | None |
| **Subprocess calls** | List-form (`shell=False`), all with timeouts | `shell=True`, no timeouts |
| **Systemd hardening** | 15 security directives (`ProtectSystem=strict`, `PrivateNetwork`, etc.) | Basic unit file |

## Hardware support

fw-fanctrl works on any Framework laptop (13 or 16, Intel or AMD).
fw-pwrctl only works on Framework 13 with 12th Gen Intel (Alder Lake)
because it depends on Intel RAPL for PL1 control.

## Community

fw-fanctrl has been around since 2022, has 425+ stars, 58 forks,
19 contributors, and is packaged for Arch (AUR), NixOS, and Fedora.
Third-party GUI tools and GNOME/Cinnamon applets exist for it.

fw-pwrctl is a new single-author project.

## When to use which

**Use fw-fanctrl if:**
- You have a non-12th-Gen or AMD Framework laptop
- You want named presets and live strategy switching
- You want the simplest path to a quieter fan

**Use fw-pwrctl if:**
- You have a Framework 13 with 12th Gen Intel
- You want the EC to retain full fan authority (no direct override)
- You want defense-in-depth safety (3 independent layers)
- You want rich sensor logging and post-mortem analysis
- You want a tested, security-hardened daemon
