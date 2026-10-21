import json
import re
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
        # Start time for a live text-edit session. The completed text is
        # recorded as one TEXT_INPUT step when the user presses Enter/Inject.
        self._live_text_started_at: Optional[float] = None
        # Cache keyboard bounds between characters; invalidate when Shift changes
        # the keyboard layer so typing stays responsive without invisible injection.
        self._keyboard_node_cache = None
        self._keyboard_shift_state: Optional[bool] = None
        # The soft-keyboard layer is tracked so digits can switch from QWERTY
        # to the symbols/number page and letters can switch back automatically.
        self._keyboard_layer: Optional[str] = None
        # Text successfully typed by this desktop Inject bar during the current
        # live-edit session. This lets a later Backspace recover after a partial
        # key-tap failure instead of repeating already-entered characters.
        self._live_phone_text = ""

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
        # A viewport tap in the lower keyboard area may change the keyboard
        # layer (ABC / ?123 / symbol page), so discard stale accessibility data.
        if device_y >= int(self.maatouch.max_y * 0.55):
            self._keyboard_node_cache = None
            self._keyboard_layer = None
            self._keyboard_shift_state = None
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

    def _screen_size_for_keyboard(self) -> Tuple[int, int]:
        """Return Android logical screen dimensions used by UI hierarchy bounds."""
        try:
            width, height = self.state_cache.u2_dev.window_size()
            width, height = int(width), int(height)
            if width > 0 and height > 0:
                return width, height
        except Exception:
            pass
        return max(1, int(self.maatouch.max_x)), max(1, int(self.maatouch.max_y))

    def _load_keyboard_nodes(self):
        """Cache accessibility nodes in the keyboard region when Gboard exposes them."""
        if self._keyboard_node_cache is not None:
            return self._keyboard_node_cache

        try:
            xml_dump = self.state_cache.u2_dev.dump_hierarchy(compressed=False)
        except Exception:
            xml_dump = ""
        if not xml_dump:
            # Some keyboard versions do not expose soft keys in the XML. The
            # coordinate fallback below is designed for that case.
            self._keyboard_node_cache = []
            return self._keyboard_node_cache
        try:
            root = ET.fromstring(xml_dump.encode("utf-8"))
        except Exception:
            try:
                root = ET.fromstring(xml_dump)
            except Exception:
                self._keyboard_node_cache = []
                return self._keyboard_node_cache

        screen_w, screen_h = self._screen_size_for_keyboard()
        nodes = []
        for elem in root.iter("node"):
            attrs = dict(elem.attrib)
            match = re.fullmatch(
                r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", "")
            )
            if not match:
                continue
            left, top, right, bottom = map(int, match.groups())
            width, height = right - left, bottom - top
            if width <= 0 or height <= 0:
                continue
            center_y = (top + bottom) / 2
            # Exclude the Android system navigation area at the very bottom.
            # In particular, its Back arrow must never be mistaken for Gboard Backspace.
            if center_y < screen_h * 0.50 or center_y > screen_h * 0.955:
                continue
            if width > screen_w * 0.80 or height > screen_h * 0.16:
                continue

            labels = {
                attrs.get("text", "").strip().lower(),
                attrs.get("content-desc", "").strip().lower(),
                attrs.get("hint", "").strip().lower(),
            }
            labels.discard("")
            nodes.append({
                "bounds": (left, top, right, bottom),
                "attrs": attrs,
                "labels": labels,
                "resource_id": attrs.get("resource-id", "").lower(),
                "area": width * height,
                "clickable_rank": 0 if attrs.get("clickable", "false").lower() == "true" else 1,
                "screen_w": screen_w,
            })

        self._keyboard_node_cache = nodes
        self._infer_keyboard_layer_from_nodes(nodes)
        return nodes

    def _infer_keyboard_layer_from_nodes(self, nodes=None):
        """Infer Gboard layout without confusing the two symbol pages.

        Some Gboard versions show an ``ABC`` key on both symbol pages. Therefore
        ABC alone is not enough to decide whether page 1 or page 2 is open.
        Prefer several distinctive page-2 glyphs, then ordinary QWERTY labels,
        and otherwise preserve our last known layer.
        """
        if nodes is None:
            nodes = self._load_keyboard_nodes()
        labels = set()
        resource_text = ""
        for node in nodes or []:
            labels.update(str(label).strip().lower() for label in node.get("labels", set()))
            resource_text += " " + str(node.get("resource_id", "")).lower()
        if not labels and not resource_text.strip():
            return self._keyboard_layer

        # Several of these glyphs are specific to Gboard's second symbol page.
        page2_markers = {
            "€", "¥", "$", "¢", "^", "°", "√", "π", "÷", "×", "§", "δ",
            "Δ", "©", "®", "™", "✓", "~", "|", "[", "]", "{", "}",
        }
        page2_hits = len(page2_markers.intersection(labels))
        if page2_hits >= 2:
            self._keyboard_layer = "symbols2"
            return self._keyboard_layer

        alpha_letters = sum(1 for char in "qwertyuiopasdfghjklzxcvbnm" if char in labels)
        if alpha_letters >= 8:
            self._keyboard_layer = "alpha"
            return self._keyboard_layer

        # The first symbol page typically exposes the number row and/or =\<.
        has_first_page_marker = any(label in labels for label in {"=\\<", "=</"})
        digit_labels = sum(1 for char in "1234567890" if char in labels)
        if has_first_page_marker or digit_labels >= 5:
            # With no distinctive page-2 glyphs, a visible full number row is
            # stronger evidence for the first symbols page than a stale cache.
            self._keyboard_layer = "symbols"
            return self._keyboard_layer

        # ?123 is the switch from QWERTY to the first symbols page.
        if any(label in labels for label in {"?123", "123?", "symbols", "symbol"}) or "key_symbols" in resource_text:
            self._keyboard_layer = "alpha"
            return self._keyboard_layer

        if any(label in labels for label in {"abc", "letters", "alphabet"}) or "key_abc" in resource_text:
            # ABC is shared by both symbol pages on some layouts.
            if self._keyboard_layer == "symbols2":
                return self._keyboard_layer
            self._keyboard_layer = "symbols"
            return self._keyboard_layer

        if any(label in labels for label in {"?123", "123?", "symbols", "symbol"}) or "key_symbols" in resource_text:
            self._keyboard_layer = "alpha"
        return self._keyboard_layer

    def _fallback_key_point(self, key_name: str) -> Tuple[int, int]:
        """Return portrait-Gboard key centers as screen-relative coordinates.

        Accessibility bounds take precedence. The fallback includes both symbol
        pages so special characters do not accidentally land on unrelated keys.
        """
        semantic = str(key_name).strip().lower()
        width, height = self._screen_size_for_keyboard()
        layer = self._keyboard_layer or "alpha"

        if semantic in {"space", "spacebar", "space bar"}:
            x, y = 0.535, 0.910
        elif semantic in {"enter", "return", "search", "done"}:
            x, y = 0.935, 0.910
        elif semantic in {"backspace", "delete", "erase", "⌫", "backspace/delete"}:
            x, y = 0.925, 0.840
        elif semantic in {"shift", "caps lock"}:
            x, y = 0.080, 0.840
        elif semantic in {"symbols_toggle", "?123", "123", "alphabet_toggle", "abc", "letters"}:
            x, y = 0.090, 0.910
        elif semantic in {"symbol_page", "=\\<", "=</"}:
            x, y = 0.100, 0.840
        elif layer == "symbols2" and semantic == "<":
            x, y = 0.170, 0.910
        elif layer == "symbols2" and semantic == ">":
            x, y = 0.820, 0.910
        elif semantic in {",", "comma"}:
            x, y = 0.215, 0.910
        elif semantic in {".", "dot", "period", "full stop"}:
            x, y = 0.820, 0.910
        elif layer == "alpha" and len(key_name) == 1 and semantic in "qwertyuiop":
            x, y = ("qwertyuiop".index(semantic) + 0.5) / 10.0, 0.720
        elif layer == "alpha" and len(key_name) == 1 and semantic in "asdfghjkl":
            x, y = ("asdfghjkl".index(semantic) + 1.0) / 10.0, 0.780
        elif layer == "alpha" and len(key_name) == 1 and semantic in "zxcvbnm":
            x, y = 0.18 + "zxcvbnm".index(semantic) * 0.092, 0.840
        elif layer == "symbols2" and len(key_name) == 1 and semantic in "~`|•√π÷×§δ":
            row = "~`|•√π÷×§δ"
            x, y = (row.index(semantic) + 0.5) / 10.0, 0.720
        elif layer == "symbols2" and len(key_name) == 1 and semantic in "€¥$¢^°−{}\\":
            row = "€¥$¢^°−{}\\"
            x, y = (row.index(semantic) + 0.5) / 10.0, 0.780
        elif layer == "symbols2" and len(key_name) == 1 and semantic in "%©®™✓[]":
            positions = {"%": 0.05, "©": 0.17, "®": 0.29, "™": 0.41, "✓": 0.55, "[": 0.67, "]": 0.78}
            x, y = positions[semantic], 0.840
        elif len(key_name) == 1 and semantic in "1234567890" and layer == "symbols":
            x, y = ("1234567890".index(semantic) + 0.5) / 10.0, 0.720
        elif layer == "symbols" and len(key_name) == 1 and semantic in "@#₹_&-+()/":
            row = "@#₹_&-+()/"
            x, y = (row.index(semantic) + 0.5) / 10.0, 0.780
        elif layer == "symbols" and len(key_name) == 1 and semantic in "*\"':;!?":
            row = "*\"':;!?"
            positions = [0.22, 0.34, 0.43, 0.53, 0.63, 0.73, 0.82]
            x, y = positions[row.index(semantic)], 0.840
        elif layer == "symbols" and len(key_name) == 1 and semantic == "_":
            x, y = 0.35, 0.780
        elif layer == "symbols" and len(key_name) == 1 and semantic == "-":
            x, y = 0.55, 0.780
        elif layer == "symbols" and len(key_name) == 1 and semantic == "+":
            x, y = 0.65, 0.780
        else:
            raise RuntimeError(
                f"No accessible key for {key_name!r} on the {layer} keyboard page. "
                "Keep the phone in portrait mode with Gboard visible."
            )

        return int(round(x * width)), int(round(y * height))

    def _find_keyboard_key(self, key_name: str):
        """Find a key by accessibility label; return None-like fallback bounds if hidden."""
        semantic = str(key_name).lower()
        aliases = {
            "shift": {"shift", "shift key", "caps lock", "shift key down", "⇧"},
            "backspace": {
                "backspace", "backspace key", "delete", "delete key",
                "erase", "⌫", "backspace/delete",
            },
            "space": {"space", "spacebar", "space bar", " ", "english (us)", "english (india)", "english (uk)"},
            "symbols_toggle": {"?123", "123?", "symbols", "symbol", "numbers", "number"},
            "alphabet_toggle": {"abc", "letters", "alphabet"},
            "symbol_page": {"=\\<", "=</", "more symbols", "symbols 2"},
            "enter": {"enter", "return", "done", "search", "go", "↵", "⌕"},
        }
        punctuation_names = {
            ",": {",", "comma"}, ".": {".", "dot", "period", "full stop"},
            "/": {"/", "slash"}, "\\": {"\\", "backslash"},
            "'": {"'", "apostrophe", "single quote"},
            '"': {'"', "double quote", "quotation mark"},
            "?": {"?", "question mark"}, "!": {"!", "exclamation mark"},
            "-": {"-", "hyphen", "minus"}, "=": {"=", "equals", "equal sign"},
            ":": {":", "colon"}, ";": {";", "semicolon"},
        }
        if semantic in aliases:
            accepted = aliases[semantic]
        elif len(key_name) == 1:
            accepted = {semantic}
            accepted.update(punctuation_names.get(key_name, set()))
        else:
            accepted = {semantic}

        matches = []
        for node in self._load_keyboard_nodes():
            found = bool(node["labels"].intersection(accepted))
            resource_id = node["resource_id"]
            if not found and semantic == "space" and "space" in resource_id:
                found = True
            elif not found and semantic == "backspace" and any(token in resource_id for token in ("delete", "backspace", "erase")):
                found = True
            elif not found and semantic == "shift" and "shift" in resource_id:
                found = True
            elif not found and semantic == "symbols_toggle" and any(token in resource_id for token in ("symbol", "number", "numeric")):
                found = True
            elif not found and semantic == "alphabet_toggle" and any(token in resource_id for token in ("abc", "alphabet", "letter")):
                found = True
            if not found:
                continue
            left, top, right, bottom = node["bounds"]
            node_width, node_height = right - left, bottom - top
            max_width = node["screen_w"] * (0.80 if semantic == "space" else 0.28)
            if node_width > max_width or node_height > 0.16 * max(1, int(self._screen_size_for_keyboard()[1])):
                continue
            matches.append(node)

        if matches:
            matches.sort(key=lambda node: (node["area"], node["clickable_rank"]))
            best = matches[0]
            return best["bounds"], best["attrs"]

        # If Gboard hides individual soft keys from the accessibility tree, use
        # a screen-relative point. It is scaled to MaaTouch's raw dimensions below.
        x, y = self._fallback_key_point(key_name)
        return (x, y, x + 1, y + 1), {"_coordinate_fallback": "true"}

    def _tap_android_keyboard_key(self, key_name: str) -> Dict[str, str]:
        """Tap one visible Android soft key with MaaTouch (accessibility first, coordinate fallback)."""
        bounds, attrs = self._find_keyboard_key(key_name)
        left, top, right, bottom = bounds
        logical_x = int((left + right) / 2)
        logical_y = int((top + bottom) / 2)
        screen_w, screen_h = self._screen_size_for_keyboard()
        x = int(round(logical_x * self.maatouch.max_x / max(1, screen_w)))
        y = int(round(logical_y * self.maatouch.max_y / max(1, screen_h)))
        x = max(0, min(self.maatouch.max_x - 1, x))
        y = max(0, min(self.maatouch.max_y - 1, y))
        pressure = max(1, min(int(self.maatouch.max_pressure), 75))
        self.maatouch.touch_down(0, x, y, pressure)
        time.sleep(0.070)
        self.maatouch.touch_up(0)
        time.sleep(0.020)

        semantic = str(key_name).lower()
        # Gboard changes its suggestion strip while typing; never reuse old bounds.
        self._keyboard_node_cache = None
        if semantic in {"shift", "symbols_toggle", "alphabet_toggle", "symbol_page", "?123", "abc"}:
            if semantic == "shift":
                self._keyboard_shift_state = None
            elif semantic in {"symbols_toggle", "?123"}:
                self._keyboard_layer = "symbols"
            elif semantic in {"alphabet_toggle", "abc"}:
                self._keyboard_layer = "alpha"
            elif semantic == "symbol_page":
                self._keyboard_layer = "symbols2"
        return attrs

    def _ensure_keyboard_layer(self, desired: str):
        """Switch between QWERTY and the numbers/symbols layer when necessary."""
        if desired not in {"alpha", "symbols", "symbols2"}:
            return
        nodes = self._load_keyboard_nodes()
        self._infer_keyboard_layer_from_nodes(nodes)
        current = self._keyboard_layer or "alpha"
        if desired == current:
            return
        if desired == "symbols2":
            if current == "alpha":
                self._tap_android_keyboard_key("symbols_toggle")
                current = "symbols"
            if current == "symbols":
                # Gboard's second-symbol-page button commonly appears as =\\<.
                try:
                    self._tap_android_keyboard_key("symbol_page")
                except Exception:
                    # Coordinate fallback is still attempted by the key helper;
                    # propagate only if the keyboard geometry is not usable.
                    raise
            self._keyboard_layer = "symbols2"
            self._keyboard_node_cache = None
            return

        if desired == "symbols" and current == "alpha":
            self._tap_android_keyboard_key("symbols_toggle")
        elif desired == "symbols" and current == "symbols2":
            # Gboard's second page returns to QWERTY through ABC; tap ?123 again
            # to reach the number row on symbols page 1.
            self._tap_android_keyboard_key("alphabet_toggle")
            self._tap_android_keyboard_key("symbols_toggle")
        elif desired == "alpha" and current != "alpha":
            self._tap_android_keyboard_key("alphabet_toggle")
        self._keyboard_layer = desired
        self._keyboard_shift_state = None
        self._keyboard_node_cache = None

    def _keyboard_shift_is_active(self):
        """Read Shift state when the keyboard exposes it."""
        if self._keyboard_shift_state is not None:
            return self._keyboard_shift_state
        try:
            _, attrs = self._find_keyboard_key("shift")
        except Exception:
            return None
        values = [attrs.get(name, "").strip().lower() for name in ("selected", "checked", "activated")]
        if any(value in ("true", "1") for value in values):
            self._keyboard_shift_state = True
        elif any(value in ("false", "0") for value in values):
            self._keyboard_shift_state = False
        else:
            return None
        return self._keyboard_shift_state

    def _ensure_keyboard_shift(self, desired: bool):
        active = self._keyboard_shift_is_active()
        if active is None:
            # When the keyboard does not expose Shift state, tap Shift only when
            # needed and track the change we requested.
            if desired:
                self._tap_android_keyboard_key("shift")
            self._keyboard_shift_state = desired
            return
        if active != desired:
            self._tap_android_keyboard_key("shift")
        self._keyboard_shift_state = desired

    def _tap_keyboard_character(self, character: str):
        """Type one character using visible Gboard key taps and explicit layouts."""
        if character == " ":
            self._ensure_keyboard_layer("alpha")
            self._tap_android_keyboard_key("space")
            self._keyboard_shift_state = None
            return
        if character in "\r\n":
            self._tap_android_keyboard_key("enter")
            return

        if character.isalpha():
            self._ensure_keyboard_layer("alpha")
            needs_shift = character.isupper()
            self._ensure_keyboard_shift(needs_shift)
            self._tap_android_keyboard_key(character.lower())
            # Gboard normally releases one-shot Shift after a character.
            self._keyboard_shift_state = False
            return

        # Digits must switch layouts BEFORE looking for a key named "1" etc.
        # On QWERTY, these may also appear as small secondary labels above letters.
        if character in "0123456789":
            self._ensure_keyboard_layer("symbols")
            self._tap_android_keyboard_key(character)
            return

        # Symbols visible on Gboard's first symbols page.
        page1_symbols = set("@#₹_&-+()/;:'\"!?*.,")
        if character in page1_symbols:
            self._ensure_keyboard_layer("symbols")
            self._tap_android_keyboard_key(character)
            self._keyboard_shift_state = None
            return

        # Symbols from Gboard's second page (including dollar, brackets and braces).
        page2_symbols = set("~`|•√π÷×§Δ€¥$¢^°−{}\\%©®™✓[]")
        if character in page2_symbols:
            self._ensure_keyboard_layer("symbols2")
            self._tap_android_keyboard_key(character)
            self._keyboard_shift_state = None
            return

        # Last attempt: use a key directly exposed by the current accessibility tree.
        # Do not silently substitute another key if it cannot be found.
        bounds, attrs = self._find_keyboard_key(character)
        if attrs.get("_coordinate_fallback") != "true":
            self._tap_android_keyboard_key(character)
            return

        raise RuntimeError(
            f"Character {character!r} is not mapped for the current Gboard layout. "
            "Keep the phone in portrait mode with Gboard visible."
        )

    def on_viewport_text_edit(self, previous_text: str, current_text: str) -> bool:
        """Mirror desktop edits live; deletion uses visible Backspace taps.

        `previous_text` is retained for compatibility with the UI callback. The
        engine's own successfully mirrored text is the source of truth, so a
        partial failure does not cause already-tapped keys to be repeated.
        """
        current_text = str(current_text)
        mirrored = str(self._live_phone_text)
        if mirrored == current_text:
            return True

        if current_text and self._live_text_started_at is None:
            self._live_text_started_at = time.perf_counter()

        common_length = min(len(mirrored), len(current_text))
        prefix_length = 0
        while prefix_length < common_length and mirrored[prefix_length] == current_text[prefix_length]:
            prefix_length += 1

        delete_count = len(mirrored) - prefix_length
        replacement = current_text[prefix_length:]

        self.state_cache.notify_interaction_start()
        try:
            for _ in range(delete_count):
                self._tap_android_keyboard_key("backspace")
                self._live_phone_text = self._live_phone_text[:-1]

            for character in replacement:
                self._tap_keyboard_character(character)
                self._live_phone_text += character

        except Exception as exc:
            print(f"[!] Live text synchronization failed after {len(self._live_phone_text)} mirrored characters: {exc}")
            return False
        finally:
            self.state_cache.notify_interaction_end()

        if not current_text:
            self._live_text_started_at = None
        return True

    def on_viewport_text_input(
        self,
        text: str,
        already_injected: bool = False,
    ) -> bool:
        """Record a finished text action, optionally without reinjecting it.

        When ``already_injected`` is True, the desktop bar has already mirrored
        the text to the phone character-by-character, so this method only
        records the semantic action in the JSON.
        """
        payload = str(text)
        if not payload.strip():
            return False

        # A final catch-up attempt prevents an immediately pressed Enter/Inject
        # from recording text that the keyboard did not finish receiving.
        if already_injected and self._live_phone_text != payload:
            if not self.on_viewport_text_edit(self._live_phone_text, payload):
                return False

        action_started_at = (
            self._live_text_started_at
            if already_injected and self._live_text_started_at is not None
            else time.perf_counter()
        )
        think_time_ms = max(
            0,
            int((action_started_at - self._last_step_end_ts) * 1000),
        )

        self.state_cache.notify_interaction_start()
        try:
            pkg, act, orient, _ = self.state_cache.get_snapshot()

            if not already_injected:
                for character in payload:
                    self._tap_keyboard_character(character)

        except Exception as exc:
            print(f"[!] Phone text injection failed: {exc}")
            return False

        finally:
            self.state_cache.notify_interaction_end()

        self._step_counter += 1
        self.steps.append(
            RecordedStep(
                step_id=self._step_counter,
                timestamp=time.time(),
                think_time_ms=think_time_ms,
                event_type="TEXT_INPUT",
                pre_package=pkg,
                pre_activity=act,
                orientation=orient,
                text_payload=payload,
            )
        )

        self._last_step_end_ts = time.perf_counter()
        self._live_text_started_at = None
        # The desktop bar is cleared after an Inject/Enter action. A later new
        # desktop entry should append to the current phone field, not backspace
        # the already-recorded text from the prior action.
        self._live_phone_text = ""
        self._keyboard_node_cache = None

        print(
            f"[+] Recorded TEXT_INPUT (step #{self._step_counter}, "
            f"{len(payload)} characters)"
        )
        return True

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