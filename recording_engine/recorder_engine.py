import json
import math
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
import lxml.etree as ET
import uiautomator2 as u2
import cv2
import numpy as np


# =====================================================================
# 1. DATA MODELS (Cross-Device Replay Schema)
# =====================================================================

@dataclass
class TouchSample:
    t_offset_ms: int       # Milliseconds since POINTER_DOWN
    x: int                 # Raw pixel X on recording device
    y: int                 # Raw pixel Y on recording device
    rx: float              # Normalized screen X (0.0 - 1.0)
    ry: float              # Normalized screen Y (0.0 - 1.0)
    dx_norm: float         # Delta from start_x normalized by screen width
    dy_norm: float         # Delta from start_y normalized by screen height
    pressure: int = 50


@dataclass
class UIAnchor:
    resource_id: str = ""
    text: str = ""
    content_desc: str = ""
    class_name: str = ""
    package: str = ""
    bounds: Tuple[int, int, int, int] = (0, 0, 0, 0)  # (left, top, right, bottom)
    # Relative hit position INSIDE the element's bounding box (0.0 to 1.0)
    element_rx: float = 0.5
    element_ry: float = 0.5
    visual_crop_path: Optional[str] = None


@dataclass
class RecordedStep:
    step_id: int
    timestamp: float
    think_time_ms: int             # Time elapsed since previous step completed
    event_type: str                # TAP, LONG_PRESS, SWIPE, KEY_EVENT, TEXT_INPUT
    pre_package: str
    pre_activity: str
    orientation: int               # 0=Portrait, 1=Landscape-90, 3=Landscape-270
    # Populated for touch events
    anchor: Optional[UIAnchor] = None
    end_anchor: Optional[UIAnchor] = None
    duration_ms: int = 0
    trajectory: List[TouchSample] = field(default_factory=list)
    # Populated for KEY_EVENT or TEXT_INPUT
    keycode: Optional[int] = None
    text_payload: Optional[str] = None


# =====================================================================
# 2. MAATOUCH STREAM CONTROLLER (0ms Pass-Through + Key/Text Injection)
# =====================================================================

