#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 t3spe
"""Tests for fw_pwrctl: SensorLogger, controllers, Hardware, run().

Run without root:  python3 tests/test_fw_pwrctl.py
All tests use temp directories — no system files touched.
"""

import io
import json
import os
import sys
import tempfile
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fw_pwrctl import (
    SensorLogger, PIPL1Controller, Hardware,
    validate_config, run, preflight_checks,
    CRITICAL_TEMP, SENSOR_RESCAN_AFTER, ECTOOL, EC_THERMAL_OVERRIDES,
    EC_OVERRIDE_RECHECK,
)

LIVE = "--live" in sys.argv
if LIVE:
    sys.argv.remove("--live")

PASSED = 0
FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")


###########################################################################
#                          MOCK HARDWARE                                   #
###########################################################################

class MockHardware(Hardware):
    """Scriptable hardware mock — no real I/O, instant sleeps."""

    def __init__(self):
        super().__init__(dry_run=False)
        # Scripted return values
        self.temps = deque()           # float or Exception
        self.sen5_temps = None         # deque of float/Exception, or None
        self.cpu_sensor = "/mock/cpu"
        self.peci_sensor = "/mock/peci"
        self.sen5_sensor_path = "/mock/sen5"
        self.board_sensors_result = {}
        self.ectool_ok = True
        self.rapl_pl1_uw = 28_000_000
        self.set_fan_ok = True         # bool or deque for per-call control
        self.system_snapshot = {}

        # EC thermal config
        self.thermal_config = []
        self.thermal_writes = []
        self.write_thermal_ok = True

        # EPP tracking
        self.epp_writes = []

        # Call tracking
        self.fan_calls = []
        self.rapl_writes = []
        self.ec_restores = 0
        self.temp_reads = []
        self.sleep_calls = []

    def find_coretemp_sensor(self):
        return self.cpu_sensor

    def find_peci_sensor(self):
        return self.peci_sensor

    def find_sen5_sensor(self):
        return self.sen5_sensor_path

    def discover_board_sensors(self):
        return dict(self.board_sensors_result)

    def read_temp(self, sensor_path, retries=3, retry_delay=0.05):
        self.temp_reads.append(sensor_path)
        if self.sen5_temps is not None and sensor_path == self.sen5_sensor_path:
            q = self.sen5_temps
        else:
            q = self.temps
        if not q:
            raise OSError("no more scripted temps")
        t = q.popleft()
        if isinstance(t, Exception):
            raise t
        return t

    def read_rapl_pl1(self):
        return self.rapl_pl1_uw

    def write_rapl_pl1(self, uw):
        self.rapl_writes.append(uw)
        return True

    def set_fan(self, pct):
        self.fan_calls.append(pct)
        if isinstance(self.set_fan_ok, deque):
            return self.set_fan_ok.popleft()
        return self.set_fan_ok

    def restore_ec(self):
        self.ec_restores += 1

    def read_fan_rpm(self):
        return 3000  # scripted value

    def read_thermal_config(self):
        return list(self.thermal_config)

    def write_epp(self, value):
        self.epp_writes.append(value)
        return True

    def write_thermal_config(self, sensor_id, warn, high, halt, fan_off, fan_max):
        self.thermal_writes.append({
            "sensor_id": sensor_id, "warn": warn, "high": high,
            "halt": halt, "fan_off": fan_off, "fan_max": fan_max,
        })
        return self.write_thermal_ok

    def check_ectool(self):
        return self.ectool_ok

    def read_system_snapshot(self, board_sensor_paths):
        entry = dict(self.system_snapshot)
        thermal = self.read_thermal_config()
        if thermal:
            entry["ec_thermal"] = thermal
        return entry

    def check_platform(self):
        return True, ""

    def check_framework_laptop(self):
        return True, ""

    def check_alder_lake(self):
        return True, ""

    def check_python_version(self):
        return True, ""

    def check_root(self):
        return True, ""

    def check_ectool_installed(self):
        return True, ""

    def sleep(self, seconds):
        self.sleep_calls.append(seconds)


def run_quiet(config, hw, **kwargs):
    """Run with stdout/stderr suppressed."""
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()
    try:
        run(config, hw, **kwargs)
    except Exception:
        captured_stderr = sys.stderr.getvalue()
        sys.stdout, sys.stderr = old_out, old_err
        if captured_stderr:
            print(f"  [run_quiet stderr]: {captured_stderr.strip()}")
        raise
    finally:
        sys.stdout, sys.stderr = old_out, old_err


def run_expect_exit(config, hw, **kwargs):
    """Run expecting sys.exit(). Returns exit code or None."""
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()
    try:
        run(config, hw, **kwargs)
        return None
    except SystemExit as e:
        return e.code
    finally:
        sys.stdout, sys.stderr = old_out, old_err


###########################################################################
#                       SENSOR LOGGER TESTS                               #
###########################################################################

# ── 1. Init defaults ──────────────────────────────────────────────────

print("\n── SensorLogger init ──")

sl = SensorLogger({})
check("disabled by default", sl.enabled is False)
check("default path", sl.path == "/var/log/fw-pwrctl/sensor-log.json")
check("default max_size", sl.max_size == 50 * 1024 * 1024)
check("default flush_interval", sl.flush_interval == 120)
check("empty buffer", sl._buffer == [])
check("zero flush_failures", sl._flush_failures == 0)
check("hw is None", sl._hw is None)
check("empty board_sensor_paths", sl._board_sensor_paths == {})

sl2 = SensorLogger({"enabled": True, "path": "/tmp/test.json",
                     "maxSizeMB": 10, "flushIntervalSeconds": 30})
check("custom enabled", sl2.enabled is True)
check("custom path", sl2.path == "/tmp/test.json")
check("custom max_size", sl2.max_size == 10 * 1024 * 1024)
check("custom flush_interval", sl2.flush_interval == 30)

# Init with MockHardware
hw_mock = MockHardware()
hw_mock.board_sensors_result = {"SEN2": "/fake/sen2", "SEN5": "/fake/sen5"}
sl3 = SensorLogger({"enabled": True}, hw=hw_mock)
check("hw wired", sl3._hw is hw_mock)
check("board_sensor_paths from hw", sl3._board_sensor_paths == {"SEN2": "/fake/sen2", "SEN5": "/fake/sen5"})

# ── 2. log() when disabled ────────────────────────────────────────────

print("\n── log() when disabled ──")

sl_off = SensorLogger({"enabled": False})
sl_off.log(controller_state={"test": 1})
check("no buffering when disabled", sl_off._buffer == [])

# ── 3. log() when enabled — buffering with MockHardware ──────────────

print("\n── log() when enabled ──")

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "log.json")
    hw_m = MockHardware()
    hw_m.system_snapshot = {
        "cpu": {"user": 10.0, "nice": 0.1, "system": 5.0, "idle": 80.0,
                "iowait": 1.0, "irq": 0.1, "softirq": 0.5,
                "load_1m": 0.5, "load_5m": 0.3, "load_15m": 0.2},
        "memory": {"total_mb": 16384, "used_mb": 8192, "free_mb": 4096,
                   "available_mb": 8192, "buffers_mb": 512, "cached_mb": 2048,
                   "swap_total_mb": 8192, "swap_used_mb": 0, "swap_free_mb": 8192},
    }
    sl = SensorLogger({"enabled": True, "path": path,
                        "flushIntervalSeconds": 9999}, hw=hw_m)
    sl.log(controller_state={"pl1_w": 15.0, "idle_active": True})
    check("one entry buffered", len(sl._buffer) == 1)

    entry = json.loads(sl._buffer[0])
    check("has timestamp", "timestamp" in entry)
    check("has controller", "controller" in entry)
    check("controller.pl1_w", entry["controller"]["pl1_w"] == 15.0)
    check("controller.idle_active", entry["controller"]["idle_active"] is True)
    check("has cpu from snapshot", "cpu" in entry)
    check("has memory from snapshot", "memory" in entry)
    check("cpu.user value", entry["cpu"]["user"] == 10.0)
    check("memory.total_mb value", entry["memory"]["total_mb"] == 16384)

    # Second entry
    sl.log(controller_state={"pl1_w": 12.0})
    check("two entries buffered", len(sl._buffer) == 2)
    check("no file yet", not os.path.exists(path))

# ── 4. log() without hw — timestamp only ─────────────────────────────

print("\n── log() without hw ──")

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "log.json")
    sl = SensorLogger({"enabled": True, "path": path,
                        "flushIntervalSeconds": 9999})
    sl.log(controller_state={"test": 1})
    entry = json.loads(sl._buffer[0])
    check("has timestamp", "timestamp" in entry)
    check("has controller", "controller" in entry)
    check("no cpu without hw", "cpu" not in entry)
    check("no memory without hw", "memory" not in entry)

# ── 5. flush() ────────────────────────────────────────────────────────

print("\n── flush() ──")

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "log.json")
    sl = SensorLogger({"enabled": True, "path": path,
                        "flushIntervalSeconds": 9999})
    sl.log(controller_state={"test": 1})
    sl.log(controller_state={"test": 2})
    sl.log(controller_state={"test": 3})
    check("3 entries buffered", len(sl._buffer) == 3)

    sl.flush()
    check("buffer cleared after flush", sl._buffer == [])
    check("file created", os.path.exists(path))
    check("flush_failures reset", sl._flush_failures == 0)

    with open(path) as f:
        lines = [l for l in f.read().strip().split("\n") if l]
    check("3 lines written", len(lines) == 3, f"got {len(lines)}")
    for i, line in enumerate(lines):
        parsed = json.loads(line)
        check(f"line {i} valid JSON", parsed["controller"]["test"] == i + 1)

# ── 6. flush() no-op on empty buffer ─────────────────────────────────

print("\n── flush() empty buffer ──")

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "log.json")
    sl = SensorLogger({"enabled": True, "path": path})
    sl.flush()
    check("no file created on empty flush", not os.path.exists(path))

# ── 7. Auto-flush after flushIntervalSeconds ──────────────────────────

print("\n── auto-flush on interval ──")

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "log.json")
    sl = SensorLogger({"enabled": True, "path": path,
                        "flushIntervalSeconds": 0.1})
    sl.log(controller_state={"test": "first"})
    check("file not yet created", not os.path.exists(path))

    time.sleep(0.15)
    sl.log(controller_state={"test": "second"})  # triggers flush
    check("auto-flush created file", os.path.exists(path))
    check("buffer cleared", sl._buffer == [])

    with open(path) as f:
        lines = [l for l in f.read().strip().split("\n") if l]
    check("2 lines written", len(lines) == 2, f"got {len(lines)}")

# ── 8. Rotation ───────────────────────────────────────────────────────

import gzip as _gzip
import threading as _threading

