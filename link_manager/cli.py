"""Interactive terminal UI for headless and SSH use: the GUI's controls and live status in a terminal.

Same router, settings and state files as the GUI, so the two mix freely:
start the router over SSH and the GUI shows it running, or the other way
round. The router runs detached, so a dropped SSH connection doesn't drop the
link; quitting asks whether to stop it.
"""

import copy
import curses
import locale
import os
import textwrap
import time

from . import profiles
from .common import (
    BAUD_RATES, HEARTBEAT_TIMEOUT_S, describe, explain_router_errors, format_duration, looks_like_autopilot,
    serial_ports, udp_listen_ports,
)
from .monitor import LinkMonitor
from .router import LOG_FILE, Router, device_holders, open_files, uptime_s

OK, WARN, BAD, OFF = "ok", "warn", "bad", "off"
REFRESH_MS = 500
HOLDERS_CACHE_S = 2.0
# How far back to look for the errors of the latest failed attempt
ERROR_SCAN_BYTES = 50_000
LOG_TAIL_BYTES = 20_000
SERIAL_FIELDS = ["mode", "port", "baud"]
SITL_FIELDS = ["mode", "udp"]
FIELD_LABELS = {"mode": "Mode", "port": "Port", "baud": "Baud", "udp": "UDP port"}
KEYS = [("↑↓", "select"), ("←→", "change"), ("Enter", "edit"), ("s", "start"), ("x", "stop"),
        ("r", "rescan"), ("l", "log"), ("q", "quit")]



# --- status and start, shared with the UI ------------------------------------

def resolve_device(arg: str) -> str:
    """A bare name (ttyUSB0) or a path."""
    return arg if "/" in arg else f"/dev/{arg}"


def last_errors(device: str) -> list:
    """Errors of the latest failed router attempt since the last Start.

    Used while the supervisor is retrying, to say why. The log is read
    directly (not Router.new_output) so a fresh CLI sees errors that were
    logged before it ran.
    """
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - ERROR_SCAN_BYTES))
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return []
    # Each attempt starts with "starting mavlink-routerd"; a Start with "supervisor:"
    attempt = []
    for line in reversed(lines):
        if "supervisor:" in line:
            break
        if "starting mavlink-routerd" in line:
            errors = explain_router_errors(attempt, device)
            if errors:
                return errors
            attempt = []
        else:
            attempt.insert(0, line)
    return []


