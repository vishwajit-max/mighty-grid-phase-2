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
    t_offset_ms: int
    x: int
    y: int
    rx: float
    ry: float
    dx_norm: float
    dy_norm: float
    pressure: int = 50


@dataclass
class UIAnchor:
    resource_id: str = ""
    text: str = ""
    content_desc: str = ""
    class_name: str = ""
    package: str = ""
    bounds: Tuple[int, int, int, int] = (0, 0, 0, 0)
    element_rx: float = 0.5
    element_ry: float = 0.5
    visual_crop_path: Optional[str] = None


@dataclass
class RecordedStep:
    step_id: int
    timestamp: float
    think_time_ms: int
    event_type: str
    pre_package: str
    pre_activity: str
    orientation: int
    anchor: Optional[UIAnchor] = None
    end_anchor: Optional[UIAnchor] = None
    duration_ms: int = 0
    trajectory: List[TouchSample] = field(default_factory=list)
    keycode: Optional[int] = None
    text_payload: Optional[str] = None


# =====================================================================
# 2. MAATOUCH STREAM CONTROLLER
# =====================================================================

class MaaTouchDriver:
    """Manage a persistent ADB shell app_process pipe to MaaTouch."""

    def __init__(
        self,
        serial: str,
        local_maatouch_path: str = (
            r"C:\Users\RDP6\Downloads\Phase-2"
            r"\android_telemetry\telemetry\maatouch"
        ),
        maatouch_remote_path: str = "/data/local/tmp/maatouch",
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
            [
                "adb", "-s", self.serial, "shell",
                f"ls {self.remote_path}",
            ],
            capture_output=True,
            text=True,
        )

        if check.returncode != 0:
            search_root = Path(__file__).resolve().parent.parent
            candidates = list(search_root.rglob("*maatouch*"))
            candidates = [
                candidate
                for candidate in candidates
                if candidate.is_file()
                and not candidate.name.endswith(".py")
            ]

            if (
                self.local_path
                and Path(self.local_path).exists()
            ):
                src = self.local_path
            elif candidates:
                src = str(candidates[0])
            else:
                print(
                    "[!] WARNING: Could not find local MaaTouch binary "
                    f"in {search_root} for {self.serial}."
                )
                return

            print(
                f"[*] Pushing {src} -> "
                f"{self.serial}:{self.remote_path} ..."
            )

            subprocess.run(
                [
                    "adb", "-s", self.serial, "push",
                    src, self.remote_path,
                ],
                check=True,
            )

            subprocess.run(
                [
                    "adb", "-s", self.serial, "shell",
                    f"chmod 777 {self.remote_path}",
                ],
                check=True,
            )

    def _query_wm_size_fallback(self):
        result = subprocess.run(
            [
                "adb", "-s", self.serial, "shell",
                "wm", "size",
            ],
            capture_output=True,
            text=True,
        )

        for line in result.stdout.splitlines():
            if "size:" in line:
                dims = line.split(":")[-1].strip().split("x")

                if (
                    len(dims) == 2
                    and dims[0].isdigit()
                    and dims[1].isdigit()
                ):
                    self.max_x = int(dims[0])
                    self.max_y = int(dims[1])

    def start(self):
        self._ensure_binary_on_device()
        self._query_wm_size_fallback()

        cmd = [
            "adb", "-s", self.serial, "shell",
            (
                f"CLASSPATH={self.remote_path} "
                "app_process / com.shxyke.MaaTouch.App"
            ),
        ]

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        header_holder = []

        def _read_header():
            try:
                line = self.proc.stdout.readline().strip()
                header_holder.append(line)
            except Exception:
                pass

        thread = threading.Thread(
            target=_read_header,
            daemon=True,
        )
        thread.start()
        thread.join(timeout=2.0)

        if (
            header_holder
            and header_holder[0].startswith("^")
        ):
            parts = header_holder[0].split()
            self.max_x = int(parts[2])
            self.max_y = int(parts[3])
            self.max_pressure = int(parts[4])

            print(
                f"[+] MaaTouch connected on {self.serial}: "
                f"{self.max_x}x{self.max_y} "
                f"(max_p={self.max_pressure})"
            )
        else:
            error = (
                header_holder[0]
                if header_holder
                else "timeout waiting for '^' header"
            )

            print(
                f"[!] MaaTouch startup warning ({error}). "
                f"Using wm size {self.max_x}x{self.max_y}."
            )

    def _send(self, payload: str):
        if not self.proc or not self.proc.stdin:
            return

        with self._lock:
            self.proc.stdin.write(payload)
            self.proc.stdin.flush()

    def touch_down(
        self,
        pointer_id: int,
        x: int,
        y: int,
        pressure: int = 50,
    ):
        self._send(
            f"d {pointer_id} {x} {y} {pressure}\nc\n"
        )

    def touch_move(
        self,
        pointer_id: int,
        x: int,
        y: int,
        pressure: int = 50,
    ):
        self._send(
            f"m {pointer_id} {x} {y} {pressure}\nc\n"
        )

    def touch_up(self, pointer_id: int):
        self._send(f"u {pointer_id}\nc\n")

    def press_key(self, keycode: int):
        self._send(f"k {keycode} o\nc\n")

    def input_text(self, text: str):
        """Retained for existing callers that use MaaTouch text input."""
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
    Refresh the UI hierarchy and activity during idle windows so touch-down
    can resolve the clicked element without blocking the gesture path.
    """

    def __init__(self, serial: str):
        self.u2_dev = u2.connect(serial)
        self.latest_xml: Optional[str] = None
        self.latest_package: str = ""
        self.latest_activity: str = ""
        self.orientation: int = 0
        self.is_touch_active = False
        self.last_interaction_ts = 0.0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(
            target=self._poll_loop,
            daemon=True,
        )

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
            idle_elapsed = (
                time.perf_counter() - self.last_interaction_ts
            )

            if (
                not self.is_touch_active
                and idle_elapsed >= 0.35
            ):
                self._refresh_now()
                time.sleep(0.4)
            else:
                time.sleep(0.05)

    def _refresh_now(self):
        try:
            app_info = self.u2_dev.app_current()
            xml_dump = self.u2_dev.dump_hierarchy(
                compressed=False
            )
            orientation = self.u2_dev.info.get(
                "displayRotation",
                0,
            )

            with self._lock:
                self.latest_package = app_info.get("package", "")
                self.latest_activity = app_info.get("activity", "")
                self.latest_xml = xml_dump
                self.orientation = orientation

        except Exception:
            pass

    def get_snapshot(
        self,
    ) -> Tuple[str, str, int, Optional[str]]:
        with self._lock:
            return (
                self.latest_package,
                self.latest_activity,
                self.orientation,
                self.latest_xml,
            )

    def resolve_node_at(
        self,
        x: int,
        y: int,
        xml_str: Optional[str],
    ) -> UIAnchor:
        """Find the smallest identifiable UI node containing (x, y)."""
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

            parts = (
                bounds_str
                .replace("][", ",")
                .strip("[]")
                .split(",")
            )

            try:
                left, top, right, bottom = map(int, parts)
            except ValueError:
                continue

            if (
                left <= x <= right
                and top <= y <= bottom
            ):
                width = right - left
                height = bottom - top
                area = width * height

                if area <= 0:
                    continue

                has_id = bool(
                    elem.attrib.get("resource-id")
                    or elem.attrib.get("text")
                    or elem.attrib.get("content-desc")
                )

                score = area * (0.5 if has_id else 1.0)

                if score <= smallest_area:
                    smallest_area = score
                    best_node = (
                        elem,
                        (left, top, right, bottom),
                    )

        if not best_node:
            return UIAnchor()

        elem, (left, top, right, bottom) = best_node
        width = max(1, right - left)
        height = max(1, bottom - top)

        return UIAnchor(
            resource_id=elem.attrib.get("resource-id", ""),
            text=elem.attrib.get("text", ""),
            content_desc=elem.attrib.get("content-desc", ""),
            class_name=elem.attrib.get("class", ""),
            package=elem.attrib.get("package", ""),
            bounds=(left, top, right, bottom),
            element_rx=round((x - left) / width, 4),
            element_ry=round((y - top) / height, 4),
        )

    def stop(self):
        self._stop_event.set()


# =====================================================================
# 4. MAIN RECORDING ENGINE CONTROLLER
# =====================================================================

class AndroidRecordingEngine:
    """
    Record viewport gestures and semantic actions from the host UI.
    """

    def __init__(
        self,
        serial: str,
        session_dir: str = "./recordings",
    ):
        self.serial = serial
        self.session_id = uuid.uuid4().hex[:8]
        self.output_dir = (
            Path(session_dir) / f"session_{self.session_id}"
        )
        self.crops_dir = self.output_dir / "crops"
        self.crops_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.maatouch = MaaTouchDriver(serial)
        self.state_cache = DeviceStateCache(serial)

        self.steps: List[RecordedStep] = []
        self._step_counter = 0
        self._last_step_end_ts = time.perf_counter()

        # Start time for a live text-edit session. The completed text is
        # recorded as one TEXT_INPUT step when the user presses Enter/Inject.
        self._live_text_started_at: Optional[float] = None

        # Active gesture state
        self._active_samples: List[TouchSample] = []
        self._down_ts: float = 0.0
        self._pre_touch_snapshot: Optional[
            Tuple[str, str, int, Optional[str]]
        ] = None
        self._pre_touch_frame: Optional[np.ndarray] = None
        self._latest_video_frame: Optional[np.ndarray] = None

    def start_session(self):
        self.maatouch.start()
        self.state_cache.start()
        self._last_step_end_ts = time.perf_counter()

    def update_mirror_frame(self, frame_bgr: np.ndarray):
        """Receive frames from the scrcpy/video decode callback."""
        self._latest_video_frame = frame_bgr

    # -----------------------------------------------------------------
    # VIEWPORT TOUCH HOOKS
    # -----------------------------------------------------------------

    def on_viewport_pointer_down(
        self,
        device_x: int,
        device_y: int,
        pressure: int = 50,
    ):
        self.maatouch.touch_down(
            0,
            device_x,
            device_y,
            pressure,
        )

        self.state_cache.notify_interaction_start()
        self._down_ts = time.perf_counter()
        self._pre_touch_snapshot = self.state_cache.get_snapshot()

        if self._latest_video_frame is not None:
            self._pre_touch_frame = (
                self._latest_video_frame.copy()
            )

        width = self.maatouch.max_x
        height = self.maatouch.max_y

        self._active_samples = [
            TouchSample(
                t_offset_ms=0,
                x=device_x,
                y=device_y,
                rx=round(device_x / width, 4),
                ry=round(device_y / height, 4),
                dx_norm=0.0,
                dy_norm=0.0,
                pressure=pressure,
            )
        ]

    def on_viewport_pointer_move(
        self,
        device_x: int,
        device_y: int,
        pressure: int = 50,
    ):
        if not self._active_samples:
            return

        self.maatouch.touch_move(
            0,
            device_x,
            device_y,
            pressure,
        )

        t_offset = int(
            (time.perf_counter() - self._down_ts) * 1000
        )
        last = self._active_samples[-1]

        if (
            last.x == device_x
            and last.y == device_y
            and t_offset - last.t_offset_ms < 8
        ):
            return

        width = self.maatouch.max_x
        height = self.maatouch.max_y
        start = self._active_samples[0]

        self._active_samples.append(
            TouchSample(
                t_offset_ms=t_offset,
                x=device_x,
                y=device_y,
                rx=round(device_x / width, 4),
                ry=round(device_y / height, 4),
                dx_norm=round(
                    (device_x - start.x) / width,
                    4,
                ),
                dy_norm=round(
                    (device_y - start.y) / height,
                    4,
                ),
                pressure=pressure,
            )
        )

    def on_viewport_pointer_up(
        self,
        device_x: int,
        device_y: int,
    ):
        if not self._active_samples:
            return

        self.maatouch.touch_up(0)
        self.state_cache.notify_interaction_end()

        now = time.perf_counter()
        duration_ms = int(
            (now - self._down_ts) * 1000
        )
        think_time_ms = int(
            (self._down_ts - self._last_step_end_ts) * 1000
        )
        self._last_step_end_ts = now

        width = self.maatouch.max_x
        height = self.maatouch.max_y
        start = self._active_samples[0]

        self._active_samples.append(
            TouchSample(
                t_offset_ms=duration_ms,
                x=device_x,
                y=device_y,
                rx=round(device_x / width, 4),
                ry=round(device_y / height, 4),
                dx_norm=round(
                    (device_x - start.x) / width,
                    4,
                ),
                dy_norm=round(
                    (device_y - start.y) / height,
                    4,
                ),
                pressure=0,
            )
        )

        pixel_dist = math.hypot(
            device_x - start.x,
            device_y - start.y,
        )

        if pixel_dist > max(width, height) * 0.025:
            gesture_type = "SWIPE"
        elif duration_ms >= 500:
            gesture_type = "LONG_PRESS"
        else:
            gesture_type = "TAP"

        (
            pkg,
            activity,
            orientation,
            xml_str,
        ) = self._pre_touch_snapshot or ("", "", 0, None)

        start_anchor = self.state_cache.resolve_node_at(
            start.x,
            start.y,
            xml_str,
        )

        end_anchor = (
            self.state_cache.resolve_node_at(
                device_x,
                device_y,
                xml_str,
            )
            if gesture_type == "SWIPE"
            else None
        )

        self._step_counter += 1

        if self._pre_touch_frame is not None:
            crop_file = self._save_visual_crop(
                self._step_counter,
                start.x,
                start.y,
                start_anchor.bounds,
            )
            start_anchor.visual_crop_path = crop_file

        step = RecordedStep(
            step_id=self._step_counter,
            timestamp=time.time(),
            think_time_ms=max(0, think_time_ms),
            event_type=gesture_type,
            pre_package=pkg,
            pre_activity=activity,
            orientation=orientation,
            anchor=start_anchor,
            end_anchor=end_anchor,
            duration_ms=duration_ms,
            trajectory=list(self._active_samples),
        )

        self.steps.append(step)
        self._active_samples.clear()

    # -----------------------------------------------------------------
    # HARDWARE KEYS & SEMANTIC TEXT HOOKS
    # -----------------------------------------------------------------

    def on_viewport_key_event(self, keycode: int):
        """Record a hardware-key action."""
        now = time.perf_counter()
        think_time_ms = int(
            (now - self._last_step_end_ts) * 1000
        )
        self._last_step_end_ts = now

        self.state_cache.notify_interaction_start()

        pkg, activity, orientation, _ = (
            self.state_cache.get_snapshot()
        )

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
                pre_activity=activity,
                orientation=orientation,
                keycode=keycode,
            )
        )

    def on_viewport_text_edit(
        self,
        previous_text: str,
        current_text: str,
    ) -> bool:
        """
        Mirror edits from the desktop injection bar to the focused Android
        field while the user types.

        This tracks text owned by the injection bar. It assumes the Android
        caret remains at the end of the injected text; normal typing, paste,
        backspace and suffix replacement are supported.
        """
        previous_text = str(previous_text)
        current_text = str(current_text)

        if previous_text == current_text:
            return True

        if current_text and self._live_text_started_at is None:
            self._live_text_started_at = time.perf_counter()

        common_length = min(
            len(previous_text),
            len(current_text),
        )

        prefix_length = 0

        while (
            prefix_length < common_length
            and previous_text[prefix_length] == current_text[prefix_length]
        ):
            prefix_length += 1

        delete_count = len(previous_text) - prefix_length
        replacement = current_text[prefix_length:]

        self.state_cache.notify_interaction_start()

        try:
            # Remove the previous suffix from the end of the phone field.
            for _ in range(delete_count):
                self.state_cache.u2_dev.press("delete")

            # Append only the newly typed/replaced suffix.
            if replacement:
                self.state_cache.u2_dev.send_keys(
                    replacement,
                    clear=False,
                )

        except Exception as exc:
            print(
                f"[!] Live text synchronization failed: {exc}"
            )
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
        """
        Record the completed semantic text action.

        If already_injected=True, the text has already been mirrored to the
        phone character-by-character; do not inject the same text twice.
        """
        payload = str(text)

        if not payload.strip():
            return False

        action_started_at = (
            self._live_text_started_at
            if (
                already_injected
                and self._live_text_started_at is not None
            )
            else time.perf_counter()
        )

        think_time_ms = max(
            0,
            int(
                (
                    action_started_at
                    - self._last_step_end_ts
                ) * 1000
            ),
        )

        self.state_cache.notify_interaction_start()

        try:
            (
                pkg,
                activity,
                orientation,
                _,
            ) = self.state_cache.get_snapshot()

            if not already_injected:
                self.state_cache.u2_dev.send_keys(
                    payload,
                    clear=False,
                )

        except Exception as exc:
            print(
                f"[!] Phone text injection failed: {exc}"
            )
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
                pre_activity=activity,
                orientation=orientation,
                text_payload=payload,
            )
        )

        self._last_step_end_ts = time.perf_counter()
        self._live_text_started_at = None

        print(
            f"[+] Recorded TEXT_INPUT "
            f"(step #{self._step_counter}, "
            f"{len(payload)} characters)"
        )

        return True

    # -----------------------------------------------------------------
    # VISUAL FALLBACK & SERIALIZATION
    # -----------------------------------------------------------------

    def _save_visual_crop(
        self,
        step_id: int,
        x: int,
        y: int,
        bounds: Tuple[int, int, int, int],
    ) -> Optional[str]:
        frame = self._pre_touch_frame

        if frame is None:
            return None

        frame_height, frame_width = frame.shape[:2]
        left, top, right, bottom = bounds

        if (
            0 < right - left < frame_width * 0.8
            and 0 < bottom - top < frame_height * 0.5
        ):
            x1 = max(0, left)
            y1 = max(0, top)
            x2 = min(frame_width, right)
            y2 = min(frame_height, bottom)
        else:
            half = 70
            x1 = max(0, x - half)
            y1 = max(0, y - half)
            x2 = min(frame_width, x + half)
            y2 = min(frame_height, y + half)

        crop = frame[y1:y2, x1:x2]

        if crop.size == 0:
            return None

        relative_path = f"crops/step_{step_id:03d}.png"

        cv2.imwrite(
            str(self.output_dir / relative_path),
            crop,
        )

        return relative_path

    def stop_and_export(self) -> Path:
        self.state_cache.stop()
        self.maatouch.stop()

        payload = {
            "session_id": self.session_id,
            "source_device_serial": self.serial,
            "source_resolution": {
                "width": self.maatouch.max_x,
                "height": self.maatouch.max_y,
            },
            "total_steps": len(self.steps),
            "steps": [
                asdict(step)
                for step in self.steps
            ],
        }

        out_file = self.output_dir / "recording.json"

        out_file.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )

        return out_file