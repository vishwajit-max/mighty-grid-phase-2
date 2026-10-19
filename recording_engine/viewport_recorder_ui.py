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

from PySide6.QtCore import Qt, Signal, QObject, QPoint
from PySide6.QtGui import QImage, QPixmap, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QLineEdit,
)

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
    """Render the phone screen and translate mouse input to device pixels."""

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


# =====================================================================
# 3. HOST RECORDER WINDOW
# =====================================================================

class RecorderMainWindow(QMainWindow):
    def __init__(self, serial: str):
        super().__init__()

        self.serial = serial
        self.setWindowTitle(
            f"Android Recorder Studio — [{serial}]"
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

        self._build_ui()

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

            btn.clicked.connect(
                lambda checked=False, kc=keycode:
                    self._trigger_key(kc)
            )

            right_col.addWidget(btn)

        right_col.addStretch()

        save_btn = QPushButton("Stop & Export JSON")
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
            "Recording active — interact with viewport or toolbar."
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
        self.stream.stop()
        out_path = self.engine.stop_and_export()

        print(f"\n[+] Session exported to: {out_path}")
        self.close()

    def closeEvent(self, event):
        self.stream.stop()
        super().closeEvent(event)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: python viewport_recorder_ui.py <DEVICE_SERIAL>"
        )
        sys.exit(1)

    app = QApplication(sys.argv)

    win = RecorderMainWindow(
        serial=sys.argv[1]
    )
    win.show()

    sys.exit(app.exec())