def status_rows(router: Router, settings: dict, monitor: LinkMonitor, holders=device_holders):
    """(state, link rows, endpoint rows, errors); rows are (level, label, text).

    holders: device -> [(pid, name)], for callers that cache the slow scan.
    """
    mode = settings["mode"]
    device = settings["serial"][mode]["device"] if mode in profiles.SERIAL_MODES else ""
    supervisor = router.supervisor_pid()
    pids = router.router_pids()
    pid = pids["ours"][0] if pids["ours"] else None
    running = pid is not None or bool(pids["external"])
    rows = []
    errors = []

    if pid is not None:
        up = uptime_s(pid)
        rows.append((OK, "Router", f"running, pid {pid}" + (f", up {format_duration(up)}" if up else "")))
    elif supervisor is not None:
        if device and not os.path.exists(device):
            rows.append((WARN, "Router", "waiting for device (starts when the port is back)"))
        else:
            rows.append((WARN, "Router", "retrying"))
            errors = last_errors(device)
    elif pids["external"]:
        rows.append((WARN, "Router", f"running (external), pid {', '.join(map(str, pids['external']))}"))
    else:
        rows.append((OFF, "Router", "stopped"))

    if pid is not None or pids["external"]:
        state = "running"
    elif supervisor is not None:
        state = "waiting"
    else:
        state = "stopped"

    rows.append(input_row(mode, device, settings, supervisor, pid, holders))

    snap = monitor.snapshot()
    if snap.error:
        if "in use" in snap.error:
            # The GUI holds the monitor endpoint; only one listener gets the traffic
            rows.append((OFF, "Heartbeat", f"port {profiles.MONITOR_PORT} taken (GUI open? it shows the heartbeat)"))
        else:
            rows.append((BAD, "Heartbeat", snap.error))
    elif not running:
        rows.append((OFF, "Heartbeat", "-"))
    elif snap.heartbeat_age_s is None:
        rows.append((WARN, "Heartbeat", "waiting for vehicle"))
    elif snap.heartbeat_age_s > HEARTBEAT_TIMEOUT_S:
        rows.append((BAD, "Heartbeat", f"lost ({snap.heartbeat_age_s:.0f} s), sysid {snap.sysid}"))
    else:
        rows.append((OK, "Heartbeat", f"sysid {snap.sysid}, {snap.heartbeat_hz:.1f} Hz"))
    if running and not snap.error:
        rows.append((OK if snap.msgs_per_s > 0 else OFF, "Traffic", f"{snap.msgs_per_s:.0f} msg/s"))
    else:
        rows.append((OFF, "Traffic", "-"))

    listening = udp_listen_ports()
    endpoints = []
    for name, address, port in profiles.endpoints_for(settings, mode):
        text = f"{address}:{port}"
        if address not in ("127.0.0.1", "localhost"):
            endpoints.append((OFF, name, f"{text}  remote, can't check from here"))
        elif port in listening:
            endpoints.append((OK, name, f"{text}  listening"))
        else:
            endpoints.append((OFF, name, f"{text}  nothing listening"))
    return state, rows, endpoints, errors


def input_row(mode: str, device: str, settings: dict, supervisor, pid, holders=device_holders):
    if mode not in profiles.SERIAL_MODES:
        port = settings["sitl"]["listen_port"]
        return (OK, "Port", f"UDP {port}") if pid is not None else (OFF, "Port", f"UDP {port}")
    if not device:
        return OFF, "Port", "-"
    name = os.path.basename(os.path.realpath(device))
    if not os.path.exists(device):
        if supervisor is not None:
            return WARN, "Port", f"{device} unplugged, reconnects when plugged back in"
        return OFF, "Port", f"{device} not connected"
    if pid is not None and os.path.realpath(device) in open_files(pid):
        return OK, "Port", f"{name} open"
    held = [h for h in holders(device) if h[0] != pid]
    if held:
        return (BAD if supervisor is not None else WARN), "Port", f"{name} held by {describe(held)}"
    if supervisor is not None:
        return WARN, "Port", f"{name} connecting..."
    return OFF, "Port", f"{name} available"


def start_monitor() -> LinkMonitor:
    monitor = LinkMonitor(profiles.MONITOR_PORT)
    monitor.start()
    return monitor


def start_link(router: Router, settings: dict, mode: str, device: str = "", baud: int = 0,
               udp_port: int = 0, ask=lambda question: False):
    """Check, save the settings and start the router; None if started, else why not.

    ask(question) -> bool decides the warnings that can be overridden; an
    empty reason means the answer was no.
    """
    if router.supervisor_pid() is not None:
        return "The router is already running; stop it first to change the link."
    others = router.router_pids(fresh=True)["external"]
    if others:
        return (f"mavlink-routerd is already running (pid {', '.join(map(str, others))}), not started from this app. "
                "Stop it first; two routers would fight over the same ports.")

    if mode in profiles.SERIAL_MODES:
        if not device:
            return "Select the serial port first."
        if mode == "flight" and looks_like_autopilot(device) and not ask(
                f"{device} looks like the Pixhawk's USB port, not a telemetry radio. "
                "In HITL the sim bridge owns the Pixhawk USB; Flight mode should use the ground radio.\n"
                "Start on this port anyway?"):
            return ""
        holders = device_holders(device) if os.path.exists(device) else []
        if holders and not ask(
                f"{device} is open by {describe(holders)}; the router can't open it until that program lets go "
                "(e.g. QGC AutoConnect).\nStart anyway? It will keep retrying."):
            return ""
        settings["serial"][mode] = {"device": device, "baud": baud}
    else:
        device, baud = "", 0
        if udp_port:
            settings["sitl"]["listen_port"] = udp_port

    settings["mode"] = mode
    profiles.save_settings(settings)
    try:
        router.start(profiles.build_conf(settings, mode, device, baud), device)
    except (OSError, FileNotFoundError) as e:
        return f"Could not start router: {e}"
    return None


