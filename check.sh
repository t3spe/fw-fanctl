#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 t3spe
set -euo pipefail

# Verify fw-pwrctl installation and runtime state.
# Usage: sudo bash check.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB_DIR="/usr/local/lib/fw-pwrctl"
CONF_DIR="/etc/fw-pwrctl"
# Python import path: prefer repo checkout, fall back to installed copy
if [ -f "$SCRIPT_DIR/fw_pwrctl.py" ]; then
    PYTHON_IMPORT_DIR="$SCRIPT_DIR"
elif [ -f "$LIB_DIR/fw_pwrctl.py" ]; then
    PYTHON_IMPORT_DIR="$LIB_DIR"
else
    PYTHON_IMPORT_DIR=""
fi
export _PWRCTL_IMPORT_DIR="$PYTHON_IMPORT_DIR"
export _PWRCTL_CONF_DIR="$CONF_DIR"
SERVICE="fw-pwrctl"
ECTOOL="/usr/local/bin/ectool"
RAPL_PL1="/sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw"

if [[ $EUID -ne 0 ]]; then
    echo "Warning: running without root — some checks will fail"
    echo "  Recommended: sudo bash check.sh"
    echo ""
fi

ERRORS=0

ok() { echo "  OK  $1"; }
fail() { echo "  FAIL  $1"; ERRORS=$((ERRORS + 1)); }
warn() { echo "  WARN  $1"; }
info() { echo "  ---  $1"; }

# --- Installed files ---
echo "Files:"
if [ -x "$ECTOOL" ]; then ok "ectool"; else fail "ectool not found at $ECTOOL"; fi
if [ -f "$LIB_DIR/fw_pwrctl.py" ]; then ok "daemon"; else fail "daemon not at $LIB_DIR/fw_pwrctl.py"; fi
if [ -f "$CONF_DIR/config.json" ]; then ok "config"; else fail "config not at $CONF_DIR/config.json"; fi
if [ -f "/etc/systemd/system/$SERVICE.service" ]; then ok "unit"; else fail "systemd unit missing"; fi
if [ -d "/var/log/fw-pwrctl" ]; then
    if [ -w "/var/log/fw-pwrctl" ]; then ok "log dir"; else warn "log dir not writable"; fi
else
    warn "log dir /var/log/fw-pwrctl missing (install.sh creates it)"
fi

# --- Files match repo ---
echo "Repo sync:"
if [ -f "$SCRIPT_DIR/fw_pwrctl.py" ]; then
    if [ -f "$LIB_DIR/fw_pwrctl.py" ]; then
        if diff -q "$SCRIPT_DIR/fw_pwrctl.py" "$LIB_DIR/fw_pwrctl.py" >/dev/null 2>&1; then
            ok "daemon matches repo"
        else
            warn "daemon differs from repo (re-run install.sh to update)"
        fi
    fi
    if [ -f "$CONF_DIR/config.json" ]; then
        if diff -q "$SCRIPT_DIR/config.json" "$CONF_DIR/config.json" >/dev/null 2>&1; then
            ok "config matches repo"
        else
            info "config differs from repo (customized)"
        fi
    fi
else
    info "repo checkout not found, skipping sync checks"
fi

