# MightyGrid
**Enterprise Synthetic Monitoring & UX Telemetry Framework**

MightyGrid is a proprietary, Python-based synthetic testing orchestrator designed for authorized frontend performance profiling and Core Web Vitals benchmarking. Built on top of Playwright and the Chrome DevTools Protocol (CDP), it executes isolated, "cold-session" browser automation while maintaining a sterile, hardware-backed desktop posture to bypass restrictive anti-bot heuristics during load testing.

## Core Architecture
MightyGrid synthesizes six telemetry and automation modules into a single execution pipeline:
1. **Browser DOM & Engine Profiling:** Ensures honest hardware-backed WebGL rendering while stripping headless automation flags (`navigator.webdriver`).
2. **Network & System Telemetry:** Validates host CPU loads and aligns geographic IP routing with injected browser timezones.
3. **Environment & Localization Spoofing:** Injects isolated CDP overrides for Timezone, Locale, and Battery APIs without leaking host configurations.
4. **Hardware Sensor Validation:** Implements strict DOM-level ghosting to eradicate mobile sensor leaks (`devicemotion`, `deviceorientation`) on desktop environments.
5. **Human-like Kinematics:** Replaces linear bot actions with mathematical Bézier curve cursor trajectories, randomized typing cadences, and smooth-scrolling emulation.
6. **Web Vitals Extraction:** Bypasses client-side DOM hydration wipes by utilizing native browser performance buffers to extract pristine FCP, LCP, and CLS timings.

## Prerequisites
* **Python 3.8+**
* **Supported OS:** Windows 10/11 (Native Chrome installation required for hardware acceleration)

Install the required Python dependencies and the Playwright engine:
```bash
pip install psutil playwright
playwright install chromium
```

## Usage

Execute the master orchestrator via the command line, passing the target QA URL as an argument. The framework defaults to headless execution using the host's native Chrome channel.

```powershell
chrome --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir="$env:TEMP\chrome-debug-profile"
```

```powershell
python mighty_grid_orchestrator.py --url [https://your-internal-qa-domain.com]
```

### Expected Output
The orchestrator generates a clean, three-part terminal report:
* **Infrastructure & Mock Posture:** Validates the zero-leak sensor state, WebGL GPU profile, and GeoIP alignment.
* **Core Web Vitals:** Outputs millisecond-accurate FCP, LCP, and CLS scores for the synthetic journey.
* **Diagnostic Event Log:** A smartly filtered log capturing critical first-party JS exceptions and network failures while silencing third-party ad-tracker noise.

### Tweaks
If you want to see the browser window, in **mighty_grid_orchestrator.py** (Line 239) change **headless=True** to **headless=False**.

## Security & Evasion Design
MightyGrid does not rely on fragile JavaScript proxy injections to spoof hardware (which fail modern canvas hashing). Instead, it enforces:
* Native `channel="chrome"` execution to utilize physical NVIDIA/AMD GPUs over SwiftShader.
* Native CDP protocol overrides for localization rather than `Object.defineProperty` hacks.
* Event-listener interception at the Blink engine level to securely ghost missing hardware APIs.

# Project Structure

## Master Orchestration

### `mighty_grid_orchestrator.py`
The unified master orchestrator that integrates all six telemetry modules into a single Playwright execution pipeline to extract full-stack QA telemetry.

### `input_orchestration.py`
Handles navigation automation and kinematics by executing trajectories and scrolling, while safely extracting Core Web Vitals to survive DOM hydration wipes.

## Read-Only Telemetry Probes

### `battery_telemetry_probe.py`
Reads local host battery metrics and queries the browser's baseline W3C Battery API.

### `browser_telemetry_probe.py`
Connects to a running Chromium browser via CDP to query runtime DOM and JavaScript engine metrics.

### `environment_telemetry_probe.py`
Queries host-level health metrics like CPU and uptime, alongside browser DOM regional and display states.

### `network_telemetry_probe.py`
Queries local network interfaces, routing tables, and public exit IPs to baseline the network footprint.

### `sensor_telemetry_probe.py`
Listens for generic DOM sensor events (DeviceMotion and DeviceOrientation) to validate that the host correctly registers as a static desktop.

## Dynamic Evasion Modules (`priming/`)

### `battery_priming.py`
Injects a dynamically randomized battery discharge profile via CDP to spoof mobile usage and evade static fingerprinting.

### `browser_priming.py`
Generates and injects a randomized, cryptographically safe User-Agent and Client Hints profile to override the browser's native identity.

### `environment_priming.py`
Injects a randomized geographic and device display profile (timezone, locale, screen geometry) via CDP.

### `network_priming.py`
Launches a Playwright session to verify the network and proxy footprint from the perspective of the browser DOM.

### `sensor_priming.py`
Injects a deep Blink-level prototype override to securely ghost mobile hardware sensors and enforce a sterile bot posture.