class MaaTouchDriver:
    """
    Manages a persistent adb shell app_process pipe to MaaTouch.
    Auto-pushes the binary if missing on the target device.
    """
    def __init__(
        self,
        serial: str,
        local_maatouch_path: str = r"C:\Users\RDP6\Downloads\Phase-2\android_telemetry\telemetry\maatouch",
        maatouch_remote_path: str = "/data/local/tmp/maatouch"
    ):
        self.serial = serial
        self.local_path = local_maatouch_path
        self.remote_path = maatouch_remote_path
        self.proc: Optional[subprocess.Popen] = None
        self.max_x = 1080
        self.max_y = 2400
        self.max_pressure = 255
        self._lock = threading.Lock()

    def _ensure_binary_on_device(self):
        check = subprocess.run(
            ["adb", "-s", self.serial, "shell", f"ls {self.remote_path}"],
            capture_output=True, text=True
        )
        if check.returncode != 0:
            search_root = Path(__file__).resolve().parent.parent
            candidates = list(search_root.rglob("*maatouch*"))
            candidates = [c for c in candidates if c.is_file() and not c.name.endswith(".py")]
            if self.local_path and Path(self.local_path).exists():
                src = self.local_path
            elif candidates:
                src = str(candidates[0])
            else:
                print(f"[!] WARNING: Could not find local maatouch binary in {search_root} to push to {self.serial}!")
                return

            print(f"[*] Pushing {src} -> {self.serial}:{self.remote_path} ...")
            subprocess.run(["adb", "-s", self.serial, "push", src, self.remote_path], check=True)
            subprocess.run(["adb", "-s", self.serial, "shell", f"chmod 777 {self.remote_path}"], check=True)

    def _query_wm_size_fallback(self):
        res = subprocess.run(
            ["adb", "-s", self.serial, "shell", "wm", "size"],
            capture_output=True, text=True
        )
        for line in res.stdout.splitlines():
            if "size:" in line:
                dims = line.split(":")[-1].strip().split("x")
                if len(dims) == 2 and dims[0].isdigit() and dims[1].isdigit():
                    self.max_x, self.max_y = int(dims[0]), int(dims[1])

    def start(self):
        self._ensure_binary_on_device()
        self._query_wm_size_fallback()

        cmd = [
            "adb", "-s", self.serial, "shell",
            f"CLASSPATH={self.remote_path} app_process / com.shxyke.MaaTouch.App"
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

        header_Holder = []
        def _read_hdr():
            try:
                line = self.proc.stdout.readline().strip()
                header_Holder.append(line)
            except Exception:
                pass

        t = threading.Thread(target=_read_hdr, daemon=True)
        t.start()
        t.join(timeout=2.0)

        if header_Holder and header_Holder[0].startswith("^"):
            parts = header_Holder[0].split()
            self.max_x = int(parts[2])
            self.max_y = int(parts[3])
            self.max_pressure = int(parts[4])
            print(f"[+] MaaTouch connected on {self.serial}: {self.max_x}x{self.max_y} (max_p={self.max_pressure})")
        else:
            err = header_Holder[0] if header_Holder else "timeout waiting for '^' header"
            print(f"[!] MaaTouch startup warning ({err}). Using wm size {self.max_x}x{self.max_y}.")

    def _send(self, payload: str):
        if not self.proc or not self.proc.stdin:
            return
        with self._lock:
            self.proc.stdin.write(payload)
            self.proc.stdin.flush()

    def touch_down(self, pointer_id: int, x: int, y: int, pressure: int = 50):
        self._send(f"d {pointer_id} {x} {y} {pressure}\nc\n")

    def touch_move(self, pointer_id: int, x: int, y: int, pressure: int = 50):
        self._send(f"m {pointer_id} {x} {y} {pressure}\nc\n")

    def touch_up(self, pointer_id: int):
        self._send(f"u {pointer_id}\nc\n")

    def press_key(self, keycode: int):
        self._send(f"k {keycode} o\nc\n")

    def input_text(self, text: str):
        sanitized = text.replace("\n", " ")
        self._send(f"t {sanitized}\nc\n")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc = None


# =====================================================================
# 3. BACKGROUND UI HIERARCHY & ACTIVITY SNAPSHOTTER
# =====================================================================

class DeviceStateCache:
    """
    Continuously refreshes the UI hierarchy and foreground activity during idle windows
    so POINTER_DOWN can resolve the clicked UI element in <1ms without blocking.
    """
    def __init__(self, serial: str):
        self.u2_dev = u2.connect(serial)
        self.latest_xml: Optional[str] = None
        self.latest_package: str = ""
        self.latest_activity: str = ""
        self.orientation: int = 0
        self.is_touch_active: bool = False
        self.last_interaction_ts: float = 0.0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._poll_loop, daemon=True)

    def start(self):
        self._refresh_now()
        self._worker.start()

    def notify_interaction_start(self):
        self.is_touch_active = True
        self.last_interaction_ts = time.perf_counter()

    def notify_interaction_end(self):
        self.is_touch_active = False
        self.last_interaction_ts = time.perf_counter()

    def _poll_loop(self):
        while not self._stop_event.is_set():
            idle_Elapsed = time.perf_counter() - self.last_interaction_ts
            # Refresh when user is not touching and 350ms have settled after last action
            if not self.is_touch_active and idle_Elapsed >= 0.35:
                self._refresh_now()
                time.sleep(0.4)
            else:
                time.sleep(0.05)

    def _refresh_now(self):
        try:
            app_info = self.u2_dev.app_current()
            xml_dump = self.u2_dev.dump_hierarchy(compressed=False)
            orient = self.u2_dev.info.get("displayRotation", 0)
            with self._lock:
                self.latest_package = app_info.get("package", "")
                self.latest_activity = app_info.get("activity", "")
                self.latest_xml = xml_dump
                self.orientation = orient
        except Exception:
            pass

    def get_snapshot(self) -> Tuple[str, str, int, Optional[str]]:
        with self._lock:
            return (
                self.latest_package,
                self.latest_activity,
                self.orientation,
                self.latest_xml
            )

    def resolve_node_at(self, x: int, y: int, xml_str: Optional[str]) -> UIAnchor:
        """
        Finds the deepest, smallest-area clickable/leaf UI node enclosing (x, y).
        """
        if not xml_str:
            return UIAnchor()

        try:
            root = ET.fromstring(xml_str.encode("utf-8"))
        except Exception:
            return UIAnchor()

        best_node = None
        smallest_area = float("inf")

        for elem in root.iter("node"):
            bounds_str = elem.attrib.get("bounds", "")
            if not bounds_str:
                continue
            # Format: "[left,top][right,bottom]"
            parts = bounds_str.replace("][", ",").strip("[]").split(",")
            l, t, r, b = map(int, parts)
            if l <= x <= r and t <= y <= b:
                w, h = (r - l), (b - t)
                area = w * h
                if area <= 0:
                    continue
                # Prefer nodes with semantic identifiers or smaller area
                has_id = bool(
                    elem.attrib.get("resource-id")
                    or elem.attrib.get("text")
                    or elem.attrib.get("content-desc")
                )
                effective_score = area * (0.5 if has_id else 1.0)
                if effective_score <= smallest_area:
                    smallest_area = effective_score
                    best_node = (elem, (l, t, r, b))

        if not best_node:
            return UIAnchor()

        elem, (l, t, r, b) = best_node
        w = max(1, r - l)
        h = max(1, b - t)
        return UIAnchor(
            resource_id=elem.attrib.get("resource-id", ""),
            text=elem.attrib.get("text", ""),
            content_desc=elem.attrib.get("content-desc", ""),
            class_name=elem.attrib.get("class", ""),
            package=elem.attrib.get("package", ""),
            bounds=(l, t, r, b),
            element_rx=round((x - l) / w, 4),
            element_ry=round((y - t) / h, 4)
        )

    def stop(self):
        self._stop_event.set()


