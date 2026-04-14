#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 t3spe
set -euo pipefail

# Install fw-pwrctl daemon to system paths:
#   /usr/local/lib/fw-pwrctl/fw_pwrctl.py
#   /etc/fw-pwrctl/config.json
#   /etc/systemd/system/fw-pwrctl.service
#
# Usage:
#   bash install.sh              # install / upgrade
#   bash install.sh --uninstall  # remove

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB_DIR="/usr/local/lib/fw-pwrctl"
CONF_DIR="/etc/fw-pwrctl"
SERVICE="/etc/systemd/system/fw-pwrctl.service"
ECTOOL="/usr/local/bin/ectool"

usage() {
    echo "Usage: $0 [--uninstall]"
    exit 1
}

# Validate arguments
if [ $# -gt 1 ]; then usage; fi
if [ $# -eq 1 ] && [ "$1" != "--uninstall" ]; then usage; fi

# Validate sudo access once upfront (installs daemon, config, and systemd unit)
if ! sudo -v; then
    echo "error: sudo access required"
    exit 1
fi

# --- Uninstall ---
if [ "${1:-}" = "--uninstall" ]; then
    echo "Uninstalling fw-pwrctl..."
    sudo systemctl stop fw-pwrctl 2>/dev/null || true
    sudo systemctl disable fw-pwrctl 2>/dev/null || true
    sudo rm -f "$SERVICE"
    # Guard: only rm -rf if the path looks right
    if [ -d "$LIB_DIR" ] && [ "$LIB_DIR" = "/usr/local/lib/fw-pwrctl" ]; then
        sudo rm -rf "$LIB_DIR"
    fi
    sudo systemctl daemon-reload
    echo "Removed service and daemon."
    echo "Config left in $CONF_DIR (remove manually if desired)."
    echo "Sensor logs left in /var/log/fw-pwrctl (remove manually if desired)."
    echo "Done."
    exit 0
fi

# --- Install ---

# Preflight: systemd
if ! command -v systemctl >/dev/null 2>&1; then
    echo "error: systemd not found (systemctl missing)"
    echo "  The installer requires systemd to manage the service."
    echo "  You can still run the daemon manually: sudo python3 fw_pwrctl.py"
    exit 1
fi

# Preflight: ectool
if [ ! -f "$ECTOOL" ]; then
    echo "error: ectool not found at $ECTOOL"
    echo "Run install-ectool.sh first."
    exit 1
fi

# Preflight: source files
for f in fw_pwrctl.py config.json fw-pwrctl.service.template; do
    if [ ! -f "$SCRIPT_DIR/$f" ]; then
        echo "error: $f not found in $SCRIPT_DIR"
        exit 1
    fi
done

# Pass SCRIPT_DIR to Python via env var (avoids quoting/injection issues)
export _PWRCTL_DIR="$SCRIPT_DIR"

# Preflight: Python 3.10+ is required
if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 not found — install it with: sudo apt install python3"
    exit 1
fi
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
    py_ver=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    echo "error: Python 3.10+ required (found $py_ver)"
    exit 1
fi

# Preflight: verify Python syntax before installing
if ! python3 -c "import py_compile, os; py_compile.compile(os.path.join(os.environ['_PWRCTL_DIR'], 'fw_pwrctl.py'), doraise=True)" 2>/dev/null; then
    echo "error: fw_pwrctl.py has syntax errors"
    exit 1
fi

# Preflight: verify config is valid JSON
if ! python3 -c "import json, os; json.load(open(os.path.join(os.environ['_PWRCTL_DIR'], 'config.json')))" 2>/dev/null; then
    echo "error: config.json is not valid JSON"
    exit 1
fi

# Preflight: validate config semantics (catches bad values before deploying)
VALIDATE_ERR=$(python3 -c "
import json, sys, os
d = os.environ['_PWRCTL_DIR']
sys.path.insert(0, d)
from fw_pwrctl import validate_config
with open(os.path.join(d, 'config.json')) as f:
    validate_config(json.load(f))
" 2>&1) || {
    echo "error: config validation failed: $VALIDATE_ERR"
    exit 1
}

# Preflight: verify lm-sensors is available (optional, logging degrades gracefully)
if ! command -v sensors >/dev/null 2>&1; then
    echo "warning: lm-sensors not installed — sensor logging will omit hardware sensor data"
fi

# Track if service was running (to restart after upgrade)
WAS_RUNNING=false
if systemctl is-active --quiet fw-pwrctl 2>/dev/null; then
    WAS_RUNNING=true
    echo "Stopping existing fw-pwrctl service..."
    sudo systemctl stop fw-pwrctl
fi

# Create sensor log directory
sudo mkdir -p /var/log/fw-pwrctl

# Install daemon
echo "Installing daemon to $LIB_DIR..."
sudo mkdir -p "$LIB_DIR"
sudo cp "$SCRIPT_DIR/fw_pwrctl.py" "$LIB_DIR/fw_pwrctl.py.new"
sudo chmod 755 "$LIB_DIR/fw_pwrctl.py.new"
sudo mv "$LIB_DIR/fw_pwrctl.py.new" "$LIB_DIR/fw_pwrctl.py"

# Install config (preserve existing customizations)
echo "Installing config to $CONF_DIR..."
sudo mkdir -p "$CONF_DIR"
if [ -f "$CONF_DIR/config.json" ]; then
    if diff -q "$SCRIPT_DIR/config.json" "$CONF_DIR/config.json" >/dev/null 2>&1; then
        echo "  Config unchanged."
    else
        echo "  Existing config differs from repo (keeping yours)."
        echo "  Repo default saved to config.json.new for reference."
        echo "  Tip: diff $CONF_DIR/config.json $CONF_DIR/config.json.new"
        sudo cp "$SCRIPT_DIR/config.json" "$CONF_DIR/config.json.new"
        # Validate the existing config so the user knows before restarting
        export _PWRCTL_CONF_DIR="$CONF_DIR"
        if ! existing_err=$(python3 -c "
import json, sys, os
d = os.environ['_PWRCTL_DIR']
sys.path.insert(0, d)
from fw_pwrctl import validate_config
with open(os.path.join(os.environ['_PWRCTL_CONF_DIR'], 'config.json')) as f:
    validate_config(json.load(f))
" 2>&1); then
            echo "  WARNING: existing config has validation errors:"
            echo "    $existing_err"
            echo "  The daemon may not start until the config is fixed."
        fi
    fi
else
    sudo cp "$SCRIPT_DIR/config.json" "$CONF_DIR/config.json"
fi

# Install service unit (substitute template variables)
echo "Installing systemd service..."
sed -e "s|@LIB_DIR@|$LIB_DIR|g" \
    -e "s|@CONF_DIR@|$CONF_DIR|g" \
    -e "s|@ECTOOL@|$ECTOOL|g" \
    -e '/^# Substituted by/,/^$/d' \
    "$SCRIPT_DIR/fw-pwrctl.service.template" | sudo tee "$SERVICE" >/dev/null

sudo systemctl daemon-reload

# Restart if it was running before (upgrade path)
if [ "$WAS_RUNNING" = true ]; then
    echo "Restarting fw-pwrctl service..."
    sudo systemctl start fw-pwrctl
fi

echo ""
echo "Installed."
echo ""
echo "Commands:"
echo "  sudo systemctl enable --now fw-pwrctl   # enable and start"
echo "  sudo bash check.sh                       # verify installation"
echo "  sudo systemctl status fw-pwrctl          # check status"
echo "  journalctl -u fw-pwrctl -f               # follow logs"
echo ""
echo "Uninstall:"
echo "  bash install.sh --uninstall"
