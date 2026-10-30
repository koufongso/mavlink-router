"""Start, stop and inspect a detached mavlink-routerd process.

The router runs in its own session so it outlives this GUI: closing the
window (or the GUI crashing) never drops the link.
"""

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from .profiles import STATE_DIR

BINARY_NAME = "mavlink-routerd"
PID_FILE = STATE_DIR / "router.pid"
LOG_FILE = STATE_DIR / "router.log"
CONF_FILE = STATE_DIR / "active.conf"
# Empty conf dir so system-wide /etc/mavlink-router/config.d files don't
# silently add endpoints
CONF_DIR = STATE_DIR / "conf.d"
CACHE_BIN = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "mavlink-router-app" / BINARY_NAME


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
            if not CACHE_BIN.exists() or CACHE_BIN.stat().st_size != bundled.stat().st_size:
                CACHE_BIN.parent.mkdir(parents=True, exist_ok=True)
                tmp = CACHE_BIN.with_suffix(".tmp")
                shutil.copy2(bundled, tmp)
                os.replace(tmp, CACHE_BIN)
            return str(CACHE_BIN)

    path = shutil.which(BINARY_NAME)
    if path is None:
        raise FileNotFoundError(f"{BINARY_NAME} not found on PATH")
    return path


def _is_router(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv0 = f.read().split(b"\0")[0]
        return os.path.basename(argv0).decode() == BINARY_NAME
    except OSError:
        return False


def all_router_pids() -> List[int]:
    pids = []
    for entry in os.listdir("/proc"):
        if entry.isdigit() and _is_router(int(entry)):
            pids.append(int(entry))
    return pids


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


class Router:
    def __init__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        # Set only when started in this session, so the child gets reaped
        self._proc: Optional[subprocess.Popen] = None

    def pid(self) -> Optional[int]:
        """Pid of the router this app started, if it's still running."""
        if self._proc is not None and self._proc.poll() is not None:
            self._proc = None
        try:
            pid = int(PID_FILE.read_text().strip())
        except (OSError, ValueError):
            return None
        return pid if _is_router(pid) else None

    def external_pids(self) -> List[int]:
        """Routers running that this app didn't start (e.g. from a terminal)."""
        ours = self.pid()
        return [p for p in all_router_pids() if p != ours]

    def start(self, conf_text: str) -> int:
        binary = find_binary()
        CONF_FILE.write_text(conf_text)
        with open(LOG_FILE, "a") as log:
            log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} starting {binary} -c {CONF_FILE}\n")
            log.flush()
            self._proc = subprocess.Popen(
                [binary, "-c", str(CONF_FILE), "-d", str(CONF_DIR)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        PID_FILE.write_text(f"{self._proc.pid}\n")
        return self._proc.pid

    def stop(self, pid: int, timeout: float = 3.0) -> bool:
        """SIGTERM, then SIGKILL after the timeout. True once it's gone."""
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._reap(pid) or not _is_router(pid):
                break
            time.sleep(0.05)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            time.sleep(0.1)
            self._reap(pid)

        if self.pid() is None:
            PID_FILE.unlink(missing_ok=True)
        return not _is_router(pid)

    def _reap(self, pid: int) -> bool:
        if self._proc is not None and self._proc.pid == pid:
            if self._proc.poll() is not None:
                self._proc = None
                return True
        return False

    def tail_log(self, max_lines: int = 200) -> str:
        try:
            with open(LOG_FILE, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 32768))
                lines = f.read().decode(errors="replace").splitlines()
            return "\n".join(lines[-max_lines:])
        except OSError:
            return ""
