#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 t3spe
set -euo pipefail

# install-ectool.sh — Install ectool, the ChromeOS Embedded Controller tool.
#
# ectool communicates with Framework Laptop's EC firmware to read board
# sensors, control fan duty, adjust thermal thresholds, etc.
#
# Source: https://gitlab.howett.net/DHowett/ectool (BSD 3-Clause)
# Build system: CMake + Clang + Ninja
#
# Strategy:
#   1. Build from source (full transparency, latest code)
#   2. Fall back to vendored pre-built binary if the build fails
#
# Usage:
#   bash install-ectool.sh          # interactive (prompts for confirmation)
#   bash install-ectool.sh --yes    # non-interactive (for scripted installs)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="/usr/local/bin/ectool"
SOURCE_REPO="https://gitlab.howett.net/DHowett/ectool.git"
VENDORED="$SCRIPT_DIR/vendor/ectool"
VENDORED_SHA256="db51252568d36c93591396e0ff425f9352bf39186bc716de9d4de56acb98877a"
BUILD_DEPS_BIN=(cmake clang ninja-build git pkg-config)
BUILD_DEPS_LIB=(libftdi1-dev libusb-1.0-0-dev)

usage() {
    echo "Usage: $0 [--yes]"
    echo ""
    echo "  --yes   Skip confirmation prompt (for scripted installs)"
    exit 1
}

if [ $# -gt 1 ]; then usage; fi

YES=false
case "${1:-}" in
    "")     ;;
    --yes)  YES=true ;;
    *)      usage ;;
esac

# --- Confirmation prompt ---

if [ "$YES" = false ]; then
    cat <<'EOF'
This script installs ectool to /usr/local/bin/ectool.
Requires sudo for the final install step.

ectool is the ChromeOS Embedded Controller tool, used to communicate
with Framework's EC firmware. It only works on Framework laptops with
Chrome EC — run this script on your Framework Laptop, not another machine.
Source: https://gitlab.howett.net/DHowett/ectool

It will:
  1. Try to build ectool from source (requires git, cmake, clang,
     ninja-build, libftdi1-dev, libusb-1.0-0-dev, pkg-config)
  2. Fall back to the vendored pre-built binary if the build fails

EOF
    read -rp "Proceed? [Y/n] " answer
    case "${answer:-Y}" in
        [Yy]*) ;;
        *) echo "Aborted."; exit 0 ;;
    esac
    echo ""
fi

# --- Cleanup trap ---

BUILD_BINARY=""
BUILD_WORK=""

