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
from .router import Router, device_holders, open_files, uptime_s

GREEN, ORANGE, RED, GRAY = "#2e9d4f", "#d98a1a", "#d0393b", "#8a8a8a"
BAUD_RATES = ["57600", "115200", "230400", "460800", "921600"]
HEARTBEAT_TIMEOUT_S = 3.0
# Words in a /dev/serial/by-id name that mean it's an autopilot, not a radio
AUTOPILOT_HINTS = ("px4", "pixhawk", "ardupilot", "cubepilot", "fmu")


def serial_ports() -> list:
    """(label, path) for serial devices; stable /dev/serial/by-id paths first.

    by-id paths survive a replug (ttyACM0 may come back as ttyACM1), so they
    are the ones to save and to wait for.
    """
    ports = []
    linked = set()
    for path in sorted(glob.glob("/dev/serial/by-id/*")):
        real = os.path.realpath(path)
        linked.add(real)
        ports.append((f"{by_id_label(path)} ({os.path.basename(real)})", path))
    for path in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
        if path not in linked:
            ports.append((path, path))
    return ports


def by_id_label(path: str) -> str:
    """'usb-Holybro_PX4_FMU_v6C.x_3A0029-if00' -> 'Holybro PX4 FMU v6C.x'."""
    name = os.path.basename(path)
    if name.startswith("usb-"):
        name = name[4:]
    name = name.rsplit("-if", 1)[0]
    parts = name.split("_")
    if len(parts) > 1 and len(parts[-1]) >= 6 and parts[-1].isalnum():
        parts = parts[:-1]  # trailing serial number
    return " ".join(parts)


def looks_like_autopilot(device: str) -> bool:
    real = os.path.realpath(device)
    names = [os.path.basename(device)] + [
        os.path.basename(p) for p in glob.glob("/dev/serial/by-id/*") if os.path.realpath(p) == real]
    return any(hint in n.lower() for n in names for hint in AUTOPILOT_HINTS)


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


def describe(holders) -> str:
    return ", ".join(f"{name} (pid {pid})" for pid, name in holders)


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

    def _device(self) -> str:
        return self._device_combo.currentData() or ""

    def _rescan_ports(self, selected: str = ""):
        current = selected or self._device()
        self._device_combo.clear()
        for label, path in serial_ports():
            self._device_combo.addItem(label, path)
        if current and self._device_combo.findData(current) < 0:
            # Keep the saved device selectable while unplugged; the router waits for it
            label = current if os.path.exists(current) else f"{current} (not connected)"
            self._device_combo.addItem(label, current)
        index = self._device_combo.findData(current)
        if index >= 0:
            self._device_combo.setCurrentIndex(index)

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
            device = self._device()
            if not device:
                QMessageBox.warning(self, "No port", "Select the serial port first.")
                return
            try:
                baud = int(self._baud_combo.currentText())
            except ValueError:
                QMessageBox.warning(self, "Invalid baud rate", "Baud rate must be a number.")
                return
            if mode == "flight" and looks_like_autopilot(device) and not self._confirm(
                    "This looks like the Pixhawk's USB port",
                    f"{device} looks like an autopilot, not a telemetry radio.\n\n"
                    "In HITL the sim bridge owns the Pixhawk USB; Flight mode should use the ground radio.\n\n"
                    "Start on this port anyway?"):
                return
            holders = device_holders(device)
            if holders and not self._confirm(
                    "Port in use",
                    f"{device} is open by {describe(holders)}.\n\n"
                    "The router can't open it until that program lets go (e.g. QGC AutoConnect). "
                    "Start anyway? It will keep retrying.\n"):
                return
            self._settings["serial"][mode] = {"device": device, "baud": baud}
        else:
            self._settings["sitl"]["listen_port"] = self._udp_spin.value()

        others = self._router.router_pids()["external"]
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
            self._router.start(conf, device)
        except (OSError, FileNotFoundError) as e:
            QMessageBox.critical(self, "Could not start router", str(e))
        self._refresh()

    def _on_stop(self):
        if self._router.supervisor_pid() is not None:
            self._router.stop()
        else:
            others = self._router.router_pids()["external"]
            if others and self._confirm(
                    "Stop external router?",
                    f"A router not started from this app is running (pid {', '.join(map(str, others))}). Stop it?"):
                self._router.stop_external(others)
        self._refresh()

    def _confirm(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def _open_config_dir(self):
        profiles.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(profiles.CONFIG_DIR)])

    def _on_log_toggled(self, shown: bool):
        self._log_btn.setText(("▾" if shown else "▸") + " Router log")
        self._log_view.setVisible(shown)
        self._refresh()

    # --- status -------------------------------------------------------------

    def _refresh(self):
        supervisor = self._router.supervisor_pid()
        pids = self._router.router_pids()
        router = pids["ours"][0] if pids["ours"] else None
        active = supervisor is not None or bool(pids["external"])

        if router is not None:
            up = uptime_s(router)
            self._router_row.set(GREEN, f"running, pid {router}" + (f", up {format_duration(up)}" if up else ""))
        elif supervisor is not None:
            self._router_row.set(ORANGE, "waiting to (re)start, reconnects automatically")
        elif pids["external"]:
            self._router_row.set(ORANGE, f"running outside this app (pid {', '.join(map(str, pids['external']))})")
        else:
            self._router_row.set(GRAY, "stopped")

        self._start_btn.setEnabled(not active)
        self._stop_btn.setEnabled(active)
        for w in (self._mode_combo, self._device_combo, self._rescan_btn, self._baud_combo, self._udp_spin):
            w.setEnabled(not active)

        self._refresh_input(supervisor, router)
        self._refresh_link(router is not None or bool(pids["external"]))

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

    def _refresh_input(self, supervisor, router):
        mode = self._mode()
        if mode not in profiles.SERIAL_MODES:
            if router is not None:
                self._input_row.set(GREEN, f"listening on UDP {self._settings['sitl']['listen_port']}")
            else:
                self._input_row.set(GRAY, "-")
            return

        # While running, show the device the router was started with
        device = self._settings["serial"][mode]["device"] if supervisor is not None else self._device()
        if not device:
            self._input_row.set(GRAY, "-")
            return
        name = os.path.basename(os.path.realpath(device))
        if not os.path.exists(device):
            if supervisor is not None:
                self._input_row.set(ORANGE, "unplugged, waiting for it to come back")
            else:
                self._input_row.set(GRAY, "not connected")
            return
        if router is not None and os.path.realpath(device) in open_files(router):
            self._input_row.set(GREEN, f"{name} open")
            return

        holders = [h for h in device_holders(device) if h[0] != router]
        if holders:
            color = RED if supervisor is not None else ORANGE
            self._input_row.set(color, f"{name} held by {describe(holders)}")
        elif supervisor is not None:
            self._input_row.set(ORANGE, f"{name} connecting...")
        else:
            self._input_row.set(GRAY, f"{name} available")

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
