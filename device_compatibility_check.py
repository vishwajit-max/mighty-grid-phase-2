#!/usr/bin/env python3
"""Read-only diagnostics for Android Recorder Studio device compatibility.

Usage:
    python device_compatibility_check.py
    python device_compatibility_check.py SERIAL

This script does not change device settings or send taps/gestures.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(command: list[str], timeout: float = 8.0) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        output = (result.stdout or "").strip()
        error = (result.stderr or "").strip()
        return result.returncode, output or error
    except Exception as exc:
        return 1, str(exc)


def get_connected_devices() -> list[tuple[str, str]]:
    code, output = run(["adb", "devices", "-l"], timeout=10)
    if code:
        raise RuntimeError(f"Could not execute adb devices: {output}")
    devices = []
    for line in output.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        serial = parts[0]
        state = parts[1] if len(parts) > 1 else "unknown"
        devices.append((serial, state))
    return devices


def main() -> int:
    try:
        devices = get_connected_devices()
    except Exception as exc:
        print(f"ERROR: {exc}")
        print("Install Android platform-tools and confirm adb is on PATH.")
        return 2

    serial = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    ready = [serial_id for serial_id, state in devices if state == "device"]

    if serial:
        states = {device_serial: state for device_serial, state in devices}
        state = states.get(serial, "not listed")
        if state != "device":
            print(f"Selected serial: {serial} — ADB state: {state}")
            print("Unlock the phone, accept its USB debugging authorization prompt, and rerun this check.")
            return 2
    elif len(ready) == 1:
        serial = ready[0]
    elif len(ready) > 1:
        print("More than one device is ready. Pass the intended serial as an argument.")
        for device_serial, state in devices:
            print(f"  {device_serial:24} {state}")
        return 2
    else:
        print("No authorized Android device is ready in ADB.")
        for device_serial, state in devices:
            print(f"  {device_serial:24} {state}")
        return 2

    print("=" * 68)
    print("ANDROID RECORDER STUDIO — DEVICE COMPATIBILITY REPORT")
    print("=" * 68)
    print(f"Selected serial: {serial}")

    properties = [
        ("Manufacturer", "ro.product.manufacturer"),
        ("Brand", "ro.product.brand"),
        ("Model", "ro.product.model"),
        ("Device codename", "ro.product.device"),
        ("Android release", "ro.build.version.release"),
        ("Android SDK", "ro.build.version.sdk"),
        ("CPU ABI", "ro.product.cpu.abi"),
        ("Hardware/SoC", "ro.hardware"),
    ]
    for label, prop in properties:
        _, value = run(["adb", "-s", serial, "shell", "getprop", prop], timeout=5)
        print(f"{label:20}: {value or 'Unknown'}")

    for label, args in (
        ("Effective screen size", ["wm", "size"]),
        ("Screen density", ["wm", "density"]),
        ("Current rotation", ["settings", "get", "system", "user_rotation"]),
    ):
        _, value = run(["adb", "-s", serial, "shell", *args], timeout=5)
        print(f"{label:20}: {value or 'Unavailable'}")

    print("\nHOST TOOLS")
    scrcpy_exe = shutil.which("scrcpy")
    print(f"scrcpy executable   : {scrcpy_exe or 'Not found on PATH'}")
    if scrcpy_exe:
        _, version = run([scrcpy_exe, "--version"], timeout=5)
        print(f"scrcpy version      : {version or 'Could not detect'}")
        adjacent_server = Path(scrcpy_exe).resolve().parent / "scrcpy-server"
        print(f"adjacent server     : {adjacent_server if adjacent_server.is_file() else 'Not found'}")
    configured_server = os.environ.get("MG_SCRCPY_SERVER")
    print(f"MG_SCRCPY_SERVER    : {configured_server or '(not set; auto-discovery used)'}")
    print(f"MG_SCRCPY_VERSION   : {os.environ.get('MG_SCRCPY_VERSION', '(auto-detected)')}")
    print(f"MG_MAATOUCH_BIN     : {os.environ.get('MG_MAATOUCH_BIN', '(auto-discovery used)')}")
    print(f"MG_TOUCH_TRANSPORT  : {os.environ.get('MG_TOUCH_TRANSPORT', 'auto')}")
    local_maatouch = ROOT / "android_telemetry" / "telemetry" / "maatouch"
    print(f"project MaaTouch    : {local_maatouch if local_maatouch.is_file() else 'Not found at standard project path'}")

    # Use ls instead of `adb shell sh -c ...`: adb shell joins remote
    # arguments into a command string, and an unquoted `sh -c` payload can
    # be parsed incorrectly on some host/shell combinations.
    remote_code, remote_info = run([
        "adb", "-s", serial, "shell", "ls", "-l", "/data/local/tmp/maatouch"
    ], timeout=5)
    if remote_code == 0 and remote_info and "No such file" not in remote_info:
        perms = remote_info.split()[0] if remote_info.split() else ""
        executable = "yes" if len(perms) >= 10 and "x" in perms[3:10] else "not confirmed"
        print(f"device MaaTouch     : present (executable: {executable})")
        print(f"device file details : {remote_info}")
    else:
        print(f"device MaaTouch     : missing or unreadable ({remote_info or 'no output'})")

    try:
        import uiautomator2  # noqa: F401
        print("uiautomator2        : Python package import OK")
    except Exception as exc:
        print(f"uiautomator2        : IMPORT ERROR — {exc}")

    brand = run(["adb", "-s", serial, "shell", "getprop", "ro.product.brand"], timeout=5)[1].lower()
    manufacturer = run(["adb", "-s", serial, "shell", "getprop", "ro.product.manufacturer"], timeout=5)[1].lower()
    if any(token in brand + " " + manufacturer for token in ("poco", "xiaomi", "redmi")):
        print("\nPOCO / XIAOMI / REDMI CHECK")
        print("If taps do not reach the phone even though ADB reports 'device', check Developer options.")
        print("Some MIUI/HyperOS versions require enabling 'USB debugging (Security settings)' as well as normal USB debugging.")
        print("The setting name and availability can vary by software build.")

    print("\nThis is a read-only report. It does not prove that touch injection succeeds;")
    print("it confirms the selected serial, device-reported properties and host prerequisites.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
