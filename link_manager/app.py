"""Main window: pick a link mode, start/stop the router, watch link health."""

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QSpinBox, QStackedWidget, QToolButton,
    QVBoxLayout, QWidget,
)

from . import profiles
from .common import (
    BAUD_RATES, HEARTBEAT_TIMEOUT_S, describe, explain_router_errors, format_duration, looks_like_autopilot,
    serial_ports, udp_listen_ports,
)
from .monitor import LinkMonitor
from .router import Router, device_holders, open_files, uptime_s

GREEN, ORANGE, RED, GRAY = "#2e9d4f", "#d98a1a", "#d0393b", "#8a8a8a"
HOLDERS_CACHE_S = 2.0
WINDOW_WIDTH = 658


class StatusRow:
    """A colored dot, a label and a detail text."""

    def __init__(self, grid: QGridLayout, row: int, label: str):
        self.dot = QLabel("●")
        self._state = None
        self.detail = QLabel()
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail.setWordWrap(True)  # the window width is fixed
        grid.addWidget(self.dot, row, 0)
        grid.addWidget(QLabel(label), row, 1)
        grid.addWidget(self.detail, row, 2)
        self.set(GRAY, "")  # not the default black until the first refresh

    def set(self, color: str, text: str, tooltip: str = ""):
        # Called every refresh; only touch the widgets when something changed
        # (setStyleSheet re-styles and repaints)
        state = (color, text, tooltip)
        if state == self._state:
            return
        self._state = state
        self.dot.setStyleSheet(f"color: {color}; font-size: 16px;")
        self.dot.setToolTip(tooltip)
        self.detail.setText(text)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MAVLink Router")
        self._quitting = False
        self._reported_errors = set()  # each problem pops up once per Start
        self._holders_cache = ("", float("-inf"), [])
        self._holders_scan = None  # Future of a background device_holders() scan
        self._scanner = ThreadPoolExecutor(max_workers=1)
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

        # Fixed size; the stacked mode pages are as large as the largest one,
        # so the size fits every mode
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
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_label = QLabel("Mode")
        grid.addWidget(mode_label, 0, 0)
        grid.addWidget(self._mode_combo, 0, 1)

        # Mode-specific inputs on stacked pages: the stack is as tall as its
        # tallest page, so the status below doesn't move when the mode changes
        self._serial_page = QWidget()
        serial_grid = QGridLayout(self._serial_page)
        serial_grid.setContentsMargins(0, 0, 0, 0)
        self._device_label = QLabel("Port")
        self._device_combo = QComboBox()
        # Long by-id names shouldn't dictate the column width
        self._device_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._device_combo.setMinimumContentsLength(20)
        self._rescan_btn = QToolButton()
        self._rescan_btn.setText("⟳")
        self._rescan_btn.setToolTip("Rescan serial ports")
        self._rescan_btn.clicked.connect(self._rescan_ports)
        serial_grid.addWidget(self._device_label, 0, 0)
        serial_grid.addWidget(self._device_combo, 0, 1)
        serial_grid.addWidget(self._rescan_btn, 0, 2)

        self._baud_label = QLabel("Baud")
        self._baud_combo = QComboBox()
        self._baud_combo.setEditable(True)
        self._baud_combo.addItems(BAUD_RATES)
        serial_grid.addWidget(self._baud_label, 1, 0)
        serial_grid.addWidget(self._baud_combo, 1, 1, 1, 2)
        serial_grid.setColumnStretch(1, 1)

        self._sitl_page = QWidget()
        sitl_grid = QGridLayout(self._sitl_page)
        sitl_grid.setContentsMargins(0, 0, 0, 0)
        self._udp_label = QLabel("UDP port")
        self._udp_spin = QSpinBox()
        self._udp_spin.setRange(1, 65535)
        self._udp_spin.setToolTip("Port PX4 SITL sends its GCS link to")
        sitl_grid.addWidget(self._udp_label, 0, 0)
        sitl_grid.addWidget(self._udp_spin, 0, 1)
        sitl_grid.setColumnStretch(1, 1)
        sitl_grid.setRowStretch(1, 1)  # top-aligned; the serial page is a row taller

        self._link_pages = QStackedWidget()
        self._link_pages.addWidget(self._serial_page)
        self._link_pages.addWidget(self._sitl_page)
        grid.addWidget(self._link_pages, 1, 0, 1, 2)
        grid.setColumnStretch(1, 1)
        # One label width on every page, so the inputs don't shift sideways either
        labels = (mode_label, self._device_label, self._baud_label, self._udp_label)
        label_width = max(label.sizeHint().width() for label in labels)
        for label in labels:
            label.setFixedWidth(label_width)

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

    def _on_mode_changed(self):
        self._load_mode(self._mode())
        self._refresh()  # the new endpoint rows show their status now, not on the next tick

    def _load_mode(self, mode: str):
        index = self._mode_combo.findData(mode)
        if index != self._mode_combo.currentIndex():
            self._mode_combo.blockSignals(True)
            self._mode_combo.setCurrentIndex(index)
            self._mode_combo.blockSignals(False)

        serial = mode in profiles.SERIAL_MODES
        self._link_pages.setCurrentWidget(self._serial_page if serial else self._sitl_page)

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

        others = self._router.router_pids(fresh=True)["external"]
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
            others = self._router.router_pids(fresh=True)["external"]
            if others and self._confirm(
                    "Stop external router?",
                    f"A router not started from this app is running (pid {', '.join(map(str, others))}). Stop it?"):
                self._router.stop_external(others)
        self._refresh()

    def _on_quit(self) -> bool:
        """Quit after confirmation; a running router is stopped. True if quitting."""
        if self._router.supervisor_pid() is not None:
            if not self._ask_quit("The router is running. Stop it and quit?", "Stop router and quit"):
                return False
            self._router.stop()
        elif not self._ask_quit("Quit MAVLink Router?", "Quit"):
            return False
        self._quitting = True
        self._monitor.stop()
        self._scanner.shutdown(wait=False, cancel_futures=True)
        QApplication.quit()
        return True

    def _ask_quit(self, text: str, action: str) -> bool:
        """Cancel + one action button; same roles in both quit dialogs, so the same button order."""
        box = QMessageBox(QMessageBox.Icon.Question, "Quit", text, parent=self)
        go = box.addButton(action, QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() == go

    def show_existing(self):
        """Bring the window back, e.g. when the app is launched a second time."""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _report_router_errors(self):
        device = self._settings["serial"].get(self._mode(), {}).get("device", "")
        for message in explain_router_errors(self._router.new_output(), device):
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
        title = f"MAVLink Router: {state}"
        if title != self.windowTitle():
            self.setWindowTitle(title)

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

        holders = [h for h in self._cached_holders(device) if h[0] != router]
        if holders:
            color = RED if supervisor is not None else ORANGE
            self._input_row.set(color, f"held by {holders[0][1]}",
                                f"{name} is open by {describe(holders)}, e.g. QGC AutoConnect")
        elif supervisor is not None:
            self._input_row.set(ORANGE, f"{name} connecting...")
        else:
            self._input_row.set(GRAY, f"{name} available")

    def _cached_holders(self, device: str):
        """Last known holders of the device.

        device_holders() scans every process's open files (tens of ms), so it
        runs in the background at most every HOLDERS_CACHE_S; until a result
        for this device is in, report none.
        """
        scan = self._holders_scan
        if scan is not None and scan.done():
            self._holders_scan = None
            try:
                self._holders_cache = (scan.device, time.monotonic(), scan.result())
            except OSError:
                pass
        cached_device, stamp, holders = self._holders_cache
        stale = cached_device != device or time.monotonic() - stamp > HOLDERS_CACHE_S
        if stale and self._holders_scan is None:
            self._holders_scan = self._scanner.submit(device_holders, device)
            self._holders_scan.device = device
        return holders if cached_device == device else []

    def _refresh_link(self, router_running: bool):
        snap = self._monitor.snapshot()
        if snap.port_busy:
            # Only one listener gets the monitor traffic; the monitor takes over once the other app closes
            self._heartbeat_row.set(GRAY, "port occupied",
                                    f"Port {profiles.MONITOR_PORT} is held by the other MAVLink Router app (GUI or "
                                    "CLI); the heartbeat shows here as soon as that one closes")
        elif snap.error:
            self._heartbeat_row.set(RED, snap.error)
        elif not router_running:
            self._heartbeat_row.set(GRAY, "-")
        elif snap.heartbeat_age_s is None:
            self._heartbeat_row.set(ORANGE, "waiting for vehicle")
        elif snap.heartbeat_age_s > HEARTBEAT_TIMEOUT_S:
            self._heartbeat_row.set(RED, f"lost ({snap.heartbeat_age_s:.0f} s)", f"sysid {snap.sysid}")
        else:
            self._heartbeat_row.set(GREEN, f"sysid {snap.sysid}, {snap.heartbeat_hz:.1f} Hz")

        if router_running and not snap.error:
            self._traffic_row.set(GREEN if snap.msgs_per_s > 0 else GRAY, f"{snap.msgs_per_s:.0f} msg/s")
        else:
            self._traffic_row.set(GRAY, "-")

    def closeEvent(self, event):
        # Closing the window quits the app (with confirmation; stops the router)
        if self._quitting or self._on_quit():
            event.accept()
        else:
            event.ignore()