# --- Config validation ---
echo "Config:"
if [ -f "$CONF_DIR/config.json" ]; then
    if python3 -c "import json, os; json.load(open(os.path.join(os.environ['_PWRCTL_CONF_DIR'], 'config.json')))" 2>/dev/null; then
        ok "valid JSON"
    else
        fail "invalid JSON"
    fi
    if [ -n "$PYTHON_IMPORT_DIR" ]; then
        # Check exit code (0 = valid), not stderr — validate_config prints
        # non-fatal WARNINGs to stderr for unknown keys etc.
        validate_err=$(python3 -c "
import sys, json, os
sys.path.insert(0, os.environ['_PWRCTL_IMPORT_DIR'])
from fw_pwrctl import validate_config
validate_config(json.load(open(os.path.join(os.environ['_PWRCTL_CONF_DIR'], 'config.json'))))
" 2>&1)
        validate_rc=$?
        if [ $validate_rc -eq 0 ]; then
            ok "passes validation"
        else
            fail "fails validation: $validate_err"
        fi
    else
        info "skipping validation (fw_pwrctl.py not found)"
    fi
fi

# --- Service state ---
echo "Service:"
enabled_state=$(systemctl is-enabled "$SERVICE" 2>/dev/null) || true
if [ "$enabled_state" = "enabled" ] || [ "$enabled_state" = "enabled-runtime" ]; then
    ok "enabled"
elif [ "$enabled_state" = "masked" ]; then
    fail "masked (systemctl unmask $SERVICE)"
else
    warn "not enabled (run: sudo systemctl enable fw-pwrctl)"
fi
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    ok "running"
    pid=$(systemctl show "$SERVICE" --property=MainPID --value)
    if [ -n "$pid" ] && [ "$pid" != "0" ]; then
        elapsed=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')
        if [ -n "$elapsed" ] && [ "$elapsed" -gt 0 ]; then
            info "uptime ${elapsed}s (pid $pid)"
        fi
    fi
else
    warn "not running (run: sudo systemctl enable --now fw-pwrctl)"
fi

# --- Conflicting services ---
echo "Conflicts:"
for svc in thermald auto-cpufreq power-profiles-daemon tlp; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        warn "$svc is running (may conflict — see FAQ)"
    else
        ok "$svc not running"
    fi
done

# --- lm-sensors ---
echo "Dependencies:"
if command -v sensors >/dev/null 2>&1; then
    ok "lm-sensors installed"
else
    warn "lm-sensors not installed (sensor logging will omit hardware sensor data)"
fi

# --- Sensors ---
echo "Sensors:"
if [ -n "$PYTHON_IMPORT_DIR" ]; then
    # coretemp
    ct_path=$(python3 -c "
import sys, os; sys.path.insert(0, os.environ['_PWRCTL_IMPORT_DIR'])
from fw_pwrctl import Hardware
p = Hardware().find_coretemp_sensor()
if p: print(p)
" 2>/dev/null)
    if [ -n "$ct_path" ] && [ -r "$ct_path" ]; then
        ct_val=$(awk '{printf "%.1f", $1/1000}' "$ct_path")
        ok "coretemp: ${ct_val}C  ($ct_path)"
    else
        fail "coretemp not found"
    fi

    # SEN5
    sen5_path=$(python3 -c "
import sys, os; sys.path.insert(0, os.environ['_PWRCTL_IMPORT_DIR'])
from fw_pwrctl import Hardware
p = Hardware().find_sen5_sensor()
if p: print(p)
" 2>/dev/null)
    if [ -n "$sen5_path" ] && [ -r "$sen5_path" ]; then
        sen5_val=$(awk '{printf "%.1f", $1/1000}' "$sen5_path")
        ok "SEN5: ${sen5_val}C  ($sen5_path)"
    else
        warn "SEN5 not found (guard disabled)"
    fi
else
    info "skipping sensor discovery (fw_pwrctl.py not found)"
fi

# --- RAPL PL1 ---
echo "RAPL:"
if [ -r "$RAPL_PL1" ]; then
    pl1_uw=$(< "$RAPL_PL1")
    # NB: -v passes the shell variable safely; the awk calls in the Sensors
    # section use $1 in single-quoted programs (awk's field ref, not shell).
    pl1_w=$(awk -v raw="$pl1_uw" 'BEGIN {printf "%.1f", raw/1000000}')
    ok "PL1: ${pl1_w}W"
else
    fail "cannot read $RAPL_PL1"
fi
if [ -w "$RAPL_PL1" ]; then
    ok "PL1 writable"
else
    fail "PL1 not writable (need root)"
fi

# --- ectool ---
echo "ectool:"
if [ -x "$ECTOOL" ]; then
    ec_ver=$("$ECTOOL" --interface=dev version 2>/dev/null | head -1) || true
    if [ -n "$ec_ver" ]; then
        ok "ectool works ($ec_ver)"
    else
        fail "ectool exists but cannot communicate with EC (need root?)"
    fi
fi

# --- Fan RPM ---
echo "Fan:"
if [ -x "$ECTOOL" ]; then
    rpm=$("$ECTOOL" --interface=dev pwmgetfanrpm 2>/dev/null | sed -n 's/.*RPM:[[:space:]]*\([0-9]*\).*/\1/p') || true
    if [ -n "$rpm" ]; then
        ok "fan: ${rpm} RPM"
    else
        warn "could not read fan RPM"
    fi
fi

# --- Summary ---
echo ""
if [ "$ERRORS" -eq 0 ]; then
    echo "All checks passed."
else
    echo "$ERRORS check(s) failed."
    exit 1
fi
