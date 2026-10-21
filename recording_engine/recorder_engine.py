import json
import re
import math
import subprocess
import threading
import time
import uuid
import queue
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

        # Multi-layer keyboard cache: {"alpha": [...], "sym1": [...], "sym2": [...]}
        self._keyboard_layer_caches: Dict[str, list] = {}
        self._current_kb_layer: str = "alpha"
        self._keyboard_shift_state: Optional[bool] = None

        # Async worker queue so keyboard taps NEVER freeze the 60FPS UI thread
        self._key_queue: "queue.Queue[Optional[Tuple[str, str]]]" = queue.Queue()
        self._key_worker = threading.Thread(target=self._process_key_queue, daemon=True)
        self._key_worker.start()

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
        self._current_kb_layer = "alpha"
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

    def _load_keyboard_nodes(self, force_fresh: bool = False):
        """Cache accessibility nodes per keyboard layer ('alpha', 'sym1', 'sym2')."""
        layer = self._current_kb_layer
        if not force_fresh and layer in self._keyboard_layer_caches:
            return self._keyboard_layer_caches[layer]

        xml_dump = None
        if not force_fresh:
            _, _, _, xml_dump = self.state_cache.get_snapshot()

        def _parse_nodes(xml_text: str):
            if not xml_text:
                return []
            try:
                root = ET.fromstring(xml_text.encode("utf-8"))
            except Exception:
                root = ET.fromstring(xml_text)

            screen_w = max(1, int(self.maatouch.max_x))
            screen_h = max(1, int(self.maatouch.max_y))

            parsed = []
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
                if center_y < screen_h * 0.48 or center_y > screen_h * 0.985:
                    continue
                if width > screen_w * 0.85 or height > screen_h * 0.16:
                    continue

                raw_text = attrs.get("text", "").strip().lower()
                raw_desc = attrs.get("content-desc", "").strip().lower()

                # CRITICAL FIX: If a key's primary text/desc is a single letter (a-z),
                # ignore its secondary 'hint' (like '1' on 'q') so numbers don't tap letters!
                primary_char = raw_text or raw_desc
                if len(primary_char) == 1 and primary_char.isalpha():
                    labels = {primary_char}
                else:
                    # Also strip comma-separated secondary hints like "q, 1" -> "q"
                    cleaned_desc = raw_desc.split(",")[0].strip() if "," in raw_desc and len(raw_desc) <= 6 else raw_desc
                    labels = {raw_text, raw_desc, cleaned_desc}
                    if not raw_text and not raw_desc:
                        labels.add(attrs.get("hint", "").strip().lower())

                labels.discard("")
                parsed.append({
                    "bounds": (left, top, right, bottom),
                    "attrs": attrs,
                    "labels": labels,
                    "resource_id": attrs.get("resource-id", "").lower(),
                    "area": width * height,
                    "clickable_rank": 0 if attrs.get("clickable", "false").lower() == "true" else 1,
                    "screen_w": screen_w,
                    "screen_h": screen_h,
                })
            return parsed

        nodes = _parse_nodes(xml_dump) if xml_dump else []
        if len(nodes) < 10 or force_fresh:
            xml_dump = self.state_cache.u2_dev.dump_hierarchy(compressed=False)
            nodes = _parse_nodes(xml_dump)

        if not nodes:
            raise RuntimeError(
                "No keyboard keys exposed. Ensure a text field is focused and the keyboard is open."
            )
        self._keyboard_layer_caches[layer] = nodes
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
        """Resolve a displayed keyboard key on the active layer."""
        semantic = str(key_name).lower()
        aliases = {
            "shift": {"shift", "shift key", "caps lock", "shift key down"},
            "backspace": {
                "backspace", "backspace key", "delete", "delete key",
                "erase", "⌫", "←", "backspace/delete",
            },
            "space": {
                "space", "spacebar", "space bar", " ",
                "english (us)", "english (india)", "english (uk)",
            },
            "enter": {
                "enter", "search", "go", "done", "next", "send", "return", "↵", "🔍",
            },
            "sym_switch": {
                "?123", "123", "!#1", "?12", "sym", "symbols",
                "symbol", "numbers and symbols", "switch to symbols",
                "more_symbols", "symbol key",
            },
            "alpha_switch": {
                "abc", "letters", "alphabet", "switch to letters", "alphabetic",
            },
            "more_sym_switch": {
                "=\\<", "~\\<", "1/2", "2/2", "more symbols", "#+=", "{&=",
            },
        }
        punctuation_names = {
            ",": {",", "comma"}, ".": {".", "dot", "period", "full stop"},
            "/": {"/", "slash"}, "\\": {"\\", "backslash"},
            "'": {"'", "apostrophe", "single quote"},
            '"': {'"', "double quote", "quotation mark"},
            "?": {"?", "question mark"}, "!": {"!", "exclamation mark"},
            "-": {"-", "hyphen", "minus"}, "=": {"=", "equals", "equal sign"},
            ":": {":", "colon"}, ";": {";", "semicolon"},
            "@": {"@", "at", "at sign"}, "#": {"#", "hash", "pound"},
            "$": {"$", "dollar", "dollar sign"}, "₹": {"₹", "rupee"},
            "%": {"%", "percent"}, "&": {"&", "ampersand"},
            "*": {"*", "asterisk", "star"}, "(": {"(", "left parenthesis", "open parenthesis"},
            ")": {")", "right parenthesis", "close parenthesis"},
            "_": {"_", "underscore"}, "+": {"+", "plus"},
        }
        if semantic in aliases:
            accepted = aliases[semantic]
        elif len(key_name) == 1:
            accepted = {semantic}
            accepted.update(punctuation_names.get(key_name, set()))
        else:
            accepted = {semantic}

        nodes = self._load_keyboard_nodes()
        matches = []
        for node in nodes:
            found = bool(node["labels"].intersection(accepted))
            resource_id = node["resource_id"]
            if not found:
                if semantic == "space" and "space" in resource_id:
                    found = True
                elif semantic == "backspace" and any(
                    t in resource_id for t in ("delete", "backspace", "erase")
                ):
                    found = True
                elif semantic == "shift" and "shift" in resource_id:
                    found = True
                elif semantic in ("sym_switch", "alpha_switch") and any(
                    t in resource_id for t in ("key_pos_switch", "symbol", "mode_change")
                ):
                    found = True
                elif semantic == "enter" and any(
                    t in resource_id for t in ("enter", "action")
                ):
                    found = True
            if not found:
                continue

            left, top, right, bottom = node["bounds"]
            width, height = right - left, bottom - top
            max_width = node["screen_w"] * (0.80 if semantic == "space" else 0.28)
            if width > max_width or height > 0.16 * node["screen_h"]:
                continue
            matches.append(node)

        # Geometric fallback for ?123 / ABC mode switch key (always bottom-left corner of keyboard)
        if not matches and semantic in ("sym_switch", "alpha_switch") and nodes:
            bottom_left_candidates = [
                n for n in nodes
                if n["bounds"][0] < n["screen_w"] * 0.22
                and (n["bounds"][1] + n["bounds"][3]) / 2 > n["screen_h"] * 0.84
            ]
            if bottom_left_candidates:
                bottom_left_candidates.sort(key=lambda n: (-n["bounds"][3], n["bounds"][0]))
                best = bottom_left_candidates[0]
                return best["bounds"], best["attrs"]

        if not matches:
            raise RuntimeError(f"Key '{key_name}' not found on layer '{self._current_kb_layer}'.")

        matches.sort(key=lambda node: (node["area"], node["clickable_rank"]))
        best = matches[0]
        return best["bounds"], best["attrs"]

    def _tap_android_keyboard_key(self, key_name: str) -> Dict[str, str]:
        """Physically tap one visible Android soft-key using batched MaaTouch."""
        bounds, attrs = self._find_keyboard_key(key_name)
        left, top, right, bottom = bounds
        x = int((left + right) / 2)
        y = int((top + bottom) / 2)
        pressure = max(1, min(int(self.maatouch.max_pressure), 75))

        # Fast 18ms tap for snappy real-time visual feedback
        self.maatouch._send(f"d 0 {x} {y} {pressure}\nc\nw 18\nu 0\nc\n")
        time.sleep(0.022)
        return attrs

    def _switch_keyboard_layer(self, target_layer: str):
        """Navigate between 'alpha' (ABC), 'sym1' (?123), and 'sym2'."""
        if self._current_kb_layer == target_layer:
            return

        if target_layer == "alpha":
            self._tap_android_keyboard_key("alpha_switch")
            self._current_kb_layer = "alpha"
            
        elif target_layer == "sym1":
            if self._current_kb_layer == "alpha":
                self._tap_android_keyboard_key("sym_switch")
            elif self._current_kb_layer == "sym2":
                # Tapping the switch key on sym2 usually toggles back to sym1 (?123)
                self._tap_android_keyboard_key("sym_switch") 
            self._current_kb_layer = "sym1"
            
        elif target_layer == "sym2":
            if self._current_kb_layer == "alpha":
                self._tap_android_keyboard_key("sym_switch")
                time.sleep(0.14) # Wait for sym1 to animate before tapping again
            self._tap_android_keyboard_key("more_sym_switch")
            self._current_kb_layer = "sym2"

        # Cache the new layer instantly if we haven't seen it yet
        if target_layer not in self._keyboard_layer_caches:
            time.sleep(0.14)
            self._load_keyboard_nodes(force_fresh=True)

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
        """Enter one character, searching across alpha, sym1, and sym2 layers."""
        if character == " ":
            self._tap_android_keyboard_key("space")
            if self._current_kb_layer != "alpha":
                self._current_kb_layer = "alpha"
            return

        if character in "\r\n":
            self._tap_android_keyboard_key("enter")
            return

        # 1. Standard letters (a-z, A-Z)
        if character.isalpha():
            if self._current_kb_layer != "alpha":
                self._switch_keyboard_layer("alpha")
            needs_shift = character.isupper()
            self._ensure_keyboard_shift(needs_shift)
            self._tap_android_keyboard_key(character.lower())
            self._keyboard_shift_state = False
            return

        # 2. Common punctuation visible directly on the 'alpha' layer (.,)
        if self._current_kb_layer == "alpha" and character in (".", ","):
            try:
                self._tap_android_keyboard_key(character)
                return
            except Exception:
                pass

        # 3. Numbers and Primary Symbols -> Switch to '?123' ('sym1') layer
        if self._current_kb_layer == "alpha":
            self._switch_keyboard_layer("sym1")

        try:
            self._tap_android_keyboard_key(character)
            return
        except Exception:
            pass # Not on sym1, move to sym2

        # 4. Secondary Symbols -> Switch to '=\<' ('sym2') layer
        try:
            self._switch_keyboard_layer("sym2")
            self._tap_android_keyboard_key(character)
            return
        except Exception:
            # Absolute fallback: clear cache and try one last time
            self._load_keyboard_nodes(force_fresh=True)
            self._tap_android_keyboard_key(character)

    def on_viewport_text_edit(
        self,
        previous_text: str,
        current_text: str,
    ) -> bool:
        """Non-blocking live mirror: queues key taps on a background thread."""
        previous_text = str(previous_text)
        current_text = str(current_text)

        if previous_text == current_text:
            return True

        if current_text and self._live_text_started_at is None:
            self._live_text_started_at = time.perf_counter()

        common_length = min(len(previous_text), len(current_text))
        prefix_length = 0
        while (
            prefix_length < common_length
            and previous_text[prefix_length] == current_text[prefix_length]
        ):
            prefix_length += 1

        delete_count = len(previous_text) - prefix_length
        replacement = current_text[prefix_length:]

        self.state_cache.notify_interaction_start()
        for _ in range(delete_count):
            self._key_queue.put(("backspace", ""))
        for character in replacement:
            self._key_queue.put(("char", character))

        if not current_text:
            self._live_text_started_at = None

        return True

    def on_viewport_text_input(
        self,
        text: str,
        already_injected: bool = False,
    ) -> bool:
        payload = str(text)
        if not payload.strip():
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

        pkg, act, orient, _ = self.state_cache.get_snapshot()

        if not already_injected:
            for character in payload:
                self._key_queue.put(("char", character))

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
        print(f"[+] Recorded TEXT_INPUT (step #{self._step_counter}, {len(payload)} chars)")
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

    def _process_key_queue(self):
        """Background consumer that taps soft-keys without blocking the Qt UI video loop."""
        while True:
            item = self._key_queue.get()
            if item is None:
                self._key_queue.task_done()
                break
            action, value = item
            try:
                if action == "backspace":
                    self._tap_android_keyboard_key("backspace")
                elif action == "char":
                    self._tap_keyboard_character(value)
            except Exception as exc:
                print(f"[!] Live key tap warning ({value!r}): {exc}")
            finally:
                self._key_queue.task_done()