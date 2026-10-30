"""Background listener on the router's monitor endpoint: is the vehicle heard, and how much traffic."""

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from pymavlink import mavutil

GCS_SYSID = 255
RATE_WINDOW_S = 5.0


def _rate(times) -> float:
    """Messages per second over the samples in the window."""
    if len(times) < 2:
        return 0.0
    span = times[-1] - times[0]
    return (len(times) - 1) / span if span > 0 else 0.0


@dataclass
class LinkSnapshot:
    error: str = ""
    sysid: Optional[int] = None
    heartbeat_age_s: Optional[float] = None
    heartbeat_hz: float = 0.0
    msgs_per_s: float = 0.0


class LinkMonitor(threading.Thread):
    def __init__(self, port: int):
        super().__init__(daemon=True)
        self._port = port
        self._lock = threading.Lock()
        self._running = True
        self._error = ""
        self._sysid: Optional[int] = None
        self._last_hb = 0.0
        self._hb_times = deque()
        self._msg_times = deque()

    def stop(self):
        self._running = False

    def run(self):
        try:
            conn = mavutil.mavlink_connection(f"udpin:127.0.0.1:{self._port}")
        except OSError as e:
            with self._lock:
                self._error = f"monitor port {self._port}: {e}"
            return

        while self._running:
            msg = conn.recv_match(blocking=True, timeout=0.5)
            if msg is None or msg.get_type() == "BAD_DATA":
                continue
            now = time.monotonic()
            with self._lock:
                self._msg_times.append(now)
                if msg.get_type() == "HEARTBEAT" and self._is_vehicle(msg):
                    self._sysid = msg.get_srcSystem()
                    self._last_hb = now
                    self._hb_times.append(now)
        conn.close()

    @staticmethod
    def _is_vehicle(msg) -> bool:
        # Skip GCS heartbeats (QGC etc. talking through the router)
        return (msg.get_srcSystem() != GCS_SYSID
                and msg.autopilot != mavutil.mavlink.MAV_AUTOPILOT_INVALID)

    def snapshot(self) -> LinkSnapshot:
        now = time.monotonic()
        with self._lock:
            for times in (self._hb_times, self._msg_times):
                while times and now - times[0] > RATE_WINDOW_S:
                    times.popleft()
            return LinkSnapshot(
                error=self._error,
                sysid=self._sysid,
                heartbeat_age_s=(now - self._last_hb) if self._last_hb else None,
                heartbeat_hz=_rate(self._hb_times),
                msgs_per_s=_rate(self._msg_times),
            )