# --- interactive UI ----------------------------------------------------------

class Tui:
    def __init__(self, screen):
        self.screen = screen
        self.settings = profiles.load_settings()
        self.router = Router()
        self.monitor = start_monitor()
        self.ports = []  # (label, path)
        self.device = ""
        self.baud = 0
        self.udp_port = int(self.settings["sitl"]["listen_port"])
        self.field = 0
        self.errors = []  # shown until dismissed
        self.reported = set()  # each problem shows once per Start
        self.notice = ""
        self.show_log = False
        self.holders_cache = ("", float("-inf"), [])
        self.was_running = False
        self.label_width = 0
        self.glyph = "●" if "utf" in locale.getpreferredencoding().lower() else "*"
        self.load_mode(self.settings["mode"])
        self.init_colors()

    def init_colors(self):
        self.colors = {level: curses.A_NORMAL for level in (OK, WARN, BAD, OFF)}
        self.colors[OFF] = curses.A_DIM
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        gray = 8 if curses.COLORS >= 16 else curses.COLOR_WHITE
        for i, (level, color) in enumerate([(OK, curses.COLOR_GREEN), (WARN, curses.COLOR_YELLOW),
                                            (BAD, curses.COLOR_RED), (OFF, gray)], 1):
            curses.init_pair(i, color, -1)
            self.colors[level] = curses.color_pair(i) | (curses.A_DIM if gray != 8 and level == OFF else 0)

    # --- selection ----------------------------------------------------------

    def fields(self) -> list:
        return SERIAL_FIELDS if self.mode in profiles.SERIAL_MODES else SITL_FIELDS

    def load_mode(self, mode: str):
        self.mode = mode
        if mode in profiles.SERIAL_MODES:
            cfg = self.settings["serial"][mode]
            self.rescan(cfg["device"])
            self.baud = int(cfg["baud"])
        self.field = min(self.field, len(self.fields()) - 1)

    def rescan(self, selected: str = ""):
        current = selected or self.device
        self.ports = serial_ports()
        if current and current not in (path for _, path in self.ports):
            # Keep the saved device selectable while unplugged; the router waits for it
            label = current if os.path.exists(current) else f"{current} (not connected)"
            self.ports.append((label, current))
        self.device = current or (self.ports[0][1] if self.ports else "")

    def port_label(self) -> str:
        return next((label for label, path in self.ports if path == self.device), self.device or "(none)")

    def active(self) -> bool:
        return self.router.supervisor_pid() is not None or bool(self.router.router_pids()["external"])

    def change(self, step: int):
        field = self.fields()[self.field]
        if field == "mode":
            modes = list(profiles.MODES)
            self.load_mode(modes[(modes.index(self.mode) + step) % len(modes)])
        elif field == "port":
            paths = [path for _, path in self.ports]
            if paths:
                index = paths.index(self.device) if self.device in paths else -1
                self.device = paths[(index + step) % len(paths)]
        elif field == "baud":
            rates = [int(b) for b in BAUD_RATES]
            index = rates.index(self.baud) if self.baud in rates else (-1 if step > 0 else 0)
            self.baud = rates[(index + step) % len(rates)]
        elif field == "udp":
            self.udp_port = min(65535, max(1, self.udp_port + step))

    def edit(self):
        field = self.fields()[self.field]
        if field == "mode":
            self.change(1)
        elif field == "port":
            text = self.prompt("Port (ttyUSB0 or a path): ")
            if text:
                try:
                    self.rescan(resolve_device(text))
                except ValueError as e:
                    self.notice = str(e)
        else:
            current = self.baud if field == "baud" else self.udp_port
            text = self.prompt(f"{FIELD_LABELS[field]} [{current}]: ")
            if text and text.isdigit() and int(text) > 0:
                if field == "baud":
                    self.baud = int(text)
                elif int(text) <= 65535:
                    self.udp_port = int(text)
            elif text:
                self.notice = f"Not a valid {FIELD_LABELS[field].lower()}: {text}"

    def view_settings(self) -> dict:
        """Settings the status is shown for: as started while running, else the current selection."""
        if self.router.supervisor_pid() is not None:
            return self.settings
        view = copy.deepcopy(self.settings)
        view["mode"] = self.mode
        if self.mode in profiles.SERIAL_MODES:
            view["serial"][self.mode] = {"device": self.device, "baud": self.baud}
        else:
            view["sitl"]["listen_port"] = self.udp_port
        return view

    # --- actions ------------------------------------------------------------

    def start(self):
        reason = start_link(self.router, self.settings, self.mode, self.device, self.baud, self.udp_port,
                                self.ask)
        if reason is None:
            self.reported.clear()
            self.errors = []
            self.notice = "Router started."
        elif reason:
            self.message(reason)

    def stop(self):
        if self.router.supervisor_pid() is not None:
            self.flash("Stopping...")
            self.router.stop()
            self.notice = "Router stopped."
            return
        others = self.router.router_pids(fresh=True)["external"]
        if not others:
            self.notice = "The router isn't running."
        elif self.ask(f"A router not started from this app is running (pid {', '.join(map(str, others))}). "
                      "Stop it?"):
            self.flash("Stopping...")
            self.router.stop_external(others)
            self.notice = "Router stopped."

    def quit(self) -> bool:
        """True to leave the TUI."""
        if self.router.supervisor_pid() is None:
            return True
        choice = self.choose("The router is running.",
                             [("s", "Stop it and quit"), ("k", "Keep it running and quit"), ("c", "Cancel")])
        if choice == "s":
            self.flash("Stopping...")
            self.router.stop()
            return True
        return choice == "k"

    def handle(self, key) -> bool:
        """False to quit."""
        self.notice = ""
        fields = self.fields()
        if key == curses.KEY_UP:
            self.field = (self.field - 1) % len(fields)
        elif key in (curses.KEY_DOWN, "\t"):
            self.field = (self.field + 1) % len(fields)
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, "\n", "\r", curses.KEY_ENTER, " "):
            if self.active():
                self.notice = "Stop the router to change the link."
            elif key == curses.KEY_LEFT:
                self.change(-1)
            elif key == curses.KEY_RIGHT:
                self.change(1)
            else:
                self.edit()
        elif key == "s":
            self.start()
        elif key == "x":
            self.stop()
        elif key == "r":
            self.rescan()
            self.notice = f"{len(serial_ports())} serial port(s) found."
        elif key == "l":
            self.show_log = not self.show_log
        elif key == "d":
            self.errors = []
        elif key in ("q", "Q", "\x03"):
            return not self.quit()
        return True

    # --- status -------------------------------------------------------------

    def cached_holders(self, device: str):
        """device_holders() scans every process's open files; at most every HOLDERS_CACHE_S."""
        cached_device, stamp, holders = self.holders_cache
        if cached_device != device or time.monotonic() - stamp > HOLDERS_CACHE_S:
            holders = device_holders(device)
            self.holders_cache = (device, time.monotonic(), holders)
        return holders

    def collect_errors(self, from_log: list):
        device = self.device if self.mode in profiles.SERIAL_MODES else ""
        for message in explain_router_errors(self.router.new_output(), device) + from_log:
            if message not in self.reported:
                self.reported.add(message)
                self.errors.append(message)
                self.notice = ""  # e.g. "Router started." would contradict it

    # --- drawing ------------------------------------------------------------

    def put(self, y: int, x: int, text: str, attr: int = 0):
        h, w = self.screen.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                self.screen.addstr(y, x, text[:w - x - (1 if y == h - 1 else 0)], attr)
            except curses.error:
                pass

    def row(self, y: int, level: str, label: str, text: str):
        self.put(y, 2, self.glyph, self.colors[level])
        self.put(y, 4, label)
        self.put(y, 4 + self.label_width, text)

    def sync_with_router(self):
        """Show the link the router was started with, also when started from the GUI or another shell."""
        running = self.router.supervisor_pid() is not None
        if running and not self.was_running:
            self.settings = profiles.load_settings()
            self.udp_port = int(self.settings["sitl"]["listen_port"])
            self.load_mode(self.settings["mode"])
        self.was_running = running

    def draw(self):
        self.sync_with_router()
        settings = self.view_settings()
        state, rows, endpoints, from_log = status_rows(self.router, settings, self.monitor, self.cached_holders)
        self.collect_errors(from_log)
        locked = self.active()
        self.label_width = max(len(label) for _, label, _ in rows + endpoints) + 2

        self.screen.erase()
        h, w = self.screen.getmaxyx()
        level = {"running": OK, "waiting": WARN}.get(state, OFF)
        self.put(0, 1, "MAVLink Router", curses.A_BOLD)
        self.put(0, 17, f"{self.glyph} {state}", self.colors[level] | curses.A_BOLD)

        y = 2
        self.put(y, 1, "Vehicle link", curses.A_BOLD)
        values = {"mode": profiles.MODES[self.mode], "port": self.port_label(), "baud": str(self.baud),
                  "udp": str(self.udp_port)}
        # Same number of rows in every mode and state, so nothing below moves
        for i, field in enumerate(self.fields()):
            selected = i == self.field
            self.put(y + 1 + i, 1, ">" if selected else " ", curses.A_BOLD)
            self.put(y + 1 + i, 4, FIELD_LABELS[field])
            value = values[field] if locked else f"< {values[field]} >"
            attr = curses.A_DIM if locked else (curses.A_REVERSE if selected else 0)
            self.put(y + 1 + i, 4 + self.label_width, value, attr)
        y += len(SERIAL_FIELDS) + 1
        if locked:
            self.put(y, 4 + self.label_width, "(stop the router to change)", curses.A_DIM)

        y += 1
        for level_, label, text in rows[1:]:
            y += 1
            self.row(y, level_, label, text)
        y += 2
        self.row(y, *rows[0])

        y += 2
        self.put(y, 1, "Endpoints", curses.A_BOLD)
        for level_, label, text in endpoints:
            y += 1
            self.row(y, level_, label, text)

        footer = h - 1
        # (glyph?, text, attr); kept above the notice line even if that covers the endpoints
        error_lines = []
        for message in self.errors:
            error_lines.append((False, "", 0))
            for i, line in enumerate(self.wrap(message, w - 6)):
                error_lines.append((i == 0, line, self.colors[BAD] if i == 0 else 0))
        if self.errors:
            error_lines.append((False, "(d to dismiss)", curses.A_DIM))
        y = min(y, footer - 2 - len(error_lines))
        for glyph, line, attr in error_lines:
            y += 1
            if 0 <= y < footer:
                self.screen.move(y, 0)
                self.screen.clrtoeol()
            if glyph:
                self.put(y, 2, self.glyph, self.colors[BAD])
            self.put(y, 4, line, attr)

        if self.show_log and footer - y > 4:
            y += 2
            self.put(y, 1, f"Router log  {LOG_FILE}", curses.A_BOLD)
            lines = self.log_tail(footer - y - 2)
            for line in lines:
                y += 1
                self.put(y, 2, line, curses.A_DIM)
        if self.notice:
            self.put(footer - 1, 1, self.notice, curses.A_BOLD)
        x = 1
        for key, action in KEYS:
            self.put(footer, x, key, curses.A_REVERSE)
            self.put(footer, x + len(key), f" {action}  ")
            x += len(key) + len(action) + 3
        self.screen.refresh()

    @staticmethod
    def wrap(message: str, width: int) -> list:
        lines = []
        for paragraph in message.split("\n"):
            lines += textwrap.wrap(paragraph, max(20, width)) or [""]
        return lines

    @staticmethod
    def log_tail(count: int) -> list:
        if count <= 0:
            return []
        try:
            with open(LOG_FILE, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - LOG_TAIL_BYTES))
                return f.read().decode(errors="replace").splitlines()[-count:]
        except OSError:
            return ["(no log yet)"]

    # --- dialogs ------------------------------------------------------------

    def flash(self, text: str):
        """Show a notice right away, before something slow."""
        self.notice = text
        self.draw()

    def choose(self, text: str, options: list):
        """Modal box; returns the key of the chosen option, the last one on Esc."""
        h, w = self.screen.getmaxyx()
        width = min(w - 2, 72)
        buttons = "   ".join(f"[{key}] {label}" for key, label in options)
        lines = self.wrap(text, width - 4) + [""] + self.wrap(buttons, width - 4)
        height = min(h, len(lines) + 2)
        win = curses.newwin(height, width, max(0, (h - height) // 2), max(0, (w - width) // 2))
        win.keypad(True)
        win.erase()
        win.box()
        for i, line in enumerate(lines[:height - 2]):
            try:
                win.addstr(1 + i, 2, line[:width - 4])
            except curses.error:
                pass
        win.refresh()
        keys = {key for key, _ in options}
        while True:
            try:
                key = win.get_wch()
            except KeyboardInterrupt:
                key = "\x1b"
            except curses.error:
                continue
            if isinstance(key, str) and key.lower() in keys:
                return key.lower()
            if key in ("\x1b", "\x03"):
                return options[-1][0]
            if key in ("\n", "\r") and len(options) == 1:
                return options[0][0]

    def ask(self, question: str) -> bool:
        return self.choose(question, [("y", "Yes"), ("n", "No")]) == "y"

    def message(self, text: str):
        self.choose(text, [("o", "OK")])

    def prompt(self, label: str) -> str:
        """One line of input on the notice line; empty on Esc."""
        h, w = self.screen.getmaxyx()
        y = h - 2
        text = ""
        curses.curs_set(1)
        self.screen.timeout(-1)
        try:
            while True:
                self.screen.move(y, 0)
                self.screen.clrtoeol()
                self.put(y, 1, label + text, curses.A_BOLD)
                self.screen.move(y, min(w - 1, 1 + len(label) + len(text)))
                key = self.screen.get_wch()
                if key in ("\n", "\r", curses.KEY_ENTER):
                    return text.strip()
                if key in ("\x1b", "\x03"):
                    return ""
                if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    text = text[:-1]
                elif isinstance(key, str) and key.isprintable():
                    text += key
        except KeyboardInterrupt:
            return ""
        finally:
            curses.curs_set(0)
            self.screen.timeout(REFRESH_MS)

    # --- main loop ----------------------------------------------------------

    def run(self):
        curses.curs_set(0)
        self.screen.keypad(True)
        self.screen.timeout(REFRESH_MS)
        while True:
            self.draw()
            try:
                key = self.screen.get_wch()
            except curses.error:
                continue  # timeout: just refresh
            except KeyboardInterrupt:
                key = "q"
            if key == curses.KEY_RESIZE:
                continue
            if not self.handle(key):
                break
        self.monitor.stop()


def run() -> int:
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")  # Esc closes dialogs without the default 1 s delay
    curses.wrapper(lambda screen: Tui(screen).run())
    return 0
