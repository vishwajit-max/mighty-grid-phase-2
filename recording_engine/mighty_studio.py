import sys
import os
import subprocess
import html
from pathlib import Path
from PySide6.QtCore import Qt, Signal, QObject, QThread
from PySide6.QtGui import QFont, QTextCursor, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QFileDialog, QTextEdit,
    QMessageBox, QFrame, QScrollArea, QProgressBar, QGraphicsDropShadowEffect
)

# Import existing engines
from viewport_recorder_ui import RecorderMainWindow
from replay_engine import run_fleet_replay

# Dynamically link personas for the live Biometric DNA preview card
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
telemetry_dir = os.path.join(project_root, "android_telemetry", "telemetry")
if telemetry_dir not in sys.path:
    sys.path.append(telemetry_dir)

try:
    import personas
except ImportError:
    personas = None


# =====================================================================
# 1. MODERN DESIGN SYSTEM (ZINC & INDIGO PALETTE)
# =====================================================================
STYLESHEET = """
QMainWindow {
    background-color: #09090B;
}
QWidget {
    font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif;
    color: #F4F4F5;
}

/* --- TOP HEADER BAR --- */
QFrame#HeaderBar {
    background-color: #121215;
    border-bottom: 1px solid #27272A;
}
QLabel#AppTitle {
    font-size: 16px;
    font-weight: 800;
    color: #FAFAFA;
    letter-spacing: 0.8px;
}
QLabel#AppSubtitle {
    font-size: 12px;
    color: #71717A;
    font-weight: 500;
}

/* --- ELEVATED CARDS --- */
QFrame#Card {
    background-color: #121215;
    border: 1px solid #27272A;
    border-radius: 12px;
}
QLabel#CardTitle {
    font-size: 11px;
    font-weight: 700;
    color: #A1A1AA;
    text-transform: uppercase;
    letter-spacing: 1px;
}

/* --- INTERACTIVE DEVICE CARDS --- */
QFrame#DeviceCard {
    background-color: #18181B;
    border: 1px solid #27272A;
    border-radius: 8px;
}
QFrame#DeviceCard:hover {
    border: 1px solid #3F3F46;
    background-color: #1F1F23;
}
QFrame#DeviceCard[selected="true"] {
    background-color: #1E1B4B;
    border: 1px solid #6366F1;
}

/* --- COMBOBOX --- */
QComboBox {
    background-color: #18181B;
    border: 1px solid #27272A;
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 13px;
    font-weight: 600;
    color: #FAFAFA;
}
QComboBox:hover {
    border: 1px solid #3F3F46;
}
QComboBox::drop-down {
    border: none;
    width: 30px;
}
QComboBox QAbstractItemView {
    background-color: #18181B;
    border: 1px solid #3F3F46;
    border-radius: 8px;
    color: #FAFAFA;
    selection-background-color: #4F46E5;
    padding: 4px;
    outline: none;
}

/* --- ACTION BUTTONS --- */
QPushButton#GhostButton {
    background-color: #18181B;
    border: 1px solid #27272A;
    border-radius: 6px;
    color: #D4D4D8;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 600;
}
QPushButton#GhostButton:hover {
    background-color: #27272A;
    color: #FFFFFF;
    border: 1px solid #3F3F46;
}

QPushButton#RecordButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #E11D48, stop:1 #BE123C);
    border: 1px solid #F43F5E;
    border-radius: 10px;
    color: #FFFFFF;
    padding: 14px;
    font-size: 13px;
    font-weight: 700;
}
QPushButton#RecordButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #F43F5E, stop:1 #E11D48);
}
QPushButton#RecordButton:disabled {
    background: #27272A;
    border: 1px solid #3F3F46;
    color: #52525B;
}

QPushButton#ReplayButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4F46E5, stop:1 #7C3AED);
    border: 1px solid #818CF8;
    border-radius: 10px;
    color: #FFFFFF;
    padding: 14px;
    font-size: 13px;
    font-weight: 700;
}
QPushButton#ReplayButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #6366F1, stop:1 #8B5CF6);
}
QPushButton#ReplayButton:disabled {
    background: #27272A;
    border: 1px solid #3F3F46;
    color: #52525B;
}

/* --- TERMINAL --- */
QTextEdit#Terminal {
    background-color: #050507;
    border: none;
    border-bottom-left-radius: 11px;
    border-bottom-right-radius: 11px;
    padding: 16px;
    font-family: 'Cascadia Code', 'JetBrains Mono', 'Consolas', monospace;
    font-size: 12px;
    color: #D4D4D8;
}

/* --- BIOMETRIC BARS --- */
QProgressBar {
    background-color: #27272A;
    border-radius: 3px;
    border: none;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #6366F1, stop:1 #A855F7);
    border-radius: 3px;
}

/* --- SCROLLBARS --- */
QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 0px;
}
QScrollBar::handle:vertical {
    background: #27272A;
    border-radius: 4px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover {
    background: #3F3F46;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
"""


