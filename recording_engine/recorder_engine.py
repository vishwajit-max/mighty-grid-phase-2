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
    # Explicitly distinguish an element crop from a point-centred crop.
    visual_crop_mode: Optional[str] = None
    visual_crop_anchor_rx: Optional[float] = None
    visual_crop_anchor_ry: Optional[float] = None
    # Live UI properties captured from the hierarchy BEFORE touch injection.
    clickable: bool = False
    long_clickable: bool = False
    focusable: bool = False
    enabled: bool = True
    visible_to_user: bool = True


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
        # Input must never be reported as successful unless MaaTouch completed
        # its protocol handshake for this process.
        self._ready = False

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
        """Start MaaTouch and fail closed unless its protocol header is valid."""
        self._ready = False
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

        header_holder = []
        def _read_header():
            try:
                if self.proc and self.proc.stdout:
                    header_holder.append(self.proc.stdout.readline().strip())
            except Exception as exc:
                header_holder.append(f"HEADER_READ_ERROR: {exc}")

        reader = threading.Thread(target=_read_header, daemon=True)
        reader.start()
        reader.join(timeout=4.0)

        header = header_holder[0] if header_holder else ""
        try:
            if reader.is_alive():
                raise RuntimeError("timed out waiting 4 seconds for MaaTouch protocol header")
            if not self.proc or self.proc.poll() is not None:
                exit_code = self.proc.returncode if self.proc else "not started"
                raise RuntimeError(f"MaaTouch process exited before handshake (code={exit_code})")
            if not header.startswith("^"):
                raise RuntimeError(
                    f"invalid MaaTouch protocol header: {header or '<no output>'!r}"
                )

            parts = header.split()
            if len(parts) < 5:
                raise RuntimeError(f"malformed MaaTouch protocol header: {header!r}")
            max_x, max_y, max_pressure = int(parts[2]), int(parts[3]), int(parts[4])
            if max_x <= 0 or max_y <= 0 or max_pressure <= 0:
                raise RuntimeError(f"invalid MaaTouch dimensions/pressure in header: {header!r}")

            self.max_x = max_x
            self.max_y = max_y
            self.max_pressure = max_pressure
            self._ready = True
            print(
                f"[+] MaaTouch connected on {self.serial}: "
                f"{self.max_x}x{self.max_y} (max_p={self.max_pressure})",
                flush=True,
            )
        except Exception:
            self._ready = False
            proc, self.proc = self.proc, None
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            raise

    def _send(self, payload: str):
        """Send commands only through a live, handshaken MaaTouch process."""
        proc = self.proc
        if not self._ready or proc is None:
            raise RuntimeError(f"MaaTouch is not ready for device {self.serial}")
        if proc.poll() is not None:
            self._ready = False
            raise RuntimeError(
                f"MaaTouch process for {self.serial} exited with code {proc.returncode}"
            )
        if proc.stdin is None or proc.stdin.closed:
            self._ready = False
            raise RuntimeError(f"MaaTouch stdin is unavailable for {self.serial}")
        try:
            with self._lock:
                proc.stdin.write(payload)
                proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._ready = False
            raise RuntimeError(f"MaaTouch command dispatch failed on {self.serial}: {exc}") from exc

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
        self._ready = False
        proc, self.proc = self.proc, None
        if proc:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


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
        # Monotonic timestamp helps detect snapshots from the previous app.
        self.latest_snapshot_ts: float = 0.0
        self.is_touch_active: bool = False
        self.last_interaction_ts: float = 0.0
        self._lock = threading.Lock()
        # Prevent the background poller and pointer-down freshness check from
        # racing two dump_hierarchy() calls and pairing package/XML from different screens.
        self._refresh_lock = threading.Lock()
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
            idle_elapsed = time.perf_counter() - self.last_interaction_ts
            # Refresh shortly after every release. The old 350ms settle plus
            # 400ms sleep could leave the next event tagged with the previous app.
            if not self.is_touch_active and idle_elapsed >= 0.12:
                self._refresh_now()
                time.sleep(0.10)
            else:
                time.sleep(0.025)

    def _refresh_now(self):
        """Capture a package-stable UI dump and timestamp it atomically."""
        with self._refresh_lock:
            package = ""
            activity = ""
            xml_dump = None
            orient = self.orientation
            stable = False
            try:
                for _ in range(3):
                    before = self.u2_dev.app_current() or {}
                    before_package = str(before.get("package", "") or "").strip()
                    before_activity = str(before.get("activity", "") or "").strip()
                    xml_candidate = self.u2_dev.dump_hierarchy(compressed=False)
                    after = self.u2_dev.app_current() or {}
                    after_package = str(after.get("package", "") or "").strip()
                    after_activity = str(after.get("activity", "") or "").strip()
                    same_package = before_package == after_package
                    same_activity = not (before_activity and after_activity) or before_activity == after_activity
                    if same_package and same_activity:
                        package = after_package
                        activity = after_activity
                        xml_dump = xml_candidate
                        stable = True
                        break
                orient = self.u2_dev.info.get("displayRotation", 0)
            except Exception:
                # Keep the last good data and its old timestamp so callers can
                # recognize it as stale; do not make an old dump look fresh.
                return

            if not stable:
                # A transition happened while dumping. Do not attach XML from
                # one screen to the package identity of another screen.
                package = ""
                activity = ""
                xml_dump = None

            snapshot_ts = time.perf_counter()
            with self._lock:
                self.latest_package = package
                self.latest_activity = activity
                self.latest_xml = xml_dump
                self.orientation = orient
                self.latest_snapshot_ts = snapshot_ts

    def get_snapshot(self) -> Tuple[str, str, int, Optional[str]]:
        with self._lock:
            return (
                self.latest_package,
                self.latest_activity,
                self.orientation,
                self.latest_xml
            )

    def get_recording_snapshot(
        self,
        max_age_s: float = 0.20,
        refresh_if_stale: bool = True,
    ) -> Tuple[str, str, int, Optional[str]]:
        """Return a fresh pre-interaction UI snapshot.

        A stale snapshot is refreshed synchronously before the physical touch is
        injected. This intentionally prefers correct target metadata over
        recording a confident-but-wrong package/anchor from the previous screen.
        """
        max_age = max(0.05, float(max_age_s))
        with self._lock:
            age_s = (
                float("inf") if self.latest_snapshot_ts <= 0.0
                else time.perf_counter() - self.latest_snapshot_ts
            )

        if refresh_if_stale and age_s > max_age:
            self._refresh_now()

        with self._lock:
            age_s = (
                float("inf") if self.latest_snapshot_ts <= 0.0
                else time.perf_counter() - self.latest_snapshot_ts
            )
            snapshot = (
                self.latest_package,
                self.latest_activity,
                self.orientation,
                self.latest_xml,
            )

        if age_s > max_age or not snapshot[3] or not snapshot[0]:
            print(
                f"[!] UI snapshot unavailable/freshness check failed ({age_s * 1000:.0f} ms); "
                "this step will not use stale semantic target metadata.",
                flush=True,
            )
            return "", "", snapshot[2], None
        return snapshot

    def resolve_node_at(self, x: int, y: int, xml_str: Optional[str]) -> UIAnchor:
        """Resolve the touch point to the most specific live interactive node.

        Prefer enabled, visible clickable/long-clickable controls. This avoids
        assigning a large decorative container (for example an app-drawer
        header) as the target when a real clickable node is under the pointer.
        """
        if not xml_str:
            return UIAnchor()
        try:
            root = ET.fromstring(xml_str.encode("utf-8"))
        except Exception:
            return UIAnchor()

        candidates = []
        fallback_candidates = []
        for elem in root.iter("node"):
            attrs = elem.attrib
            bounds_str = attrs.get("bounds", "")
            match = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds_str)
            if not match:
                continue
            l, t, r, b = map(int, match.groups())
            if r <= l or b <= t or not (l <= x <= r and t <= y <= b):
                continue
            enabled = str(attrs.get("enabled", "true")).casefold() != "false"
            visible = str(attrs.get("visible-to-user", "true")).casefold() != "false"
            if not enabled or not visible:
                continue
            area = (r - l) * (b - t)
            if area <= 0:
                continue
            clickable = str(attrs.get("clickable", "false")).casefold() == "true"
            long_clickable = str(attrs.get("long-clickable", "false")).casefold() == "true"
            focusable = str(attrs.get("focusable", "false")).casefold() == "true"
            has_label = bool(
                attrs.get("resource-id") or attrs.get("text") or attrs.get("content-desc")
            )
            # Accessibility bounds often contain nested nodes. For input
            # actions select the smallest actionable enclosing element first.
            rank = (
                0 if (clickable or long_clickable) else
                1 if focusable else
                2 if has_label else 3,
                area,
                0 if has_label else 1,
            )
            record = (rank, elem, (l, t, r, b), {
                "clickable": clickable,
                "long_clickable": long_clickable,
                "focusable": focusable,
                "enabled": enabled,
                "visible_to_user": visible,
            })
            fallback_candidates.append(record)
            if clickable or long_clickable or focusable:
                candidates.append(record)

        pool = candidates or fallback_candidates
        if not pool:
            return UIAnchor()
        pool.sort(key=lambda item: item[0])
        _, elem, (l, t, r, b), flags = pool[0]
        w = max(1, r - l)
        h = max(1, b - t)
        attrs = elem.attrib
        return UIAnchor(
            resource_id=attrs.get("resource-id", ""),
            text=attrs.get("text", ""),
            content_desc=attrs.get("content-desc", ""),
            class_name=attrs.get("class", ""),
            package=attrs.get("package", ""),
            bounds=(l, t, r, b),
            element_rx=max(0.0, min(1.0, round((x - l) / w, 4))),
            element_ry=max(0.0, min(1.0, round((y - t) / h, 4))),
            clickable=flags["clickable"],
            long_clickable=flags["long_clickable"],
            focusable=flags["focusable"],
            enabled=flags["enabled"],
            visible_to_user=flags["visible_to_user"],
        )

    def resolve_focused_input(self, xml_str: Optional[str]) -> UIAnchor:
        """Resolve the visible focused text field from a live hierarchy dump."""
        if not xml_str:
            return UIAnchor()
        try:
            root = ET.fromstring(xml_str.encode("utf-8"))
        except Exception:
            return UIAnchor()
        candidates = []
        for elem in root.iter("node"):
            attrs = elem.attrib
            class_name = str(attrs.get("class", "") or "")
            lower_class = class_name.casefold()
            editable = any(token in lower_class for token in (
                "edittext", "autocompletetextview", "textinputedittext",
            ))
            if not editable:
                continue
            if str(attrs.get("enabled", "true")).casefold() == "false":
                continue
            if str(attrs.get("visible-to-user", "true")).casefold() == "false":
                continue
            bounds_str = attrs.get("bounds", "")
            match = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds_str)
            if not match:
                continue
            l, t, r, b = map(int, match.groups())
            if r <= l or b <= t:
                continue
            focused = str(attrs.get("focused", "false")).casefold() == "true"
            focusable = str(attrs.get("focusable", "false")).casefold() == "true"
            if not focused and not focusable:
                continue
            area = (r - l) * (b - t)
            candidates.append((0 if focused else 1, area, elem, (l, t, r, b)))
        if not candidates:
            return UIAnchor()
        # Prefer an actually focused editable node; then choose the smallest
        # focused/editable field if Android exposes multiple controls.
        candidates.sort(key=lambda item: (item[0], item[1]))
        _, _, elem, (l, t, r, b) = candidates[0]
        attrs = elem.attrib
        w, h = max(1, r - l), max(1, b - t)
        return UIAnchor(
            resource_id=attrs.get("resource-id", ""),
            text=attrs.get("text", ""),
            content_desc=attrs.get("content-desc", ""),
            class_name=attrs.get("class", ""),
            package=attrs.get("package", ""),
            bounds=(l, t, r, b),
            element_rx=0.5,
            element_ry=0.5,
            clickable=str(attrs.get("clickable", "false")).casefold() == "true",
            long_clickable=str(attrs.get("long-clickable", "false")).casefold() == "true",
            focusable=str(attrs.get("focusable", "false")).casefold() == "true",
            enabled=str(attrs.get("enabled", "true")).casefold() != "false",
            visible_to_user=str(attrs.get("visible-to-user", "true")).casefold() != "false",
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
        self._live_text_pre_snapshot: Optional[Tuple[str, str, int, Optional[str]]] = None
        self._live_text_anchor: Optional[UIAnchor] = None

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
        # CRITICAL ORDER: freeze the pre-touch hierarchy/package and video frame
        # BEFORE injecting touch-down. The old order injected first, paused the
        # cache poller, and then read a potentially stale/transitioned snapshot.
        self._current_kb_layer = "alpha"
        self._pre_touch_snapshot = self.state_cache.get_recording_snapshot(
            max_age_s=0.20,
            refresh_if_stale=True,
        )
        frame = self._latest_video_frame
        self._pre_touch_frame = frame.copy() if frame is not None else None

        # Pause background dumps only after the snapshot is captured, then pass
        # the action through to MaaTouch. If injection fails, resume cache polling.
        self.state_cache.notify_interaction_start()
        self._down_ts = time.perf_counter()
        try:
            self.maatouch.touch_down(0, device_x, device_y, pressure)
        except Exception:
            self.state_cache.notify_interaction_end()
            self._active_samples.clear()
            raise

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
            crop_file = self._save_visual_crop(
                self._step_counter, start.x, start.y, start_anchor
            )
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

        pkg, act, orient, _ = self.state_cache.get_recording_snapshot(
            max_age_s=0.20, refresh_if_stale=True
        )
        self.state_cache.notify_interaction_start()
        try:
            self.maatouch.press_key(keycode)
        finally:
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
            # Capture the text field context before the first character is sent
            # through the keyboard. This lets replay verify it is typing into the
            # expected field, rather than whichever field happens to have focus.
            self._live_text_pre_snapshot = self.state_cache.get_recording_snapshot(
                max_age_s=0.20, refresh_if_stale=True
            )
            _, _, _, pre_xml = self._live_text_pre_snapshot
            self._live_text_anchor = self.state_cache.resolve_focused_input(pre_xml)
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

        if already_injected and self._live_text_pre_snapshot is not None:
            pkg, act, orient, _ = self._live_text_pre_snapshot
            text_anchor = self._live_text_anchor or UIAnchor()
        else:
            pkg, act, orient, live_xml = self.state_cache.get_recording_snapshot(
                max_age_s=0.20, refresh_if_stale=True
            )
            text_anchor = self.state_cache.resolve_focused_input(live_xml)

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
                anchor=text_anchor,
                text_payload=payload,
            )
        )

        self._last_step_end_ts = time.perf_counter()
        self._live_text_started_at = None
        self._live_text_pre_snapshot = None
        self._live_text_anchor = None
        print(f"[+] Recorded TEXT_INPUT (step #{self._step_counter}, {len(payload)} chars)")
        return True

    # -----------------------------------------------------------------
    # VISUAL FALLBACK & SERIALIZATION
    # -----------------------------------------------------------------

    def _save_visual_crop(
        self, step_id: int, x: int, y: int, anchor: UIAnchor
    ) -> Optional[str]:
        """Save a replay template using correct device-to-preview coordinates.

        The scrcpy stream is downscaled (max_size=1280) while touch events and
        UIAutomator bounds use device/logical-screen coordinates. Convert bounds
        into frame coordinates before slicing, then resize the crop back to the
        corresponding device-space dimensions so the replay matcher can compare
        it with uiautomator2's full-resolution screenshot.
        """
        frame = self._pre_touch_frame
        if frame is None:
            return None

        fh, fw = frame.shape[:2]
        touch_w = max(1, int(self.maatouch.max_x))
        touch_h = max(1, int(self.maatouch.max_y))
        try:
            ui_w, ui_h = self.state_cache.u2_dev.window_size()
            ui_w, ui_h = max(1, int(ui_w)), max(1, int(ui_h))
        except Exception:
            ui_w, ui_h = touch_w, touch_h

        # Pointer coordinates are in MaaTouch/device space; the hierarchy's
        # bounds are in UIAutomator logical-screen space. Both must map to the
        # actual (downscaled) scrcpy frame before indexing the numpy array.
        l, t, r, b = tuple(int(v) for v in (anchor.bounds or (0, 0, 0, 0)))
        left = int(round(l * fw / ui_w))
        top = int(round(t * fh / ui_h))
        right = int(round(r * fw / ui_w))
        bottom = int(round(b * fh / ui_h))

        bounds_w = max(0, r - l)
        bounds_h = max(0, b - t)
        use_element_crop = (
            bounds_w >= 12 and bounds_h >= 12
            and bounds_w < ui_w * 0.80
            and bounds_h < ui_h * 0.50
            and right > left and bottom > top
        )

        if use_element_crop:
            x1, y1 = max(0, left), max(0, top)
            x2, y2 = min(fw, right), min(fh, bottom)
            target_w = max(1, int(round(bounds_w * touch_w / ui_w)))
            target_h = max(1, int(round(bounds_h * touch_h / ui_h)))
            anchor.visual_crop_mode = "element"
            anchor.visual_crop_anchor_rx = max(0.0, min(1.0, float(anchor.element_rx)))
            anchor.visual_crop_anchor_ry = max(0.0, min(1.0, float(anchor.element_ry)))
        else:
            # A viewport-sized container is a poor visual template. Save a
            # 140x140-device-pixel patch around the tap instead.
            half = 70
            nx1 = max(0, int(x) - half)
            ny1 = max(0, int(y) - half)
            nx2 = min(touch_w, int(x) + half)
            ny2 = min(touch_h, int(y) + half)
            x1 = max(0, int(round(nx1 * fw / touch_w)))
            y1 = max(0, int(round(ny1 * fh / touch_h)))
            x2 = min(fw, int(round(nx2 * fw / touch_w)))
            y2 = min(fh, int(round(ny2 * fh / touch_h)))
            target_w = max(1, nx2 - nx1)
            target_h = max(1, ny2 - ny1)
            anchor.visual_crop_mode = "point"
            anchor.visual_crop_anchor_rx = max(
                0.0, min(1.0, (int(x) - nx1) / max(1, nx2 - nx1))
            )
            anchor.visual_crop_anchor_ry = max(
                0.0, min(1.0, (int(y) - ny1) / max(1, ny2 - ny1))
            )

        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            anchor.visual_crop_mode = None
            anchor.visual_crop_anchor_rx = None
            anchor.visual_crop_anchor_ry = None
            return None

        # Normalize the template to device-coordinate pixel dimensions. This
        # also removes the scrcpy max_size scaling factor from the saved crop.
        if crop.shape[1] != target_w or crop.shape[0] != target_h:
            interpolation = cv2.INTER_CUBIC if (
                crop.shape[1] < target_w or crop.shape[0] < target_h
            ) else cv2.INTER_AREA
            crop = cv2.resize(crop, (target_w, target_h), interpolation=interpolation)

        rel_path = f"crops/step_{step_id:03d}.png"
        if not cv2.imwrite(str(self.output_dir / rel_path), crop):
            return None
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
