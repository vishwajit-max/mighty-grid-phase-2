# Android Multi-Device Record & Replay Engine

A high-performance, cross-device testing automation framework. It records human interactions on a primary Android device via a 60 FPS mirrored desktop viewport and replays them simultaneously across a heterogeneous fleet of Android devices (Realme, Samsung, Poco, Pixel, etc.).

Unlike traditional coordinate-based macro recorders that break immediately on different screen sizes or aspect ratios, this engine uses a **3-Tier Cascade** to intelligently locate targets on replay.

## Core Features

*   **Zero-Latency Recording:** Streams raw H.264 video from `scrcpy-server` into a PyQt/PySide6 viewport and passes touches instantly via `MaaTouch`.
*   **3-Tier Target Resolution:**
    1.  **Semantic XML (Tier 1):** Resolves targets via `uiautomator2` (`resource-id`, `text`, `content-desc`).
    2.  **Multi-Scale Vision (Tier 2):** Fallback OpenCV template matching using recorded bounding-box crops (scales 0.8x to 1.2x for DPI resilience).
    3.  **Screen-Normalized (Tier 3):** Percent-based coordinate scaling for general viewport swipes.
*   **Hardware & Text Injection:** Bypasses soft-keyboard geometric differences by injecting semantic `TEXT_INPUT` and capturing hardware keys (Home, Back, Volume).
*   **Lockstep Fleet Synchronization:** Uses Python threading barriers to ensure all devices in the fleet execute steps simultaneously, natively recovering from OEM popups and varying app drawer geometries.

## Prerequisites

### 1. Host Machine Setup
*   Python 3.13+
*   ADB (Android Debug Bridge) installed and added to your system `PATH`.
*   [Scrcpy](https://github.com/Genymobile/scrcpy) installed locally (the script will attempt to auto-locate `scrcpy-server` via WinGet or PATH).

### 2. Android Device Setup (Critical)
To allow ADB to inject raw touches via MaaTouch and dump the UI via UIA2, you **must** configure these OEM-specific developer options:
*   **All Devices:** Enable **USB Debugging**.
*   **Poco / Xiaomi (HyperOS/MIUI):** Enable **USB Debugging (Security settings)**. (Failing to do this will result in MaaTouch connecting but touches doing nothing).
*   **Realme / Oppo (ColorOS):** Scroll to the bottom of Developer Options and enable **Disable permission monitoring**.
*   **Samsung (One UI 6+):** Go to Settings -> Security and Privacy -> Auto Blocker and turn **OFF** the Auto Blocker.

## Installation

1. Clone the repository and navigate to the project root.
2. Install the required Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

### Step 1: Record a Test Session
Connect your primary device and run the viewport UI.
*(Tip: Find your device serial using `adb devices`)*

```bash
python recording_engine/viewport_recorder_ui.py <PRIMARY_DEVICE_SERIAL>
```
*   Interact with the device directly through the desktop window.
*   Use the text bar to type semantic text (avoids recording specific keyboard taps).
*   Use the right-side toolbar for hardware buttons.
*   Click **Stop & Export JSON** when finished. 
*   *Output:* A new folder is created in `recording_engine/recordings/session_XXXXXXXX/` containing `recording.json` and visual crop snippets.

### Step 2: Replay Across a Device Fleet
Connect your target devices (you can replay on one device or multiple simultaneously). Run the replay engine, passing the path to your generated JSON and all target device serials.

```bash
# Replay on a single device
python recording_engine/replay_engine.py recording_engine/recordings/session_YOUR_ID/recording.json <DEVICE_SERIAL>

# Replay on multiple devices simultaneously in lockstep
python recording_engine/replay_engine.py recording_engine/recordings/session_YOUR_ID/recording.json <SERIAL_1> <SERIAL_2> <SERIAL_3>
```

The console will output a live JSON pass/fail report for each device, detailing exactly which matching tier (XML, Visual, or Normalized) was used for every step.

## Project Structure (Recording Engine)
*   `viewport_recorder_ui.py`: PySide6 frontend for mirror streaming and input capture.
*   `recorder_engine.py`: Background threads for UI-automator hierarchy dumping and MaaTouch translation.
*   `replay_engine.py`: Fleet orchestrator utilizing concurrent futures, thread barriers, and 3-tier cascade resolution.