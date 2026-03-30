#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 t3spe
#
# sensor-plot.sh — Multi-panel plot of CPU, board temps, fan, and CPU load.
#
# Reads sensor-log.json (JSONL produced by fw_fanctl daemon's SensorLogger)
# and generates a 5-panel PNG chart:
#   Panel 1: Fan RPM (the outcome)
#   Panel 2: CPU temperature + controller median (why the controller acts)
#   Panel 3: PL1 power — RAPL + controller commanded (what the controller does)
#   Panel 4: EC board sensors SEN2-SEN5 with trip-point lines (board context)
#   Panel 5: CPU active % + load avg (workload trigger)
#
# Usage (sudo needed — log files are owned by root):
#   sudo bash sensor-plot.sh              # show plot in a window
#   sudo bash sensor-plot.sh -o FILE.png  # save to file instead
#   sudo bash sensor-plot.sh --hours 6    # override time window (default: 3)
#   sudo bash sensor-plot.sh --rolling 5m # overlay rolling average (e.g. 30s, 5m, 1h)
#   sudo bash sensor-plot.sh --log-dir /path/to/logs  # override log directory
#
# Dependencies: python3, matplotlib (pip3 install matplotlib)

set -euo pipefail

LOG_DIR="/var/log/fw-fanctl"
OUTPUT=""
OUTPUT_EXPLICIT=false
HOURS=3
ROLLING=""

ORIG_ARGS=("$@")
need_arg() { [[ $1 -ge 2 ]] || { echo "error: $2 requires an argument" >&2; exit 1; }; }

show_help() {
    echo "Usage: sudo bash $0 [-o output.png] [--hours N] [--rolling DURATION] [--log-dir DIR]"
    echo "  -o, --output FILE   Save plot to file (default: /tmp/sensor-plot-Nh.png)"
    echo "  --hours N            Time window in hours (default: 3)"
    echo "  --rolling DURATION   Overlay rolling average (e.g. 30s, 5m, 1h)"
    echo "  --log-dir DIR        Log directory (default: /var/log/fw-fanctl)"
    echo "  -h, --help           Show this help"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)     show_help; exit 0 ;;
        -o|--output)   need_arg "$#" "$1"; OUTPUT="$2"; OUTPUT_EXPLICIT=true; shift 2 ;;
        --hours)       need_arg "$#" "$1"; HOURS="$2";   shift 2 ;;
        --rolling)     need_arg "$#" "$1"; ROLLING="$2"; shift 2 ;;
        --log-dir)     need_arg "$#" "$1"; LOG_DIR="$2"; shift 2 ;;
        *)             show_help >&2; exit 1 ;;
    esac
done

# Preflight: python3
if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 not found — install with: sudo apt install python3" >&2
    exit 1
fi

# Default output to /tmp if not specified
if [[ -z "$OUTPUT" ]]; then
    OUTPUT="/tmp/sensor-plot-${HOURS}h.png"
fi

# Collect sensor log files using metadata index for time-based selection.
# Only loads files whose data overlaps the requested --hours window.
# Falls back to loading all files if metadata doesn't exist.
META_FILE="$LOG_DIR/sensor-log-meta.json"
CUTOFF=$(date -d "-${HOURS} hours" +%Y-%m-%dT%H:%M:%S 2>/dev/null) || CUTOFF=""

declare -A _seen_files
LOG_FILES=()
_add_file() { [[ -f "$1" && -r "$1" && -z "${_seen_files[$1]:-}" ]] && _seen_files[$1]=1 && LOG_FILES+=("$1"); return 0; }

if [[ -f "$META_FILE" && -r "$META_FILE" && -n "$CUTOFF" ]]; then
    # Use metadata for precise file selection (variables passed via argv, not interpolation)
    while IFS= read -r f; do
        _add_file "$f"
    done < <(python3 - "$META_FILE" "$CUTOFF" "$LOG_DIR" << 'METAEOF'
import json, sys, os
meta_file, cutoff, log_dir = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(meta_file) as f:
        meta = json.load(f)
except (json.JSONDecodeError, ValueError):
    sys.exit(0)
for e in meta:
    end = e.get("end", "")
    if end >= cutoff or not end:
        print(os.path.join(log_dir, e["file"]))
METAEOF
)
    # Also include rotated files not in metadata (pre-metadata or failed compression)
    for f in "$LOG_DIR"/sensor-log.*.json.gz "$LOG_DIR"/sensor-log.*.json; do
        _add_file "$f"
    done
