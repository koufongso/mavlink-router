"""Main window: pick a link mode, start/stop the router, watch link health."""

import glob
import os
import subprocess

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import (
    QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QToolButton,
    QVBoxLayout, QWidget,
)

from . import profiles
from .monitor import LinkMonitor
from .router import Router, open_files, uptime_s

GREEN, ORANGE, RED, GRAY = "#2e9d4f", "#d98a1a", "#d0393b", "#8a8a8a"
BAUD_RATES = ["57600", "115200", "230400", "460800", "921600"]
HEARTBEAT_TIMEOUT_S = 3.0


def serial_ports() -> list:
    """Serial devices, stable /dev/serial/by-id names first."""
    by_id = sorted(glob.glob("/dev/serial/by-id/*"))
    raw = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    linked = {os.path.realpath(p) for p in by_id}
    return by_id + [p for p in raw if p not in linked]


def udp_listen_ports() -> set:
    """Local UDP ports something on this machine is bound to."""
    ports = set()
    for path in ("/proc/net/udp", "/proc/net/udp6"):
        try:
            with open(path) as f:
                next(f)
                for line in f:
                    local = line.split()[1]
                    ports.add(int(local.rsplit(":", 1)[1], 16))
        except (OSError, StopIteration, ValueError, IndexError):
            pass
    return ports


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


