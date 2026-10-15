#!/usr/bin/env python3
"""
app_priming.py
=====================================================================
App Lifecycle & Cache Inflation for MightyGrid.
Supports --silent mode: micro-cycles the Activity to spoof Session ID 
while keeping the app alive in the background for telemetry hooks.
=====================================================================
"""

import argparse
import json
import subprocess
import sys
import time


def run_adb(cmd_list, serial=None):
    base = ["adb"]
    if serial:
        base.extend(["-s", serial])
    base.extend(cmd_list)
    try:
        return subprocess.check_output(base, stderr=subprocess.STDOUT, text=True).strip()
    except subprocess.CalledProcessError as e:
        return e.output.strip()


def get_launch_component(package, serial=None):
    out = run_adb(["shell", "cmd", "package", "resolve-activity", "--brief", package], serial)
    for line in out.splitlines():
        if "/" in line and package in line:
            return line.strip()
    if "chrome" in package.lower():
        return f"{package}/com.google.android.apps.chrome.Main"
    return package


def get_app_telemetry(package, serial=None):
    telemetry = {
        "target_app (Installer Package)": package,
        "installed (Install Status)": False,
        "app_version (App version / build)": "Unknown",
        "first_install_time (Install time)": "Unknown",
        "last_update_time (Update time)": "Unknown",
        "priv_data_kb (App storage / cache)": 0,
        "ext_data_kb (App storage / cache)": 0,
        "session_state (Session ID)": "Inactive"
    }

    pkg_out = run_adb(["shell", "dumpsys", "package", package], serial)
    if "Unable to find package" not in pkg_out and pkg_out.strip():
        telemetry["installed (Install Status)"] = True
        for line in pkg_out.splitlines():
            line = line.strip()
            if line.startswith("versionName="):
                # Overwrite on every match, so we end up with the LAST one —
                # the running (updated) build, not the frozen system APK.
                telemetry["app_version (App version / build)"] = line.split("=", 1)[1]
            elif line.startswith("firstInstallTime="):
                telemetry["first_install_time (Install time)"] = line.split("=", 1)[1]
            elif line.startswith("lastUpdateTime="):
                telemetry["last_update_time (Update time)"] = line.split("=", 1)[1]

    pid_out = run_adb(["shell", "pidof", package], serial)
    if pid_out.strip():
        main_pid = pid_out.split()[0]
        telemetry["session_state (Session ID)"] = f"Active (PID {main_pid})"

    priv_du = run_adb(["shell", "su", "-c", f"du -k -d 0 /data/data/{package}"], serial)
    if priv_du and not priv_du.startswith("du:"):
        try:
            telemetry["priv_data_kb (App storage / cache)"] = int(priv_du.split()[0])
        except ValueError:
            pass

    ext_du = run_adb(["shell", "du", "-k", "-d", "0", f"/sdcard/Android/data/{package}"], serial)
    if ext_du and not ext_du.startswith("du:"):
        try:
            telemetry["ext_data_kb (App storage / cache)"] = int(ext_du.split()[0])
        except ValueError:
            pass

    return telemetry


def reset_application(package, serial=None, full_reset=False, silent=False):
    print("\n=== 2. RESET & INJECT ===")
    if full_reset:
        print(f"[*] FULL RESET: pm clear {package}")
        run_adb(["shell", "pm", "clear", package], serial)
    elif silent:
        print(f"[*] Silent mode active: Using 'am kill' to safely drop Session PID.")
        run_adb(["shell", "am", "kill", package], serial)
    else:
        print(f"[*] SESSION RESET: am force-stop {package}")
        run_adb(["shell", "am", "force-stop", package], serial)
    time.sleep(1)


def inflate_caches(package, serial=None):
    print("\n=== 3. SPOOFING CACHES ===")
    print(f"[*] Bloating priv_data_kb (Internal App Cache)...")
    run_adb(["shell", "su", "-c", f"dd if=/dev/urandom of=/data/data/{package}/cache/mg_junk.bin bs=1024 count=12000 2>/dev/null"], serial)
    
    print(f"[*] Bloating ext_data_kb (External App Cache)...")
    run_adb(["shell", "mkdir", "-p", f"/sdcard/Android/data/{package}/cache"], serial)
    run_adb(["shell", "dd", "if=/dev/urandom", f"of=/sdcard/Android/data/{package}/cache/mg_junk.bin", "bs=1024", "count=5000", "2>/dev/null"], serial)


def warm_application(package, serial=None, silent=False):
    print("\n=== 4. WARM (CYCLING SESSION PID) ===")
    print("  [i] App launch disabled -- Chrome will not be opened.")


def print_summary(baseline, final):
    print("\n=== 5. SUMMARY: ORIGINAL VS CHANGED ===")
    print(f"{'Parameter':<45} | {'Original (Baseline)':<26} | {'Changed (Final Posture)':<26}")
    print("-" * 101)

    for k in baseline.keys():
        orig = str(baseline[k])
        post = str(final[k])
        marker = "*" if orig != post else " "
        print(f"{marker} {k:<43} | {orig:<26} | {post:<26}")

    print("-" * 101)
    print("(*) indicates a dynamically injected or updated parameter.")


def main():
    parser = argparse.ArgumentParser(description="MightyGrid App Lifecycle & Cache Inflation")
    parser.add_argument("-p", "--package", required=True, help="Target application package")
    parser.add_argument("-s", "--serial", help="Device ADB serial")
    parser.add_argument("--full-reset", action="store_true", help="Clear app data completely")
    parser.add_argument("--silent", action="store_true", help="Minimize app immediately upon launch")
    args = parser.parse_args()

    baseline_telemetry = get_app_telemetry(args.package, args.serial)
    print(f"=== 1. BASELINE ({args.package}) ===")
    print(json.dumps(baseline_telemetry, indent=2))

    reset_application(args.package, args.serial, args.full_reset, args.silent)
    inflate_caches(args.package, args.serial)
    warm_application(args.package, args.serial, args.silent)

    final_telemetry = get_app_telemetry(args.package, args.serial)
    print("\n=== 6. VERIFY FINAL POSTURE ===")
    print(json.dumps(final_telemetry, indent=2))

    print_summary(baseline_telemetry, final_telemetry)


if __name__ == "__main__":
    main()