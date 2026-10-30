"""Main window: pick a link mode, start/stop the router, watch link health."""

import glob
import os
import subprocess

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QSpinBox, QToolButton,
    QVBoxLayout, QWidget,
)

from . import profiles
from .monitor import LinkMonitor
from .router import Router, device_holders, open_files, uptime_s

GREEN, ORANGE, RED, GRAY = "#2e9d4f", "#d98a1a", "#d0393b", "#8a8a8a"
BAUD_RATES = ["57600", "115200", "230400", "460800", "921600"]
HEARTBEAT_TIMEOUT_S = 3.0
WINDOW_WIDTH = 658
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


def explain_router_error(line: str, device: str):
    """(message, specific) for router errors that need the user to act, else None.

    specific=False is the raw line as a fallback. Unplugging is expected (the
    router restarts on its own), so it isn't reported.
    """
    if "Could not open" in line and "Permission denied" in line:
        return ("No permission to open the serial port.\n\n"
                "Add yourself to the dialout group, then log out and back in:\n"
                "sudo usermod -aG dialout $USER", True)
    if "Could not open" in line and "busy" in line.lower():
        return (f"{device or 'The serial port'} is in use by another program "
                "(often QGC AutoConnect). Close it or turn off AutoConnect for serial devices in QGC.", True)
    if "Error binding" in line and "in use" in line.lower():
        port = line.split(":")[-1].split()[0] if ":" in line else "?"
        return (f"UDP port {port} is already used by another program.\n\n"
                f"In SITL mode the router needs this port for PX4; make sure QGC isn't listening on it "
                "(QGC uses 14570 in SITL mode).", True)
    if "Could not" in line or "Error" in line or "Invalid" in line:
        return line.strip(), False
    return None


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
        self.detail.setWordWrap(True)  # the window width is fixed
        grid.addWidget(self.dot, row, 0)
        grid.addWidget(QLabel(label), row, 1)
        grid.addWidget(self.detail, row, 2)

    def set(self, color: str, text: str, tooltip: str = ""):
        self.dot.setStyleSheet(f"color: {color}; font-size: 16px;")
        self.dot.setToolTip(tooltip)
        self.detail.setText(text)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MAVLink Router")
        self._quitting = False
        self._reported_errors = set()  # each problem pops up once per Start
        self._settings = profiles.load_settings()
        self._router = Router()
        self._monitor = LinkMonitor(profiles.MONITOR_PORT)
        self._monitor.start()

        # Left to right like the data: vehicle link -> router -> endpoints
        columns = QHBoxLayout()
        self._input_box = self._build_input_box()
        columns.addWidget(self._input_box)
        columns.addWidget(self._build_endpoint_box(), 1)  # endpoints get the remaining width

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(columns)
        layout.addLayout(self._build_router_row())
        QShortcut(QKeySequence.StandardKey.Quit, self, activated=self.close)
        self.setCentralWidget(central)

        # Fixed width, measured while the (widest) serial-port rows are still
        # visible, i.e. before _load_mode hides them for SITL
        self._rebuild_endpoint_rows()
        self._input_box.setMinimumWidth(self._input_box.sizeHint().width())  # columns don't shift between modes
        self.setFixedWidth(max(self.sizeHint().width(), WINDOW_WIDTH))
        self.setFixedHeight(self.sizeHint().height())
        self._load_mode(self._settings["mode"])
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(500)
        self._refresh()

    # --- layout -------------------------------------------------------------

    def _build_input_box(self) -> QGroupBox:
        box = QGroupBox("Vehicle link")
        col = QVBoxLayout(box)
        grid = QGridLayout()
        col.addLayout(grid)

        self._mode_combo = QComboBox()
        for key, label in profiles.MODES.items():
            self._mode_combo.addItem(label, key)
        self._mode_combo.currentIndexChanged.connect(
            lambda: self._load_mode(self._mode_combo.currentData()))
        grid.addWidget(QLabel("Mode"), 0, 0)
        grid.addWidget(self._mode_combo, 0, 1, 1, 2)

        self._device_label = QLabel("Port")
        self._device_combo = QComboBox()
        # Long by-id names shouldn't dictate the column width
        self._device_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._device_combo.setMinimumContentsLength(20)
        self._rescan_btn = QToolButton()
        self._rescan_btn.setText("⟳")
        self._rescan_btn.setToolTip("Rescan serial ports")
        self._rescan_btn.clicked.connect(self._rescan_ports)
        grid.addWidget(self._device_label, 1, 0)
        grid.addWidget(self._device_combo, 1, 1)
        grid.addWidget(self._rescan_btn, 1, 2)

        self._baud_label = QLabel("Baud")
        self._baud_combo = QComboBox()
        self._baud_combo.setEditable(True)
        self._baud_combo.addItems(BAUD_RATES)
        grid.addWidget(self._baud_label, 2, 0)
        grid.addWidget(self._baud_combo, 2, 1, 1, 2)

        self._udp_label = QLabel("UDP port")
        self._udp_spin = QSpinBox()
        self._udp_spin.setRange(1, 65535)
        self._udp_spin.setToolTip("Port PX4 SITL sends its GCS link to")
        grid.addWidget(self._udp_label, 3, 0)
        grid.addWidget(self._udp_spin, 3, 1, 1, 2)
        grid.setColumnStretch(1, 1)

        status = QGridLayout()
        status.setColumnStretch(2, 1)
        self._input_row = StatusRow(status, 0, "Port")
        self._heartbeat_row = StatusRow(status, 1, "Heartbeat")
        self._traffic_row = StatusRow(status, 2, "Traffic")
        col.addSpacing(6)
        col.addLayout(status)
        col.addStretch()
        return box

    def _build_endpoint_box(self) -> QGroupBox:
        box = QGroupBox("Endpoints")
        col = QVBoxLayout(box)
        self._endpoint_grid = QGridLayout()
        self._endpoint_grid.setColumnStretch(2, 1)
        self._endpoint_rows = []
        col.addLayout(self._endpoint_grid)
        col.addStretch()
        return box

    def _build_router_row(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setColumnStretch(2, 1)
        self._router_row = StatusRow(grid, 0, "Router")
        self._start_btn = QPushButton("Start")
        self._start_btn.clicked.connect(self._on_start)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.clicked.connect(self._on_stop)
        grid.addWidget(self._start_btn, 0, 3)
        grid.addWidget(self._stop_btn, 0, 4)
        config_btn = QPushButton("Open config folder")
        config_btn.setToolTip(f"Endpoints and defaults: {profiles.SETTINGS_FILE}")
        config_btn.clicked.connect(self._open_config_dir)
        grid.addWidget(config_btn, 0, 5)
        return grid



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
        for w in (self._device_label, self._device_combo, self._rescan_btn, self._baud_label, self._baud_combo):
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
        self._reported_errors.clear()
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

    def _on_quit(self) -> bool:
        """Quit after confirmation; a running router is stopped. True if quitting."""
        if self._router.supervisor_pid() is not None:
            box = QMessageBox(QMessageBox.Icon.Question, "Quit",
                              "The router is running. Stop it and quit?", parent=self)
            stop = box.addButton("Stop router and quit", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() != stop:
                return False
            self._router.stop()
        elif not self._confirm("Quit", "Quit MAVLink Router?"):
            return False
        self._quitting = True
        self._monitor.stop()
        QApplication.quit()
        return True

    def show_existing(self):
        """Bring the window back, e.g. when the app is launched a second time."""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _report_router_errors(self):
        device = self._settings["serial"].get(self._mode(), {}).get("device", "")
        found = [e for e in (explain_router_error(line, device) for line in self._router.new_output()) if e]
        # One failure often prints several lines; prefer the explained one
        if any(specific for _, specific in found):
            found = [e for e in found if e[1]]
        for message, _ in found:
            if message not in self._reported_errors:
                self._reported_errors.add(message)
                # Non-modal, so status keeps updating behind it
                box = QMessageBox(QMessageBox.Icon.Warning, "MAVLink Router", message, parent=self)
                box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
                box.show()

    def _confirm(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def _open_config_dir(self):
        profiles.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(profiles.CONFIG_DIR)])

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
            device = self._settings["serial"].get(self._mode(), {}).get("device", "")
            if device and not os.path.exists(device):
                self._router_row.set(ORANGE, "waiting for device", "Starts automatically when the port is back")
            else:
                self._router_row.set(ORANGE, "retrying", "The router exited; it restarts every second")
        elif pids["external"]:
            self._router_row.set(ORANGE, "running (external)",
                                 f"Started outside this app, pid {', '.join(map(str, pids['external']))}")
        else:
            self._router_row.set(GRAY, "stopped")

        if router is not None or pids["external"]:
            state = "running"
        elif supervisor is not None:
            state = "waiting"
        else:
            state = "stopped"
        self.setWindowTitle(f"MAVLink Router: {state}")

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
                status.set(GRAY, text, "Remote address, can't check from here")
            elif port in listening:
                status.set(GREEN, text, "Listening")
            else:
                status.set(GRAY, text, "Nothing listening")

        self._report_router_errors()

    def _refresh_input(self, supervisor, router):
        mode = self._mode()
        if mode not in profiles.SERIAL_MODES:
            if router is not None:
                self._input_row.set(GREEN, f"UDP {self._settings['sitl']['listen_port']}")
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
                self._input_row.set(ORANGE, "unplugged", "Reconnects automatically when plugged back in")
            else:
                self._input_row.set(GRAY, "not connected")
            return
        if router is not None and os.path.realpath(device) in open_files(router):
            self._input_row.set(GREEN, f"{name} open")
            return

        holders = [h for h in device_holders(device) if h[0] != router]
        if holders:
            color = RED if supervisor is not None else ORANGE
            self._input_row.set(color, f"held by {holders[0][1]}",
                                f"{name} is open by {describe(holders)}, e.g. QGC AutoConnect")
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
            self._heartbeat_row.set(RED, f"lost ({snap.heartbeat_age_s:.0f} s)", f"sysid {snap.sysid}")
        else:
            self._heartbeat_row.set(GREEN, f"sysid {snap.sysid}, {snap.heartbeat_hz:.1f} Hz")

        if router_running:
            self._traffic_row.set(GREEN if snap.msgs_per_s > 0 else GRAY, f"{snap.msgs_per_s:.0f} msg/s")
        else:
            self._traffic_row.set(GRAY, "-")

    def closeEvent(self, event):
        # Closing the window quits the app (with confirmation; stops the router)
        if self._quitting or self._on_quit():
            event.accept()
        else:
            event.ignore()