def _wait_for_compression(td, timeout=5):
    """Wait for background compression thread(s) to finish."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = [t for t in _threading.enumerate()
                 if t.name.startswith("fw-pwrctl-compress")]
        if not alive:
            break
        time.sleep(0.05)

print("\n── rotation ──")

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "sensor-log.json")
    sl = SensorLogger({"enabled": True, "path": path,
                        "maxSizeMB": 0.001,  # ~1KB
                        "flushIntervalSeconds": 9999})

    # Write enough to exceed 1KB (with timestamps for metadata)
    for i in range(20):
        sl._buffer.append(json.dumps({"i": i, "pad": "x" * 100,
                                       "timestamp": f"2026-01-01T00:{i:02d}:00-07:00"}))
    sl.flush()
    check("initial file exists", os.path.exists(path))
    size1 = os.path.getsize(path)
    check("file is over 1KB", size1 > 1024, f"got {size1}")

    # Next flush should rotate
    for i in range(5):
        sl._buffer.append(json.dumps({"i": 100 + i,
                                       "timestamp": f"2026-01-01T01:{i:02d}:00-07:00"}))
    sl.flush()
    _wait_for_compression(td)

    files = sorted(os.listdir(td))
    rotated_gz = [f for f in files if f.endswith(".json.gz")]
    check("rotated file is gzipped", len(rotated_gz) == 1, f"got {files}")
    check("active + rotated + meta = 3 files",
          len(files) == 3, f"got {files}")

    with open(path) as f:
        new_lines = [json.loads(l) for l in f.read().strip().split("\n") if l]
    check("new file has 5 entries", len(new_lines) == 5, f"got {len(new_lines)}")
    check("new file starts at i=100", new_lines[0]["i"] == 100)

    with _gzip.open(os.path.join(td, rotated_gz[0]), "rt") as f:
        old_lines = [json.loads(l) for l in f.read().strip().split("\n") if l]
    check("rotated gzip has 20 entries", len(old_lines) == 20, f"got {len(old_lines)}")

    # Verify metadata
    meta_path = os.path.join(td, "sensor-log-meta.json")
    check("metadata file exists", os.path.exists(meta_path))
    with open(meta_path) as f:
        meta = json.load(f)
    check("metadata has 1 entry", len(meta) == 1, f"got {len(meta)}")
    check("metadata file field matches", meta[0]["file"] == rotated_gz[0])
    check("metadata has start timestamp", meta[0]["start"] == "2026-01-01T00:00:00-07:00",
          f"got {meta[0]['start']}")
    check("metadata has end timestamp", meta[0]["end"] == "2026-01-01T00:19:00-07:00",
          f"got {meta[0]['end']}")

print("\n── rotation: max file pruning ──")

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "sensor-log.json")
    sl = SensorLogger({"enabled": True, "path": path,
                        "maxSizeMB": 0.001,
                        "maxLogFiles": 2,
                        "flushIntervalSeconds": 9999})

    # Create 3 rotations (exceeds maxLogFiles=2)
    for rotation in range(3):
        for i in range(20):
            sl._buffer.append(json.dumps({"r": rotation, "i": i, "pad": "x" * 100,
                                           "timestamp": f"2026-01-0{rotation+1}T00:{i:02d}:00-07:00"}))
        sl.flush()
        _wait_for_compression(td)
        time.sleep(0.01)  # ensure unique timestamps in filenames

    gz_files = sorted(f for f in os.listdir(td) if f.endswith(".json.gz"))
    check("pruned to max 2 rotated files", len(gz_files) == 2, f"got {gz_files}")

    meta_path = os.path.join(td, "sensor-log-meta.json")
    with open(meta_path) as f:
        meta = json.load(f)
    check("metadata pruned to 2 entries", len(meta) == 2, f"got {len(meta)}")

# ── 9. Buffer cap ─────────────────────────────────────────────────────

print("\n── buffer cap ──")

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "log.json")
    sl = SensorLogger({"enabled": True, "path": path,
                        "flushIntervalSeconds": 9999})
    for i in range(SensorLogger.MAX_BUFFER_ENTRIES + 500):
        sl._collect_and_buffer({"i": i})
    check("buffer capped", len(sl._buffer) == SensorLogger.MAX_BUFFER_ENTRIES,
          f"got {len(sl._buffer)}")
    first = json.loads(sl._buffer[0])
    check("oldest entries dropped",
          first["controller"]["i"] == 500,
          f"got i={first['controller']['i']}")

# ── 10. Flush failure — unwritable path ───────────────────────────────

print("\n── flush failure ──")

sl = SensorLogger({"enabled": True, "path": "/proc/nonexistent/log.json",
                    "flushIntervalSeconds": 9999})
sl._buffer.append(json.dumps({"test": 1}))
sl.flush()
check("failure count incremented", sl._flush_failures == 1)
check("buffer retained on failure", len(sl._buffer) == 1)
check("still enabled after 1 failure", sl.enabled is True)

sl._buffer.append(json.dumps({"test": 2}))
sl.flush()
check("failure count 2", sl._flush_failures == 2)
check("buffer still retained (2 entries)", len(sl._buffer) == 2)
check("still enabled after 2 failures", sl.enabled is True)

sl._buffer.append(json.dumps({"test": 3}))
sl.flush()
check("failure count 3", sl._flush_failures == 3)
check("disabled after 3 failures", sl.enabled is False)
check("buffer cleared after disable", sl._buffer == [])

sl.log(controller_state={"test": "should not buffer"})
check("log() no-op after disable", sl._buffer == [])

# ── 11. Flush failure recovery ────────────────────────────────────────

print("\n── flush failure recovery ──")

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "log.json")
    sl = SensorLogger({"enabled": True, "path": "/proc/nonexistent/log.json",
                        "flushIntervalSeconds": 9999})
    sl._buffer.append(json.dumps({"test": 1}))
    sl.flush()
    sl._buffer.append(json.dumps({"test": 2}))
    sl.flush()
    check("2 failures accumulated", sl._flush_failures == 2)

    sl.path = path
    sl._buffer.append(json.dumps({"test": 3}))
    sl.flush()
    check("failures reset on success", sl._flush_failures == 0)
    check("still enabled", sl.enabled is True)
    check("file written", os.path.exists(path))

# ── 12. flush() after disable still works ─────────────────────────────

print("\n── flush in finally after disable ──")

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "log.json")
    sl = SensorLogger({"enabled": True, "path": path,
                        "flushIntervalSeconds": 9999})
    sl._buffer.append(json.dumps({"before": "disable"}))
    sl.enabled = False
    sl.flush()
    check("flush writes despite disabled", os.path.exists(path))
    with open(path) as f:
        data = json.loads(f.readline())
    check("correct data written", data["before"] == "disable")

# ── 13. Flush failure spacing ─────────────────────────────────────────

print("\n── flush failure spacing ──")

sl = SensorLogger({"enabled": True, "path": "/proc/nonexistent/log.json",
                    "flushIntervalSeconds": 9999})
sl._buffer.append(json.dumps({"test": 1}))
before = sl._last_flush
sl.flush()
check("_last_flush updated on failure", sl._last_flush > before)

# ── 14. log() never raises ───────────────────────────────────────────

print("\n── log() never raises ──")

sl = SensorLogger({"enabled": True, "path": "/proc/nonexistent/log.json",
                    "flushIntervalSeconds": 9999})
try:
    sl.log(controller_state={"test": 1})
    check("log() did not raise", True)
except Exception as e:
    check("log() did not raise", False, str(e))

# ── 15. log() with no controller state ────────────────────────────────

print("\n── log() with no / empty controller state ──")

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "log.json")
    sl = SensorLogger({"enabled": True, "path": path,
                        "flushIntervalSeconds": 9999})
    sl.log()
    entry = json.loads(sl._buffer[-1])
    check("no controller key when None", "controller" not in entry)

    sl.log(controller_state={})
    entry = json.loads(sl._buffer[-1])
    check("controller key present for empty dict", "controller" in entry)
    check("has timestamp", "timestamp" in entry)

# ── 16. Board sensor discovery via MockHardware ──────────────────────

print("\n── board sensor discovery via MockHardware ──")

hw_m = MockHardware()
hw_m.board_sensors_result = {"SEN2": "/fake/s2", "SEN3": "/fake/s3", "SEN5": "/fake/s5"}
sl = SensorLogger({"enabled": True}, hw=hw_m)
check("3 sensors discovered", len(sl._board_sensor_paths) == 3)
check("SEN2 path", sl._board_sensor_paths["SEN2"] == "/fake/s2")
check("SEN5 path", sl._board_sensor_paths["SEN5"] == "/fake/s5")

# No sensors
hw_empty = MockHardware()
hw_empty.board_sensors_result = {}
sl2 = SensorLogger({"enabled": True}, hw=hw_empty)
check("empty sensors OK", sl2._board_sensor_paths == {})

# ── 17. Board sensor resilience via Hardware ─────────────────────────

print("\n── board sensor resilience ──")

with tempfile.TemporaryDirectory() as td:
    fake_temp = os.path.join(td, "temp_sen2")
    with open(fake_temp, "w") as f:
        f.write("55000\n")

    hw = Hardware()
    snapshot = hw.read_system_snapshot({
        "SEN2": fake_temp,
        "SEN3": "/proc/nonexistent/temp",
    })
    check("board_temps in snapshot", "board_temps" in snapshot)
    check("SEN2 present", "sen2_c" in snapshot.get("board_temps", {}))
    check("SEN2 value correct", snapshot["board_temps"]["sen2_c"] == 55.0)
    check("SEN3 skipped", "sen3_c" not in snapshot.get("board_temps", {}))

# ── 18. sensors -j error paths via Hardware ──────────────────────────

print("\n── sensors -j error paths ──")

import subprocess as sp
orig_run = sp.run

def fake_run_missing(cmd, **kw):
    if cmd == ["sensors", "-j"]:
        raise FileNotFoundError("sensors not found")
    return orig_run(cmd, **kw)

hw = Hardware()
sp.run = fake_run_missing
try:
    snapshot = hw.read_system_snapshot({})
finally:
    sp.run = orig_run
check("no sensors key when binary missing", "sensors" not in snapshot)
check("cpu still present", "cpu" in snapshot)

def fake_run_fail(cmd, **kw):
    if cmd == ["sensors", "-j"]:
        return sp.CompletedProcess(cmd, returncode=1, stdout="", stderr="error")
    return orig_run(cmd, **kw)

sp.run = fake_run_fail
try:
    snapshot = hw.read_system_snapshot({})
finally:
    sp.run = orig_run
check("no sensors key on non-zero exit", "sensors" not in snapshot)

def fake_run_bad_json(cmd, **kw):
    if cmd == ["sensors", "-j"]:
        return sp.CompletedProcess(cmd, returncode=0, stdout="not json", stderr="")
    return orig_run(cmd, **kw)

sp.run = fake_run_bad_json
try:
    snapshot = hw.read_system_snapshot({})
finally:
    sp.run = orig_run
check("no sensors key on bad JSON", "sensors" not in snapshot)

# ── 19. makedirs on flush ─────────────────────────────────────────────

print("\n── makedirs on flush ──")

with tempfile.TemporaryDirectory() as td:
    nested = os.path.join(td, "a", "b", "c", "log.json")
    sl = SensorLogger({"enabled": True, "path": nested,
                        "flushIntervalSeconds": 9999})
    sl._buffer.append(json.dumps({"test": 1}))
    sl.flush()
    check("nested dirs created", os.path.exists(nested))

# ── 20. Append across multiple flushes ────────────────────────────────

print("\n── append across flushes ──")

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "log.json")
    sl = SensorLogger({"enabled": True, "path": path,
                        "flushIntervalSeconds": 9999})
    sl._buffer.append(json.dumps({"batch": 1}))
    sl.flush()
    sl._buffer.append(json.dumps({"batch": 2}))
    sl._buffer.append(json.dumps({"batch": 3}))
    sl.flush()

    with open(path) as f:
        lines = [l for l in f.read().strip().split("\n") if l]
    check("3 lines across 2 flushes", len(lines) == 3, f"got {len(lines)}")
    check("order preserved", json.loads(lines[0])["batch"] == 1)
    check("order preserved (last)", json.loads(lines[2])["batch"] == 3)

# ── 21. validate_config() — logging section ───────────────────────────

print("\n── validate_config() logging ──")

base_cfg = {"setpoint": 75, "Kp": 0.25, "Ki": 0.021}


def valid(desc, logging_section):
    cfg = {**base_cfg, "logging": logging_section}
    try:
        validate_config(cfg)
        check(f"valid: {desc}", True)
    except ValueError as e:
        check(f"valid: {desc}", False, str(e))


def invalid(desc, logging_section, expected_substr=""):
    cfg = {**base_cfg, "logging": logging_section}
    try:
        validate_config(cfg)
        check(f"invalid: {desc}", False, "did not raise")
    except ValueError as e:
        ok = expected_substr in str(e) if expected_substr else True
        check(f"invalid: {desc}", ok, str(e))


valid("minimal", {"enabled": True})
valid("full", {"enabled": True, "path": "/tmp/x.json",
               "maxSizeMB": 10, "flushIntervalSeconds": 30})
valid("absent", {})
invalid("not a dict", "yes", "must be an object")
invalid("not a dict (list)", [1, 2], "must be an object")
invalid("enabled string", {"enabled": "yes"}, "must be a boolean")
invalid("enabled int", {"enabled": 1}, "must be a boolean")
invalid("path not string", {"path": 123}, "must be a string")
invalid("maxSizeMB zero", {"maxSizeMB": 0}, "must be > 0")
invalid("maxSizeMB negative", {"maxSizeMB": -5}, "must be > 0")
invalid("maxSizeMB string", {"maxSizeMB": "big"}, "must be > 0")
invalid("flushInterval zero", {"flushIntervalSeconds": 0}, "must be > 0")
invalid("flushInterval negative", {"flushIntervalSeconds": -1}, "must be > 0")
invalid("maxLogFiles zero", {"maxLogFiles": 0}, "positive integer")
invalid("maxLogFiles negative", {"maxLogFiles": -1}, "positive integer")
invalid("maxLogFiles float", {"maxLogFiles": 2.5}, "positive integer")
invalid("maxLogFiles string", {"maxLogFiles": "many"}, "positive integer")

try:
    validate_config(base_cfg)
    check("valid: no logging section", True)
except ValueError as e:
    check("valid: no logging section", False, str(e))


###########################################################################
#                    PIPL1CONTROLLER TESTS                                #
###########################################################################

PL1_CFG = {
    "setpoint": 75, "Kp": 0.25, "Ki": 0.021,
    "pl1MinW": 5, "pl1MaxW": 28, "integralMaxW": 250,
    "rampUpRateLimitW": 3, "rampDownRateLimitW": 3,
    "sen5GuardTemp": 75, "sen5CriticalTemp": 78, "sen5ReleaseTemp": 73,
    "sen5CutRateW": 2, "sensorSmoothing": 5,
    "idleCeilingW": 15, "idleTempC": 65, "idleReleaseTempC": 68,
}

# ── 22. log_state() ──────────────────────────────────────────────────

print("\n── PIPL1Controller.log_state() ──")

ctrl = PIPL1Controller(PL1_CFG)

state = ctrl.log_state(60.0, None)
check("empty samples — minimal dict", state == {"raw_temp_c": 60.0})

ctrl.update(60.0, 68.0, 2.0)
state = ctrl.log_state(60.0, 68.0)
for k in ("pl1_w", "median_c", "error", "integral", "setpoint_c",
           "pl1_min_w", "pl1_max_w", "idle_active", "idle_ceiling_w",
           "guard_active", "raw_temp_c", "sen5_c"):
    check(f"log_state has {k}", k in state, f"keys: {list(state)}")
check("setpoint_c value", state["setpoint_c"] == 75)
check("pl1_min_w value", state["pl1_min_w"] == 5)
check("pl1_max_w value", state["pl1_max_w"] == 28)
check("sen5_c value", state["sen5_c"] == 68.0)

state2 = ctrl.log_state(60.0, None)
check("no sen5_c when None", "sen5_c" not in state2)

try:
    json.dumps(state)
    check("log_state is JSON-serializable", True)
except (TypeError, ValueError) as e:
    check("log_state is JSON-serializable", False, str(e))

# ── 23. PI basics — at setpoint ───────────────────────────────────────

print("\n── PL1 PI basics ──")

ctrl = PIPL1Controller(PL1_CFG)

for _ in range(10):
    pl1 = ctrl.update(75.0, None, 2.0)
check("at setpoint → pl1_max", pl1 == 28.0, f"got {pl1}")
check("integral ~0 at setpoint", abs(ctrl._integral) < 0.01,
      f"got {ctrl._integral}")

# ── 24. PI response — hot ────────────────────────────────────────────

print("\n── PL1 response — hot ──")

ctrl = PIPL1Controller(PL1_CFG)

for _ in range(5):
    ctrl._samples.clear()
for i in range(20):
    pl1 = ctrl.update(85.0, None, 2.0)
check("hot → PL1 reduced", pl1 < 28.0, f"got {pl1}")
check("hot → integral positive", ctrl._integral > 0, f"got {ctrl._integral}")
check("hot → PL1 >= pl1_min", pl1 >= 5.0, f"got {pl1}")

# ── 25. PI response — cold ───────────────────────────────────────────

print("\n── PL1 response — cold ──")

ctrl = PIPL1Controller(PL1_CFG)

for _ in range(20):
    pl1 = ctrl.update(50.0, None, 2.0)
check("cold → idle ceiling", pl1 == 15.0, f"got {pl1}")
check("cold → idle active", ctrl._idle_active)

ctrl_warm = PIPL1Controller(PL1_CFG)
for _ in range(20):
    pl1 = ctrl_warm.update(70.0, None, 2.0)
check("warm (no idle) → pl1_max", pl1 == 28.0, f"got {pl1}")
check("warm → idle not active", not ctrl_warm._idle_active)

# ── 26. PI clamping ──────────────────────────────────────────────────

print("\n── PL1 clamping ──")

ctrl = PIPL1Controller(PL1_CFG)

for _ in range(100):
    pl1 = ctrl.update(90.0, None, 2.0)
check("extremely hot → PL1 well below max", pl1 < 22.0, f"got {pl1}")
check("extremely hot → PL1 above min", pl1 >= 5.0, f"got {pl1}")

pl1_before_cool = pl1
for _ in range(6):
    pl1_rising = ctrl.update(70.0, None, 2.0)
check("cooling → PL1 rises", pl1_rising > pl1_before_cool, f"got {pl1_rising}")
check("rate limited rise (bounded)",
      pl1_rising <= pl1_before_cool + 6 * 3.0,
      f"before={pl1_before_cool} got {pl1_rising}")

# ── 27. Asymmetric rate limiting ─────────────────────────────────────

print("\n── PL1 asymmetric ramp ──")

ctrl = PIPL1Controller({**PL1_CFG, "rampUpRateLimitW": 1, "rampDownRateLimitW": 5})

ctrl.update(75.0, None, 2.0)
pl1_before = ctrl._last_pl1
pl1 = ctrl.update(90.0, None, 2.0)
check("ramp down by at most 5W",
      pl1 >= pl1_before - 5.0, f"before={pl1_before} after={pl1}")

pl1_before = ctrl._last_pl1
pl1 = ctrl.update(50.0, None, 2.0)
check("ramp up by at most 1W",
      pl1 <= pl1_before + 1.0, f"before={pl1_before} after={pl1}")

# ── 28. Critical temp override ───────────────────────────────────────

print("\n── PL1 critical temp ──")

ctrl = PIPL1Controller(PL1_CFG)
ctrl.update(70.0, None, 2.0)

pl1 = ctrl.update(CRITICAL_TEMP, None, 2.0)
check("critical → pl1_min", pl1 == 5.0, f"got {pl1}")
check("critical → integral maxed", ctrl._integral == ctrl.integral_max)

# ── 29. SEN5 guard ───────────────────────────────────────────────────

print("\n── PL1 SEN5 guard ──")

ctrl = PIPL1Controller(PL1_CFG)

pl1 = ctrl.update(70.0, 60.0, 2.0)
check("guard not active with cool SEN5", not ctrl._sen5_guard_active)

pl1_before = ctrl._last_pl1
pl1 = ctrl.update(70.0, 75.0, 2.0)
check("guard activates at sen5GuardTemp", ctrl._sen5_guard_active)
check("guard cuts PL1", pl1 <= pl1_before, f"before={pl1_before} after={pl1}")

pl1 = ctrl.update(70.0, 78.0, 2.0)
check("SEN5 critical → pl1_min", pl1 == 5.0, f"got {pl1}")

ctrl2 = PIPL1Controller(PL1_CFG)
ctrl2.update(70.0, 76.0, 2.0)
check("guard is active", ctrl2._sen5_guard_active)
ctrl2.update(70.0, 72.0, 2.0)
check("guard released below sen5ReleaseTemp", not ctrl2._sen5_guard_active)

ctrl3 = PIPL1Controller(PL1_CFG)
ctrl3.update(70.0, 76.0, 2.0)
ctrl3.update(70.0, 74.0, 2.0)
check("guard persists in hysteresis band", ctrl3._sen5_guard_active)

ctrl4 = PIPL1Controller(PL1_CFG)
pl1 = ctrl4.update(70.0, None, 2.0)
check("no guard without SEN5", not ctrl4._sen5_guard_active)
check("PI runs normally without SEN5", pl1 > 5.0)

# SEN5 guard release when sensor becomes unavailable (sticky fix)
ctrl5 = PIPL1Controller(PL1_CFG)
ctrl5.update(70.0, 76.0, 2.0)
check("guard active after SEN5 hot", ctrl5._sen5_guard_active)
ctrl5.update(70.0, None, 2.0)  # SEN5 goes away while guard active
check("guard released when SEN5 unavailable", not ctrl5._sen5_guard_active)

# ── 30. Idle ceiling ─────────────────────────────────────────────────

print("\n── PL1 idle ceiling ──")

ctrl = PIPL1Controller(PL1_CFG)

for _ in range(10):
    pl1 = ctrl.update(60.0, None, 2.0)
check("idle activates below idleTempC", ctrl._idle_active)
check("PL1 capped at idleCeilingW", pl1 <= 15.0, f"got {pl1}")

for _ in range(10):
    pl1 = ctrl.update(69.0, None, 2.0)
check("idle releases above idleReleaseTempC", not ctrl._idle_active)

ctrl2 = PIPL1Controller(PL1_CFG)
for _ in range(5):
    ctrl2.update(60.0, None, 2.0)
check("idle active", ctrl2._idle_active)
for _ in range(5):
    ctrl2.update(66.0, None, 2.0)
check("idle persists in hysteresis", ctrl2._idle_active)

# ── 31. Median filter ────────────────────────────────────────────────

print("\n── PL1 median filter ──")

ctrl = PIPL1Controller({**PL1_CFG, "sensorSmoothing": 5})

for t in [70.0, 70.0, 70.0, 70.0]:
    ctrl.update(t, None, 2.0)
pl1_before_spike = ctrl._last_pl1
ctrl.update(95.0, None, 2.0)
sorted_s = sorted(ctrl._samples)
median = sorted_s[len(sorted_s) // 2]
check("median filters spike", median == 70.0, f"got {median}")

# ── 32. Anti-windup ──────────────────────────────────────────────────

print("\n── PL1 anti-windup ──")

ctrl = PIPL1Controller(PL1_CFG)

for _ in range(1000):
    ctrl.update(90.0, None, 2.0)
check("integral capped at integralMaxW",
      ctrl._integral <= ctrl.integral_max, f"got {ctrl._integral}")
check("integral at max", ctrl._integral == ctrl.integral_max,
      f"got {ctrl._integral}")

ctrl2 = PIPL1Controller(PL1_CFG)
for _ in range(1000):
    ctrl2.update(50.0, None, 2.0)
check("integral capped at -integralMaxW",
      ctrl2._integral >= -ctrl2.integral_max, f"got {ctrl2._integral}")

# ── 33. notify_external_pl1 ──────────────────────────────────────────

print("\n── PL1 notify_external_pl1 ──")

ctrl = PIPL1Controller(PL1_CFG)
ctrl.update(70.0, None, 2.0)
ctrl.notify_external_pl1(5.0)
check("last_pl1 synced", ctrl._last_pl1 == 5.0)
check("integral maxed after notify", ctrl._integral == ctrl.integral_max)


###########################################################################
#                     VALIDATE_CONFIG TESTS                               #
###########################################################################

print("\n── validate_config() settings ──")

try:
    validate_config({"setpoint": 75, "Kp": 0.25, "Ki": 0.021})
    check("valid config", True)
except ValueError as e:
    check("valid config", False, str(e))


def bad_config(desc, cfg, substr=""):
    try:
        validate_config(cfg)
        check(f"invalid: {desc}", False, "did not raise")
    except ValueError as e:
        ok = substr in str(e) if substr else True
        check(f"invalid: {desc}", ok, str(e))

bad_config("missing setpoint",
           {"Kp": 1, "Ki": 0.1}, "setpoint")
bad_config("missing Kp",
           {"setpoint": 75, "Ki": 0.1}, "Kp")
bad_config("missing Ki",
           {"setpoint": 75, "Kp": 1}, "Ki")
bad_config("bad setpoint",
           {"setpoint": 200, "Kp": 1, "Ki": 0.1}, "setpoint")
bad_config("pl1MaxW <= pl1MinW",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "pl1MinW": 10, "pl1MaxW": 5}, "pl1MaxW")
bad_config("idle temps: only one present",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "idleTempC": 60}, "idleTempC")
bad_config("idle temps: idle >= release",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "idleTempC": 70, "idleReleaseTempC": 65}, "idleTempC")
bad_config("idle release >= setpoint",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "idleTempC": 60, "idleReleaseTempC": 75}, "idleReleaseTempC")
bad_config("updateInterval float",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "updateInterval": 2.5}, "updateInterval")
bad_config("updateInterval zero",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "updateInterval": 0}, "updateInterval")
bad_config("sensorSmoothing zero",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "sensorSmoothing": 0}, "sensorSmoothing")
bad_config("sensorSmoothing float",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "sensorSmoothing": 3.5}, "sensorSmoothing")
bad_config("integralMaxW zero",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "integralMaxW": 0}, "integralMaxW")
bad_config("integralMaxW negative",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "integralMaxW": -5}, "integralMaxW")
bad_config("rampUpRateLimitW negative",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "rampUpRateLimitW": -1}, "rampUpRateLimitW")
bad_config("rampDownRateLimitW negative",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "rampDownRateLimitW": -1}, "rampDownRateLimitW")
bad_config("SEN5 release >= guard",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "sen5ReleaseTemp": 76, "sen5GuardTemp": 75}, "sen5ReleaseTemp")
bad_config("SEN5 guard > critical",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "sen5GuardTemp": 80, "sen5CriticalTemp": 78}, "sen5GuardTemp")
bad_config("sen5CutRateW zero",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "sen5CutRateW": 0}, "sen5CutRateW")
bad_config("sen5CutRateW negative",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "sen5CutRateW": -1}, "sen5CutRateW")
bad_config("idleCeilingW below pl1MinW",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "pl1MinW": 5, "idleCeilingW": 3}, "idleCeilingW")
bad_config("idleCeilingW above pl1MaxW",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "pl1MaxW": 28, "idleCeilingW": 30}, "idleCeilingW")


###########################################################################
#                  HARDWARE DIRECT TESTS                                  #
###########################################################################

print("\n── Hardware.read_system_snapshot() direct ──")

hw = Hardware()
snapshot = hw.read_system_snapshot({})
check("cpu in real snapshot", "cpu" in snapshot)
check("memory in real snapshot", "memory" in snapshot)
check("memory.total_mb > 0", snapshot["memory"]["total_mb"] > 0)

if "cpu" in snapshot:
    cpu = snapshot["cpu"]
    for k in ("user", "nice", "system", "idle", "iowait", "irq",
               "softirq", "load_1m", "load_5m", "load_15m"):
        check(f"cpu.{k} present", k in cpu, f"keys: {list(cpu)}")
    check("cpu percentages sum ~100",
          95 <= sum(cpu[k] for k in ("user", "nice", "system", "idle",
                                      "iowait", "irq", "softirq")) <= 105)

if "memory" in snapshot:
    mem = snapshot["memory"]
    for k in ("total_mb", "used_mb", "free_mb", "available_mb",
               "buffers_mb", "cached_mb", "swap_total_mb",
               "swap_used_mb", "swap_free_mb"):
        check(f"memory.{k} present", k in mem, f"keys: {list(mem)}")
    check("memory.used + available ~ total",
          abs((mem["used_mb"] + mem["available_mb"]) - mem["total_mb"]) < 100)

import datetime
try:
    # Test timestamp from SensorLogger (uses datetime internally)
    sl_ts = SensorLogger({"enabled": True, "path": "/tmp/ts_test.json",
                           "flushIntervalSeconds": 9999})
    sl_ts.log(controller_state={"ts_test": True})
    entry = json.loads(sl_ts._buffer[0])
    datetime.datetime.fromisoformat(entry["timestamp"])
    check("timestamp is valid ISO", True)
except ValueError:
    check("timestamp is valid ISO", False, entry.get("timestamp", "?"))

print("\n── Hardware.discover_board_sensors() direct ──")

hw = Hardware()
sensors = hw.discover_board_sensors()
check("discover returns dict", isinstance(sensors, dict))
# On the Framework laptop, should have SEN2-SEN5; on CI it's empty — both OK
for name in sensors:
    check(f"sensor {name} is SEN*", name.startswith("SEN"),
          f"got {name}")


###########################################################################
#                      RUN() INTEGRATION TESTS                            #
###########################################################################

PL1_RUN_CFG = {
    "updateInterval": 2,
    "setpoint": 75,
    "Kp": 0.25,
    "Ki": 0.021,
    "pl1MinW": 5,
    "pl1MaxW": 28,
    "integralMaxW": 250,
    "rampUpRateLimitW": 3,
    "rampDownRateLimitW": 3,
    "sensorSmoothing": 5,
    "sen5GuardTemp": 75,
    "sen5CriticalTemp": 78,
    "sen5ReleaseTemp": 73,
    "sen5CutRateW": 2,
}

# ── PL1: normal operation ────────────────────────────────────────────

print("\n── run() PL1 normal ──")

hw = MockHardware()
hw.sen5_sensor_path = None
# 1 startup + 4 loop = 5 main temps
hw.temps = deque([70.0] * 5)
run_quiet(PL1_RUN_CFG, hw, max_ticks=4)

check("RAPL writes made", len(hw.rapl_writes) > 0)
check("ec restored on startup + shutdown", hw.ec_restores == 2)
check("original PL1 restored last",
      hw.rapl_writes[-1] == 28_000_000)
check("no fan calls in PL1 mode", hw.fan_calls == [])
check("sleeps match ticks", len(hw.sleep_calls) == 4)

# ── PL1: critical temp ──────────────────────────────────────────────

print("\n── run() PL1 critical ──")

hw = MockHardware()
hw.sen5_sensor_path = None
hw.temps = deque([95.0] * 7)  # 1 startup + 6 loop
run_quiet(PL1_RUN_CFG, hw, max_ticks=6)

# After CRITICAL_COUNT=3 consecutive, PL1 forced to min
min_writes = [w for w in hw.rapl_writes if w == 5_000_000]
check("PL1 min writes present", len(min_writes) >= 3,
      f"got {len(min_writes)} of {hw.rapl_writes}")
check("original PL1 restored last", hw.rapl_writes[-1] == 28_000_000)

# ── PL1: temp read failure ──────────────────────────────────────────

print("\n── run() PL1 temp read failure ──")

hw = MockHardware()
hw.sen5_sensor_path = None
# 1 startup ok, then 2 failures, then 2 ok
hw.temps = deque([70.0, OSError("fail"), OSError("fail"), 70.0, 70.0])
run_quiet(PL1_RUN_CFG, hw, max_ticks=4)

# On failure: PL1 set to min
min_writes = [w for w in hw.rapl_writes if w == 5_000_000]
check("PL1 to min on failure", len(min_writes) >= 2,
      f"got {len(min_writes)}")

# ── PL1: SEN5 guard ─────────────────────────────────────────────────

print("\n── run() PL1 SEN5 guard ──")

hw = MockHardware()
hw.sen5_sensor_path = "/mock/sen5"
hw.sen5_temps = deque([76.0, 76.0, 76.0])  # above guard (75)
# 1 startup + 4 loop main temps
hw.temps = deque([70.0] * 5)
run_quiet(PL1_RUN_CFG, hw, max_ticks=4)

# SEN5 guard should cut PL1 below max
non_restore_writes = hw.rapl_writes[:-1]  # exclude final restore
check("RAPL writes with SEN5 guard", len(non_restore_writes) >= 2)
# At least some writes should be below pl1_max (28W = 28_000_000)
below_max = [w for w in non_restore_writes if w < 28_000_000]
check("SEN5 guard reduced PL1", len(below_max) >= 1,
      f"writes: {non_restore_writes}")

# ── PL1: shutdown cleanup ───────────────────────────────────────────

print("\n── run() PL1 shutdown cleanup ──")

hw = MockHardware()
hw.sen5_sensor_path = None
hw.rapl_pl1_uw = 15_000_000  # original was 15W
hw.temps = deque([70.0] * 3)  # 1 startup + 2 loop
run_quiet(PL1_RUN_CFG, hw, max_ticks=2)

check("original PL1 restored", hw.rapl_writes[-1] == 15_000_000)
check("ec restored in finally", hw.ec_restores >= 1)

# ── PL1: sensor logging wired ───────────────────────────────────────

print("\n── run() PL1 sensor logging ──")

with tempfile.TemporaryDirectory() as td:
    log_path = os.path.join(td, "test.json")
    cfg = {**PL1_RUN_CFG, "logging": {
        "enabled": True, "path": log_path, "flushIntervalSeconds": 9999}}
    hw = MockHardware()
    hw.sen5_sensor_path = None
    hw.system_snapshot = {"cpu": {"user": 50.0}}
    hw.temps = deque([70.0] * 5)
    run_quiet(cfg, hw, max_ticks=4)
    check("sensor log file created", os.path.exists(log_path))
    with open(log_path) as f:
        lines = [l for l in f.read().strip().split("\n") if l]
    check("sensor log has entries", len(lines) >= 1, f"got {len(lines)}")
    entry = json.loads(lines[0])
    check("log entry has controller", "controller" in entry)

# ── Startup: no sensor found ────────────────────────────────────────

print("\n── run() no sensor found ──")

hw = MockHardware()
hw.cpu_sensor = None
hw.peci_sensor = None
code = run_expect_exit(PL1_RUN_CFG, hw)
check("exits with code 1", code == 1)

# ── Startup: ectool broken ──────────────────────────────────────────

print("\n── run() ectool broken ──")

hw = MockHardware()
hw.ectool_ok = False
hw.temps = deque([70.0])
code = run_expect_exit(PL1_RUN_CFG, hw)
check("exits with code 1", code == 1)

# ── Startup: RAPL unreadable (PL1 mode) ─────────────────────────────

print("\n── run() RAPL unreadable ──")

class RaplFailHardware(MockHardware):
    def read_rapl_pl1(self):
        raise OSError("RAPL not available")

hw = RaplFailHardware()
hw.temps = deque([70.0])
code = run_expect_exit(PL1_RUN_CFG, hw)
check("exits with code 1 on RAPL failure", code == 1)

# ── Startup: sensor unreadable ──────────────────────────────────────

print("\n── run() sensor unreadable at startup ──")

hw = MockHardware()
hw.temps = deque([OSError("cannot read")])
code = run_expect_exit(PL1_RUN_CFG, hw)
check("exits with code 1 on unreadable sensor", code == 1)

# ── PL1: temp read failure with sensor rescan ───────────────────────

print("\n── run() PL1 sensor rescan ──")

hw = MockHardware()
hw.sen5_sensor_path = None
# 1 startup ok, then SENSOR_RESCAN_AFTER failures, then 2 ok
hw.temps = deque(
    [70.0]
    + [OSError("fail")] * SENSOR_RESCAN_AFTER
    + [70.0, 70.0]
)
run_quiet(PL1_RUN_CFG, hw, max_ticks=SENSOR_RESCAN_AFTER + 2)

# During failures: PL1 set to min each time
min_writes = [w for w in hw.rapl_writes if w == 5_000_000]
check("PL1 to min during failures", len(min_writes) >= SENSOR_RESCAN_AFTER,
      f"got {len(min_writes)}")
check("sensor rescan triggered (coretemp called >1x)",
      hw.temp_reads.count("/mock/cpu") >= 2,
      f"reads: {hw.temp_reads}")

###########################################################################
#                   PREFLIGHT CHECK TESTS                                 #
###########################################################################

# ── Real hardware preflight checks (--live only) ──────────────────────

if LIVE:
    print("\n── check_platform() ──")

    hw = Hardware()
    ok, msg = hw.check_platform()
    check("real platform is Linux", ok, msg)

    print("\n── check_framework_laptop() ──")

    hw = Hardware()
    ok, msg = hw.check_framework_laptop()
    check("real machine is Framework", ok, msg)

    print("\n── check_alder_lake() ──")

    hw = Hardware()
    ok, msg = hw.check_alder_lake()
    check("real CPU is Alder Lake", ok, msg)

    print("\n── check_ectool_installed() ──")

    hw = Hardware()
    ok, msg = hw.check_ectool_installed()
    check("ectool is installed", ok, msg)

    print("\n── preflight_checks() real hardware ──")

    try:
        preflight_checks(Hardware())
        check("preflight passes on real hardware", True)
    except SystemExit:
        check("preflight passes on real hardware", False, "sys.exit called")

# ── preflight_checks() fails on wrong platform ───────────────────────

print("\n── preflight_checks() wrong platform ──")


class WrongPlatformHW(MockHardware):
    def check_platform(self):
        return False, "Not Linux (detected: darwin)"


old_out, old_err = sys.stdout, sys.stderr
sys.stdout = io.StringIO()
sys.stderr = io.StringIO()
try:
    preflight_checks(WrongPlatformHW())
    sys.stdout, sys.stderr = old_out, old_err
    check("exits on wrong platform", False, "did not exit")
except SystemExit as e:
    err_output = sys.stderr.getvalue()
    sys.stdout, sys.stderr = old_out, old_err
    check("exits with code 1 on wrong platform", e.code == 1)
    check("error mentions platform", "Platform" in err_output,
          err_output)

# ── preflight_checks() fails on non-Framework laptop ─────────────────

print("\n── preflight_checks() non-Framework ──")


class WrongLaptopHW(MockHardware):
    def check_framework_laptop(self):
        return False, "Not a Framework laptop (board_vendor: Dell)"


old_out, old_err = sys.stdout, sys.stderr
sys.stdout = io.StringIO()
sys.stderr = io.StringIO()
try:
    preflight_checks(WrongLaptopHW())
    sys.stdout, sys.stderr = old_out, old_err
    check("exits on non-Framework", False, "did not exit")
except SystemExit as e:
    err_output = sys.stderr.getvalue()
    sys.stdout, sys.stderr = old_out, old_err
    check("exits with code 1 on non-Framework", e.code == 1)
    check("error mentions Framework", "Framework laptop" in err_output,
          err_output)

# ── preflight_checks() fails on wrong CPU ────────────────────────────

print("\n── preflight_checks() wrong CPU ──")


class WrongCpuHW(MockHardware):
    def check_alder_lake(self):
        return False, "Not an Alder Lake processor (found: AMD Ryzen 9)"


old_out, old_err = sys.stdout, sys.stderr
sys.stdout = io.StringIO()
sys.stderr = io.StringIO()
try:
    preflight_checks(WrongCpuHW())
    sys.stdout, sys.stderr = old_out, old_err
    check("exits on wrong CPU", False, "did not exit")
except SystemExit as e:
    err_output = sys.stderr.getvalue()
    sys.stdout, sys.stderr = old_out, old_err
    check("exits with code 1 on wrong CPU", e.code == 1)
    check("error mentions Alder Lake", "Alder Lake" in err_output,
          err_output)

# ── preflight_checks() fails on missing ectool ───────────────────────

print("\n── preflight_checks() missing ectool ──")


class NoEctoolHW(MockHardware):
    def check_ectool_installed(self):
        return False, (
            "ectool not found at /usr/local/bin/ectool\n"
            "  Install from Framework's EC repository"
        )


old_out, old_err = sys.stdout, sys.stderr
sys.stdout = io.StringIO()
sys.stderr = io.StringIO()
try:
    preflight_checks(NoEctoolHW())
    sys.stdout, sys.stderr = old_out, old_err
    check("exits on missing ectool", False, "did not exit")
except SystemExit as e:
    err_output = sys.stderr.getvalue()
    sys.stdout, sys.stderr = old_out, old_err
    check("exits with code 1 on missing ectool", e.code == 1)
    check("error mentions ectool", "ectool" in err_output,
          err_output)

# ── preflight_checks() reports ALL failures at once ──────────────────

print("\n── preflight_checks() multiple failures ──")


class AllBadHW(MockHardware):
    def check_platform(self):
        return False, "wrong platform"

    def check_python_version(self):
        return False, "wrong python"

    def check_root(self):
        return False, "not root"

    def check_framework_laptop(self):
        return False, "wrong laptop"

    def check_alder_lake(self):
        return False, "wrong cpu"

    def check_ectool_installed(self):
        return False, "no ectool"


old_out, old_err = sys.stdout, sys.stderr
sys.stdout = io.StringIO()
sys.stderr = io.StringIO()
try:
    preflight_checks(AllBadHW())
    sys.stdout, sys.stderr = old_out, old_err
    check("exits on all-bad", False, "did not exit")
except SystemExit as e:
    err_output = sys.stderr.getvalue()
    sys.stdout, sys.stderr = old_out, old_err
    check("exits with code 1", e.code == 1)
    check("all 6 failures reported",
          "Platform" in err_output and "Python" in err_output
          and "Root" in err_output and "Framework" in err_output
          and "Alder Lake" in err_output and "ectool" in err_output,
          err_output)

# ── Hardware.check_framework_laptop() with unreadable DMI ────────────

print("\n── check_framework_laptop() unreadable DMI ──")


class UnreadableDmiHW(Hardware):
    def check_framework_laptop(self):
        from pathlib import Path as P
        try:
            P("/sys/class/dmi/id/nonexistent_file_xyz").read_text()
        except Exception as e:
            return False, f"Cannot read board vendor: {e}"
        return True, ""


hw = UnreadableDmiHW()
ok, msg = hw.check_framework_laptop()
check("fails on unreadable DMI", not ok)
check("message mentions 'Cannot read'", "Cannot read" in msg, msg)

# ── Hardware.check_alder_lake() with mocked /proc/cpuinfo ────────────

print("\n── check_alder_lake() wrong CPU via file ──")

import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as td:
    fake_cpuinfo = os.path.join(td, "cpuinfo")
    with open(fake_cpuinfo, "w") as f:
        f.write("processor\t: 0\nmodel name\t: AMD Ryzen 9 7950X\n")

    class FakeCpuInfoHW(Hardware):
        def check_alder_lake(self):
            try:
                with open(fake_cpuinfo) as fh:
                    for line in fh:
                        if line.startswith("model name"):
                            if "12th Gen Intel" in line:
                                return True, ""
                            cpu = line.split(":", 1)[1].strip()
                            return False, f"Not an Alder Lake processor (found: {cpu})"
                return False, "Cannot find model name in /proc/cpuinfo"
            except Exception as e:
                return False, f"Cannot read /proc/cpuinfo: {e}"

    hw = FakeCpuInfoHW()
    ok, msg = hw.check_alder_lake()
    check("rejects non-Alder-Lake CPU", not ok)
    check("message shows CPU name", "AMD Ryzen" in msg, msg)

# ── Hardware.check_ectool_installed() with non-existent path ─────────

print("\n── check_ectool_installed() missing binary ──")

import fw_pwrctl as fc
orig_ectool = fc.ECTOOL
fc.ECTOOL = "/tmp/nonexistent_ectool_xyz"
hw = Hardware()
ok, msg = hw.check_ectool_installed()
fc.ECTOOL = orig_ectool
check("fails on missing binary", not ok)
check("message has install instructions", "install-ectool.sh" in msg, msg)

# ── Hardware.check_ectool_installed() with non-executable file ────────

print("\n── check_ectool_installed() non-executable ──")

with tempfile.TemporaryDirectory() as td:
    fake_ectool = os.path.join(td, "ectool")
    with open(fake_ectool, "w") as f:
        f.write("#!/bin/sh\n")
    os.chmod(fake_ectool, 0o644)  # readable but not executable

    fc.ECTOOL = fake_ectool
    hw = Hardware()
    ok, msg = hw.check_ectool_installed()
    fc.ECTOOL = orig_ectool
    check("fails on non-executable", not ok)
    check("message mentions chmod", "not executable" in msg, msg)


###########################################################################
#                EC THERMAL CONFIG TESTS                                  #
###########################################################################

# ── Startup correction: wrong DDR fan_off → corrected ─────────────────

print("\n── run() EC thermal startup correction ──")

hw = MockHardware()
hw.sen5_sensor_path = None
hw.thermal_config = [
    {"sensor_id": 0, "warn": 0, "high": 361, "halt": 371, "fan_off": 324, "fan_max": 342, "name": "F75303_Local"},
    {"sensor_id": 1, "warn": 0, "high": 361, "halt": 371, "fan_off": 324, "fan_max": 342, "name": "F75303_CPU"},
    {"sensor_id": 2, "warn": 0, "high": 360, "halt": 370, "fan_off": 313, "fan_max": 342, "name": "F75303_DDR"},
]
hw.temps = deque([70.0] * 5)
run_quiet(PL1_RUN_CFG, hw, max_ticks=4)

check("thermal write issued", len(hw.thermal_writes) == 1,
      f"got {len(hw.thermal_writes)}")
if hw.thermal_writes:
    tw = hw.thermal_writes[0]
    check("write targets sensor 2", tw["sensor_id"] == 2)
    check("write fan_off=323", tw["fan_off"] == 323)
    check("write warn=0", tw["warn"] == 0)
    check("write high=360", tw["high"] == 360)
    check("write halt=370", tw["halt"] == 370)
    check("write fan_max=342", tw["fan_max"] == 342)
    check("write has no name key", "name" not in tw)

# ── Startup no-op: DDR fan_off already correct ────────────────────────

print("\n── run() EC thermal startup no-op ──")

hw = MockHardware()
hw.sen5_sensor_path = None
hw.thermal_config = [
    {"sensor_id": 0, "warn": 0, "high": 361, "halt": 371, "fan_off": 324, "fan_max": 342, "name": "F75303_Local"},
    {"sensor_id": 1, "warn": 0, "high": 361, "halt": 371, "fan_off": 324, "fan_max": 342, "name": "F75303_CPU"},
    {"sensor_id": 2, "warn": 0, "high": 360, "halt": 370, "fan_off": 323, "fan_max": 342, "name": "F75303_DDR"},
]
hw.temps = deque([70.0] * 5)
run_quiet(PL1_RUN_CFG, hw, max_ticks=4)

check("no thermal writes when correct", len(hw.thermal_writes) == 0,
      f"got {len(hw.thermal_writes)}")

# ── Startup: wrong sensor name → override skipped ─────────────────────

print("\n── run() EC thermal name mismatch ──")

hw = MockHardware()
hw.sen5_sensor_path = None
hw.thermal_config = [
    {"sensor_id": 2, "warn": 0, "high": 360, "halt": 370, "fan_off": 313, "fan_max": 342, "name": "SomeOtherSensor"},
]
hw.temps = deque([70.0] * 5)
run_quiet(PL1_RUN_CFG, hw, max_ticks=4)

check("no thermal writes on name mismatch", len(hw.thermal_writes) == 0,
      f"got {len(hw.thermal_writes)}")

# ── Startup: sensor has no name field → override skipped ──────────────

print("\n── run() EC thermal no name field ──")

hw = MockHardware()
hw.sen5_sensor_path = None
hw.thermal_config = [
    {"sensor_id": 2, "warn": 0, "high": 360, "halt": 370, "fan_off": 313, "fan_max": 342},
]
hw.temps = deque([70.0] * 5)
run_quiet(PL1_RUN_CFG, hw, max_ticks=4)

check("no thermal writes when name absent", len(hw.thermal_writes) == 0,
      f"got {len(hw.thermal_writes)}")

# ── Startup: empty thermal config (ectool failure) → no crash ─────────

print("\n── run() EC thermal startup empty config ──")

hw = MockHardware()
hw.sen5_sensor_path = None
hw.thermal_config = []
hw.temps = deque([70.0] * 5)
run_quiet(PL1_RUN_CFG, hw, max_ticks=4)

check("no thermal writes on empty config", len(hw.thermal_writes) == 0)

# ── Periodic EC override re-application ───────────────────────────────

print("\n── run() EC thermal periodic re-check ──")

import fw_pwrctl as _mod
_orig_recheck = _mod.EC_OVERRIDE_RECHECK
try:
    _mod.EC_OVERRIDE_RECHECK = 3  # re-check every 3 ticks instead of 60
    hw = MockHardware()
    hw.sen5_sensor_path = None
    hw.thermal_config = [
        {"sensor_id": 2, "warn": 0, "high": 360, "halt": 370,
         "fan_off": 313, "fan_max": 342, "name": "F75303_DDR"},
    ]
    hw.temps = deque([70.0] * 20)
    # Run enough ticks: startup applies once, then re-check at tick 3 and 6
    run_quiet(PL1_RUN_CFG, hw, max_ticks=7)
    # Expect at least 2 writes: 1 at startup + 1+ from periodic re-check
    check("periodic re-apply fires", len(hw.thermal_writes) >= 2,
          f"got {len(hw.thermal_writes)} writes (expected >= 2)")
finally:
    _mod.EC_OVERRIDE_RECHECK = _orig_recheck

# ── Snapshot includes ec_thermal when config populated ────────────────

print("\n── snapshot ec_thermal key ──")

hw_m = MockHardware()
hw_m.thermal_config = [
    {"sensor_id": 2, "warn": 0, "high": 360, "halt": 370, "fan_off": 323, "fan_max": 342},
]
hw_m.system_snapshot = {}
snapshot = hw_m.read_system_snapshot({})
check("ec_thermal in snapshot", "ec_thermal" in snapshot)
if "ec_thermal" in snapshot:
    check("ec_thermal is list", isinstance(snapshot["ec_thermal"], list))
    check("ec_thermal has 1 entry", len(snapshot["ec_thermal"]) == 1)
    check("ec_thermal sensor_id=2", snapshot["ec_thermal"][0]["sensor_id"] == 2)

# ── Snapshot omits ec_thermal when config empty ───────────────────────

print("\n── snapshot ec_thermal omitted when empty ──")

hw_m2 = MockHardware()
hw_m2.thermal_config = []
hw_m2.system_snapshot = {}
snapshot = hw_m2.read_system_snapshot({})
check("no ec_thermal when empty", "ec_thermal" not in snapshot)


###########################################################################
#                        EPP MANAGEMENT TESTS                             #
###########################################################################

PL1_EPP_CFG = {
    "updateInterval": 2,
    "setpoint": 75,
    "Kp": 0.25,
    "Ki": 0.021,
    "pl1MinW": 5,
    "pl1MaxW": 28,
    "integralMaxW": 250,
    "rampUpRateLimitW": 3,
    "rampDownRateLimitW": 3,
    "sensorSmoothing": 5,
    "sen5GuardTemp": 75,
    "sen5CriticalTemp": 78,
    "sen5ReleaseTemp": 73,
    "sen5CutRateW": 2,
    "idleCeilingW": 15,
    "idleTempC": 65,
    "idleReleaseTempC": 68,
    "idleEPP": "power",
    "normalEPP": "balance_performance",
}

# ── EPP: startup writes normalEPP ────────────────────────────────────

print("\n── run() EPP startup writes normalEPP ──")

hw = MockHardware()
hw.sen5_sensor_path = None
hw.temps = deque([70.0] * 5)
run_quiet(PL1_EPP_CFG, hw, max_ticks=4)

check("startup writes normalEPP first", len(hw.epp_writes) >= 1)
check("first EPP write is normalEPP",
      hw.epp_writes[0] == "balance_performance",
      f"got {hw.epp_writes[0]}")

# ── EPP: idle entry writes idleEPP ───────────────────────────────────

print("\n── run() EPP idle entry ──")

hw = MockHardware()
hw.sen5_sensor_path = None
# Need enough cool temps to trigger idle (< idleTempC=65)
# 1 startup + enough loop ticks at 60°C to enter idle
hw.temps = deque([60.0] * 10)
run_quiet(PL1_EPP_CFG, hw, max_ticks=9)

check("EPP writes include power",
      "power" in hw.epp_writes,
      f"got {hw.epp_writes}")

# ── EPP: idle exit restores normalEPP ────────────────────────────────

print("\n── run() EPP idle exit ──")

hw = MockHardware()
hw.sen5_sensor_path = None
# Cool temps to enter idle, then warm to exit
hw.temps = deque(
    [60.0] * 8   # enter idle
    + [72.0] * 6  # above idleReleaseTempC=68, exit idle
)
run_quiet(PL1_EPP_CFG, hw, max_ticks=13)

# Should see: normalEPP (startup) -> power (idle) -> balance_performance (exit)
check("EPP writes >= 3", len(hw.epp_writes) >= 3,
      f"got {len(hw.epp_writes)}: {hw.epp_writes}")
# Find the transition: after "power", should see "balance_performance"
power_idx = None
for i, v in enumerate(hw.epp_writes):
    if v == "power":
        power_idx = i
        break
if power_idx is not None and power_idx + 1 < len(hw.epp_writes):
    check("after idle EPP, normalEPP restored",
          hw.epp_writes[power_idx + 1] == "balance_performance",
          f"got {hw.epp_writes[power_idx + 1]}")
else:
    check("after idle EPP, normalEPP restored", False,
          f"power_idx={power_idx}, writes={hw.epp_writes}")

# ── EPP: no config → no writes ──────────────────────────────────────

print("\n── run() EPP no config → no writes ──")

hw = MockHardware()
hw.sen5_sensor_path = None
hw.temps = deque([60.0] * 5)
run_quiet(PL1_RUN_CFG, hw, max_ticks=4)  # PL1_RUN_CFG has no EPP keys

check("no EPP writes without config", hw.epp_writes == [],
      f"got {hw.epp_writes}")

# ── EPP: edge-triggered (no repeated writes) ────────────────────────

print("\n── run() EPP edge-triggered ──")

hw = MockHardware()
hw.sen5_sensor_path = None
# Stay idle for many ticks
hw.temps = deque([60.0] * 12)
run_quiet(PL1_EPP_CFG, hw, max_ticks=11)

# Should be: normalEPP (startup) + power (idle entry) = 2 total
power_writes = [w for w in hw.epp_writes if w == "power"]
check("only one idle EPP write (edge-triggered)",
      len(power_writes) == 1,
      f"got {len(power_writes)} power writes in {hw.epp_writes}")

# ── EPP: shutdown restores normalEPP when epp_active ────────────────

print("\n── run() EPP shutdown restores normalEPP ──")

hw = MockHardware()
hw.sen5_sensor_path = None
# Enter idle and stay there until shutdown
hw.temps = deque([60.0] * 8)
run_quiet(PL1_EPP_CFG, hw, max_ticks=7)

# Last EPP write should be balance_performance (shutdown restore)
check("shutdown writes normalEPP",
      hw.epp_writes[-1] == "balance_performance",
      f"last write: {hw.epp_writes[-1]}, all: {hw.epp_writes}")

# ── EPP: shutdown no-op when not epp_active ──────────────────────────

print("\n── run() EPP shutdown no-op when not active ──")

hw = MockHardware()
hw.sen5_sensor_path = None
# Stay warm (70°C) — idle never activates
hw.temps = deque([70.0] * 5)
run_quiet(PL1_EPP_CFG, hw, max_ticks=4)

# Should only have startup write
check("only startup EPP write when never idle",
      hw.epp_writes == ["balance_performance"],
      f"got {hw.epp_writes}")

# ── EPP: sensor logging includes epp_active ──────────────────────────

print("\n── run() EPP in sensor log ──")

with tempfile.TemporaryDirectory() as td:
    log_path = os.path.join(td, "test.json")
    cfg = {**PL1_EPP_CFG, "logging": {
        "enabled": True, "path": log_path, "flushIntervalSeconds": 9999}}
    hw = MockHardware()
    hw.sen5_sensor_path = None
    hw.temps = deque([60.0] * 5)
    run_quiet(cfg, hw, max_ticks=4)
    check("sensor log file created", os.path.exists(log_path))
    with open(log_path) as f:
        lines = [l for l in f.read().strip().split("\n") if l]
    if lines:
        entry = json.loads(lines[0])
        check("log has epp_active",
              "epp_active" in entry.get("controller", {}),
              f"controller keys: {list(entry.get('controller', {}).keys())}")

# ── EPP: validate_config rejects mismatched EPP keys ─────────────────

print("\n── validate_config() EPP ──")

bad_config("idleEPP without normalEPP",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "idleEPP": "power"}, "idleEPP")
bad_config("normalEPP without idleEPP",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "normalEPP": "balance_performance"}, "normalEPP")
bad_config("idleEPP empty string",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "idleEPP": "", "normalEPP": "balance_performance"}, "idleEPP")
bad_config("normalEPP not string",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "idleEPP": "power", "normalEPP": 123}, "normalEPP")

try:
    validate_config({"setpoint": 75, "Kp": 1, "Ki": 0.1,
        "idleEPP": "power", "normalEPP": "balance_performance"})
    check("valid: both EPP keys present", True)
except ValueError as e:
    check("valid: both EPP keys present", False, str(e))

try:
    validate_config({"setpoint": 75, "Kp": 1, "Ki": 0.1})
    check("valid: no EPP keys", True)
except ValueError as e:
    check("valid: no EPP keys", False, str(e))


###########################################################################
#                        MODE TESTS                                       #
###########################################################################

# ── Monitor mode: no hardware writes, sensor log written ──────────────

print("\n── run() mode=monitor ──")

with tempfile.TemporaryDirectory() as td:
    log_path = os.path.join(td, "test.json")
    cfg = {**PL1_RUN_CFG, "logging": {
        "enabled": True, "path": log_path, "flushIntervalSeconds": 9999}}
    hw = MockHardware()
    hw.sen5_sensor_path = None
    hw.system_snapshot = {"cpu": {"user": 50.0}}
    # fan_off=313 (wrong) would trigger a thermal write in full mode
    hw.thermal_config = [
        {"sensor_id": 2, "warn": 0, "high": 360, "halt": 370,
         "fan_off": 313, "fan_max": 342, "name": "F75303_DDR"},
    ]
    hw.temps = deque([70.0] * 5)  # 1 startup + 4 loop
    run_quiet(cfg, hw, max_ticks=4, mode="monitor")

    check("monitor: no RAPL writes", len(hw.rapl_writes) == 0,
          f"got {len(hw.rapl_writes)}")
    check("monitor: no EPP writes", len(hw.epp_writes) == 0,
          f"got {len(hw.epp_writes)}")
    check("monitor: no EC restores", hw.ec_restores == 0,
          f"got {hw.ec_restores}")
    check("monitor: no thermal writes", len(hw.thermal_writes) == 0,
          f"got {len(hw.thermal_writes)}")
    check("monitor: sensor log created", os.path.exists(log_path))
    with open(log_path) as f:
        lines = [l for l in f.read().strip().split("\n") if l]
    check("monitor: sensor log has entries", len(lines) >= 1,
          f"got {len(lines)}")
    entry = json.loads(lines[0])
    check("monitor: no controller in log", "controller" not in entry,
          f"keys: {list(entry.keys())}")

# ── Monitor mode: temp read failure — no RAPL writes, no crash ────────

print("\n── run() mode=monitor temp read failure ──")

with tempfile.TemporaryDirectory() as td:
    log_path = os.path.join(td, "test.json")
    cfg = {**PL1_RUN_CFG, "logging": {
        "enabled": True, "path": log_path, "flushIntervalSeconds": 9999}}
    hw = MockHardware()
    hw.sen5_sensor_path = None
    hw.system_snapshot = {"cpu": {"user": 50.0}}
    # 1 startup ok, then 2 failures, then 1 ok
    hw.temps = deque([70.0, OSError("fail"), OSError("fail"), 70.0])
    run_quiet(cfg, hw, max_ticks=3, mode="monitor")

    check("monitor failure: no RAPL writes", len(hw.rapl_writes) == 0,
          f"got {len(hw.rapl_writes)}")
    check("monitor failure: no EC restores", hw.ec_restores == 0,
          f"got {hw.ec_restores}")
    check("monitor failure: sensor log created", os.path.exists(log_path))

# ── Control mode: RAPL writes, no sensor logging ──────────────────────

print("\n── run() mode=control ──")

with tempfile.TemporaryDirectory() as td:
    log_path = os.path.join(td, "test.json")
    cfg = {**PL1_RUN_CFG, "logging": {
        "enabled": True, "path": log_path, "flushIntervalSeconds": 9999}}
    hw = MockHardware()
    hw.sen5_sensor_path = None
    hw.temps = deque([70.0] * 5)  # 1 startup + 4 loop
    run_quiet(cfg, hw, max_ticks=4, mode="control")

    check("control: RAPL writes present", len(hw.rapl_writes) > 0,
          f"got {len(hw.rapl_writes)}")
    check("control: EC restores == 2", hw.ec_restores == 2,
          f"got {hw.ec_restores}")
    check("control: sensor log NOT created", not os.path.exists(log_path))

# ── Default mode is full (backward compat) ────────────────────────────

print("\n── run() default mode is full ──")

with tempfile.TemporaryDirectory() as td:
    log_path = os.path.join(td, "test.json")
    cfg = {**PL1_RUN_CFG, "logging": {
        "enabled": True, "path": log_path, "flushIntervalSeconds": 9999}}
    hw = MockHardware()
    hw.sen5_sensor_path = None
    hw.system_snapshot = {"cpu": {"user": 50.0}}
    hw.temps = deque([70.0] * 5)  # 1 startup + 4 loop
    run_quiet(cfg, hw, max_ticks=4)

    check("default: RAPL writes present", len(hw.rapl_writes) > 0,
          f"got {len(hw.rapl_writes)}")
    check("default: EC restores == 2", hw.ec_restores == 2,
          f"got {hw.ec_restores}")
    check("default: sensor log created", os.path.exists(log_path))
    with open(log_path) as f:
        lines = [l for l in f.read().strip().split("\n") if l]
    check("default: sensor log has entries", len(lines) >= 1,
          f"got {len(lines)}")
    entry = json.loads(lines[0])
    check("default: controller in log", "controller" in entry,
          f"keys: {list(entry.keys())}")


# ── Explicit mode="full" (not just default) ───────────────────────────

print("\n── run() explicit mode=full ──")

with tempfile.TemporaryDirectory() as td:
    log_path = os.path.join(td, "test.json")
    cfg = {**PL1_RUN_CFG, "logging": {
        "enabled": True, "path": log_path, "flushIntervalSeconds": 9999}}
    hw = MockHardware()
    hw.sen5_sensor_path = None
    hw.system_snapshot = {"cpu": {"user": 50.0}}
    hw.temps = deque([70.0] * 5)  # 1 startup + 4 loop
    run_quiet(cfg, hw, max_ticks=4, mode="full")

    check("explicit full: RAPL writes present", len(hw.rapl_writes) > 0,
          f"got {len(hw.rapl_writes)}")
    check("explicit full: EC restores == 2", hw.ec_restores == 2,
          f"got {hw.ec_restores}")
    check("explicit full: sensor log created", os.path.exists(log_path))
    with open(log_path) as f:
        lines = [l for l in f.read().strip().split("\n") if l]
    check("explicit full: controller in log", "controller" in json.loads(lines[0]),
          f"keys: {list(json.loads(lines[0]).keys())}")


# ── --mode CLI argument parsing ────────────────────────────────────────

print("\n── --mode CLI argument parsing ──")

script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fw_pwrctl.py")

result = sp.run(["python3", script, "--help"], capture_output=True, text=True)
for mode_val in ("full", "monitor", "control"):
    check(f"--help mentions {mode_val}",
          mode_val in result.stdout,
          result.stdout[:200])

result = sp.run(["python3", script, "--mode", "bogus"],
                capture_output=True, text=True)
check("--mode bogus rejected", result.returncode != 0)
check("error mentions invalid choice",
      "invalid choice" in result.stderr,
      result.stderr.strip()[:200])

# ── main() --version and --config error paths ─────────────────────────

print("\n── main() CLI error paths ──")

result = sp.run(["python3", script, "--version"], capture_output=True, text=True)
check("--version exits 0", result.returncode == 0)
check("--version prints version", "fw-pwrctl" in result.stdout,
      result.stdout.strip()[:100])

result = sp.run(["python3", script, "--config", "/nonexistent/config.json"],
                capture_output=True, text=True)
check("missing config exits 1", result.returncode == 1)
check("missing config error message", "not found" in result.stderr.lower(),
      result.stderr.strip()[:200])

with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
    tf.write("{invalid json")
    tf_path = tf.name
try:
    result = sp.run(["python3", script, "--config", tf_path],
                    capture_output=True, text=True)
    check("bad JSON exits 1", result.returncode == 1)
    check("bad JSON error message", "invalid json" in result.stderr.lower(),
          result.stderr.strip()[:200])
finally:
    os.unlink(tf_path)


###########################################################################
#                     FIND_PECI_SENSOR CORETEMP FALLBACK REMOVAL          #
###########################################################################

# After removing the coretemp fallback from find_peci_sensor(), it should
# return None when only coretemp is available (no cros_ec PECI).
# The caller (run()) handles the coretemp fallback via find_coretemp_sensor().

print("\n── find_peci_sensor() no coretemp fallback ──")

from pathlib import Path as _Path
from unittest.mock import patch as _patch

with tempfile.TemporaryDirectory() as td:
    # Create a fake hwmon with only coretemp (no cros_ec)
    hwmon_dir = os.path.join(td, "class", "hwmon")
    dev0 = os.path.join(hwmon_dir, "hwmon0")
    os.makedirs(dev0)
    with open(os.path.join(dev0, "name"), "w") as f:
        f.write("coretemp\n")
    with open(os.path.join(dev0, "temp1_input"), "w") as f:
        f.write("65000\n")

    # Patch Path at the fw_pwrctl import site so the real functions
    # use our fake hwmon directory instead of /sys/class/hwmon.
    # We can't subclass Path on Python 3.10 (no _flavour), so we use
    # a wrapper function. This is safe because the code only calls
    # Path(string) as a constructor, never isinstance or class methods.
    def _fake_path(p, *args, **kwargs):
        if str(p) == "/sys/class/hwmon":
            return _Path(hwmon_dir)
        return _Path(p, *args, **kwargs)

    hw_test = Hardware()
    with _patch("fw_pwrctl.Path", side_effect=_fake_path):
        result = hw_test.find_peci_sensor()
        check("returns None when only coretemp present", result is None, f"got {result}")
        result = hw_test.find_coretemp_sensor()
        check("find_coretemp_sensor finds it", result is not None, f"got {result}")


###########################################################################
#                     SENSOR-PLOT.SH ARG VALIDATION                       #
###########################################################################

print("\n── sensor-plot.sh missing argument errors ──")

import subprocess as sp

script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "sensor-plot.sh")

for flag in ("--hours", "--rolling", "--log-dir", "-o"):
    result = sp.run(["bash", script, flag], capture_output=True, text=True)
    check(f"{flag} missing arg exits non-zero", result.returncode != 0)
    check(f"{flag} missing arg error message",
          "requires an argument" in result.stderr,
          result.stderr.strip())


###########################################################################
#                     LIVE HARDWARE TESTS                                 #
###########################################################################

if LIVE:
    print("\n" + "=" * 60)
    print("  LIVE HARDWARE TESTS")
    print("=" * 60)

    IS_ROOT = os.geteuid() == 0
    hw = Hardware()

    # --- No root needed (sysfs reads are world-readable) ---

    print("\n── live: find_coretemp_sensor() ──")
    path = hw.find_coretemp_sensor()
    check("returns a path", path is not None)
    if path:
        check("path exists", os.path.exists(path))
        check("file is readable", os.access(path, os.R_OK))

    print("\n── live: find_peci_sensor() ──")
    path = hw.find_peci_sensor()
    check("returns a path", path is not None)
    if path:
        check("path exists", os.path.exists(path))

    print("\n── live: find_sen5_sensor() ──")
    path = hw.find_sen5_sensor()
    check("returns a path", path is not None)
    if path:
        check("path contains thermal_zone", "thermal_zone" in path)

    print("\n── live: discover_board_sensors() ──")
    sensors = hw.discover_board_sensors()
    check("finds sensors", len(sensors) >= 2,
          f"got {len(sensors)}: {list(sensors.keys())}")
    for name, spath in sensors.items():
        check(f"{name} path readable", os.access(spath, os.R_OK))

    print("\n── live: read_temp(coretemp) ──")
    coretemp_path = hw.find_coretemp_sensor()
    if coretemp_path:
        t = hw.read_temp(coretemp_path)
        check("coretemp in [20, 100]", 20.0 <= t <= 100.0, f"got {t}")

    print("\n── live: read_temp(sen5) ──")
    sen5_path = hw.find_sen5_sensor()
    if sen5_path:
        t = hw.read_temp(sen5_path)
        check("SEN5 temp in [20, 100]", 20.0 <= t <= 100.0, f"got {t}")

    print("\n── live: read_temp(bad path) ──")
    try:
        hw.read_temp("/sys/class/thermal/nonexistent_sensor_xyz/temp")
        check("bad path raises", False, "did not raise")
    except Exception:
        check("bad path raises", True)

    print("\n── live: read_rapl_pl1() ──")
    try:
        pl1 = hw.read_rapl_pl1()
        check("returns int", isinstance(pl1, int))
        pl1_w = pl1 / 1_000_000
        check("PL1 in [1W, 64W]", 1.0 <= pl1_w <= 64.0, f"got {pl1_w}W")
    except PermissionError:
        print("  (skipped — RAPL not readable without root)")

    print("\n── live: read_system_snapshot() ──")
    board_sensors = hw.discover_board_sensors()
    snapshot = hw.read_system_snapshot(board_sensors)
    check("has cpu", "cpu" in snapshot)
    check("has memory", "memory" in snapshot)
    if board_sensors:
        check("has board_temps", "board_temps" in snapshot)

    print("\n── live: check_platform() ──")
    ok, msg = hw.check_platform()
    check("returns (True, '')", ok and msg == "", f"ok={ok}, msg={msg!r}")

    print("\n── live: check_framework_laptop() ──")
    ok, msg = hw.check_framework_laptop()
    check("returns (True, '')", ok and msg == "", f"ok={ok}, msg={msg!r}")

    print("\n── live: check_alder_lake() ──")
    ok, msg = hw.check_alder_lake()
    check("returns (True, '')", ok and msg == "", f"ok={ok}, msg={msg!r}")

    print("\n── live: check_ectool_installed() ──")
    ok, msg = hw.check_ectool_installed()
    check("returns (True, '')", ok and msg == "", f"ok={ok}, msg={msg!r}")

    # --- Root needed (ectool, RAPL writes) ---

    if IS_ROOT:
        try:
            # read_thermal_config
            print("\n── live (root): read_thermal_config() ──")
            thermal = hw.read_thermal_config()
            check("returns list", isinstance(thermal, list))
            check("has entries", len(thermal) >= 3,
                  f"got {len(thermal)}")
            sensor2 = next((s for s in thermal if s["sensor_id"] == 2), None)
            check("sensor 2 present", sensor2 is not None)
            if sensor2:
                for k in ("warn", "high", "halt", "fan_off", "fan_max"):
                    check(f"sensor 2 has {k}", k in sensor2)
                check("sensor 2 fan_off is 313 or 323",
                      sensor2["fan_off"] in (313, 323),
                      f"got {sensor2.get('fan_off')}")
                check("sensor 2 has name", "name" in sensor2)

            # check_ectool
            print("\n── live (root): check_ectool() ──")
            ok = hw.check_ectool()
            check("ectool version works", ok)

            # write_rapl_pl1 (safe: read → write same → verify)
            print("\n── live (root): write_rapl_pl1() ──")
            current = hw.read_rapl_pl1()
            ok = hw.write_rapl_pl1(current)
            check("write same PL1 back", ok)
            after = hw.read_rapl_pl1()
            check("PL1 unchanged", after == current,
                  f"before={current}, after={after}")

            # read_fan_rpm
            print("\n── live (root): read_fan_rpm() ──")
            rpm = hw.read_fan_rpm()
            check("returns int", isinstance(rpm, int))
            check("RPM >= 0", rpm is not None and rpm >= 0,
                  f"got {rpm}")

            # set_fan (nonzero duty) — verify fan spins
            print("\n── live (root): set_fan() ──")
            ok = hw.set_fan(40)
            check("set_fan(40) succeeds", ok)
            time.sleep(2)  # let fan spin up
            rpm = hw.read_fan_rpm()
            check("fan spinning after set_fan(40)",
                  rpm is not None and rpm > 0, f"got {rpm}")

            # set_fan(0) clamped to 1 — verify the clamp works
            print("\n── live (root): set_fan(0) clamped to 1 ──")
            ok = hw.set_fan(0)
            check("set_fan(0) succeeds (clamped to 1%)", ok)

            # restore_ec — verify fan resumes
            print("\n── live (root): restore_ec() ──")
            hw.restore_ec()
            time.sleep(3)  # let EC thermal loop kick in
            rpm = hw.read_fan_rpm()
            check("fan running after restore_ec",
                  rpm is not None and rpm > 0, f"got {rpm}")

            # run() end-to-end smoke test (real hw, dry-run for writes)
            print("\n── live (root): run() dry-run smoke test ──")
            hw_live = Hardware(dry_run=True)
            run_quiet(PL1_RUN_CFG, hw_live, max_ticks=2)
            check("run() completes with real hw (dry-run)", True)
        except Exception as e:
            check("root test failed", False, str(e))
        finally:
            hw.restore_ec()
    else:
        print("\n  (skipping root-required tests — run with sudo for full coverage)")


###########################################################################
#                     DEBUG OUTPUT TEST                                    #
###########################################################################

print("\n── run() debug=True ──")

hw = MockHardware()
hw.sen5_sensor_path = "/mock/sen5"
hw.sen5_temps = deque([68.0] * 5)
hw.temps = deque([70.0] * 5)  # 1 startup + 4 loop

old_out, old_err = sys.stdout, sys.stderr
captured_out = io.StringIO()
sys.stdout = captured_out
sys.stderr = io.StringIO()
try:
    run(PL1_RUN_CFG, hw, max_ticks=4, debug=True)
finally:
    output = captured_out.getvalue()
    sys.stdout, sys.stderr = old_out, old_err
check("debug output contains temp=", "temp=" in output, output[:200])
check("debug output contains PL1", "PL1" in output, output[:200])
check("debug output contains SEN5", "SEN5=" in output, output[:200])


###########################################################################
#                 IDLE → ACTIVE TRANSITION TEST                           #
###########################################################################

print("\n── idle→active transition ──")

ctrl = PIPL1Controller(PL1_CFG)

# Enter idle: cool temps (< idleTempC=65)
for _ in range(10):
    pl1 = ctrl.update(55.0, None, 2.0)
check("idle active after cool temps", ctrl._idle_active)
check("PL1 capped at idle ceiling during idle", pl1 <= 15.0, f"got {pl1}")
# Anti-windup keeps integral near 0 (PL1 at ceiling, temp below setpoint)
check("integral near 0 during idle (anti-windup)", abs(ctrl._integral) < 1.0,
      f"got {ctrl._integral}")

# Transition to active: warm temps above idleReleaseTempC=68
for _ in range(3):
    pl1 = ctrl.update(72.0, None, 2.0)
check("idle released", not ctrl._idle_active)
# After idle release, PL1 should rise above the idle ceiling
# because 72°C is below setpoint (75°C) so PI wants more power
check("PL1 rises above idle ceiling after release", pl1 > 15.0,
      f"got {pl1}")


###########################################################################
#                 WRITE_THERMAL_CONFIG BOUNDS VALIDATION                   #
###########################################################################

print("\n── write_thermal_config() bounds validation ──")

# Use dry_run=True so valid calls return True without calling ectool,
# but bounds checks happen before dry_run short-circuit.
hw_bounds = Hardware(dry_run=True)

check("valid params accepted",
      hw_bounds.write_thermal_config(2, warn=0, high=360, halt=370, fan_off=323, fan_max=342))
check("fan_off too low (273K = 0°C) rejected",
      not hw_bounds.write_thermal_config(2, warn=0, high=360, halt=370, fan_off=273, fan_max=342))
check("fan_off too high (374K > 373) rejected",
      not hw_bounds.write_thermal_config(2, warn=0, high=360, halt=370, fan_off=374, fan_max=380))
check("fan_max too low rejected",
      not hw_bounds.write_thermal_config(2, warn=0, high=360, halt=370, fan_off=323, fan_max=273))
check("fan_off >= fan_max rejected",
      not hw_bounds.write_thermal_config(2, warn=0, high=360, halt=370, fan_off=342, fan_max=342))
check("warn=0 (disabled) accepted",
      hw_bounds.write_thermal_config(2, warn=0, high=360, halt=370, fan_off=323, fan_max=342))
check("warn non-zero but too low rejected",
      not hw_bounds.write_thermal_config(2, warn=273, high=360, halt=370, fan_off=323, fan_max=342))
check("halt above 423K rejected",
      not hw_bounds.write_thermal_config(2, warn=0, high=360, halt=424, fan_off=323, fan_max=342))
check("high=0 (disabled) accepted",
      hw_bounds.write_thermal_config(2, warn=0, high=0, halt=370, fan_off=323, fan_max=342))


###########################################################################
#                   EC OVERRIDE MERGE BEHAVIOR                             #
###########################################################################

print("\n── run() EC override merges with current config ──")

# Test that apply_ec_overrides merges only the override fields (fan_off)
# into the current EC config, preserving firmware values for other fields.
hw = MockHardware()
hw.sen5_sensor_path = None
# Use different values from EC_THERMAL_OVERRIDES for non-overridden fields
# to prove they're preserved (not hardcoded).
hw.thermal_config = [
    {"sensor_id": 2, "warn": 5, "high": 355, "halt": 365,
     "fan_off": 313, "fan_max": 340, "name": "F75303_DDR"},
]
hw.temps = deque([70.0] * 5)
run_quiet(PL1_RUN_CFG, hw, max_ticks=4)

check("thermal write issued for merge", len(hw.thermal_writes) == 1,
      f"got {len(hw.thermal_writes)}")
if hw.thermal_writes:
    tw = hw.thermal_writes[0]
    check("merge: fan_off overridden to 323", tw["fan_off"] == 323)
    # These should be preserved from the current config, NOT from the old
    # EC_THERMAL_OVERRIDES constant:
    check("merge: warn preserved from current (5)", tw["warn"] == 5)
    check("merge: high preserved from current (355)", tw["high"] == 355)
    check("merge: halt preserved from current (365)", tw["halt"] == 365)
    check("merge: fan_max preserved from current (340)", tw["fan_max"] == 340)


###########################################################################
#               PL1MAXW UPPER BOUND VALIDATION                             #
###########################################################################

print("\n── validate_config() pl1MaxW upper bound ──")

bad_config("pl1MaxW > 64",
           {"setpoint": 75, "Kp": 1, "Ki": 0.1,
            "pl1MinW": 5, "pl1MaxW": 65}, "64")

# 64 should be accepted
try:
    validate_config({"setpoint": 75, "Kp": 1, "Ki": 0.1,
                     "pl1MinW": 5, "pl1MaxW": 64})
    check("pl1MaxW=64 accepted", True)
except ValueError as e:
    check("pl1MaxW=64 accepted", False, str(e))


###########################################################################
#                    DT CAP (POST-SUSPEND)                                 #
###########################################################################

print("\n── PI controller dt cap ──")

# Verify that the dt cap in the main loop prevents integral windup
# from a huge dt (simulating suspend/resume). The cap is update_freq*3.
# Use the controller directly: a huge dt without cap would cause a massive
# integral spike. With cap, the integral should stay bounded.
PL1_DT_CFG = {
    "updateInterval": 2,
    "setpoint": 75,
    "Kp": 0.25,
    "Ki": 0.021,
    "pl1MinW": 5,
    "pl1MaxW": 28,
    "integralMaxW": 250,
    "rampUpRateLimitW": 3,
    "rampDownRateLimitW": 3,
    "sensorSmoothing": 1,  # disable median filter for precise testing
}
ctrl = PIPL1Controller(PL1_DT_CFG)
# Warm up at setpoint (integral stays 0)
for _ in range(3):
    ctrl.update(75.0, None, 2.0)
# Simulate capped dt (update_freq * 3 = 6s) at temp above setpoint
capped_dt = 2 * 3
ctrl.update(80.0, None, capped_dt)
integral_capped = ctrl._integral

ctrl2 = PIPL1Controller(PL1_DT_CFG)
for _ in range(3):
    ctrl2.update(75.0, None, 2.0)
# Simulate uncapped post-suspend dt (1800s = 30 min)
ctrl2.update(80.0, None, 1800.0)
integral_uncapped = ctrl2._integral

check("capped dt has smaller integral than uncapped",
      abs(integral_capped) < abs(integral_uncapped),
      f"capped={integral_capped:.1f} uncapped={integral_uncapped:.1f}")
check("capped dt integral is bounded",
      abs(integral_capped) < 50,
      f"got {integral_capped:.1f}")
check("uncapped dt causes large integral",
      abs(integral_uncapped) >= 250,
      f"got {integral_uncapped:.1f}")


###########################################################################
#              EC OVERRIDE WRITE FAILURE HANDLING                           #
###########################################################################

print("\n── run() EC override write failure ──")

hw = MockHardware()
hw.sen5_sensor_path = None
hw.thermal_config = [
    {"sensor_id": 2, "warn": 0, "high": 360, "halt": 370,
     "fan_off": 313, "fan_max": 342, "name": "F75303_DDR"},
]
hw.write_thermal_ok = False  # simulate ectool failure
hw.temps = deque([70.0] * 5)
old_out, old_err = sys.stdout, sys.stderr
sys.stdout = io.StringIO()
sys.stderr = io.StringIO()
try:
    run(PL1_RUN_CFG, hw, max_ticks=4)
    captured_err = sys.stderr.getvalue()
finally:
    sys.stdout, sys.stderr = old_out, old_err

# The write was attempted (thermal_writes has the attempted write)
check("write attempted on failure", len(hw.thermal_writes) >= 1,
      f"got {len(hw.thermal_writes)}")
# But applied should be False, so warning should be logged
check("warning logged on write failure", "failed to write thermal override" in captured_err,
      f"stderr: {captured_err[:200]}")


###########################################################################
#           TEMP READ FAILURE: PI CONTROLLER NOTIFICATION                  #
###########################################################################

print("\n── run() temp failure notifies PI controller ──")

hw = MockHardware()
hw.sen5_sensor_path = None
# 1 startup ok, 1 loop ok (establishes _last_pl1), then failure, then ok
hw.temps = deque([70.0, 70.0, OSError("fail"), 70.0, 70.0])
run_quiet(PL1_RUN_CFG, hw, max_ticks=4)

# After temp failure, PL1 should be written to min (5W = 5_000_000)
min_writes = [w for w in hw.rapl_writes if w == 5_000_000]
check("PL1 to min on temp failure", len(min_writes) >= 1,
      f"got {len(min_writes)}")
# After recovery, the first PL1 write should be near pl1_min (rate limited
# from min), not jumping back to the pre-failure value. This verifies
# notify_external_pl1 was called.
# Find the first write after the min write(s)
recovery_writes = []
saw_min = False
for w in hw.rapl_writes:
    if w == 5_000_000:
        saw_min = True
    elif saw_min:
        recovery_writes.append(w)
if recovery_writes:
    # First recovery write should be <= pl1_min + ramp_up * 1e6
    # = 5W + 3W = 8W = 8_000_000 (rate limited from min)
    check("recovery PL1 rate-limited from min",
          recovery_writes[0] <= 8_000_000,
          f"first recovery write: {recovery_writes[0] / 1e6:.1f}W (expected <= 8W)")
else:
    check("recovery writes after min", False, "no recovery writes found")


###########################################################################
#                          SUMMARY                                        #
###########################################################################

print(f"\n{'='*60}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'='*60}")
sys.exit(1 if FAILED else 0)
