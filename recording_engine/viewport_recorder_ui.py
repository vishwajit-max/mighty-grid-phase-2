import argparse
import math
import random
import socket
import subprocess
import sys
import threading
import time
import shutil
from pathlib import Path
from typing import Optional

import av
import cv2
import numpy as np

from PySide6.QtCore import Qt, Signal, QObject, QPoint, QTimer
from PySide6.QtGui import QImage, QPixmap, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QPushButton,
    QLineEdit,
    QScrollArea,
    QFrame,
)

# Make the shared persona catalogue available when the recorder is launched
# directly or as a subprocess from the FastAPI dashboard.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TELEMETRY_DIR = PROJECT_ROOT / "android_telemetry" / "telemetry"
if str(TELEMETRY_DIR) not in sys.path:
    sys.path.insert(0, str(TELEMETRY_DIR))

import personas
from recorder_engine import AndroidRecordingEngine


# =====================================================================
# 1. SCRCPY RAW H.264 STREAM READER
# =====================================================================

class ScrcpyFrameBridge(QObject):
    frame_ready = Signal(np.ndarray)
    status_msg = Signal(str)


class ScrcpyStreamWorker:
    """
    Push scrcpy-server to the Android device and decode its H.264 stream.
    """

    def __init__(
        self,
        serial: str,
        local_port: int = 27183,
        local_server_path: str = (
            r"C:\Users\RDP6\AppData\Local\Microsoft\WinGet\Packages"
            r"\Genymobile.scrcpy_Microsoft.Winget.Source_8wekyb3d8bbwe"
            r"\scrcpy-win64-v4.1\scrcpy-server"
        ),
        server_jar_remote: str = "/data/local/tmp/scrcpy-server.jar",
        scrcpy_version: str = "4.1",
    ):
        self.serial = serial
        self.local_port = local_port
        self.local_server_path = local_server_path
        self.server_jar_remote = server_jar_remote
        self.scrcpy_version = scrcpy_version

        self.bridge = ScrcpyFrameBridge()
        self._stop_event = threading.Event()
        self._server_proc: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None

    def _ensure_server_on_device(self):
        src = Path(self.local_server_path)

        if not src.exists():
            scrcpy_exe = shutil.which("scrcpy")

            if scrcpy_exe:
                candidate = (
                    Path(scrcpy_exe).resolve().parent
                    / "scrcpy-server"
                )

                if candidate.exists():
                    src = candidate

        if not src.exists():
            raise FileNotFoundError(
                f"Could not find local scrcpy-server binary at: {src}"
            )

        subprocess.run(
            [
                "adb", "-s", self.serial, "shell",
                "pkill -f com.genymobile.scrcpy.Server",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        subprocess.run(
            [
                "adb", "-s", self.serial, "push",
                str(src), self.server_jar_remote,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        subprocess.run(
            [
                "adb", "-s", self.serial, "shell",
                f"chmod 777 {self.server_jar_remote}",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def start(self):
        self._ensure_server_on_device()

        subprocess.run(
            [
                "adb", "-s", self.serial, "forward",
                f"tcp:{self.local_port}",
                "localabstract:scrcpy",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        cmd = [
            "adb", "-s", self.serial, "shell",
            f"CLASSPATH={self.server_jar_remote}",
            "app_process", "/",
            "com.genymobile.scrcpy.Server",
            self.scrcpy_version,
            "tunnel_forward=true",
            "audio=false",
            "control=false",
            "cleanup=false",
            "max_size=1280",
            "max_fps=60",
            "video_bit_rate=8000000",
            "send_device_meta=false",
            "send_frame_meta=false",
            "send_dummy_byte=false",
        ]

        self._server_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        threading.Thread(
            target=self._decode_loop,
            daemon=True,
        ).start()

    def _decode_loop(self):
        codec = av.CodecContext.create("h264", "r")
        time.sleep(0.8)

        self._sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM,
        )
        self._sock.settimeout(5.0)

        try:
            self._sock.connect(
                ("127.0.0.1", self.local_port)
            )

            self.bridge.status_msg.emit(
                "Scrcpy 4.1 stream connected at 60 FPS."
            )

        except Exception as exc:
            self.bridge.status_msg.emit(
                f"Scrcpy connection failed: {exc}"
            )
            return

        while not self._stop_event.is_set():
            try:
                chunk = self._sock.recv(65536)

                if not chunk:
                    break

                for packet in codec.parse(chunk):
                    for frame in codec.decode(packet):
                        img_bgr = frame.to_ndarray(
                            format="bgr24"
                        )
                        self.bridge.frame_ready.emit(img_bgr)

            except socket.timeout:
                continue

            except Exception:
                if self._stop_event.is_set():
                    break

    def stop(self):
        self._stop_event.set()

        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

        if self._server_proc:
            try:
                self._server_proc.terminate()
            except Exception:
                pass


# =====================================================================
# 2. INTERACTIVE MIRRORED CANVAS WIDGET
# =====================================================================

class DeviceViewportLabel(QLabel):
    """Render the phone screen and translate mouse/keyboard input to device actions."""

    swipe_requested = Signal(str)

    def __init__(
        self,
        engine: AndroidRecordingEngine,
        canvas_w: int = 450,
        canvas_h: int = 1000,
    ):
        super().__init__()

        self.engine = engine
        self.canvas_w = canvas_w
        self.canvas_h = canvas_h

        self.setFixedSize(
            self.canvas_w,
            self.canvas_h,
        )
        self.setStyleSheet(
            "background-color: #111; border: 1px solid #333;"
        )
        self.setAlignment(Qt.AlignCenter)
        self.setFocusPolicy(Qt.StrongFocus)
        self._is_pressing = False

    def _to_device_coords(
        self,
        pos: QPoint,
    ) -> tuple[int, int]:
        x = max(
            0,
            min(pos.x(), self.canvas_w),
        )
        y = max(
            0,
            min(pos.y(), self.canvas_h),
        )

        dev_x = int(
            x * (
                self.engine.maatouch.max_x
                / self.canvas_w
            )
        )

        dev_y = int(
            y * (
                self.engine.maatouch.max_y
                / self.canvas_h
            )
        )

        return dev_x, dev_y

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.setFocus(Qt.MouseFocusReason)
            self._is_pressing = True

            dx, dy = self._to_device_coords(
                event.position().toPoint()
            )

            self.engine.on_viewport_pointer_down(
                dx,
                dy,
            )

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._is_pressing:
            dx, dy = self._to_device_coords(
                event.position().toPoint()
            )

            self.engine.on_viewport_pointer_move(
                dx,
                dy,
            )

    def mouseReleaseEvent(self, event: QMouseEvent):
        if (
            event.button() == Qt.LeftButton
            and self._is_pressing
        ):
            self._is_pressing = False

            dx, dy = self._to_device_coords(
                event.position().toPoint()
            )

            self.engine.on_viewport_pointer_up(
                dx,
                dy,
            )

    def keyPressEvent(self, event):
        """Arrow keys trigger a recorded swipe when the viewport has focus."""
        if event.isAutoRepeat():
            event.accept()
            return

        direction_by_key = {
            Qt.Key_Up: "up",
            Qt.Key_Down: "down",
            Qt.Key_Left: "left",
            Qt.Key_Right: "right",
        }
        direction = direction_by_key.get(event.key())
        if direction is not None:
            self.swipe_requested.emit(direction)
            event.accept()
            return

        super().keyPressEvent(event)


# =====================================================================
# 3. HOST RECORDER WINDOW
# =====================================================================

class RecorderMainWindow(QMainWindow):
    def __init__(
        self,
        serial: str,
        persona_name: str = "average",
        persona_seed: Optional[int] = None,
    ):
        super().__init__()

        self.serial = serial
        # Recreate the exact sampled persona selected in the dashboard. Passing
        # its seed makes Random and the named personas reproducible.
        self.persona = personas.generate_persona(
            seed=persona_seed,
            archetype=persona_name,
        )
        self.persona_name = self.persona.archetype
        self.persona_seed = self.persona.seed
        self._gesture_rng = random.Random(self.persona_seed ^ 0x5A17C3)
        self.setWindowTitle(
            f"Android Recorder Studio — [{serial}] — {self.persona.name}"
        )

        self.engine = AndroidRecordingEngine(
            serial=serial
        )
        self.engine.start_session()

        # Exact text successfully mirrored from the desktop bar.
        self._live_synced_text = ""

        avail_rect = (
            QApplication.primaryScreen().availableGeometry()
        )

        max_canvas_h = max(
            420,
            int(avail_rect.height() - 135),
        )

        aspect = (
            self.engine.maatouch.max_x
            / max(1, self.engine.maatouch.max_y)
        )

        canvas_h = max_canvas_h
        canvas_w = int(canvas_h * aspect)

        self.viewport = DeviceViewportLabel(
            self.engine,
            canvas_w,
            canvas_h,
        )
        self.viewport.swipe_requested.connect(
            self._start_directional_swipe
        )

        # A timer animates programmed swipes without sleeping/blocking the UI.
        self._directional_swipe_timer = QTimer(self)
        self._directional_swipe_timer.setInterval(16)  # approximately 60 updates/sec
        self._directional_swipe_timer.timeout.connect(
            self._advance_directional_swipe
        )
        self._directional_swipe_points: list[tuple[int, int]] = []
        self._directional_swipe_intervals: list[int] = []
        self._directional_swipe_index = 0
        self._directional_swipe_last = (0, 0)
        self._directional_swipe_direction: Optional[str] = None
        self._directional_swipe_shape: Optional[str] = None
        self._directional_swipe_duration_ms = 0

        # Signature cache avoids rebuilding the step list on every poll when
        # no new action has been committed.
        self._step_list_signature = None

        self._build_ui()

        # Pointer gestures, keyboard taps and text commits are recorded by the
        # engine. Polling on the Qt thread keeps the step panel responsive and
        # avoids adding cross-thread UI updates to the recording engine.
        self._steps_refresh_timer = QTimer(self)
        self._steps_refresh_timer.setInterval(200)
        self._steps_refresh_timer.timeout.connect(self._refresh_step_list)
        self._steps_refresh_timer.start()
        self._refresh_step_list(force=True)

        QTimer.singleShot(0, self.viewport.setFocus)

        self.adjustSize()
        win_geo = self.frameGeometry()
        win_geo.moveCenter(avail_rect.center())

        self.move(
            win_geo.topLeft().x(),
            max(
                avail_rect.top() + 10,
                win_geo.topLeft().y(),
            ),
        )

        self.stream = ScrcpyStreamWorker(
            serial=serial
        )
        self.stream.bridge.frame_ready.connect(
            self._on_new_frame
        )
        self.stream.bridge.status_msg.connect(
            self.statusBar().showMessage
        )
        self.stream.start()

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)

        main_layout = QHBoxLayout(root)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(10)

        left_col = QVBoxLayout()
        left_col.setSpacing(6)
        left_col.addWidget(
            self.viewport,
            alignment=Qt.AlignCenter,
        )

        text_bar = QHBoxLayout()

        self.text_input = QLineEdit()
        self.text_input.setFixedHeight(30)
        self.text_input.setPlaceholderText(
            "Type text & press Enter..."
        )

        # Send every user edit immediately to the phone.
        self.text_input.textEdited.connect(
            self._sync_text_live
        )

        # Enter finishes the action; it must not send the text again.
        self.text_input.returnPressed.connect(
            self._send_semantic_text
        )

        send_btn = QPushButton("Inject")
        send_btn.setFixedHeight(30)
        send_btn.setFocusPolicy(Qt.NoFocus)
        send_btn.clicked.connect(
            self._send_semantic_text
        )

        text_bar.addWidget(self.text_input)
        text_bar.addWidget(send_btn)
        left_col.addLayout(text_bar)

        right_col = QVBoxLayout()
        right_col.setSpacing(6)

        hw_buttons = [
            ("Back (KEY_BACK)", 4),
            ("Home (KEY_HOME)", 3),
            ("Recents (APP_SWITCH)", 187),
            ("Volume Up", 24),
            ("Volume Down", 25),
            ("Enter / Search", 66),
            ("Power / Lock", 26),
        ]

        for label, keycode in hw_buttons:
            btn = QPushButton(label)
            btn.setFixedHeight(32)
            btn.setMinimumWidth(145)
            # Keep keyboard focus on the viewport, unless the text bar is active.
            btn.setFocusPolicy(Qt.NoFocus)

            btn.clicked.connect(
                lambda checked=False, kc=keycode:
                    self._trigger_key(kc)
            )

            right_col.addWidget(btn)

        # Compact directional pad for common scroll/navigation gestures.
        swipe_title = QLabel("Swipe / Scroll")
        swipe_title.setStyleSheet("font-weight: bold; padding-top: 5px;")
        right_col.addWidget(swipe_title)

        self.gesture_profile_label = QLabel(
            f"Gesture profile: {self.persona_name.title()}  ·  seed {self.persona_seed}"
        )
        self.gesture_profile_label.setWordWrap(True)
        self.gesture_profile_label.setToolTip(
            "Generated directional swipes use this persona's motion profile, "
            "flick vigor, tremor, curve bias and fling bias."
        )
        self.gesture_profile_label.setStyleSheet(
            "color: #a5b4fc; font-size: 10px; padding-bottom: 3px;"
        )
        right_col.addWidget(self.gesture_profile_label)

        swipe_pad = QGridLayout()
        swipe_pad.setContentsMargins(0, 0, 0, 0)
        swipe_pad.setHorizontalSpacing(4)
        swipe_pad.setVerticalSpacing(4)

        swipe_buttons = [
            ("↑", "up", 0, 1, "Swipe up / scroll forward"),
            ("←", "left", 1, 0, "Swipe left"),
            ("↓", "down", 1, 1, "Swipe down / scroll backward"),
            ("→", "right", 1, 2, "Swipe right"),
        ]
        for caption, direction, row, column, tooltip in swipe_buttons:
            btn = QPushButton(caption)
            btn.setFixedSize(44, 34)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setToolTip(tooltip + f" (arrow key: {direction})")
            btn.clicked.connect(
                lambda checked=False, d=direction:
                    self._start_directional_swipe(d)
            )
            swipe_pad.addWidget(btn, row, column)

        right_col.addLayout(swipe_pad)
        swipe_hint = QLabel("Focus the phone preview, then use ↑ ↓ ← →")
        swipe_hint.setWordWrap(True)
        swipe_hint.setStyleSheet("color: #888; font-size: 10px;")
        right_col.addWidget(swipe_hint)

        # -------------------------------------------------------------
        # LIVE RECORDED-STEPS PANEL
        # -------------------------------------------------------------
        steps_header = QHBoxLayout()
        self.steps_count_label = QLabel("Recorded Steps (0)")
        self.steps_count_label.setStyleSheet(
            "font-weight: bold; color: #e4e4e7; padding-top: 5px;"
        )
        steps_header.addWidget(self.steps_count_label)
        steps_header.addStretch(1)
        right_col.addLayout(steps_header)

        self.steps_scroll = QScrollArea()
        self.steps_scroll.setWidgetResizable(True)
        self.steps_scroll.setMinimumWidth(270)
        self.steps_scroll.setMinimumHeight(170)
        self.steps_scroll.setMaximumHeight(340)
        self.steps_scroll.setFrameShape(QFrame.NoFrame)
        self.steps_scroll.setStyleSheet(
            "QScrollArea { background: #101014; border: 1px solid #303039; "
            "border-radius: 6px; }"
        )

        self.steps_host = QWidget()
        self.steps_host.setStyleSheet("background: transparent;")
        self.steps_rows_layout = QVBoxLayout(self.steps_host)
        self.steps_rows_layout.setContentsMargins(5, 5, 5, 5)
        self.steps_rows_layout.setSpacing(5)
        self.steps_scroll.setWidget(self.steps_host)
        right_col.addWidget(self.steps_scroll, 1)

        steps_note = QLabel(
            "Remove deletes an action from the saved recording only; "
            "it does not undo the action already performed on the phone."
        )
        steps_note.setWordWrap(True)
        steps_note.setStyleSheet("color: #888; font-size: 9px;")
        right_col.addWidget(steps_note)

        right_col.addStretch(1)

        save_btn = QPushButton("Stop & Export JSON")
        save_btn.setFocusPolicy(Qt.NoFocus)
        save_btn.setFixedHeight(38)
        save_btn.setStyleSheet(
            "background-color: #d9534f; color: white; "
            "font-weight: bold; border-radius: 4px;"
        )
        save_btn.clicked.connect(
            self._finish_session
        )
        right_col.addWidget(save_btn)

        main_layout.addLayout(left_col)
        main_layout.addLayout(right_col)

        self.statusBar().showMessage(
            "Recording active — drag on the preview or use arrow keys / swipe buttons."
        )

    def _step_description(self, step) -> str:
        """Return a compact, readable description for a recorded action."""
        event_type = str(getattr(step, "event_type", "ACTION") or "ACTION").upper()
        trajectory = getattr(step, "trajectory", None) or []

        if event_type == "TEXT_INPUT":
            text = str(getattr(step, "text_payload", "") or "")
            text = text.replace("\n", "\\n").replace("\r", "\\r")
            if len(text) > 34:
                text = text[:31] + "..."
            return f'Text: "{text}"'

        if event_type == "KEY_EVENT":
            keycode = getattr(step, "keycode", None)
            key_names = {
                3: "Home", 4: "Back", 24: "Volume Up",
                25: "Volume Down", 26: "Power / Lock",
                66: "Enter / Search", 187: "Recents",
            }
            return f"Key: {key_names.get(keycode, f'keycode {keycode}')}"

        if event_type == "SWIPE" and len(trajectory) >= 2:
            first, last = trajectory[0], trajectory[-1]
            dx = int(getattr(last, "x", 0)) - int(getattr(first, "x", 0))
            dy = int(getattr(last, "y", 0)) - int(getattr(first, "y", 0))
            if abs(dx) >= abs(dy):
                direction = "right →" if dx >= 0 else "left ←"
            else:
                direction = "down ↓" if dy >= 0 else "up ↑"
            return f"Swipe {direction}  ({abs(dx)} × {abs(dy)} px)"

        if event_type in {"TAP", "LONG_PRESS"} and trajectory:
            point = trajectory[0]
            anchor = getattr(step, "anchor", None)
            # Android keyboards often expose key labels as content-desc rather
            # than text. Prefer those before falling back to raw coordinates.
            anchor_text = str(
                getattr(anchor, "text", "")
                or getattr(anchor, "content_desc", "")
                or ""
            ).strip()
            if anchor_text:
                if len(anchor_text) > 24:
                    anchor_text = anchor_text[:21] + "..."
                detail = f' · "{anchor_text}"'
            else:
                detail = f' · ({getattr(point, "x", 0)}, {getattr(point, "y", 0)})'
            label = "Long press" if event_type == "LONG_PRESS" else "Tap"
            return label + detail

        return event_type.replace("_", " ").title()

    def _refresh_step_list(self, force: bool = False):
        """Refresh the live action list only when the engine's steps change."""
        steps = list(getattr(self.engine, "steps", []))
        signature = tuple(
            (
                getattr(step, "step_id", index),
                getattr(step, "timestamp", None),
                getattr(step, "event_type", ""),
                getattr(step, "text_payload", None),
                getattr(step, "keycode", None),
            )
            for index, step in enumerate(steps, start=1)
        )
        if not force and signature == self._step_list_signature:
            return

        self._step_list_signature = signature
        previous_scroll_value = self.steps_scroll.verticalScrollBar().value()
        self.steps_count_label.setText(f"Recorded Steps ({len(steps)})")

        while self.steps_rows_layout.count():
            item = self.steps_rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        if not steps:
            empty = QLabel("No steps recorded yet.\nInteract with the phone to add steps.")
            empty.setWordWrap(True)
            empty.setAlignment(Qt.AlignCenter)
            empty.setMinimumHeight(90)
            empty.setStyleSheet("color: #888; padding: 12px;")
            self.steps_rows_layout.addWidget(empty)
        else:
            for position, step in enumerate(steps, start=1):
                row = QFrame()
                row.setStyleSheet(
                    "QFrame { background: #19191f; border: 1px solid #303039; "
                    "border-radius: 5px; }"
                )
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(7, 5, 5, 5)
                row_layout.setSpacing(6)

                description = self._step_description(step)
                detail = QLabel(f"#{position}  {description}")
                detail.setWordWrap(True)
                detail.setToolTip(
                    f"Recorded action #{position} (internal id {getattr(step, 'step_id', position)}): "
                    f"{description}\nType: {getattr(step, 'event_type', 'ACTION')}"
                )
                detail.setStyleSheet(
                    "color: #e4e4e7; font-size: 10px; border: none;"
                )
                row_layout.addWidget(detail, 1)

                remove_btn = QPushButton("×")
                remove_btn.setFixedSize(27, 27)
                remove_btn.setFocusPolicy(Qt.NoFocus)
                remove_btn.setToolTip(
                    "Remove this action from the saved recording. "
                    "This does not undo it on the phone."
                )
                remove_btn.setStyleSheet(
                    "QPushButton { background: #3b171b; color: #fda4af; "
                    "border: 1px solid #7f1d1d; border-radius: 4px; "
                    "font-weight: bold; font-size: 16px; }"
                    "QPushButton:hover { background: #7f1d1d; color: white; }"
                )
                step_id = getattr(step, "step_id", position)
                remove_btn.clicked.connect(
                    lambda checked=False, sid=step_id: self._remove_recorded_step(sid)
                )
                row_layout.addWidget(remove_btn, 0, Qt.AlignTop)
                self.steps_rows_layout.addWidget(row)

        self.steps_rows_layout.addStretch(1)
        # Keep newest actions visible when recording continues; for a deletion,
        # preserve a valid nearby scroll position.
        if len(steps) > 0:
            self.steps_scroll.verticalScrollBar().setValue(
                self.steps_scroll.verticalScrollBar().maximum()
            )
        else:
            self.steps_scroll.verticalScrollBar().setValue(previous_scroll_value)

    def _remove_recorded_step(self, step_id: int):
        """Remove one action from the export list and preserve surrounding delay."""
        steps = self.engine.steps
        index = next(
            (i for i, step in enumerate(steps)
             if getattr(step, "step_id", None) == step_id),
            None,
        )
        if index is None:
            self.statusBar().showMessage("That step is no longer in the recording.")
            self._refresh_step_list(force=True)
            return

        removed = steps.pop(index)
        carried_delay = max(
            0,
            int(getattr(removed, "think_time_ms", 0) or 0)
            + int(getattr(removed, "duration_ms", 0) or 0),
        )
        if index < len(steps):
            # Fold the removed action's preceding wait and duration into the next
            # retained step, preserving the approximate time gap in replay.
            next_step = steps[index]
            next_step.think_time_ms = max(
                0,
                int(getattr(next_step, "think_time_ms", 0) or 0) + carried_delay,
            )
        else:
            # If the last step is removed during recording, roll the engine clock
            # back to the end of the previous retained step so the next action's
            # think-time is measured from the correct point.
            last_end = getattr(self.engine, "_last_step_end_ts", None)
            if last_end is not None and carried_delay:
                self.engine._last_step_end_ts = max(
                    0.0,
                    float(last_end) - carried_delay / 1000.0,
                )

        # Keep the engine's monotonic internal counter unchanged. IDs are stable
        # for delete callbacks and crop paths; list positions are the UI numbering.
        self._refresh_step_list(force=True)
        self.statusBar().showMessage(
            f"Removed recorded action #{index + 1}. "
            "It will not be included in the exported JSON; the phone action was not undone."
        )

    def _on_new_frame(self, frame_bgr: np.ndarray):
        self.engine.update_mirror_frame(frame_bgr)

        rgb = cv2.cvtColor(
            frame_bgr,
            cv2.COLOR_BGR2RGB,
        )

        height, width, channels = rgb.shape

        qimg = QImage(
            rgb.data,
            width,
            height,
            channels * width,
            QImage.Format_RGB888,
        ).copy()

        pix = QPixmap.fromImage(qimg).scaled(
            self.viewport.canvas_w,
            self.viewport.canvas_h,
            Qt.IgnoreAspectRatio,
            Qt.SmoothTransformation,
        )

        self.viewport.setPixmap(pix)

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    def _make_persona_swipe_path(
        self,
        direction: str,
        width: int,
        height: int,
    ) -> tuple[tuple[int, int], list[tuple[int, int]], list[int], int, str]:
        """Build a bounded directional QA gesture using the selected persona.

        The requested direction and endpoints are preserved. The selected profile
        only varies path shape, cross-axis drift, speed and timing within safe
        screen bounds; it does not change what direction the user requested.
        """
        rng = self._gesture_rng
        profile = self.persona.swipe_profile

        fling = self._clamp(float(self.persona.fling_bias), 0.0, 1.0)
        travel_fraction = 0.44 + 0.22 * fling
        center_x, center_y = width / 2.0, height / 2.0
        if direction == "up":
            start = (round(center_x), round(height * (0.5 + travel_fraction / 2)))
            end = (round(center_x), round(height * (0.5 - travel_fraction / 2)))
        elif direction == "down":
            start = (round(center_x), round(height * (0.5 - travel_fraction / 2)))
            end = (round(center_x), round(height * (0.5 + travel_fraction / 2)))
        elif direction == "left":
            start = (round(width * (0.5 + travel_fraction / 2)), round(center_y))
            end = (round(width * (0.5 - travel_fraction / 2)), round(center_y))
        else:  # right
            start = (round(width * (0.5 - travel_fraction / 2)), round(center_y))
            end = (round(width * (0.5 + travel_fraction / 2)), round(center_y))

        dx, dy = end[0] - start[0], end[1] - start[1]
        distance = max(1.0, math.hypot(dx, dy))
        perp_x, perp_y = -dy / distance, dx / distance

        lateral_values = profile.get("lateral_ratio", (0.008, 0.04, 0.09))
        if not isinstance(lateral_values, (tuple, list)) or len(lateral_values) != 3:
            lateral_values = (0.008, 0.04, 0.09)
        lateral_low, lateral_typical, lateral_high = map(float, lateral_values)
        lateral_low = max(0.0, lateral_low)
        lateral_high = max(lateral_low, lateral_high)
        lateral_typical = self._clamp(lateral_typical, lateral_low, lateral_high)
        lateral_ratio = rng.triangular(lateral_low, lateral_high, lateral_typical)
        lateral_amplitude = min(distance * lateral_ratio, min(width, height) * 0.095)

        weights = profile.get("shape_weights", {})
        shape_names = [name for name, weight in weights.items() if float(weight) > 0]
        shape_weights = [float(weights[name]) for name in shape_names]
        shape = (
            rng.choices(shape_names, weights=shape_weights, k=1)[0]
            if shape_names and sum(shape_weights) > 0
            else "single_bend"
        )

        tremor = self._clamp(float(self.persona.tremor_scale), 0.3, 2.0)
        roughness = self._clamp(float(profile.get("roughness", 0.3)) * tremor, 0.04, 1.0)
        curve_sign = 1.0 if float(self.persona.curve_bias) >= 0 else -1.0
        bend_sign = curve_sign * rng.choice((-1.0, 1.0))
        phase_a = rng.uniform(-math.pi, math.pi)
        phase_b = rng.uniform(-math.pi, math.pi)
        phase_c = rng.uniform(-math.pi, math.pi)
        wander_nodes = [rng.uniform(-1.0, 1.0) for _ in range(6)]

        vigor = self._clamp(float(self.persona.flick_vigor), 0.5, 1.8)
        dwell = self._clamp(float(self.persona.dwell_scale), 0.5, 1.8)
        duration_ms = round(350.0 / vigor * (0.94 + 0.12 * dwell))
        duration_ms = int(self._clamp(duration_ms, 210, 540))
        step_count = max(13, min(36, round(duration_ms / 16)))

        # Higher vigor produces a brisker ease-out; the end point remains exact.
        ease_power = self._clamp(1.05 + (vigor - 0.8) * 0.55, 0.9, 1.45)
        noise_amplitude = min(
            min(width, height) * 0.026 * roughness,
            min(width, height) * 0.035,
        )

        points: list[tuple[int, int]] = []
        for index in range(1, step_count + 1):
            t = index / step_count
            progress = 1.0 - (1.0 - t) ** ease_power
            base_x = start[0] + dx * progress
            base_y = start[1] + dy * progress

            # Select a motion shape from this archetype's profile. The final
            # point is forced onto the exact requested endpoint below.
            if shape == "near_line":
                shape_curve = 0.16 * math.sin(math.pi * t)
            elif shape == "shallow_s":
                shape_curve = 0.55 * math.sin(2.0 * math.pi * t)
            elif shape == "wander":
                scaled = t * (len(wander_nodes) - 1)
                node_index = min(len(wander_nodes) - 2, int(scaled))
                local_t = scaled - node_index
                # Smoothly blend adjacent knots to avoid abrupt corners.
                local_t = local_t * local_t * (3.0 - 2.0 * local_t)
                wander = (
                    wander_nodes[node_index] * (1.0 - local_t)
                    + wander_nodes[node_index + 1] * local_t
                )
                shape_curve = wander * math.sin(math.pi * t)
            else:  # single_bend
                shape_curve = bend_sign * math.sin(math.pi * t)

            micro_wiggle = (
                0.48 * math.sin(2.0 * math.pi * t + phase_a)
                + 0.32 * math.sin(4.0 * math.pi * t + phase_b)
                + 0.20 * math.sin(7.0 * math.pi * t + phase_c)
            ) * math.sin(math.pi * t)

            offset = (lateral_amplitude * shape_curve) + (noise_amplitude * micro_wiggle)
            x = round(base_x + perp_x * offset)
            y = round(base_y + perp_y * offset)

            # Keep all points inside the device canvas and keep the swipe's
            # direction by retaining monotonic progress along its main axis.
            x = int(self._clamp(x, 1, width - 2))
            y = int(self._clamp(y, 1, height - 2))
            if index == step_count:
                x, y = end
            points.append((x, y))

        # Timing varies within a small profile-based envelope, keeping the Qt UI
        # responsive while allowing shakier profiles to have less uniform motion.
        burstiness = self._clamp(float(self.persona.burstiness), 0.7, 2.8)
        timing_variation = min(0.24, 0.035 + 0.14 * roughness + 0.035 * abs(burstiness - 1.4))
        nominal_interval = duration_ms / step_count
        intervals = [
            int(self._clamp(
                round(nominal_interval * rng.uniform(1.0 - timing_variation, 1.0 + timing_variation)),
                8, 32,
            ))
            for _ in points
        ]
        return start, points, intervals, duration_ms, shape

    def _start_directional_swipe(self, direction: str):
        """Start a persona-shaped, animated and recorded directional swipe."""
        direction = str(direction).strip().lower()
        if direction not in {"up", "down", "left", "right"}:
            self.statusBar().showMessage(f"Unknown swipe direction: {direction}")
            return

        if self._directional_swipe_timer.isActive():
            self.statusBar().showMessage("A directional swipe is already in progress.")
            return

        if getattr(self.engine, "_active_samples", None):
            self.statusBar().showMessage("Finish the current touch gesture first.")
            return

        width = int(self.engine.maatouch.max_x)
        height = int(self.engine.maatouch.max_y)
        if width <= 0 or height <= 0:
            self.statusBar().showMessage("Could not determine Android screen dimensions.")
            return

        try:
            start, points, intervals, duration_ms, shape = self._make_persona_swipe_path(
                direction, width, height
            )
            self._directional_swipe_points = points
            self._directional_swipe_intervals = intervals
            self._directional_swipe_index = 0
            self._directional_swipe_last = start
            self._directional_swipe_direction = direction
            self._directional_swipe_shape = shape
            self._directional_swipe_duration_ms = duration_ms
            self.engine.on_viewport_pointer_down(*start)
        except Exception as exc:
            self._clear_directional_swipe_state()
            self.statusBar().showMessage(f"Could not start swipe: {exc}")
            return

        self.viewport.setFocus(Qt.OtherFocusReason)
        self._directional_swipe_timer.setInterval(self._directional_swipe_intervals[0])
        self.statusBar().showMessage(
            f"{self.persona_name.title()} profile: {direction} swipe "
            f"({duration_ms} ms, {shape} path)"
        )
        self._directional_swipe_timer.start()

    def _advance_directional_swipe(self):
        """Advance one animated touch sample; never sleeps in the UI thread."""
        if self._directional_swipe_index >= len(self._directional_swipe_points):
            self._complete_directional_swipe()
            return

        point = self._directional_swipe_points[self._directional_swipe_index]
        try:
            self.engine.on_viewport_pointer_move(*point)
            self._directional_swipe_last = point
            self._directional_swipe_index += 1
            if self._directional_swipe_index >= len(self._directional_swipe_points):
                self._complete_directional_swipe()
            else:
                self._directional_swipe_timer.setInterval(
                    self._directional_swipe_intervals[self._directional_swipe_index]
                )
        except Exception as exc:
            self._directional_swipe_timer.stop()
            self.statusBar().showMessage(f"Swipe interrupted: {exc}")
            # Release the device contact if move processing failed mid-gesture.
            try:
                self.engine.on_viewport_pointer_up(*self._directional_swipe_last)
            except Exception:
                pass
            self._clear_directional_swipe_state()

    def _complete_directional_swipe(self):
        self._directional_swipe_timer.stop()
        direction = self._directional_swipe_direction or "directional"
        point = self._directional_swipe_last
        try:
            self.engine.on_viewport_pointer_up(*point)
            self.statusBar().showMessage(
                f"Recorded {self.persona_name.title()} {direction} swipe "
                f"as a SWIPE action (Step #{len(self.engine.steps)})."
            )
        except Exception as exc:
            self.statusBar().showMessage(f"Could not finish swipe: {exc}")
        finally:
            self._clear_directional_swipe_state()

    def _clear_directional_swipe_state(self):
        self._directional_swipe_points = []
        self._directional_swipe_intervals = []
        self._directional_swipe_index = 0
        self._directional_swipe_direction = None
        self._directional_swipe_shape = None
        self._directional_swipe_duration_ms = 0

    def _cancel_directional_swipe(self):
        """Release an in-progress generated swipe before export or window close."""
        if not self._directional_swipe_timer.isActive():
            return
        self._directional_swipe_timer.stop()
        point = self._directional_swipe_last
        try:
            self.engine.on_viewport_pointer_up(*point)
        except Exception:
            pass
        self._clear_directional_swipe_state()

    def _sync_text_live(self, current_text: str):
        """
        Mirror changes from the desktop text bar to the phone field.
        """
        previous_text = self._live_synced_text

        if previous_text == current_text:
            return

        success = self.engine.on_viewport_text_edit(
            previous_text,
            current_text,
        )

        if success:
            self._live_synced_text = current_text

            if current_text:
                self.statusBar().showMessage(
                    "Live text mirrored to phone: "
                    f"{len(current_text)} characters"
                )
            else:
                self.statusBar().showMessage(
                    "Live text cleared from the focused phone field."
                )

        else:
            self.statusBar().showMessage(
                "Live text sync failed. Check the phone connection and "
                "focused text field. Your desktop text has been retained."
            )

    def _send_semantic_text(self):
        """
        Finalize one TEXT_INPUT action without injecting it again.
        """
        text = self.text_input.text()

        if not text.strip():
            return

        # Retry the final change in case an earlier live edit failed.
        if text != self._live_synced_text:
            success = self.engine.on_viewport_text_edit(
                self._live_synced_text,
                text,
            )

            if not success:
                self.statusBar().showMessage(
                    "Could not finish text synchronization. "
                    "Your desktop text has been retained."
                )
                return

            self._live_synced_text = text

        success = self.engine.on_viewport_text_input(
            text,
            already_injected=True,
        )

        if not success:
            self.statusBar().showMessage(
                "Could not record the text action. "
                "Your desktop text has been retained."
            )
            return

        character_count = len(text)

        # Programmatic clear() does not emit textEdited.
        self._live_synced_text = ""
        self.text_input.clear()

        self.statusBar().showMessage(
            "Text injected live and recorded as one action "
            f"({character_count} characters, "
            f"Step #{len(self.engine.steps)})"
        )

    def _trigger_key(self, keycode: int):
        self.engine.on_viewport_key_event(keycode)

        self.statusBar().showMessage(
            f"Recorded KEY_EVENT: {keycode} "
            f"(Step #{len(self.engine.steps)})"
        )

    def _finish_session(self):
        self._cancel_directional_swipe()
        if hasattr(self, "_steps_refresh_timer"):
            self._steps_refresh_timer.stop()
        self.stream.stop()
        out_path = self.engine.stop_and_export()

        print(f"\n[+] Session exported to: {out_path}")
        self.close()

    def closeEvent(self, event):
        self._cancel_directional_swipe()
        if hasattr(self, "_steps_refresh_timer"):
            self._steps_refresh_timer.stop()
        self.stream.stop()
        super().closeEvent(event)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="MightyGrid Android viewport recorder"
    )
    parser.add_argument("serial", help="ADB serial of the Android device")
    parser.add_argument(
        "--persona",
        default="average",
        choices=[*personas.ARCHETYPE_NAMES, "random"],
        help="Persona used to shape generated directional swipes",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Persona seed from the dashboard; preserves the sampled profile",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])

    win = RecorderMainWindow(
        serial=args.serial,
        persona_name=args.persona,
        persona_seed=args.seed,
    )
    win.show()

    sys.exit(app.exec())