# =====================================================================
# 2. CUSTOM INTERACTIVE DEVICE CARD WIDGET
# =====================================================================
class DeviceCard(QFrame):
    toggled = Signal()

    def __init__(self, serial: str, model: str = "Android Device", android_ver: str = "14"):
        super().__init__()
        self.serial = serial
        self.model = model
        self.android_ver = android_ver
        self.is_selected = True

        self.setObjectName("DeviceCard")
        self.setProperty("selected", "true")
        self.setCursor(Qt.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)

        # Custom Checkbox Pill
        self.check_indicator = QLabel("✓")
        self.check_indicator.setAlignment(Qt.AlignCenter)
        self.check_indicator.setFixedSize(22, 22)
        self._update_indicator_style()
        layout.addWidget(self.check_indicator)

        # Device Details (Model + Serial)
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)
        self.model_lbl = QLabel(self.model)
        self.model_lbl.setStyleSheet("font-weight: 700; font-size: 13px; color: #FAFAFA; background: transparent;")
        self.serial_lbl = QLabel(f"{self.serial}  •  Android {self.android_ver}")
        self.serial_lbl.setStyleSheet("font-size: 11px; color: #A1A1AA; font-family: 'Consolas', monospace; background: transparent;")
        info_layout.addWidget(self.model_lbl)
        info_layout.addWidget(self.serial_lbl)
        layout.addLayout(info_layout, stretch=1)

        # Online Badge
        badge = QLabel("ONLINE")
        badge.setStyleSheet(
            "background-color: rgba(16, 185, 129, 0.15); color: #34D399; "
            "border: 1px solid rgba(16, 185, 129, 0.3); font-size: 10px; "
            "font-weight: 700; padding: 3px 6px; border-radius: 4px;"
        )
        layout.addWidget(badge)

    def _update_indicator_style(self):
        if self.is_selected:
            self.check_indicator.setText("✓")
            self.check_indicator.setStyleSheet(
                "background-color: #6366F1; color: #FFFFFF; font-weight: 900; "
                "border-radius: 6px; font-size: 12px;"
            )
        else:
            self.check_indicator.setText("")
            self.check_indicator.setStyleSheet(
                "background-color: #27272A; border: 1px solid #3F3F46; border-radius: 6px;"
            )

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.is_selected = not self.is_selected
            self.setProperty("selected", "true" if self.is_selected else "false")
            self._update_indicator_style()
            self.style().unpolish(self)
            self.style().polish(self)
            self.toggled.emit()
        super().mousePressEvent(event)


# =====================================================================
# 3. STDOUT STREAM & BACKGROUND REPLAY WORKER
# =====================================================================
class StreamToLogger(QObject):
    new_text = Signal(str)

    def write(self, text):
        self.new_text.emit(str(text))

    def flush(self):
        pass


