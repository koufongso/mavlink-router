"""Start, stop and inspect a detached, self-restarting mavlink-routerd.

The app starts supervisor.sh in its own session; it runs mavlink-routerd and
restarts it whenever it exits (e.g. the serial device was unplugged), waiting
for the device to come back first. Being detached, it outlives this GUI:
closing the window (or the GUI crashing) never drops the link.
"""

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .profiles import STATE_DIR

BINARY_NAME = "mavlink-routerd"
PID_FILE = STATE_DIR / "supervisor.pid"
LOG_FILE = STATE_DIR / "router.log"
CONF_FILE = STATE_DIR / "active.conf"
# Empty conf dir so system-wide /etc/mavlink-router/config.d files don't
# silently add endpoints
CONF_DIR = STATE_DIR / "conf.d"
# Copied out of the package so it keeps working after an AppImage exits
SUPERVISOR = STATE_DIR / "supervisor.sh"
CACHE_BIN = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "mavlink-router-app" / BINARY_NAME


def _copy_if_changed(src: Path, dst: Path):
    if dst.exists() and dst.read_bytes() == src.read_bytes():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def find_binary() -> str:
    """Locate mavlink-routerd, preferring the copy bundled in the AppImage.

    The AppImage's files only exist while the AppImage runs, so the bundled
    binary is copied to the cache dir first; a router started from inside the
    mount would break once the GUI exits.
    """
    appdir = os.environ.get("APPDIR")
    if appdir:
        bundled = Path(appdir) / "usr" / "bin" / BINARY_NAME
        if bundled.exists():
            _copy_if_changed(bundled, CACHE_BIN)
            return str(CACHE_BIN)

    path = shutil.which(BINARY_NAME)
    if path is None:
        raise FileNotFoundError(f"{BINARY_NAME} not found on PATH")
    return path


# --- /proc helpers -----------------------------------------------------------

def _cmdline(pid: int) -> List[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def _ppid(pid: int) -> Optional[int]:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def process_name(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return "?"


def _pids() -> List[int]:
    return [int(e) for e in os.listdir("/proc") if e.isdigit()]


def _is_router(pid: int) -> bool:
    argv = _cmdline(pid)
    return bool(argv) and os.path.basename(argv[0]) == BINARY_NAME


def _is_supervisor(pid: int) -> bool:
    return str(SUPERVISOR) in _cmdline(pid)[:2]


def open_files(pid: int) -> List[str]:
    """Paths of the files a process has open (empty if not permitted)."""
    paths = []
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                paths.append(os.readlink(f"/proc/{pid}/fd/{fd}"))
            except OSError:
                pass
    except OSError:
        pass
    return paths


def device_holders(device: str) -> List[Tuple[int, str]]:
    """(pid, name) of processes that have the device open (own processes only)."""
    target = os.path.realpath(device)
    return [(pid, process_name(pid)) for pid in _pids() if target in open_files(pid)]


def uptime_s(pid: int) -> Optional[float]:
    try:
        with open(f"/proc/{pid}/stat") as f:
            # Field 22 (starttime) counted after the ")" that ends the comm field
            start_ticks = int(f.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/uptime") as f:
            system_uptime = float(f.read().split()[0])
        return system_uptime - start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def _trim_log(max_bytes: int = 2_000_000, keep_bytes: int = 500_000):
    """Keep the log bounded; a router retrying a busy port logs every second."""
    try:
        if LOG_FILE.stat().st_size > max_bytes:
            with open(LOG_FILE, "rb") as f:
                f.seek(-keep_bytes, os.SEEK_END)
                tail = f.read()
            LOG_FILE.write_bytes(tail[tail.find(b"\n") + 1:])
    except OSError:
        pass


# --- router control ----------------------------------------------------------

class Router:
    def __init__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        # Set only when started in this session, so the child gets reaped
        self._proc: Optional[subprocess.Popen] = None
        # Router output is only read to explain failures; start at the end so
        # errors from earlier runs aren't reported again
        self._log_offset = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0

    def supervisor_pid(self) -> Optional[int]:
        """Pid of the supervisor this app started, if it's still running."""
        if self._proc is not None and self._proc.poll() is not None:
            self._proc = None
        try:
            pid = int(PID_FILE.read_text().strip())
        except (OSError, ValueError):
            return None
        return pid if _is_supervisor(pid) else None

    def router_pids(self) -> Dict[str, List[int]]:
        """mavlink-routerd processes: "ours" (run by our supervisor) and "external"."""
        sup = self.supervisor_pid()
        result = {"ours": [], "external": []}
        for pid in _pids():
            if _is_router(pid):
                result["ours" if sup is not None and _ppid(pid) == sup else "external"].append(pid)
        return result

    def start(self, conf_text: str, device: str = "") -> int:
        binary = find_binary()
        _copy_if_changed(Path(__file__).parent / "supervisor.sh", SUPERVISOR)
        CONF_FILE.write_text(conf_text)
        _trim_log()
        self._log_offset = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
        with open(LOG_FILE, "a") as log:
            log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} supervisor: {binary} -c {CONF_FILE}\n")
            log.flush()
            self._proc = subprocess.Popen(
                ["/bin/sh", str(SUPERVISOR), binary, str(CONF_FILE), str(CONF_DIR), device],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        PID_FILE.write_text(f"{self._proc.pid}\n")
        return self._proc.pid

    def stop(self, timeout: float = 3.0):
        """Stop our supervisor and its router (they share a process group)."""
        sup = self.supervisor_pid()
        if sup is not None:
            self._kill_group(sup, timeout)
        PID_FILE.unlink(missing_ok=True)

    def stop_external(self, pids: List[int], timeout: float = 3.0):
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self._wait_gone(pids, timeout)

    def _kill_group(self, pgid: int, timeout: float):
        members = [p for p in _pids() if self._pgid(p) == pgid]
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        self._wait_gone(members, timeout)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self._reap()

    @staticmethod
    def _pgid(pid: int) -> Optional[int]:
        try:
            return os.getpgid(pid)
        except ProcessLookupError:
            return None

    def _wait_gone(self, pids: List[int], timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._reap()
            if not any(os.path.exists(f"/proc/{p}") and (_is_router(p) or _is_supervisor(p)) for p in pids):
                return
            time.sleep(0.05)

    def _reap(self):
        if self._proc is not None and self._proc.poll() is not None:
            self._proc = None

    def new_output(self) -> List[str]:
        """Router/supervisor output lines written since the last call."""
        try:
            with open(LOG_FILE, "rb") as f:
                f.seek(0, os.SEEK_END)
                if f.tell() < self._log_offset:  # trimmed
                    self._log_offset = 0
                f.seek(self._log_offset)
                data = f.read()
        except OSError:
            return []
        # Only consume complete lines
        end = data.rfind(b"\n") + 1
        self._log_offset += end
        return data[:end].decode(errors="replace").splitlines()