else
    # No metadata — load all rotated files
    for f in "$LOG_DIR"/sensor-log.*.json.gz "$LOG_DIR"/sensor-log.*.json; do
        _add_file "$f"
    done
fi
# Always include active log
_add_file "$LOG_DIR/sensor-log.json"

if [[ ${#LOG_FILES[@]} -eq 0 ]]; then
    echo "Error: no sensor-log files found in $LOG_DIR" >&2
    echo "  Is fw-fanctl running? Check: sudo systemctl status fw-fanctl" >&2
    echo "  Logs need a few minutes to appear (first flush is after 120s)." >&2
    echo "  Use --log-dir to point at a different directory." >&2
    exit 1
fi

# Check log files are readable (they're owned by root)
UNREADABLE=0
for f in "${LOG_FILES[@]}"; do
    if [[ ! -r "$f" ]]; then
        UNREADABLE=$((UNREADABLE + 1))
    fi
done
if [[ $UNREADABLE -gt 0 ]]; then
    echo "Error: $UNREADABLE log file(s) not readable (owned by root)" >&2
    echo "  Run with: sudo bash sensor-plot.sh ${ORIG_ARGS[*]}" >&2
    exit 1
fi

# Check matplotlib before running the plot script
if ! python3 -c "import matplotlib" 2>/dev/null; then
    echo "error: matplotlib not installed" >&2
    echo "  Install with one of:" >&2
    echo "    sudo apt install python3-matplotlib    # system package (recommended)" >&2
    echo "    pip3 install matplotlib                 # pip (may need --break-system-packages on Ubuntu 24.04+)" >&2
    exit 1
fi

python3 - "$HOURS" "$OUTPUT" "$ROLLING" "${LOG_FILES[@]}" << 'PYEOF'
import gzip, json, sys, statistics
from datetime import datetime, timedelta, timezone

hours = float(sys.argv[1])
output = sys.argv[2]
rolling_arg = sys.argv[3]
log_files = sys.argv[4:]

def parse_duration(s):
    """Parse duration string like '30s', '5m', '1h' to seconds."""
    if not s:
        return 0
    s = s.strip().lower()
    try:
        if s.endswith("s"):
            return int(s[:-1])
        elif s.endswith("m"):
            return int(s[:-1]) * 60
        elif s.endswith("h"):
            return int(s[:-1]) * 3600
        else:
            return int(s)
    except ValueError:
        print(f"error: invalid duration '{s}' — use e.g. 30s, 5m, 1h", file=sys.stderr)
        sys.exit(1)

rolling_secs = parse_duration(rolling_arg)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

local_tz = datetime.now(timezone.utc).astimezone().tzinfo
now = datetime.now(tz=local_tz)
cutoff = now - timedelta(hours=hours)

# Data arrays
timestamps = []
cpu_temps = []       # coretemp package
peci_temps = []      # PECI (hottest core instant)
fan_rpms = []
cpu_active = []      # 100 - idle%
load_1m = []
# Board sensors (from cros_ec, available in all historical data)
board_sen2 = []      # F75303_Local — charger/VRM area
board_sen3 = []      # F75303_DDR — memory area
board_sen4 = []      # Battery
board_sen5 = []      # F75303_CPU — hottest, near CPU VRM
pl1_w = []           # RAPL PL1 power limit (watts)
# Controller state (from SensorLogger, absent in old logs)
ctrl_pl1 = []        # commanded PL1
ctrl_median = []     # median-filtered coretemp
ctrl_guard = []      # bool: SEN5 guard active
ctrl_idle = []       # bool: idle ceiling active
ctrl_epp = []        # bool: EPP idle active

# Reference values from controller config (extracted from first entry with them)
setpoint_c = None
idle_ceiling_w = None

lines_read = 0
lines_bad = 0
for log_file in log_files:
    opener = gzip.open if log_file.endswith('.gz') else open
    try:
        f = opener(log_file, 'rt')
    except (OSError, gzip.BadGzipFile):
        lines_bad += 1
        continue
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            lines_read += 1
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                lines_bad += 1
                continue

            ts_str = d.get("timestamp")
            if not ts_str:
                lines_bad += 1
                continue
            ts = datetime.fromisoformat(ts_str)
            if ts < cutoff:
                continue

            sensors = d.get("sensors", {})

            # CPU package temperature (coretemp)
            coretemp = sensors.get("coretemp-isa-0000", {})
            pkg = coretemp.get("Package id 0", {})
            temp = pkg.get("temp1_input")

            # Controller may provide raw_temp_c as fallback
            ctrl = d.get("controller", {})
            ctrl_raw = ctrl.get("raw_temp_c")

            # Skip only when no temperature source is found
            if temp is None and ctrl_raw is None:
                continue

            # Fan speed (cros_ec) — may be absent in PL1-only mode
            ec = sensors.get("cros_ec-isa-0000", {})
            fan = ec.get("fan1", {})
            rpm = fan.get("fan1_input")

            timestamps.append(ts)
            cpu_temps.append(temp if temp is not None else ctrl_raw)
            fan_rpms.append(rpm)

            # PECI from cros_ec
            peci = ec.get("PECI", {})
            peci_temps.append(peci.get("temp5_input"))

            # Board sensors — prefer board_temps field (int340x, exact match),
            # fall back to cros_ec readings (always available in historical data)
            bt = d.get("board_temps", {})
            if bt:
                board_sen2.append(bt.get("sen2_c"))
                board_sen3.append(bt.get("sen3_c"))
                board_sen4.append(bt.get("sen4_c"))
                board_sen5.append(bt.get("sen5_c"))
            else:
                # Extract from cros_ec (offset by ~0.05 from int340x, negligible)
                local = ec.get("F75303_Local", {})
                board_sen2.append(local.get("temp1_input"))
                ddr = ec.get("F75303_DDR", {})
                board_sen3.append(ddr.get("temp3_input"))
                bat = ec.get("Battery", {})
                board_sen4.append(bat.get("temp4_input"))
                cpu_vrm = ec.get("F75303_CPU", {})
                board_sen5.append(cpu_vrm.get("temp2_input"))

            # RAPL PL1
            pl1_w.append(d.get("rapl_pl1_w"))

            # Controller state
            ctrl_pl1.append(ctrl.get("pl1_w"))
            ctrl_median.append(ctrl.get("median_c"))
            ctrl_guard.append(ctrl.get("guard_active"))
            ctrl_idle.append(ctrl.get("idle_active"))
            ctrl_epp.append(ctrl.get("epp_active"))

            # Extract reference values from first entry that has them
            if setpoint_c is None and ctrl.get("setpoint_c") is not None:
                setpoint_c = ctrl["setpoint_c"]
            if idle_ceiling_w is None and ctrl.get("idle_ceiling_w") is not None:
                idle_ceiling_w = ctrl["idle_ceiling_w"]

            # CPU usage
            cpu = d.get("cpu", {})
            idle = cpu.get("idle")
            if idle is not None:
                cpu_active.append(100.0 - idle)
            else:
                cpu_active.append(None)
            load_1m.append(cpu.get("load_1m"))

if not timestamps:
    if lines_bad > 0 and lines_bad == lines_read:
        print(f"Found {lines_read} log lines but none were valid JSON.", file=sys.stderr)
    else:
        print(f"No data in the last {hours} hours.", file=sys.stderr)
    sys.exit(1)

# Defaults for reference lines
if setpoint_c is None:
    setpoint_c = 75.0
if idle_ceiling_w is None:
    idle_ceiling_w = 15.0

n = len(timestamps)
has_ctrl = any(v is not None for v in ctrl_pl1)
has_epp = any(v is not None for v in ctrl_epp)
print(f"Plotting {n} samples "
      f"({timestamps[0].strftime('%H:%M')} \u2013 {timestamps[-1].strftime('%H:%M')})"
      f"{' [controller data present]' if has_ctrl else ''}")

# Stats helper
def compute_stats(values):
    """Return (min, avg, median, p90, max) for a list, skipping None."""
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    n_c = len(clean)
    return {
        "min": clean[0],
        "avg": statistics.mean(clean),
        "med": statistics.median(clean),
        "p90": clean[int(n_c * 0.9)],
        "max": clean[-1],
    }

def fmt_stats(name, s, unit="", prec=0):
    """Format stats dict as a compact string."""
    f = f".{prec}f"
    return (f"{name}: min {s['min']:{f}}  avg {s['avg']:{f}}  "
            f"med {s['med']:{f}}  P90 {s['p90']:{f}}  max {s['max']:{f}}{unit}")

st_cpu = compute_stats(cpu_temps)
st_peci = compute_stats(peci_temps)
st_fan = compute_stats(fan_rpms)
st_sen2 = compute_stats(board_sen2)
st_sen3 = compute_stats(board_sen3)
st_sen4 = compute_stats(board_sen4)
st_sen5 = compute_stats(board_sen5)
st_load = compute_stats(load_1m)
st_active = compute_stats(cpu_active)
st_pl1 = compute_stats(pl1_w)
st_ctrl_pl1 = compute_stats(ctrl_pl1) if has_ctrl else None

for name, st, unit, prec in [
    ("CPU Pkg", st_cpu, "\u00b0C", 1), ("PECI", st_peci, "\u00b0C", 1),
    ("SEN5",    st_sen5, "\u00b0C", 1), ("SEN3", st_sen3, "\u00b0C", 1),
    ("SEN2",    st_sen2, "\u00b0C", 1), ("SEN4", st_sen4, "\u00b0C", 1),
    ("Fan",     st_fan, " RPM", 0), ("PL1", st_pl1, "W", 1),
    ("Ctrl PL1", st_ctrl_pl1, "W", 1),
    ("Load1m", st_load, "", 1), ("CPU Act", st_active, "%", 1),
]:
    if st:
        print(fmt_stats(name, st, unit, prec))

# --- Rolling average helper ---
def rolling_avg(ts_list, val_list, window_secs):
    """Compute rolling average over a time window. Skips None values.
    Uses O(n) sliding window with running sum."""
    if window_secs <= 0:
        return [], []
    window = timedelta(seconds=window_secs)
    out_ts, out_val = [], []
    win_sum = 0.0
    win_count = 0
    left = 0
    for right, t in enumerate(ts_list):
        # Add right edge
        v = val_list[right]
        if v is not None:
            win_sum += v
            win_count += 1
        # Shrink left edge
        cutoff = t - window
        while left < right and ts_list[left] < cutoff:
            v_left = val_list[left]
            if v_left is not None:
                win_sum -= v_left
                win_count -= 1
            left += 1
        if win_count > 0:
            out_ts.append(t)
            out_val.append(win_sum / win_count)
    return out_ts, out_val

# Stats table formatter — produces aligned tabular output
def stats_table(rows):
    """Format [(name, stats_dict, unit, prec), ...] as aligned columns."""
    if not rows:
        return ""
    # Format all values first to find max width per column
    col_names = ["min", "avg", "med", "P90", "max"]
    col_keys  = ["min", "avg", "med", "p90", "max"]
    formatted = []  # list of (name_str, [val_str, ...], unit)
    for name, s, unit, prec in rows:
        vals = [f"{s[k]:.{prec}f}" for k in col_keys]
        formatted.append((name, vals, unit))
    # Compute column widths: max of header label and all values in that column
    name_w = max(len(r[0]) for r in formatted)
    name_w = max(name_w, 4)  # minimum for header gap
    col_w = []
    for ci, cn in enumerate(col_names):
        w = len(cn)
        for _, vals, _ in formatted:
            w = max(w, len(vals[ci]))
        col_w.append(w)
    unit_w = max((len(r[2]) for r in formatted), default=0)
    # Build header
    hdr = " " * name_w + "  "
    hdr += "  ".join(cn.rjust(col_w[ci]) for ci, cn in enumerate(col_names))
    if unit_w:
        hdr += " " * (unit_w + 1)
    lines = [hdr]
    # Build data rows
    for name, vals, unit in formatted:
        row = name.rjust(name_w) + "  "
        row += "  ".join(vals[ci].rjust(col_w[ci]) for ci in range(len(col_names)))
        if unit:
            row += " " + unit.ljust(unit_w)
        else:
            row += " " * (unit_w + 1)
        lines.append(row)
    return "\n".join(lines)

# --- Boolean-to-numeric helper ---
def bool_to_numeric(bool_list):
    """Convert boolean list to numeric for plotting. True->1, False->0, None->None."""
    return [1.0 if v is True else 0.0 if v is False else None for v in bool_list]

def plot_flags(ax, ts_list, flag_series, rolling_secs_val, rolling_label, show_raw_val, lw_val):
    """Plot boolean flags as 0/1 step lines on a right twin axis.
    flag_series: [(bool_list, label, color), ...]"""
    if not flag_series:
        return None
    ax_fl = ax.twinx()
    for bool_list, label, color in flag_series:
        num = bool_to_numeric(bool_list)
        if show_raw_val:
            clean = [v if v is not None else float("nan") for v in num]
            ax_fl.plot(ts_list, clean, drawstyle="steps-post",
                       color=color, linewidth=lw_val * 0.8, alpha=0.45, label=label)
        else:
            rt, rv = rolling_avg(ts_list, num, rolling_secs_val)
            ax_fl.plot(rt, rv, color=color, linewidth=lw_val, alpha=0.55,
                       label=f"{label} ({rolling_label})")
    ax_fl.set_ylim(-0.15, 1.5)
    ax_fl.set_yticks([0, 1])
    ax_fl.tick_params(axis="y", labelsize=7, colors="#999999")
    return ax_fl

# --- Plot ---
# Use GridSpec: 5 panels + 5 stats gaps = 10 rows
from matplotlib.gridspec import GridSpec
fig = plt.figure(figsize=(14, 18))
gs = GridSpec(10, 1, figure=fig,
              height_ratios=[2, 0.35, 2.5, 0.35, 2, 0.35, 2.5, 0.35, 2, 0.4],
              hspace=0.05)

ax_fan   = fig.add_subplot(gs[0])
ax_st1   = fig.add_subplot(gs[1])   # stats gap 1
ax_cpu   = fig.add_subplot(gs[2], sharex=ax_fan)
ax_st2   = fig.add_subplot(gs[3])   # stats gap 2
ax_pl1   = fig.add_subplot(gs[4], sharex=ax_fan)
ax_st3   = fig.add_subplot(gs[5])   # stats gap 3
ax_board = fig.add_subplot(gs[6], sharex=ax_fan)
ax_st4   = fig.add_subplot(gs[7])   # stats gap 4
ax_load  = fig.add_subplot(gs[8], sharex=ax_fan)
ax_st5   = fig.add_subplot(gs[9])   # stats gap 5

# Hide stats gap axes (used only for text)
for ax_gap in (ax_st1, ax_st2, ax_st3, ax_st4, ax_st5):
    ax_gap.set_axis_off()

show_raw = rolling_secs <= 0
lw, alpha = (0.8, 0.85) if show_raw else (1.5, 0.9)

legend_ncol = 4
legend_fs = 8
legend_loc = "lower left"

def padded_ylim(lo, hi, pad_top=0.10, pad_bot=0.18):
    """Add pad fraction of range to top and bottom."""
    span = hi - lo
    return lo - span * pad_bot, hi + span * pad_top

colors = {
    # Panel 2: CPU temperature data
    "pkg":       "#d62728",  # red
    "peci":      "#ff7f0e",  # orange
    "ctrl_med":  "#9467bd",  # purple — controller median temp
    # Panel 3: PL1 power data
    "pl1":       "#2ca02c",  # green
    "ctrl_pl1":  "#17becf",  # cyan — controller commanded PL1
    # Panels 2+3: boolean flags (must avoid red/orange/purple/green/cyan)
    "guard":     "#cc7777",  # muted salmon — SEN5 guard
    "idle":      "#888888",  # gray — idle ceiling
    "epp":       "#daa520",  # goldenrod — EPP idle
    # Panel 1: fan
    "fan":       "#1f77b4",  # blue
    # Panel 4: board sensors
    "sen5":      "#e41a1c",  # bright red (hottest)
    "sen3":      "#984ea3",  # purple
    "sen2":      "#4daf4a",  # green
    "sen4":      "#377eb8",  # steel blue (battery, coolest)
    # Panel 5: system load
    "load":      "#8c564b",  # brown
    "lavg":      "#e377c2",  # pink
}

# == Panel 1: Fan RPM ==
if show_raw:
    fan_clean = [v if v is not None else float("nan") for v in fan_rpms]
    ax_fan.plot(timestamps, fan_clean, color=colors["fan"], linewidth=lw,
                alpha=alpha, label="Fan RPM")
else:
    rt, rv = rolling_avg(timestamps, fan_rpms, rolling_secs)
    ax_fan.plot(rt, rv, color=colors["fan"], linewidth=lw, alpha=alpha,
                label=f"Fan ({rolling_arg} avg)")

ax_fan.set_ylabel("Fan Speed (RPM)", color=colors["fan"])
ax_fan.tick_params(axis="y", labelcolor=colors["fan"])
fan_valid = [v for v in fan_rpms if v is not None]
fan_max = max(fan_valid) if fan_valid else 8500
ax_fan.set_ylim(*padded_ylim(0, max(fan_max, 1000)))
ax_fan.grid(True, alpha=0.3)
ax_fan.legend(loc=legend_loc, fontsize=legend_fs, ncol=legend_ncol)
plt.setp(ax_fan.get_xticklabels(), visible=False)

# Stats below panel 1
st1_rows = []
if st_fan:
    st1_rows.append(("Fan", st_fan, "RPM", 0))
if st1_rows:
    ax_st1.text(0.5, 0.5, stats_table(st1_rows),
                transform=ax_st1.transAxes, fontsize=7, family="monospace",
                va="center", ha="center", color="#333333",
                bbox=dict(boxstyle="round,pad=0.4", fc="#f0f0f0", alpha=0.9,
                          ec="#cccccc", lw=0.5))

# == Panel 2: CPU Temperature ==
if show_raw:
    ax_cpu.plot(timestamps, cpu_temps, color=colors["pkg"], linewidth=lw,
                alpha=alpha, label="Package")
    peci_clean = [v if v is not None else float("nan") for v in peci_temps]
    ax_cpu.plot(timestamps, peci_clean, color=colors["peci"], linewidth=lw * 0.7,
                alpha=alpha * 0.7, label="PECI")
    if has_ctrl:
        ctrl_med_clean = [v if v is not None else float("nan") for v in ctrl_median]
        ax_cpu.plot(timestamps, ctrl_med_clean, color=colors["ctrl_med"],
                    linewidth=lw, alpha=alpha, label="Ctrl median")
else:
    rt, rv = rolling_avg(timestamps, cpu_temps, rolling_secs)
    ax_cpu.plot(rt, rv, color=colors["pkg"], linewidth=lw, alpha=alpha,
                label=f"Pkg ({rolling_arg} avg)")
    rt, rv = rolling_avg(timestamps, peci_temps, rolling_secs)
    ax_cpu.plot(rt, rv, color=colors["peci"], linewidth=lw, alpha=alpha,
                label=f"PECI ({rolling_arg} avg)")
    if has_ctrl:
        rt, rv = rolling_avg(timestamps, ctrl_median, rolling_secs)
        ax_cpu.plot(rt, rv, color=colors["ctrl_med"], linewidth=lw, alpha=alpha,
                    label=f"Ctrl med ({rolling_arg} avg)")

# Guard/idle/EPP flags as 0/1 step lines on right axis
flag_series_2 = []
if has_ctrl:
    flag_series_2.append((ctrl_guard, "SEN5 guard", colors["guard"]))
    flag_series_2.append((ctrl_idle, "Idle ceiling", colors["idle"]))
if has_epp:
    flag_series_2.append((ctrl_epp, "EPP idle", colors["epp"]))
ax_fl2 = plot_flags(ax_cpu, timestamps, flag_series_2, rolling_secs, rolling_arg, show_raw, lw)

# Setpoint reference line
ax_cpu.axhline(setpoint_c, color="#888888", linestyle="--", linewidth=0.8,
               alpha=0.6)
ax_cpu.text(timestamps[0], setpoint_c + 0.3, f" setpoint {setpoint_c:.0f}\u00b0C",
            fontsize=6.5, color="#888888", alpha=0.8, va="bottom")

ax_cpu.set_ylabel("CPU Temperature (\u00b0C)")
ax_cpu.set_ylim(*padded_ylim(35, 105))
ax_cpu.grid(True, alpha=0.3)
lines_c, labels_c = ax_cpu.get_legend_handles_labels()
if ax_fl2:
    lines_f, labels_f = ax_fl2.get_legend_handles_labels()
    lines_c, labels_c = lines_c + lines_f, labels_c + labels_f
ax_cpu.legend(lines_c, labels_c, loc=legend_loc, fontsize=legend_fs, ncol=legend_ncol)
plt.setp(ax_cpu.get_xticklabels(), visible=False)

# Stats below panel 2
st2_rows = []
if st_cpu:
    st2_rows.append(("Pkg", st_cpu, "\u00b0C", 1))
if st_peci:
    st2_rows.append(("PECI", st_peci, "\u00b0C", 1))
if st2_rows:
    ax_st2.text(0.5, 0.5, stats_table(st2_rows),
                transform=ax_st2.transAxes, fontsize=7, family="monospace",
                va="center", ha="center", color="#333333",
                bbox=dict(boxstyle="round,pad=0.4", fc="#f0f0f0", alpha=0.9,
                          ec="#cccccc", lw=0.5))

# == Panel 3: PL1 Power ==
has_pl1 = any(v is not None for v in pl1_w)
if has_pl1:
    if show_raw:
        pl1_clean = [v if v is not None else float("nan") for v in pl1_w]
        ax_pl1.plot(timestamps, pl1_clean, color=colors["pl1"], linewidth=lw,
                    alpha=alpha, label="RAPL PL1")
    else:
        rt, rv = rolling_avg(timestamps, pl1_w, rolling_secs)
        ax_pl1.plot(rt, rv, color=colors["pl1"], linewidth=lw, alpha=alpha,
                    label=f"PL1 ({rolling_arg} avg)")

# Controller commanded PL1
if has_ctrl:
    if show_raw:
        ctrl_pl1_clean = [v if v is not None else float("nan") for v in ctrl_pl1]
        ax_pl1.plot(timestamps, ctrl_pl1_clean, color=colors["ctrl_pl1"],
                    linewidth=lw, alpha=alpha, label="Ctrl PL1")
    else:
        rt, rv = rolling_avg(timestamps, ctrl_pl1, rolling_secs)
        ax_pl1.plot(rt, rv, color=colors["ctrl_pl1"], linewidth=lw, alpha=alpha,
                    label=f"Ctrl PL1 ({rolling_arg} avg)")

# Guard/idle/EPP flags as 0/1 step lines on right axis
flag_series_3 = []
if has_ctrl:
    flag_series_3.append((ctrl_guard, "SEN5 guard", colors["guard"]))
    flag_series_3.append((ctrl_idle, "Idle ceiling", colors["idle"]))
if has_epp:
    flag_series_3.append((ctrl_epp, "EPP idle", colors["epp"]))
ax_fl3 = plot_flags(ax_pl1, timestamps, flag_series_3, rolling_secs, rolling_arg, show_raw, lw)

# Idle ceiling reference line
ax_pl1.axhline(idle_ceiling_w, color="#888888", linestyle="--", linewidth=0.8,
               alpha=0.6)
ax_pl1.text(timestamps[0], idle_ceiling_w + 0.3,
            f" idle ceiling {idle_ceiling_w:.0f}W",
            fontsize=6.5, color="#888888", alpha=0.8, va="bottom")

ax_pl1.set_ylabel("PL1 Power (W)", color=colors["pl1"])
ax_pl1.tick_params(axis="y", labelcolor=colors["pl1"])
ax_pl1.set_ylim(*padded_ylim(0, 32))
ax_pl1.grid(True, alpha=0.3)
lines_p, labels_p = ax_pl1.get_legend_handles_labels()
if ax_fl3:
    lines_f, labels_f = ax_fl3.get_legend_handles_labels()
    lines_p, labels_p = lines_p + lines_f, labels_p + labels_f
ax_pl1.legend(lines_p, labels_p, loc=legend_loc, fontsize=legend_fs, ncol=legend_ncol)
plt.setp(ax_pl1.get_xticklabels(), visible=False)

# Stats below panel 3
st3_rows = []
if st_pl1:
    st3_rows.append(("PL1", st_pl1, "W", 1))
if st_ctrl_pl1:
    st3_rows.append(("Ctrl PL1", st_ctrl_pl1, "W", 1))
if st3_rows:
    ax_st3.text(0.5, 0.5, stats_table(st3_rows),
                transform=ax_st3.transAxes, fontsize=7, family="monospace",
                va="center", ha="center", color="#333333",
                bbox=dict(boxstyle="round,pad=0.4", fc="#f0f0f0", alpha=0.9,
                          ec="#cccccc", lw=0.5))

# == Panel 4: Board temperatures (EC sensors) ==
board_series = [
    (board_sen5, "SEN5 (CPU/VRM)", colors["sen5"]),
    (board_sen3, "SEN3 (DDR)",     colors["sen3"]),
    (board_sen2, "SEN2 (local)",   colors["sen2"]),
    (board_sen4, "SEN4 (battery)", colors["sen4"]),
]
for data_list, label, color in board_series:
    if not any(v is not None for v in data_list):
        continue
    if show_raw:
        clean = [v if v is not None else float("nan") for v in data_list]
        ax_board.plot(timestamps, clean, color=color, linewidth=lw,
                      alpha=alpha, label=label)
    else:
        short = label.split(" ")[0]  # "SEN5"
        rt, rv = rolling_avg(timestamps, data_list, rolling_secs)
        ax_board.plot(rt, rv, color=color, linewidth=lw, alpha=alpha,
                      label=f"{short} ({rolling_arg} avg)")

# EC trip-point reference lines
trip_lines = [
    (80, "critical", "#d62728", "-"),
    (75, "hot",      "#ff7f0e", "--"),
    (65, "passive",  "#bcbd22", "--"),
    (60, "active",   "#17becf", ":"),
]
for temp_val, label, color, ls in trip_lines:
    ax_board.axhline(temp_val, color=color, linestyle=ls, linewidth=0.8,
                     alpha=0.6)
    ax_board.text(timestamps[0], temp_val + 0.3, f" {label} {temp_val}\u00b0C",
                  fontsize=6.5, color=color, alpha=0.8, va="bottom")

ax_board.set_ylabel("Board Temperature (\u00b0C)")
ax_board.set_ylim(*padded_ylim(25, 85))
ax_board.legend(loc=legend_loc, fontsize=legend_fs, ncol=legend_ncol)
ax_board.grid(True, alpha=0.3)
plt.setp(ax_board.get_xticklabels(), visible=False)

# Stats below panel 4
st4_rows = []
for name, st in [("SEN5", st_sen5), ("SEN3", st_sen3),
                 ("SEN2", st_sen2), ("SEN4", st_sen4)]:
    if st:
        st4_rows.append((name, st, "\u00b0C", 1))
if st4_rows:
    ax_st4.text(0.5, 0.5, stats_table(st4_rows),
                transform=ax_st4.transAxes, fontsize=7, family="monospace",
                va="center", ha="center", color="#333333",
                bbox=dict(boxstyle="round,pad=0.4", fc="#f0f0f0", alpha=0.9,
                          ec="#cccccc", lw=0.5))

# == Panel 5: System Load ==
if show_raw:
    active_clean = [v if v is not None else float("nan") for v in cpu_active]
    ax_load.fill_between(timestamps, 0, active_clean, color=colors["load"],
                         alpha=0.15)
    ax_load.plot(timestamps, active_clean, color=colors["load"], linewidth=lw,
                 alpha=0.7, label="CPU active %")
else:
    rt, rv = rolling_avg(timestamps, cpu_active, rolling_secs)
    ax_load.plot(rt, rv, color=colors["load"], linewidth=lw, alpha=alpha,
                 label=f"CPU act ({rolling_arg} avg)")
ax_load.set_ylabel("CPU Active %", color=colors["load"])
ax_load.tick_params(axis="y", labelcolor=colors["load"])
ax_load.set_ylim(*padded_ylim(0, 100))
ax_load.grid(True, alpha=0.3)

# Load avg on right axis
ax_lavg = ax_load.twinx()
if show_raw:
    load_clean = [v if v is not None else float("nan") for v in load_1m]
    ax_lavg.plot(timestamps, load_clean, color=colors["lavg"], linewidth=lw,
                 alpha=0.7, label="Load avg (1m)")
else:
    rt, rv = rolling_avg(timestamps, load_1m, rolling_secs)
    ax_lavg.plot(rt, rv, color=colors["lavg"], linewidth=lw, alpha=alpha,
                 label=f"Load 1m ({rolling_arg} avg)")
ax_lavg.set_ylabel("Load Avg (1m)", color=colors["lavg"])
ax_lavg.tick_params(axis="y", labelcolor=colors["lavg"])
load_max = max((v for v in load_1m if v is not None), default=1)
ax_lavg.set_ylim(*padded_ylim(0, max(load_max * 1.1, 1)))

lines_l, labels_l = ax_load.get_legend_handles_labels()
lines_r, labels_r = ax_lavg.get_legend_handles_labels()
ax_load.legend(lines_l + lines_r, labels_l + labels_r,
               loc=legend_loc, fontsize=legend_fs, ncol=legend_ncol)

ax_load.set_xlabel("Time")

# Stats below panel 5
st5_rows = []
if st_active:
    st5_rows.append(("CPU Act", st_active, "%", 1))
if st_load:
    st5_rows.append(("Load 1m", st_load, "", 1))
if st5_rows:
    ax_st5.text(0.5, 0.5, stats_table(st5_rows),
                transform=ax_st5.transAxes, fontsize=7, family="monospace",
                va="center", ha="center", color="#333333",
                bbox=dict(boxstyle="round,pad=0.4", fc="#f0f0f0", alpha=0.9,
                          ec="#cccccc", lw=0.5))

# == X-axis formatting ==
if hours <= 3:
    major_interval = 15
elif hours <= 12:
    major_interval = 60
elif hours <= 48:
    major_interval = 120
else:
    major_interval = 360
ax_load.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=local_tz))
ax_load.xaxis.set_major_locator(mdates.MinuteLocator(interval=major_interval))
fig.autofmt_xdate(rotation=45)

# == Title ==
title = f"System Thermal Dashboard \u2014 last {hours:.0f}h"
if rolling_secs > 0:
    title += f" (rolling avg: {rolling_arg})"
fig.suptitle(title, fontsize=13, fontweight="bold")

fig.savefig(output, dpi=150, bbox_inches="tight")
print(output)
PYEOF

# Open in viewer unless user specified an explicit output path or no display
if [[ "$OUTPUT_EXPLICIT" = false ]] && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$OUTPUT" >/dev/null 2>&1 &
    fi
fi