class ReplayThread(QThread):
    finished_signal = Signal()
    error_signal = Signal(str)

    def __init__(self, recording_path: str, serials: list, persona: str, lockstep: bool):
        super().__init__()
        self.recording_path = recording_path
        self.serials = serials
        self.persona = persona
        self.lockstep = lockstep

    def run(self):
        try:
            run_fleet_replay(
                recording_json_path=self.recording_path,
                target_serials=self.serials,
                persona_name=self.persona,
                lockstep=self.lockstep
            )
        except Exception as e:
            self.error_signal.emit(str(e))
        finally:
            self.finished_signal.emit()


# =====================================================================
# 4. MAIN STUDIO WINDOW
# =====================================================================
class MightyGridStudio(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MightyGrid Studio — Android Fleet Automation")
        self.resize(1180, 760)
        self.setStyleSheet(STYLESHEET)
        self._apply_windows_dark_titlebar()

        self.recorder_window = None
        self.replay_thread = None
        self.device_cards = []

        self._build_ui()
        self._setup_logging()
        self.refresh_devices()
        self._update_persona_Visualizer(self.persona_combo.currentText())

    def _apply_windows_dark_titlebar(self):
        """Forces Windows 10/11 to render a pitch-black native title bar."""
        if sys.platform == "win32":
            try:
                import ctypes
                hwnd = int(self.winId())
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                    ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int)
                )
            except Exception:
                pass

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # -----------------------------------------------------------------
        # TOP BRANDING HEADER BAR
        # -----------------------------------------------------------------
        header = QFrame()
        header.setObjectName("HeaderBar")
        header.setFixedHeight(60)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(24, 0, 24, 0)

        brand_badge = QLabel("⚡")
        brand_badge.setAlignment(Qt.AlignCenter)
        brand_badge.setFixedSize(32, 32)
        brand_badge.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #4F46E5, stop:1 #EC4899); "
            "border-radius: 8px; font-size: 16px;"
        )
        header_layout.addWidget(brand_badge)
        header_layout.addSpacing(8)

        title = QLabel("MIGHTYGRID STUDIO")
        title.setObjectName("AppTitle")
        header_layout.addWidget(title)

        sep = QLabel("  /  ")
        sep.setStyleSheet("color: #3F3F46; font-size: 16px;")
        header_layout.addWidget(sep)

        subtitle = QLabel("Biometric Kinematics & Multi-Device Fleet Orchestrator")
        subtitle.setObjectName("AppSubtitle")
        header_layout.addWidget(subtitle)
        header_layout.addStretch()

        self.status_pill = QLabel("●  SYSTEM IDLE")
        self._set_status_pill("IDLE", "#10B981")
        header_layout.addWidget(self.status_pill)

        root_layout.addWidget(header)

        # -----------------------------------------------------------------
        # WORKSPACE BODY (Left Sidebar + Right Terminal Card)
        # -----------------------------------------------------------------
        workspace = QWidget()
        ws_layout = QHBoxLayout(workspace)
        ws_layout.setContentsMargins(20, 20, 20, 20)
        ws_layout.setSpacing(20)

        # === LEFT SIDEBAR ===
        left_col = QVBoxLayout()
        left_col.setSpacing(16)

        # 1. Fleet Card
        fleet_card = QFrame()
        fleet_card.setObjectName("Card")
        fleet_layout = QVBoxLayout(fleet_card)
        fleet_layout.setContentsMargins(16, 16, 16, 16)
        fleet_layout.setSpacing(12)

        fleet_hdr = QHBoxLayout()
        fleet_title = QLabel("CONNECTED ADB FLEET")
        fleet_title.setObjectName("CardTitle")
        fleet_hdr.addWidget(fleet_title)
        fleet_hdr.addStretch()

        self.fleet_count_lbl = QLabel("0 Selected")
        self.fleet_count_lbl.setStyleSheet("color: #818CF8; font-size: 12px; font-weight: 700;")
        fleet_hdr.addWidget(self.fleet_count_lbl)

        refresh_btn = QPushButton("↻ Scan")
        refresh_btn.setObjectName("GhostButton")
        refresh_btn.setCursor(Qt.PointingHandCursor)
        refresh_btn.clicked.connect(self.refresh_devices)
        fleet_hdr.addWidget(refresh_btn)
        fleet_layout.addLayout(fleet_hdr)

        # Scrollable container for DeviceCards
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.cards_container = QWidget()
        self.cards_container.setStyleSheet("background: transparent;")
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setContentsMargins(0, 0, 4, 0)
        self.cards_layout.setSpacing(8)
        self.cards_layout.addStretch()
        self.scroll_area.setWidget(self.cards_container)
        fleet_layout.addWidget(self.scroll_area)

        left_col.addWidget(fleet_card, stretch=1)

        # 2. Biometric Persona Card
        persona_card = QFrame()
        persona_card.setObjectName("Card")
        p_layout = QVBoxLayout(persona_card)
        p_layout.setContentsMargins(16, 16, 16, 16)
        p_layout.setSpacing(12)

        p_title = QLabel("BIOMETRIC PERSONA PROFILE")
        p_title.setObjectName("CardTitle")
        p_layout.addWidget(p_title)

        self.persona_combo = QComboBox()
        self.persona_combo.setCursor(Qt.PointingHandCursor)
        self.persona_combo.addItems(["average", "speedy", "careful", "shaky", "restless", "random"])
        self.persona_combo.currentTextChanged.connect(self._update_persona_Visualizer)
        p_layout.addWidget(self.persona_combo)

        # Trait Progress Bars
        self.trait_bars = {}
        traits = [
            ("Typing Speed (WPM)", "wpm"),
            ("Swipe Vigor & Fling", "vigor"),
            ("Hand Tremor Amplitude", "tremor"),
            ("Touch Scatter Radius", "scatter")
        ]
        for label_txt, key in traits:
            row = QVBoxLayout()
            row.setSpacing(4)
            lbl_row = QHBoxLayout()
            t_lbl = QLabel(label_txt)
            t_lbl.setStyleSheet("font-size: 11px; color: #A1A1AA;")
            v_lbl = QLabel("0")
            v_lbl.setStyleSheet("font-size: 11px; color: #E4E4E7; font-weight: 600; font-family: 'Consolas', monospace;")
            lbl_row.addWidget(t_lbl)
            lbl_row.addStretch()
            lbl_row.addWidget(v_lbl)

            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(False)
            bar.setFixedHeight(6)

            row.addLayout(lbl_row)
            row.addWidget(bar)
            p_layout.addLayout(row)
            self.trait_bars[key] = (bar, v_lbl)

        left_col.addWidget(persona_card)

        # 3. Action Buttons
        btn_row = QVBoxLayout()
        btn_row.setSpacing(10)

        self.record_btn = QPushButton("⏺   Launch Viewport Recorder")
        self.record_btn.setObjectName("RecordButton")
        self.record_btn.setCursor(Qt.PointingHandCursor)
        self.record_btn.clicked.connect(self.launch_recorder)
        btn_row.addWidget(self.record_btn)

        self.replay_btn = QPushButton("▶   Execute Lockstep Fleet Replay")
        self.replay_btn.setObjectName("ReplayButton")
        self.replay_btn.setCursor(Qt.PointingHandCursor)
        self.replay_btn.clicked.connect(self.launch_replay)
        btn_row.addWidget(self.replay_btn)

        left_col.addLayout(btn_row)

        # === RIGHT PANEL: LIVE TELEMETRY TERMINAL ===
        term_card = QFrame()
        term_card.setObjectName("Card")
        term_layout = QVBoxLayout(term_card)
        term_layout.setContentsMargins(1, 1, 1, 1)
        term_layout.setSpacing(0)

        # Terminal Top Bar (macOS style dots + Clear button)
        term_header = QFrame()
        term_header.setFixedHeight(44)
        term_header.setStyleSheet(
            "background-color: #141418; border-bottom: 1px solid #27272A; "
            "border-top-left-radius: 11px; border-top-right-radius: 11px;"
        )
        th_layout = QHBoxLayout(term_header)
        th_layout.setContentsMargins(16, 0, 16, 0)
        th_layout.setSpacing(8)

        for dot_color in ("#EF4444", "#F59E0B", "#10B981"):
            dot = QLabel()
            dot.setFixedSize(10, 10)
            dot.setStyleSheet(f"background-color: {dot_color}; border-radius: 5px;")
            th_layout.addWidget(dot)

        th_layout.addSpacing(8)
        term_title = QLabel("LIVE EXECUTION & TELEMETRY STREAM")
        term_title.setObjectName("CardTitle")
        th_layout.addWidget(term_title)
        th_layout.addStretch()

        clear_btn = QPushButton("Clear Console")
        clear_btn.setObjectName("GhostButton")
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.clicked.connect(lambda: self.console_output.clear())
        th_layout.addWidget(clear_btn)

        term_layout.addWidget(term_header)

        self.console_output = QTextEdit()
        self.console_output.setObjectName("Terminal")
        self.console_output.setReadOnly(True)
        term_layout.addWidget(self.console_output)

        # Assemble Left (380px fixed) & Right (expanding)
        left_widget = QWidget()
        left_widget.setFixedWidth(380)
        left_widget.setLayout(left_col)

        ws_layout.addWidget(left_widget)
        ws_layout.addWidget(term_card, stretch=1)
        root_layout.addWidget(workspace, stretch=1)

    # -----------------------------------------------------------------
    # HELPERS: STATUS PILL, PERSONA VISUALIZER & SYNTAX LOGGING
    # -----------------------------------------------------------------
    def _set_status_pill(self, text: str, hex_color: str):
        self.status_pill.setText(f"●  {text}")
        self.status_pill.setStyleSheet(
            f"background-color: rgba(255, 255, 255, 0.04); color: {hex_color}; "
            f"border: 1px solid {hex_color}; border-radius: 12px; "
            f"padding: 5px 14px; font-size: 11px; font-weight: 800; letter-spacing: 0.6px;"
        )

    def _update_persona_Visualizer(self, archetype: str):
        if personas is None:
            return
        p = personas.generate_persona(seed=42, archetype=archetype)

        wpm_pct = min(100, int((p.wpm / 65.0) * 100))
        vigor_pct = min(100, int((p.fling_bias / 1.0) * 100))
        tremor_pct = min(100, int((p.tremor_scale / 1.8) * 100))
        scatter_pct = min(100, int((p.scatter_px / 7.5) * 100))

        self.trait_bars["wpm"][0].setValue(wpm_pct)
        self.trait_bars["wpm"][1].setText(f"{p.wpm:.0f} WPM")

        self.trait_bars["vigor"][0].setValue(vigor_pct)
        self.trait_bars["vigor"][1].setText(f"{p.flick_vigor:.2f}x")

        self.trait_bars["tremor"][0].setValue(tremor_pct)
        self.trait_bars["tremor"][1].setText(f"{p.tremor_scale:.2f} σ")

        self.trait_bars["scatter"][0].setValue(scatter_pct)
        self.trait_bars["scatter"][1].setText(f"±{p.scatter_px:.1f} px")

    def _setup_logging(self):
        self.sys_stdout = sys.stdout
        self.logger = StreamToLogger()
        self.logger.new_text.connect(self.append_log)
        sys.stdout = self.logger
        print("[*] MightyGrid Unified Studio initialized. Ready for fleet operations.")

    def append_log(self, text: str):
        if not text:
            return
        cursor = self.console_output.textCursor()
        cursor.movePosition(QTextCursor.End)

        for line in text.splitlines(True):
            escaped = html.escape(line).replace(" ", "&nbsp;").replace("\n", "<br>")
            if "[+]" in line or "PASSED" in line:
                color = "#34D399"  # Emerald
            elif "[!]" in line or "FAILED" in line or "ERROR" in line:
                color = "#F87171"  # Rose
            elif "[*]" in line:
                color = "#60A5FA"  # Blue
            elif "===" in line:
                color = "#C084FC"  # Purple Header
            else:
                color = "#A1A1AA"  # Zinc Muted

            cursor.insertHtml(f"<span style='color:{color};'>{escaped}</span>")

        self.console_output.setTextCursor(cursor)
        self.console_output.ensureCursorVisible()

    # -----------------------------------------------------------------
    # ADB FLEET DISCOVERY & WORKFLOW ACTIONS
    # -----------------------------------------------------------------
    def _fetch_device_metadata(self, serial: str) -> tuple[str, str]:
        try:
            brand = subprocess.run(
                ["adb", "-s", serial, "shell", "getprop", "ro.product.brand"],
                capture_output=True, text=True, timeout=2
            ).stdout.strip().capitalize()
            model = subprocess.run(
                ["adb", "-s", serial, "shell", "getprop", "ro.product.model"],
                capture_output=True, text=True, timeout=2
            ).stdout.strip()
            ver = subprocess.run(
                ["adb", "-s", serial, "shell", "getprop", "ro.build.version.release"],
                capture_output=True, text=True, timeout=2
            ).stdout.strip()
            full_name = f"{brand} {model}".strip() or "Android Device"
            return full_name, (ver or "14")
        except Exception:
            return "Android Device", "?"

    def refresh_devices(self):
        # Clear existing cards
        for card in self.device_cards:
            card.deleteLater()
        self.device_cards.clear()

        try:
            res = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=5)
            lines = res.stdout.strip().splitlines()[1:]
            for line in lines:
                if "\tdevice" in line:
                    serial = line.split("\t")[0].strip()
                    model, ver = self._fetch_device_metadata(serial)
                    card = DeviceCard(serial, model, ver)
                    card.toggled.connect(self._update_selection_count)
                    self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)
                    self.device_cards.append(card)
        except Exception as e:
            print(f"[!] ADB scan error: {e}")

        self._update_selection_count()

    def _update_selection_count(self):
        selected = len(self.get_selected_devices())
        total = len(self.device_cards)
        self.fleet_count_lbl.setText(f"{selected} / {total} Selected")

    def get_selected_devices(self) -> list[str]:
        return [c.serial for c in self.device_cards if c.is_selected]

    def launch_recorder(self):
        serials = self.get_selected_devices()
        if not serials:
            QMessageBox.warning(self, "No Device Selected", "Select exactly ONE device card to record from.")
            return
        if len(serials) > 1:
            QMessageBox.warning(self, "Multiple Devices Selected", "Deselect extra devices. Recording requires a single primary source device.")
            return

        target = serials[0]
        self._set_status_pill("RECORDING ACTIVE", "#F43F5E")
        print(f"\n[*] Launching 60FPS Viewport Recorder on {target}...")

        self.recorder_window = RecorderMainWindow(serial=target)
        self.recorder_window.destroyed.connect(lambda: self._set_status_pill("SYSTEM IDLE", "#10B981"))
        self.recorder_window.show()

    def launch_replay(self):
        serials = self.get_selected_devices()
        if not serials:
            QMessageBox.warning(self, "No Target Fleet", "Select at least one device card to run the replay.")
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Recorded Session JSON", "./recordings", "JSON Files (*.json)"
        )
        if not file_path:
            return

        persona = self.persona_combo.currentText()

        self.replay_btn.setEnabled(False)
        self.record_btn.setEnabled(False)
        self.console_output.clear()
        self._set_status_pill(f"REPLAYING ({persona.upper()})", "#818CF8")

        self.replay_thread = ReplayThread(file_path, serials, persona, lockstep=True)
        self.replay_thread.finished_signal.connect(self.on_replay_finished)
        self.replay_thread.error_signal.connect(lambda e: print(f"\n[!] Fatal Replay Error: {e}"))
        self.replay_thread.start()

    def on_replay_finished(self):
        self.replay_btn.setEnabled(True)
        self.record_btn.setEnabled(True)
        self._set_status_pill("SYSTEM IDLE", "#10B981")
        print("\n[+] Fleet replay execution complete.")

    def closeEvent(self, event):
        sys.stdout = self.sys_stdout
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    studio = MightyGridStudio()
    studio.show()
    sys.exit(app.exec())