class StatusRow:
    """A colored dot, a label and a detail text."""

    def __init__(self, grid: QGridLayout, row: int, label: str):
        self.dot = QLabel("●")
        self.detail = QLabel()
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        grid.addWidget(self.dot, row, 0)
        grid.addWidget(QLabel(label), row, 1)
        grid.addWidget(self.detail, row, 2)

    def set(self, color: str, text: str):
        self.dot.setStyleSheet(f"color: {color}; font-size: 16px;")
        self.detail.setText(text)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MAVLink Router")
        self.setMinimumWidth(520)
        self._settings = profiles.load_settings()
        self._router = Router()
        self._monitor = LinkMonitor(profiles.MONITOR_PORT)
        self._monitor.start()

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self._build_input_box())
        layout.addLayout(self._build_buttons())
        layout.addWidget(self._build_status_box())
        layout.addWidget(self._build_endpoint_box())
        layout.addLayout(self._build_log())
        self.setCentralWidget(central)
        self.statusBar().showMessage("Closing this window leaves the router running.")

        self._load_mode(self._settings["mode"])
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(500)
        self._refresh()

    # --- layout -------------------------------------------------------------

    def _build_input_box(self) -> QGroupBox:
        box = QGroupBox("Vehicle link")
        grid = QGridLayout(box)

        self._mode_combo = QComboBox()
        for key, label in profiles.MODES.items():
            self._mode_combo.addItem(label, key)
        self._mode_combo.currentIndexChanged.connect(
            lambda: self._load_mode(self._mode_combo.currentData()))
        grid.addWidget(QLabel("Mode"), 0, 0)
        grid.addWidget(self._mode_combo, 0, 1, 1, 3)

        self._device_label = QLabel("Port")
        self._device_combo = QComboBox()
        self._device_combo.setEditable(True)
        self._device_combo.setMinimumWidth(320)
        self._rescan_btn = QToolButton()
        self._rescan_btn.setText("⟳")
        self._rescan_btn.setToolTip("Rescan serial ports")
        self._rescan_btn.clicked.connect(self._rescan_ports)
        self._baud_combo = QComboBox()
        self._baud_combo.setEditable(True)
        self._baud_combo.addItems(BAUD_RATES)
        grid.addWidget(self._device_label, 1, 0)
        grid.addWidget(self._device_combo, 1, 1)
        grid.addWidget(self._rescan_btn, 1, 2)
        grid.addWidget(self._baud_combo, 1, 3)

        self._udp_label = QLabel("PX4 UDP port")
        self._udp_spin = QSpinBox()
        self._udp_spin.setRange(1, 65535)
        grid.addWidget(self._udp_label, 2, 0)
        grid.addWidget(self._udp_spin, 2, 1)
        return box

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self._start_btn = QPushButton("Start")
        self._start_btn.clicked.connect(self._on_start)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.clicked.connect(self._on_stop)
        config_btn = QPushButton("Open config folder")
        config_btn.setToolTip(f"Endpoints and defaults: {profiles.SETTINGS_FILE}")
        config_btn.clicked.connect(self._open_config_dir)
        row.addWidget(self._start_btn)
        row.addWidget(self._stop_btn)
        row.addStretch()
        row.addWidget(config_btn)
        return row

    def _build_status_box(self) -> QGroupBox:
        box = QGroupBox("Status")
        grid = QGridLayout(box)
        grid.setColumnStretch(2, 1)
        self._router_row = StatusRow(grid, 0, "Router")
        self._input_row = StatusRow(grid, 1, "Vehicle port")
        self._heartbeat_row = StatusRow(grid, 2, "Vehicle heartbeat")
        self._traffic_row = StatusRow(grid, 3, "Traffic")
        return box

    def _build_endpoint_box(self) -> QGroupBox:
        box = QGroupBox("Endpoints (green: something is listening on the port)")
        self._endpoint_grid = QGridLayout(box)
        self._endpoint_grid.setColumnStretch(2, 1)
        self._endpoint_rows = []
        return box

    def _build_log(self) -> QVBoxLayout:
        col = QVBoxLayout()
        self._log_btn = QToolButton()
        self._log_btn.setText("▸ Router log")
        self._log_btn.setCheckable(True)
        self._log_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._log_btn.toggled.connect(self._on_log_toggled)
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._log_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self._log_view.setMinimumHeight(160)
        self._log_view.hide()
        col.addWidget(self._log_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        col.addWidget(self._log_view)
        return col

    # --- mode / inputs ------------------------------------------------------

    def _mode(self) -> str:
        return self._mode_combo.currentData()

    def _load_mode(self, mode: str):
        index = self._mode_combo.findData(mode)
        if index != self._mode_combo.currentIndex():
            self._mode_combo.blockSignals(True)
            self._mode_combo.setCurrentIndex(index)
            self._mode_combo.blockSignals(False)

        serial = mode in profiles.SERIAL_MODES
        for w in (self._device_label, self._device_combo, self._rescan_btn, self._baud_combo):
            w.setVisible(serial)
        self._udp_label.setVisible(not serial)
        self._udp_spin.setVisible(not serial)

        if serial:
            cfg = self._settings["serial"][mode]
            self._rescan_ports(selected=cfg["device"])
            self._baud_combo.setCurrentText(str(cfg["baud"]))
        else:
            self._udp_spin.setValue(int(self._settings["sitl"]["listen_port"]))
        self._rebuild_endpoint_rows()

    def _rescan_ports(self, selected: str = ""):
        current = selected or self._device_combo.currentText()
        self._device_combo.clear()
        self._device_combo.addItems(serial_ports())
        self._device_combo.setCurrentText(current)

    def _rebuild_endpoint_rows(self):
        while self._endpoint_grid.count():
            widget = self._endpoint_grid.takeAt(0).widget()
            widget.setParent(None)  # detach now; deleteLater alone leaves it drawn until the next event loop pass
            widget.deleteLater()
        self._endpoint_rows = []
        for row, (name, address, port) in enumerate(profiles.endpoints_for(self._settings, self._mode())):
            status = StatusRow(self._endpoint_grid, row, name)
            self._endpoint_rows.append((status, address, port))

    # --- actions ------------------------------------------------------------

    def _on_start(self):
        mode = self._mode()
        device, baud = "", 0
        if mode in profiles.SERIAL_MODES:
            device = self._device_combo.currentText().strip()
            try:
                baud = int(self._baud_combo.currentText())
            except ValueError:
                QMessageBox.warning(self, "Invalid baud rate", "Baud rate must be a number.")
                return
            if not os.path.exists(device):
                QMessageBox.warning(self, "Port not found", f"{device} doesn't exist. Is the radio plugged in?")
                return
            self._settings["serial"][mode] = {"device": device, "baud": baud}
        else:
            self._settings["sitl"]["listen_port"] = self._udp_spin.value()

        others = self._router.external_pids()
        if others:
            QMessageBox.warning(
                self, "Router already running",
                f"mavlink-routerd is already running (pid {', '.join(map(str, others))}), "
                "not started from this app. Stop it first; two routers would fight over the same ports.")
            return

        self._settings["mode"] = mode
        profiles.save_settings(self._settings)
        conf = profiles.build_conf(self._settings, mode, device, baud)
        try:
            self._router.start(conf)
        except (OSError, FileNotFoundError) as e:
            QMessageBox.critical(self, "Could not start router", str(e))
        self._refresh()

    def _on_stop(self):
        pid = self._router.pid()
        if pid is None:
            others = self._router.external_pids()
            if not others:
                return
            answer = QMessageBox.question(
                self, "Stop external router?",
                f"A router not started from this app is running (pid {', '.join(map(str, others))}). Stop it?")
            if answer != QMessageBox.StandardButton.Yes:
                return
            for p in others:
                self._router.stop(p)
        else:
            self._router.stop(pid)
        self._refresh()

    def _open_config_dir(self):
        profiles.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(profiles.CONFIG_DIR)])

    def _on_log_toggled(self, shown: bool):
        self._log_btn.setText(("▾" if shown else "▸") + " Router log")
        self._log_view.setVisible(shown)
        self._refresh()

    # --- status -------------------------------------------------------------

    def _refresh(self):
        pid = self._router.pid()
        others = self._router.external_pids()
        running_pid = pid or (others[0] if others else None)

        if pid:
            up = uptime_s(pid)
            self._router_row.set(GREEN, f"running, pid {pid}" + (f", up {format_duration(up)}" if up else ""))
        elif others:
            self._router_row.set(ORANGE, f"running outside this app (pid {', '.join(map(str, others))})")
        else:
            self._router_row.set(GRAY, "stopped")

        self._start_btn.setEnabled(running_pid is None)
        self._stop_btn.setEnabled(running_pid is not None)
        for w in (self._mode_combo, self._device_combo, self._baud_combo, self._udp_spin):
            w.setEnabled(running_pid is None)

        self._refresh_input(pid)
        self._refresh_link(running_pid is not None)

        listening = udp_listen_ports()
        for status, address, port in self._endpoint_rows:
            text = f"{address}:{port}"
            if address not in ("127.0.0.1", "localhost"):
                status.set(GRAY, text + " (remote)")
            elif port in listening:
                status.set(GREEN, text)
            else:
                status.set(GRAY, text + " (nothing listening)")

        if self._log_view.isVisible():
            text = self._router.tail_log()
            if text != self._log_view.toPlainText():
                self._log_view.setPlainText(text)
                self._log_view.verticalScrollBar().setValue(self._log_view.verticalScrollBar().maximum())

    def _refresh_input(self, pid):
        mode = self._mode()
        if pid is None:
            self._input_row.set(GRAY, "-")
        elif mode in profiles.SERIAL_MODES:
            device = os.path.realpath(self._settings["serial"][mode]["device"])
            if device in open_files(pid):
                self._input_row.set(GREEN, f"{device} open")
            else:
                self._input_row.set(RED, f"{device} not open (see router log)")
        else:
            self._input_row.set(GREEN, f"listening on UDP {self._settings['sitl']['listen_port']}")

    def _refresh_link(self, router_running: bool):
        snap = self._monitor.snapshot()
        if snap.error:
            self._heartbeat_row.set(RED, snap.error)
        elif not router_running:
            self._heartbeat_row.set(GRAY, "-")
        elif snap.heartbeat_age_s is None:
            self._heartbeat_row.set(ORANGE, "waiting for vehicle")
        elif snap.heartbeat_age_s > HEARTBEAT_TIMEOUT_S:
            self._heartbeat_row.set(RED, f"lost, last seen {snap.heartbeat_age_s:.0f} s ago (sysid {snap.sysid})")
        else:
            self._heartbeat_row.set(GREEN, f"sysid {snap.sysid}, {snap.heartbeat_hz:.1f} Hz")

        if router_running:
            self._traffic_row.set(GREEN if snap.msgs_per_s > 0 else GRAY, f"{snap.msgs_per_s:.0f} msg/s")
        else:
            self._traffic_row.set(GRAY, "-")

    def closeEvent(self, event):
        self._monitor.stop()
        event.accept()