cleanup() {
    if [ -n "${BUILD_WORK:-}" ]; then
        rm -rf "$BUILD_WORK"
    fi
    # Remove partial install if script exits before the final mv
    if [ -n "${DEST_NEW:-}" ] && [ -f "${DEST_NEW:-}" ]; then
        sudo rm -f "$DEST_NEW" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# --- Build from source ---

build_from_source() {
    echo "==> Building ectool from source..."

    # Check build dependencies
    local missing=()
    # Binary deps: check via command -v
    for dep in "${BUILD_DEPS_BIN[@]}"; do
        if [ "$dep" = "ninja-build" ]; then
            # ninja-build on Debian/Ubuntu, ninja on Arch/Fedora
            if ! command -v ninja-build >/dev/null 2>&1 && ! command -v ninja >/dev/null 2>&1; then
                missing+=("$dep")
            fi
        elif ! command -v "$dep" >/dev/null 2>&1; then
            missing+=("$dep")
        fi
    done
    # Library deps: check via pkg-config (distro-agnostic), fall back to dpkg
    # Explicit mapping: Debian package name → pkg-config module name
    pkg_config_name() {
        case "$1" in
            libftdi1-dev)     echo "libftdi1" ;;
            libusb-1.0-0-dev) echo "libusb-1.0" ;;
            *)                echo "${1%-dev}" ;;
        esac
    }
    for dep in "${BUILD_DEPS_LIB[@]}"; do
        local pc_name
        pc_name="$(pkg_config_name "$dep")"
        if command -v pkg-config >/dev/null 2>&1 && pkg-config --exists "$pc_name" 2>/dev/null; then
            continue
        elif command -v dpkg >/dev/null 2>&1 && dpkg -s "$dep" 2>/dev/null | grep -q '^Status:.*installed'; then
            continue
        fi
        missing+=("$dep")
    done
    if [ ${#missing[@]} -gt 0 ]; then
        echo "    Missing build deps: ${missing[*]}"
        if command -v apt >/dev/null 2>&1; then
            echo "    Install with: sudo apt install ${missing[*]}"
        elif command -v dnf >/dev/null 2>&1; then
            echo "    On Fedora/RHEL: sudo dnf install <equivalent packages>"
            echo "    (names differ: -dev becomes -devel, e.g. libftdi1-devel)"
        elif command -v pacman >/dev/null 2>&1; then
            echo "    On Arch: sudo pacman -S <equivalent packages>"
            echo "    (names differ: e.g. libftdi, libusb)"
        else
            echo "    Install the packages listed above using your package manager."
        fi
        return 1
    fi

    local work
    work="$(mktemp -d)"
    BUILD_WORK="$work"

    echo "    Cloning $SOURCE_REPO..."
    if ! git clone --quiet --depth 1 "$SOURCE_REPO" "$work/ectool"; then
        echo "    Clone failed."
        return 1
    fi

    echo "    Initializing submodules..."
    if ! git -C "$work/ectool" submodule update --init --recursive --quiet; then
        echo "    Submodule init failed."
        return 1
    fi

    local build_log="$work/build.log"

    echo "    Configuring (cmake)..."
    mkdir -p "$work/ectool/_build"
    if ! (cd "$work/ectool/_build" && CC=clang CXX=clang++ cmake -GNinja ..) \
         >"$build_log" 2>&1; then
        echo "    CMake configure failed. Build log:"
        sed 's/^/      /' "$build_log" | tail -20
        return 1
    fi

    echo "    Compiling..."
    if ! cmake --build "$work/ectool/_build" >>"$build_log" 2>&1; then
        echo "    Build failed. Last 20 lines:"
        sed 's/^/      /' "$build_log" | tail -20
        return 1
    fi

    if [ ! -f "$work/ectool/_build/src/ectool" ]; then
        echo "    Build produced no binary."
        return 1
    fi

    echo "    Build succeeded."
    BUILD_BINARY="$work/ectool/_build/src/ectool"
    return 0
}

# --- Vendored fallback ---

use_vendored() {
    echo "==> Falling back to vendored binary..."

    if [ ! -f "$VENDORED" ]; then
        echo "error: vendored binary not found at $VENDORED"
        echo "  Try re-cloning the repository."
        return 1
    fi

    echo "    Verifying SHA256..."
    local actual
    actual="$(sha256sum "$VENDORED" | cut -d' ' -f1)"
    if [ "$actual" != "$VENDORED_SHA256" ]; then
        echo "error: SHA256 mismatch!"
        echo "  expected: $VENDORED_SHA256"
        echo "  actual:   $actual"
        echo "  Try re-cloning the repository, or build from source manually"
        echo "  (see vendor/README.md for build instructions)."
        return 1
    fi
    echo "    SHA256 verified."

    # Check runtime dependencies (the vendored binary is dynamically linked)
    if ! ldconfig -p 2>/dev/null | grep -q libftdi1; then
        echo "    note: libftdi1 not found — install with: sudo apt install libftdi1-2"
    fi

    BUILD_BINARY="$VENDORED"
    BUILD_WORK=""
    return 0
}

# --- Main ---

if build_from_source; then
    SOURCE="source build"
elif use_vendored; then
    SOURCE="vendored binary"
else
    echo ""
    echo "error: both source build and vendored fallback failed."
    exit 1
fi

echo ""
echo "Installing to $DEST ($SOURCE)..."
DEST_NEW="$DEST.new"
sudo cp "$BUILD_BINARY" "$DEST_NEW"
sudo chmod +x "$DEST_NEW"

echo "Verifying..."
if sudo "$DEST_NEW" version; then
    sudo mv "$DEST_NEW" "$DEST"
    DEST_NEW=""  # clear so trap doesn't remove the installed binary
    echo ""
    echo "Done. ectool installed to $DEST"
else
    sudo rm -f "$DEST_NEW"
    DEST_NEW=""
    echo ""
    echo "error: 'ectool version' failed — new binary not installed."
    if [ -f "$DEST" ]; then
        echo "  Previous installation at $DEST is unchanged."
    fi
    echo "  Possible causes:"
    echo "    - Missing library: sudo apt install libftdi1-2"
    echo "    - EC kernel module: sudo modprobe cros_ec"
    echo "    - Not a Framework laptop (ectool requires Chrome EC firmware)"
    exit 1
fi
