# MightyGrid Telemetry — Full Pipeline Documentation

**Dates:** September 16–17, 2026  
**Project:** MightyGrid  
**Purpose:** Network telemetry preparation, device-environment QA, telemetry verification, and restoration.

The documented workflow covers carrier identity changes, traffic-counter warming, public-IP rotation through a proxy pool, rooted-device local-IP changes, hardware identity overrides, app telemetry changes, battery state changes, verification, and restoration.

The `android-proxy-rotator` functionality is integrated directly into the scripts; there is no separate rotator folder to run.

---

## Table of Contents

- [Overview](#overview)
- [Pipeline Phases](#pipeline-phases)
- [Files](#files)
- [Usage](#usage)
- [How Rotation Works](#how-rotation-works)
- [How the Local IP Change Works](#how-the-local-ip-change-works)
- [How Hardware Identity Spoofing Works (Phase 4.1)](#how-hardware-identity-spoofing-works-phase-41)
- [How Extended Telemetry Works (Phase 4.2)](#how-extended-telemetry-works-phase-42)
- [Device Verification — Independent ADB Check](#device-verification--independent-adb-check)
- [Reading the Output](#reading-the-output)
- [Restoring the Phone](#restoring-the-phone)
- [Requirements](#requirements)
- [Notes & Troubleshooting](#notes--troubleshooting)
- [Quick Command Reference](#quick-command-reference)
- [Project Structure](#project-structure)
- [Scope](#scope)

---

# Overview

MightyGrid Telemetry is a controlled Android device QA pipeline designed to prepare, modify, verify, and restore selected network and application-observable device state.

The workflow includes:

- Network preparation
- Proxy rotation
- Public-IP verification
- Local-IP preparation on rooted devices
- Hardware identity QA profiles
- Regional persona configuration
- Extended app telemetry
- Battery state testing
- Media-count testing
- Application priming
- Independent ADB verification
- State restoration

The system distinguishes between:

- **Changeable values**
- **Synthetic / report-only values**
- **Read-only values**
- **Device ground-truth values**

This distinction is important when interpreting the generated reports.

---

# Pipeline Phases

## Phase 1 — Network Preparation

Phase 1 handles network-related preparation:

- Carrier identity
- Traffic-counter warming
- Public-IP proxy rotation
- Proxy verification
- Network-location information
- Rooted-device local-IP changes
- Timezone information

## Phase 3 — Application Priming

Phase 3 handles application-level preparation:

- Cache inflation
- PID cycling

## Phase 4.1 — Hardware Identity

Phase 4.1 handles logical Android hardware identity properties such as:

- Brand
- Manufacturer
- Model
- Device
- Product
- Build
- Fingerprint
- SoC
- ABI

## Phase 4.2 — Extended Telemetry

Phase 4.2 handles additional application-observable state:

- System locale
- Timezone
- Screen orientation
- Photo/media count
- Battery level
- Battery status
- AC/USB state
- Wireless state

---

# Files

| File | Purpose |
|---|---|
| `Tele/network_telemetry_prep.py` | Standalone network preparation, in-file proxy rotator, and local-IP change. Source of truth for `proxies.txt` and `state.json`. |
| `telemetry/network_priming.py` | Pipeline version of Phase 1 in `mighty_grid_run.py`. Delegates proxy rotation to the Tele script. |
| `telemetry/mighty_grid_run.py` | Master orchestrator covering Phases 1–4.2. |
| `telemetry/device_profiles.json` | Device hardware profiles for Phase 4.1. |
| `telemetry/regional_profiles.json` | Regional QA profiles for locale, timezone, and location configuration. |
| `telemetry/regional_persona.py` | Loader for regional profiles. |
| `telemetry/app_priming.py` | Phase 3 application cache inflation and PID cycling. |
| `telemetry/browser_cloaking.js` | CDP browser shim. |
| `telemetry/sensor_inject.js` | Frida sensor-injection script. |
| `telemetry/sensor_persona_manager.py` | Frida attach helper. |
| `Tele/proxies.txt` | Proxy pool configuration. |
| `Tele/state.json` | Proxy rotation state, liveness cache, and IP history. |

## Proxy Entry Formats

`Tele/proxies.txt` supports:

```text
host:port
user:pass@host:port
*host:port
```

Optional location labels can be added:

```text
host:port # Country City
```

Example:

```text
192.0.2.10:8080 # India Pune
```

For gateway entries beginning with `*`, the script can generate a fresh random username and password per run.

## Runtime State Files

The pipeline may create:

```text
telemetry/hardware_identity_state.json
telemetry/extended_telemetry_state.json
telemetry/.mg_last_device_profile
```

---

# Usage

## Full Preparation — Standalone

Run:

```bash
cd Tele
python network_telemetry_prep.py
```

The standalone script produces:

1. **BASELINE JSON**
2. **GENERATING & APPLYING NETWORK PROFILE**
3. **VERIFY FINAL POSTURE JSON**
4. **ORIGINAL VS CHANGED** summary

The report can contain:

```text
PASS
FAIL
READ-ONLY
```

---

## Pipeline Version

Run:

```bash
cd telemetry
python network_priming.py
```

For a specific device:

```bash
python network_priming.py -s <serial>
```

The pipeline version uses the same network preparation logic and delegates proxy rotation to:

```text
Tele/network_telemetry_prep.py
```

---

## Full Pipeline

Run:

```bash
cd telemetry
python mighty_grid_run.py -p com.android.chrome
```

Use a specific device profile:

```bash
python mighty_grid_run.py \
    -p com.android.chrome \
    --device-profile poco-m6-pro
```

Use profile cycling:

```bash
python mighty_grid_run.py \
    -p com.android.chrome \
    --device-profile cycle
```

---

## Device Profile Selection

The `--device-profile` option supports:

| Value | Behavior |
|---|---|
| `random` | Default. Randomly selects a profile while avoiding the immediately previous profile. |
| `cycle` | Uses profiles in round-robin order. |
| `<id>` | Uses a specific profile. |

Available explicit profile IDs:

```text
realme-narzo-70x
poco-m6-pro
oppo-reno-12
pixel-6
```

---

# Proxy-Only Utilities

Run these commands from the `Tele` directory:

```bash
cd Tele

python network_telemetry_prep.py --check
python network_telemetry_prep.py --reset
python network_telemetry_prep.py --list
python network_telemetry_prep.py --refresh-free
python network_telemetry_prep.py --rotate-only
```

| Command | Purpose |
|---|---|
| `--check` | Show the current proxy and observed exit IP. |
| `--reset` | Remove the proxy and restore direct connection. |
| `--list` | Show the configured proxy pool. |
| `--refresh-free` | Fetch and test a fresh free proxy list. |
| `--rotate-only` | Rotate the proxy without running the complete preparation flow. |

> `telemetry/network_priming.py` does not expose these proxy-only options.

---

# How Rotation Works

Each run selects a working proxy while avoiding:

- The previously used proxy
- The proxy currently configured on the phone

## Instant Path

A proxy verified within the previous 10 minutes may be reused from the liveness cache stored in:

```text
Tele/state.json
```

This reduces unnecessary re-testing.

## Parallel Path

If no valid cached proxy is available, proxy candidates are tested in batches.

The documented implementation tests candidates in batches of:

```text
10
```

Candidates are tested using `curl` against:

```text
https://api.ipify.org
```

A candidate must:

- Support CONNECT
- Respond successfully
- Produce a working exit IP
- Produce an observed exit IP different from the direct connection IP

---

## Applying the Proxy

The Android global HTTP proxy is configured using:

```bash
adb shell settings put global http_proxy <host>:<port>
```

Rotation state is recorded in:

```text
Tele/state.json
```

Relevant state fields include:

```text
previous_ip
current_ip
history
```

---

## Network Location

After rotation, the new public IP is geolocated.

The workflow records:

- Country
- State
- City
- Postal code
- Latitude
- Longitude
- IANA timezone

This data is:

- Printed after rotation
- Included in the report
- Stored in the IP history

---

## Device-Observed Public IP

The report reads the public IP from the phone's perspective through the configured proxy.

The comparison is:

```text
Baseline = previous exit IP
Final    = rotated exit IP
```

---

## Timezone Handling

Phase 4.2 reads the timezone recorded during Phase 1 from:

```text
Tele/state.json
```

The documented pipeline applies it to:

```text
persist.sys.timezone
```

This allows the device timezone to align with the selected network location.

---

## Authenticated Proxies

For authenticated proxy entries:

```text
user:pass@host:port
```

a local `pproxy` relay can run on the PC.

The phone connects to:

```text
PC_IP:3128
```

Install the dependency:

```bash
pip install pproxy
```

The `--reset` command stops the local relay when one is running.

---

## Gateway Proxies

Entries beginning with `*` can generate fresh credentials per run.

Configuration variables are located near the top of:

```text
Tele/network_telemetry_prep.py
```

Relevant variables:

```text
AUTO_CREDENTIALS
USER_PREFIX
USER_LEN
PASS_LEN
```

---

# How the Local IP Change Works

On rooted devices, the workflow can change the source IP used by network routes.

The sequence is:

1. Add the new address to `wlan0` as a secondary IP.
2. Pin `src` on the per-network default route.
3. Pin `src` on the main-table connected route.
4. Verify using `ip route get 8.8.8.8`.
5. Perform a live ping.
6. Roll back the route changes if the operation fails.

The existing DHCP address is retained.

This avoids unnecessarily removing the active DHCP lease.

Without root, the local-IP operation is skipped gracefully.

The configured local-IP state normally reverts during a later Wi-Fi reconnect or DHCP renewal.

---

# How Hardware Identity Spoofing Works (Phase 4.1)

The Phase 4.1 flow is implemented through:

```text
execute_hardware_identity_flow
```

The logical fields include:

- Brand
- Manufacturer
- Model
- Device
- Product
- Build
- Fingerprint
- SoC
- ABI

The documented implementation uses:

```text
Magisk resetprop -n
```

The overrides are runtime/in-memory changes and a reboot restores the stock Android properties.

---

## Device Profile Selection

Profiles are stored in:

```text
telemetry/device_profiles.json
```

### Random

```text
random
```

Selects a random profile while avoiding the previously used profile.

The previous selection is stored in:

```text
telemetry/.mg_last_device_profile
```

### Cycle

```text
cycle
```

Uses profiles in round-robin order.

### Explicit Profile

Example:

```text
poco-m6-pro
```

---

## Validation

After applying the selected profile, the pipeline reads the properties back.

The report can contain:

```text
PASS
FAIL
```

A baseline safety check is also performed.

If the captured baseline already matches the selected target profile, the state file is not written because the device may still have an existing override.

---

## Application Restart

The target application is force-stopped and relaunched after the identity changes.

This is required because:

```text
android.os.Build
```

is a static class populated when initially loaded.

An application that is already running may continue using previously loaded values.

---

# How Extended Telemetry Works (Phase 4.2)

The Phase 4.2 flow is implemented through:

```text
execute_extended_telemetry_flow
```

---

## Changeable Fields

| Field | How it changes |
|---|---|
| System Locale | `setprop persist.sys.locale` |
| Timezone | `setprop persist.sys.timezone` |
| Screen Orientation | `settings put system user_rotation` |
| Photo / Media Count | Creates new `mg_qa_<N>.png` files |
| Battery Level | `dumpsys battery set level` |
| Battery Status | `dumpsys battery set status` |
| Battery AC / USB | `dumpsys battery set ac` / `set usb` |
| Battery Wireless | Cleared |

---

## Screen Orientation Distribution

The documented weighted distribution is:

```text
50%  Portrait
40%  Landscape
5%   Reverse Portrait
5%   Reverse Landscape
```

---

## Photo / Media Count

The pipeline creates uniquely indexed files:

```text
mg_qa_<N>.png
```

The files are stored under:

```text
/sdcard/DCIM/Camera/
```

The unique index allows the pipeline-created media count to grow across runs.

---

## Battery State

The documented workflow can modify:

```text
Battery Level
Battery Status
AC Power
USB Power
Wireless Power
```

Battery level is selected to differ from the previous value.

Battery status is randomized between charging and discharging states.

AC and USB values are kept consistent with the resulting battery state.

Wireless power is cleared.

---

# Synthetic Telemetry

When:

```python
SYNTHETIC_TELEMETRY = True
```

is enabled in:

```text
telemetry/mighty_grid_run.py
```

the following fields are randomized **in the report only**:

```text
Free Storage
Free RAM
Installed App Count
```

The actual device values are not changed.

The report contains an explicit marker:

```text
[!] NOTE: ... SYNTHETIC (randomized)
```

---

# Read-Only Fields

Some values are observed but intentionally not changed.

The report marks these as:

```text
READ-ONLY
```

## PackageManager Metadata

```text
Installer Package
Install Status
app_version
first_install_time
last_update_time
```

These values are controlled by Android package-management infrastructure.

## Hardware Battery Measurements

```text
Battery Temperature
Battery Voltage
```

These are hardware/fuel-gauge readings.

## App Signature

```text
App Signature
```

This is intentionally outside the documented scope.

## Target Application

```text
target_app
```

The target application is echoed for reporting and is not spoofed.

---

# Device Verification — Independent ADB Check

The recommended verification process is to run the following ADB command before and after the pipeline.

It reports:

- Network information
- Hardware identity
- App telemetry
- Battery state
- Photo/media count
- App sandbox state
- Device ground truth

```bash
adb shell "echo '========== 1. NETWORK (Phase 1) =========='; \
echo '-- Carrier alpha:'; getprop gsm.sim.operator.alpha; \
echo '-- Carrier numeric:'; getprop gsm.sim.operator.numeric; \
echo '-- Operator alpha:'; getprop gsm.operator.alpha; \
echo '-- Operator numeric:'; getprop gsm.operator.numeric; \
echo '-- Local IP (kernel src):'; ip route get 8.8.8.8 | head -1; \
echo '-- Proxy:'; settings get global http_proxy; \
echo '-- RX/TX bytes:'; cat /proc/net/dev | grep wlan0; \
echo ''; \
echo '========== 2. HARDWARE IDENTITY (Phase 4.1) =========='; \
echo '-- Brand:'; getprop ro.product.brand; \
echo '-- Manufacturer:'; getprop ro.product.manufacturer; \
echo '-- Model:'; getprop ro.product.model; \
echo '-- Device:'; getprop ro.product.device; \
echo '-- Product:'; getprop ro.product.name; \
echo '-- Build:'; getprop ro.build.display.id; \
echo '-- Fingerprint:'; getprop ro.build.fingerprint; \
echo '-- Hardware (SoC):'; getprop ro.hardware; \
echo '-- Board platform:'; getprop ro.board.platform; \
echo '-- SoC manufacturer:'; getprop ro.soc.manufacturer; \
echo '-- SoC model:'; getprop ro.soc.model; \
echo '-- CPU ABI:'; getprop ro.product.cpu.abi; \
echo ''; \
echo '========== 3. APP TELEMETRY (Phase 4.2) =========='; \
echo '-- Locale:'; getprop persist.sys.locale; \
echo '-- Timezone:'; getprop persist.sys.timezone; \
echo '-- Screen rotation:'; settings get system user_rotation; \
echo ''; \
echo '========== 4. BATTERY (Phase 4.2) =========='; \
echo '-- Full dumpsys battery:'; dumpsys battery; \
echo ''; \
echo '========== 5. PHOTOS (Phase 4.2) =========='; \
echo '-- mg_qa photos:'; ls /sdcard/DCIM/Camera/mg_qa_*.png 2>/dev/null | wc -l; \
echo '-- Total DCIM/Camera files:'; ls /sdcard/DCIM/Camera/ 2>/dev/null | wc -l; \
echo ''; \
echo '========== 6. APP SANDBOX (Phase 3) =========='; \
echo '-- Chrome priv cache (KB):'; su -c 'du -k -d 0 /data/data/com.android.chrome' 2>/dev/null; \
echo '-- Chrome ext cache (KB):'; du -k -d 0 /sdcard/Android/data/com.android.chrome 2>/dev/null; \
echo '-- Chrome PID:'; pidof com.android.chrome; \
echo ''; \
echo '========== 7. DEVICE GROUND TRUTH (never changed) =========='; \
echo '-- Real storage:'; df -k /data | tail -1; \
echo '-- Real RAM available:'; cat /proc/meminfo | grep MemAvailable; \
echo '-- Real 3rd-party apps count:'; pm list packages -3 | wc -l; \
echo '-- 3rd-party app list (first 40):'; pm list packages -3 | head -40; \
echo '-- Chrome version:'; dumpsys package com.android.chrome | grep versionName; \
echo '========== END =========='"
```

---

# Verification Workflow

## 1. Capture Baseline

Before running the pipeline:

```powershell
adb shell "..." > before.txt
```

## 2. Run the Pipeline

```bash
python mighty_grid_run.py -p com.android.chrome
```

## 3. Capture Final State

```powershell
adb shell "..." > after.txt
```

## 4. Compare Results

Using PowerShell:

```powershell
Compare-Object (Get-Content before.txt) (Get-Content after.txt)
```

Or:

```powershell
fc.exe before.txt after.txt
```

The comparison provides an independent check of device state before and after the pipeline.

---

# Reading the Output

## 1. Network — Phase 1

The network section includes:

```text
Carrier alpha
Carrier numeric
Operator alpha
Operator numeric
Local IP
Proxy
RX/TX bytes
```

The following values are treated as read-only:

```text
SSID
RSSI
Connection type
Cell ID
```

These values depend on the router, driver, modem, or surrounding network environment.

---

## 2. Hardware Identity — Phase 4.1

The hardware section includes:

```text
Brand
Manufacturer
Model
Device
Product
Build
Fingerprint
Hardware
Board platform
SoC manufacturer
SoC model
CPU ABI
```

---

## 3. App Telemetry — Phase 4.2

The application telemetry section includes:

```text
Locale
Timezone
Screen rotation
```

---

## 4. Battery — Phase 4.2

The battery section includes:

```text
Level
Status
Temperature
Voltage
```

The documented behavior is:

```text
Level       -> Changeable
Status      -> Changeable
Temperature -> READ-ONLY
Voltage     -> READ-ONLY
```

---

## 5. Photos — Phase 4.2

The photo section reports:

```text
mg_qa_*.png count
Total DCIM/Camera file count
```

---

## 6. App Sandbox — Phase 3

For Chrome, the sandbox section reports:

```text
Chrome private cache
Chrome external cache
Chrome PID
```

---

## 7. Device Ground Truth

The ground-truth section reports:

```text
Real storage
Available RAM
Installed third-party applications
Chrome version
```

This section represents values the documented pipeline does not intentionally modify.

---

## Synthetic Telemetry

When:

```text
SYNTHETIC_TELEMETRY = True
```

the following values may be report-only randomized values:

```text
Free Storage
Free RAM
Installed App Count
```

The report explicitly identifies synthetic telemetry.

---

# Restoring the Phone

## Restore Hardware Identity — Phase 4.1

Run:

```bash
cd telemetry
python -c "import mighty_grid_run as m; m.revert_hardware_identity(None)"
```

The function reads the saved baseline from:

```text
telemetry/hardware_identity_state.json
```

and restores the recorded values.

If the state file is missing or the baseline is empty, reboot the device.

---

# Restore Battery — Phase 4.2

Run:

```bash
cd telemetry
python -c "import mighty_grid_run as m; m.reset_battery(None)"
```

This clears the documented battery overrides.

Direct ADB equivalent:

```bash
adb shell "su -c 'dumpsys battery reset'"
```

Battery overrides also revert on reboot.

---

# Restore Proxy — Phase 1

Run:

```bash
python Tele/network_telemetry_prep.py --reset
```

This:

- Clears `global http_proxy`
- Stops the local `pproxy` relay if one is running

---

# Restore Local IP — Phase 1

The documented local-IP configuration normally reverts after:

- Wi-Fi reconnect
- DHCP renewal

No permanent manual cleanup is normally required.

---

# Restore to poco
python restore_all_poco.py


# Remove Pipeline Photos

Remove pipeline-created images with:

```bash
adb shell "rm /sdcard/DCIM/Camera/mg_qa_*.png"
```

These files are persistent and are not removed by reboot.

---

# Reset Everything

For a runtime reset:

```bash
adb reboot
```

A reboot restores:

- Stock `ro.*` properties
- Battery overrides
- Proxy runtime state
- Local-IP runtime state

Photos remain on storage and must be removed separately.

---

# Requirements

The documented pipeline requires:

- Python 3.8+
- Python 3.10+ for the `telemetry/` scripts
- ADB available in `PATH`
- Android device connected over USB
- USB debugging enabled
- Root access for root-dependent operations
- `su`
- Magisk or KernelSU
- `curl` on the PC for proxy liveness checks

Root is required for operations such as:

```text
resetprop
local-IP changes
root-level network preparation
dumpsys battery set
```

Without root, root-dependent operations are skipped or unavailable.

---

# Python Dependencies

## Authenticated Proxy Support

Install:

```bash
pip install pproxy
```

## Optional Frida Support

For the optional Frida-based sensor injection:

```bash
pip install frida frida-tools websocket-client
```

Frida-based functionality may be unavailable on hardened Android devices.

---

# Notes & Troubleshooting

## Public IP Shows `✗` but the IP Changed

Run:

```bash
python network_telemetry_prep.py --check
```

The final posture also verifies the device-observed IP through the configured proxy.

---

## Local IP Shows `✗`

Root access is required.

Some kernels may reject the documented source-selection configuration.

Failed local-IP modifications are rolled back automatically.

---

## Data Usage Shows `✗ (+0r / +0t)`

The traffic-warming ping may not have registered.

Re-run the preparation step.

If a large negative difference appears, check whether another process is modifying the Wi-Fi interface.

---

## No Working Proxy Found

The free proxy list may be stale.

Refresh it:

```bash
python network_telemetry_prep.py --refresh-free
```

Additional proxies can be added to:

```text
Tele/proxies.txt
```

Supported formats:

```text
host:port
user:pass@host:port
*host:port
```

---

## Phone Has No Internet After Rotation

The selected proxy may have become unavailable.

Check:

```bash
python network_telemetry_prep.py --check
```

Reset:

```bash
python network_telemetry_prep.py --reset
```

Then perform another rotation.

---

## Baseline Already Equals the Target Profile

If the pipeline reports:

```text
[!] WARNING: baseline for ... already equals the spoofed target
```

the device may still contain a previous override.

Reboot:

```bash
adb reboot
```

Then run the pipeline again so the baseline can be captured from the stock state.

---

## Frida Sensor Injection Skipped

On hardened Android devices, the kernel may reject:

```text
PTRACE_POKEDATA
```

The pipeline detects the condition and continues with the remaining QA operations.

---

## Battery Temperature / Voltage Always Show `READ-ONLY`

The battery interface supports documented changes to:

```text
level
status
ac
usb
wireless
```

Temperature and voltage remain hardware/fuel-gauge measurements and are treated as:

```text
READ-ONLY
```

---

## Application Version / Install Times Show `READ-ONLY`

The following are package-management metadata:

```text
app_version
first_install_time
last_update_time
```

The documented workflow does not modify these values.

---

## Some Applications Ignore Proxy Settings

The Android global:

```text
http_proxy
```

setting is used by many applications, but some applications can bypass system proxy settings.

---

## Chrome External Cache Stays Flat

On Android 11+, scoped-storage restrictions can prevent direct ADB writes to:

```text
/sdcard/Android/data/<pkg>/cache/
```

This is treated as a platform limitation.

---

## Phase 1 Network Integration

`mighty_grid_run.py` performs the documented Phase 1 network preparation automatically.

No additional flags are required for the normal pipeline path.

---

# Quick Command Reference

## Check Connected Devices

```bash
adb devices
```

---

## Network Preparation

```bash
cd Tele

python network_telemetry_prep.py
python network_telemetry_prep.py --check
python network_telemetry_prep.py --reset
python network_telemetry_prep.py --list
python network_telemetry_prep.py --refresh-free
python network_telemetry_prep.py --rotate-only
```

---

## Network Priming

```bash
cd telemetry

python network_priming.py
python network_priming.py -s <serial>
```

---

## MightyGrid Orchestrator

```bash
cd telemetry

python mighty_grid_run.py -p com.android.chrome

python mighty_grid_run.py \
    -p com.android.chrome \
    --device-profile poco-m6-pro

python mighty_grid_run.py \
    -p com.android.chrome \
    --device-profile cycle
```

---

## Restore Hardware Identity

```bash
cd telemetry

python -c "import mighty_grid_run as m; m.revert_hardware_identity(None)"
```

---

## Restore Battery

```bash
cd telemetry

python -c "import mighty_grid_run as m; m.reset_battery(None)"
```

Or:

```bash
adb shell "su -c 'dumpsys battery reset'"
```

---

## Restore Proxy

```bash
python Tele/network_telemetry_prep.py --reset
```

---

## Remove Pipeline Photos

```bash
adb shell "rm /sdcard/DCIM/Camera/mg_qa_*.png"
```

---

## Full Runtime Reset

```bash
adb reboot
```

---

# Project Structure

```text
MightyGrid/
│
├── Tele/
│   ├── network_telemetry_prep.py
│   ├── proxies.txt
│   └── state.json
│
└── telemetry/
    ├── mighty_grid_run.py
    ├── network_priming.py
    ├── app_priming.py
    ├── device_profiles.json
    ├── regional_profiles.json
    ├── regional_persona.py
    ├── browser_cloaking.js
    ├── sensor_inject.js
    ├── sensor_persona_manager.py
    ├── hardware_identity_state.json
    ├── extended_telemetry_state.json
    └── .mg_last_device_profile
```

Additional runtime/state files may be created by the pipeline.

---

# Scope

This README documents the MightyGrid Telemetry pipeline for authorized development and QA environments.

The documented workflow targets:

- App-observable environment fields
- Build and product identity
- Locale
- Timezone
- Screen orientation
- Battery level/status
- Network-level proxy configuration
- Carrier identity used for QA routing
- Rooted-device local-IP preparation
- App sandbox state
- Cache inflation
- PID cycling

The documented scope does **not** include:

```text
IMEI modification
Serial-number modification
MAC-address modification
android_id modification
Play Integrity bypass
Hardware-attestation bypass
Application-signature tampering
```

No IMEI, serial number, MAC address, or `android_id` is read or written by the documented workflow.

---

# Verification and Authorization

Before running the workflow, verify that:

1. The Android device is authorized for the intended testing.
2. The proxy infrastructure is authorized for the intended environment.
3. The target application is approved for testing.
4. Network and device modifications are permitted within the applicable QA environment.

Use the pipeline only within controlled and authorized testing environments.

---

# Summary

The MightyGrid Telemetry pipeline combines:

- Network preparation
- Proxy rotation
- Device-environment QA
- Hardware-property handling
- Extended telemetry preparation
- Application priming
- Independent ADB verification
- Runtime restoration

The verification process separates:

```text
CHANGEABLE
SYNTHETIC / REPORT-ONLY
READ-ONLY
GROUND TRUTH
```

This makes it possible to distinguish actual device changes from report-only values and from device values that are intentionally left unchanged.