# =====================================================================
# 4. MAIN RECORDING ENGINE CONTROLLER
# =====================================================================

class AndroidRecordingEngine:
    """
    Hook this directly into your host viewport UI (PyQt / PySide / WebSocket canvas).
    """
    def __init__(self, serial: str, session_dir: str = "./recordings"):
        self.serial = serial
        self.session_id = uuid.uuid4().hex[:8]
        self.output_dir = Path(session_dir) / f"session_{self.session_id}"
        self.crops_dir = self.output_dir / "crops"
        self.crops_dir.mkdir(parents=True, exist_ok=True)

        self.maatouch = MaaTouchDriver(serial)
        self.state_cache = DeviceStateCache(serial)

        self.steps: List[RecordedStep] = []
        self._step_counter = 0
        self._last_step_end_ts = time.perf_counter()

        # Active gesture state
        self._active_samples: List[TouchSample] = []
        self._down_ts: float = 0.0
        self._pre_touch_snapshot: Optional[Tuple[str, str, int, Optional[str]]] = None
        self._pre_touch_frame: Optional[np.ndarray] = None
        self._latest_video_frame: Optional[np.ndarray] = None

    def start_session(self):
        self.maatouch.start()
        self.state_cache.start()
        self._last_step_end_ts = time.perf_counter()

    def update_mirror_frame(self, frame_bgr: np.ndarray):
        """
        Call this from your scrcpy/video decode callback on every new frame.
        """
        self._latest_video_frame = frame_bgr

    # -----------------------------------------------------------------
    # VIEWPORT TOUCH HOOKS (Call from host mouse/touch event handlers)
    # -----------------------------------------------------------------

    def on_viewport_pointer_down(self, device_x: int, device_y: int, pressure: int = 50):
        # 1. Immediate pass-through to MaaTouch (0ms perceived latency)
        self.maatouch.touch_down(0, device_x, device_y, pressure)

        # 2. Freeze pre-touch state before UI transitions
        self.state_cache.notify_interaction_start()
        self._down_ts = time.perf_counter()
        self._pre_touch_snapshot = self.state_cache.get_snapshot()
        if self._latest_video_frame is not None:
            self._pre_touch_frame = self._latest_video_frame.copy()

        w, h = self.maatouch.max_x, self.maatouch.max_y
        self._active_samples = [
            TouchSample(
                t_offset_ms=0,
                x=device_x,
                y=device_y,
                rx=round(device_x / w, 4),
                ry=round(device_y / h, 4),
                dx_norm=0.0,
                dy_norm=0.0,
                pressure=pressure
            )
        ]

    def on_viewport_pointer_move(self, device_x: int, device_y: int, pressure: int = 50):
        if not self._active_samples:
            return
        self.maatouch.touch_move(0, device_x, device_y, pressure)

        t_offset = int((time.perf_counter() - self._down_ts) * 1000)
        # Deduplicate stationary sub-millisecond mouse jitter
        last = self._active_samples[-1]
        if last.x == device_x and last.y == device_y and (t_offset - last.t_offset_ms) < 8:
            return

        w, h = self.maatouch.max_x, self.maatouch.max_y
        start = self._active_samples[0]
        self._active_samples.append(
            TouchSample(
                t_offset_ms=t_offset,
                x=device_x,
                y=device_y,
                rx=round(device_x / w, 4),
                ry=round(device_y / h, 4),
                dx_norm=round((device_x - start.x) / w, 4),
                dy_norm=round((device_y - start.y) / h, 4),
                pressure=pressure
            )
        )

    def on_viewport_pointer_up(self, device_x: int, device_y: int):
        if not self._active_samples:
            return
        self.maatouch.touch_up(0)
        self.state_cache.notify_interaction_end()

        now = time.perf_counter()
        duration_ms = int((now - self._down_ts) * 1000)
        think_time_ms = int((self._down_ts - self._last_step_end_ts) * 1000)
        self._last_step_end_ts = now

        # Append final release sample
        w, h = self.maatouch.max_x, self.maatouch.max_y
        start = self._active_samples[0]
        self._active_samples.append(
            TouchSample(
                t_offset_ms=duration_ms,
                x=device_x,
                y=device_y,
                rx=round(device_x / w, 4),
                ry=round(device_y / h, 4),
                dx_norm=round((device_x - start.x) / w, 4),
                dy_norm=round((device_y - start.y) / h, 4),
                pressure=0
            )
        )

        # Classify gesture: TAP vs LONG_PRESS vs SWIPE
        pixel_dist = math.hypot(device_x - start.x, device_y - start.y)
        if pixel_dist > max(w, h) * 0.025:
            gesture_type = "SWIPE"
        elif duration_ms >= 500:
            gesture_type = "LONG_PRESS"
        else:
            gesture_type = "TAP"

        pkg, act, orient, xml_str = self._pre_touch_snapshot or ("", "", 0, None)
        start_anchor = self.state_cache.resolve_node_at(start.x, start.y, xml_str)
        end_anchor = (
            self.state_cache.resolve_node_at(device_x, device_y, xml_str)
            if gesture_type == "SWIPE" else None
        )

        self._step_counter += 1
        # Save visual template crop around touch-down point for fallback matching
        if self._pre_touch_frame is not None:
            crop_file = self._save_visual_crop(self._step_counter, start.x, start.y, start_anchor.bounds)
            start_anchor.visual_crop_path = crop_file

        step = RecordedStep(
            step_id=self._step_counter,
            timestamp=time.time(),
            think_time_ms=max(0, think_time_ms),
            event_type=gesture_type,
            pre_package=pkg,
            pre_activity=act,
            orientation=orient,
            anchor=start_anchor,
            end_anchor=end_anchor,
            duration_ms=duration_ms,
            trajectory=list(self._active_samples)
        )
        self.steps.append(step)
        self._active_samples.clear()

    # -----------------------------------------------------------------
    # HARDWARE KEYS & SEMANTIC TEXT HOOKS
    # -----------------------------------------------------------------

    def on_viewport_key_event(self, keycode: int):
        """
        Bind host toolbar buttons or shortcuts here:
        Back=4, Home=3, AppSwitch=187, VolUp=24, VolDown=25, Power=26, Enter=66
        """
        now = time.perf_counter()
        think_time_ms = int((now - self._last_step_end_ts) * 1000)
        self._last_step_end_ts = now

        self.state_cache.notify_interaction_start()
        pkg, act, orient, _ = self.state_cache.get_snapshot()
        self.maatouch.press_key(keycode)
        self.state_cache.notify_interaction_end()

        self._step_counter += 1
        self.steps.append(
            RecordedStep(
                step_id=self._step_counter,
                timestamp=time.time(),
                think_time_ms=max(0, think_time_ms),
                event_type="KEY_EVENT",
                pre_package=pkg,
                pre_activity=act,
                orientation=orient,
                keycode=keycode
            )
        )

    def on_viewport_text_input(self, text: str):
        """
        Call when the tester submits text via host keyboard or 'Send Text' bar.
        Uses uiautomator2 FastInputIME / MaaTouch 't' command so replay works
        identically across Gboard (Poco/Realme) and Samsung Keyboard.
        """
        now = time.perf_counter()
        think_time_ms = int((now - self._last_step_end_ts) * 1000)
        self._last_step_end_ts = now

        self.state_cache.notify_interaction_start()
        pkg, act, orient, _ = self.state_cache.get_snapshot()
        # Inject via MaaTouch text command
        self.maatouch.input_text(text)
        self.state_cache.notify_interaction_end()

        self._step_counter += 1
        self.steps.append(
            RecordedStep(
                step_id=self._step_counter,
                timestamp=time.time(),
                think_time_ms=max(0, think_time_ms),
                event_type="TEXT_INPUT",
                pre_package=pkg,
                pre_activity=act,
                orientation=orient,
                text_payload=text
            )
        )

    # -----------------------------------------------------------------
    # VISUAL FALLBACK & SERIALIZATION
    # -----------------------------------------------------------------

    def _save_visual_crop(
        self, step_id: int, x: int, y: int, bounds: Tuple[int, int, int, int]
    ) -> Optional[str]:
        frame = self._pre_touch_frame
        if frame is None:
            return None
        fh, fw = frame.shape[:2]
        l, t, r, b = bounds
        # Use element bounds if valid and reasonably sized; otherwise 140x140 patch around (x, y)
        if 0 < (r - l) < fw * 0.8 and 0 < (b - t) < fh * 0.5:
            x1, y1, x2, y2 = max(0, l), max(0, t), min(fw, r), min(fh, b)
        else:
            half = 70
            x1, y1 = max(0, x - half), max(0, y - half)
            x2, y2 = min(fw, x + half), min(fh, y + half)

        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return None
        rel_path = f"crops/step_{step_id:03d}.png"
        cv2.imwrite(str(self.output_dir / rel_path), crop)
        return rel_path

    def stop_and_export(self) -> Path:
        self.state_cache.stop()
        self.maatouch.stop()

        payload = {
            "session_id": self.session_id,
            "source_device_serial": self.serial,
            "source_resolution": {
                "width": self.maatouch.max_x,
                "height": self.maatouch.max_y
            },
            "total_steps": len(self.steps),
            "steps": [asdict(s) for s in self.steps]
        }
        out_file = self.output_dir / "recording.json"
        out_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return out_file